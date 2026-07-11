import Foundation

/// Ashby public job-board API. Boards come from search.ashbyBoards (the
/// <board> part of jobs.ashbyhq.com/<board>). One request per board:
/// ?includeCompensation=true returns descriptions and compensation inline.
public struct AshbySource: JobSource {
    public static let id = "ashby"
    public static let timeout: Duration = .seconds(120)

    static let boardConcurrency = 4
    // Must finish under the pipeline's 120s timeout so collected boards
    // survive a slow straggler.
    static let internalBudget: Duration = .seconds(100)
    static let headers = ["User-Agent": "Jobsmith/1.0"]

    public init() {}

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let keywords = config.search.keywords
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)
        let boards = config.search.ashbyBoards.filter { !$0.isEmpty && $0 != "example-company" }
        guard !boards.isEmpty else { return [] }

        let deadline = ContinuousClock.now + Self.internalBudget
        let limiter = AsyncLimiter(Self.boardConcurrency)
        var results: [NormalizedJob] = []

        await withTaskGroup(of: [NormalizedJob].self) { group in
            for board in boards {
                group.addTask {
                    await limiter.acquire()
                    if ContinuousClock.now >= deadline {
                        await limiter.release()
                        return []
                    }
                    let jobs = await Self.fetchBoard(board: board, keywords: keywords,
                                                     excludePatterns: excludePatterns)
                    await limiter.release()
                    return jobs
                }
            }
            for await jobs in group { results += jobs }
        }
        return results
    }

    private static func fetchBoard(board: String, keywords: [String],
                                   excludePatterns: [NSRegularExpression]) async -> [NormalizedJob] {
        guard let url = URL(string:
            "https://api.ashbyhq.com/posting-api/job-board/\(board)?includeCompensation=true")
        else { return [] }
        do {
            let response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 30)
            guard response.status == 200 else { return [] }
            return try parse(data: response.data, board: board, keywords: keywords,
                             excludePatterns: excludePatterns)
        } catch {
            return []
        }
    }

    static func parse(data: Data, board: String, keywords: [String],
                      excludePatterns: [NSRegularExpression]) throws -> [NormalizedJob] {
        let root = jsonDict(try JSONSerialization.jsonObject(with: data))
        var results: [NormalizedJob] = []
        for case let job as [String: Any] in jsonArray(root["jobs"]) {
            // Unlisted postings are internal-only — the public board hides them.
            if let listed = job["isListed"] as? Bool, listed == false { continue }

            let title = jsonString(job["title"])
            if JobFilters.matchesExclude(title, excludePatterns) { continue }
            if !keywords.isEmpty && !JobFilters.matchesKeywords(title, keywords) { continue }

            var description = jsonString(job["descriptionPlain"])
            if description.isEmpty {
                description = JobFilters.cleanDescription(jsonString(job["descriptionHtml"]))
            }
            let location = jsonString(job["location"])
            let isRemote = jsonBool(job["isRemote"]) || location.lowercased().contains("remote")
            let (salaryMin, salaryMax, salaryPeriod) = parseCompensation(job["compensation"])
            let jobID = jsonString(job["id"])
            var url = jsonString(job["jobUrl"])
            if url.isEmpty { url = "https://jobs.ashbyhq.com/\(board)/\(jobID)" }

            results.append(NormalizedJob(
                source: "ashby",
                externalId: "ashby-\(board)-\(jobID)",
                title: title,
                company: titleCased(slug: board),
                location: !location.isEmpty ? location : (isRemote ? "Remote" : ""),
                url: url,
                description: description,
                salaryMin: salaryMin,
                salaryMax: salaryMax,
                salaryPeriod: salaryPeriod,
                tags: [jsonString(job["department"]), jsonString(job["team"])].filter { !$0.isEmpty },
                datePosted: jsonString(job["publishedAt"]),
                isRemote: isRemote,
                isEasyApply: false,
                applyType: ApplyTypeDetector.detect(source: "ashby", url: url)))
        }
        return results
    }

    /// Ashby reports compensation as typed components; the base salary is the
    /// one with compensationType == "Salary". Interval "1 YEAR" is annual,
    /// "1 HOUR" hourly. Equity/bonus components are ignored.
    static func parseCompensation(_ compensation: Any?) -> (Int?, Int?, String) {
        guard let comp = compensation as? [String: Any] else { return (nil, nil, "unknown") }
        var components = jsonArray(comp["summaryComponents"]).compactMap { $0 as? [String: Any] }
        for case let tier as [String: Any] in jsonArray(comp["compensationTiers"]) {
            components += jsonArray(tier["components"]).compactMap { $0 as? [String: Any] }
        }
        for component in components {
            guard jsonString(component["compensationType"]) == "Salary" else { continue }
            let minVal = jsonInt(component["minValue"])
            let maxVal = jsonInt(component["maxValue"])
            if minVal == nil && maxVal == nil { continue }
            let interval = jsonString(component["interval"]).uppercased()
            let period: String
            if interval.contains("HOUR") { period = "hourly" }
            else if interval.contains("YEAR") { period = "annual" }
            else { period = "unknown" }
            return (minVal, maxVal, period)
        }
        return (nil, nil, "unknown")
    }
}
