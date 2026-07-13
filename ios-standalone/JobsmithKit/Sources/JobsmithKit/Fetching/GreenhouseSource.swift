import Foundation

/// Greenhouse and Lever public job-board APIs (one module, like the Python
/// source — Lever jobs are emitted with source "lever" but fetched here).
/// Boards come from search.greenhouseBoards / search.leverCompanies.
///
/// Each board is a single request: the Greenhouse list endpoint with
/// ?content=true returns every job's full description and location inline.
public struct GreenhouseSource: JobSource {
    public static let id = "greenhouse"
    public static let timeout: Duration = .seconds(300)

    static let boardConcurrency = 4
    // Internal budget — must finish under the pipeline's 300s per-source
    // timeout so already-collected boards are returned instead of everything
    // being discarded by the timeout race.
    static let internalBudget: Duration = .seconds(240)

    static let headers = ["User-Agent": "Jobsmith/1.0"]

    public init() {}

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let keywords = config.search.keywords
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)
        let ghBoards = config.search.greenhouseBoards.filter { $0 != "example-company" }
        let lvCompanies = config.search.leverCompanies.filter { $0 != "example-company" }

        let deadline = ContinuousClock.now + Self.internalBudget
        let limiter = AsyncLimiter(Self.boardConcurrency)
        var results: [NormalizedJob] = []

        // Throwing group: a 401/403 means the board is gated, not empty — that
        // must reach the user rather than looking like "no jobs" (see REL-02).
        try await withThrowingTaskGroup(of: [NormalizedJob].self) { group in
            for slug in ghBoards {
                group.addTask {
                    try await Self.runBoard(limiter: limiter, deadline: deadline) {
                        try await Self.fetchGreenhouseBoard(slug: slug, keywords: keywords,
                                                            excludePatterns: excludePatterns,
                                                            knownIDs: knownExternalIDs)
                    }
                }
            }
            for slug in lvCompanies {
                group.addTask {
                    try await Self.runBoard(limiter: limiter, deadline: deadline) {
                        try await Self.fetchLeverBoard(slug: slug, keywords: keywords,
                                                       excludePatterns: excludePatterns,
                                                       knownIDs: knownExternalIDs)
                    }
                }
            }
            for try await jobs in group { results += jobs }
        }
        return results
    }

    private static func runBoard(limiter: AsyncLimiter, deadline: ContinuousClock.Instant,
                                 _ fetch: () async throws -> [NormalizedJob]) async throws -> [NormalizedJob] {
        await limiter.acquire()
        if ContinuousClock.now >= deadline {
            await limiter.release()
            return []
        }
        do {
            let jobs = try await fetch()
            await limiter.release()
            return jobs
        } catch {
            await limiter.release()
            throw error
        }
    }

    // MARK: - Greenhouse

    private static func fetchGreenhouseBoard(slug: String, keywords: [String],
                                             excludePatterns: [NSRegularExpression],
                                             knownIDs: Set<String>) async throws -> [NormalizedJob] {
        guard let url = URL(string: "https://boards-api.greenhouse.io/v1/boards/\(slug)/jobs?content=true")
        else { return [] }
        do {
            // content=true payloads for large boards run to several MB —
            // allow more than the usual 30s.
            let response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 45)
            if SourceAuthError.isAuthFailure(response.status) {
                throw SourceAuthError(source: id, status: response.status)
            }
            guard response.status == 200 else { return [] }
            return try parseBoard(data: response.data, slug: slug, keywords: keywords,
                                  excludePatterns: excludePatterns, knownIDs: knownIDs)
        } catch let authError as SourceAuthError {
            throw authError
        } catch {
            return []
        }
    }

    static func parseBoard(data: Data, slug: String, keywords: [String],
                           excludePatterns: [NSRegularExpression],
                           knownIDs: Set<String>) throws -> [NormalizedJob] {
        let root = jsonDict(try JSONSerialization.jsonObject(with: data))
        var results: [NormalizedJob] = []
        for case let job as [String: Any] in jsonArray(root["jobs"]) {
            let jobID = jsonString(job["id"])
            let title = jsonString(job["title"])
            if JobFilters.matchesExclude(title, excludePatterns) { continue }

            // Already in the DB — the stored record has the description and
            // upsert would be a no-op anyway.
            let externalID = "gh-\(slug)-\(jobID)"
            if knownIDs.contains(externalID) { continue }

            // Keywords match the title only — matching descriptions floods
            // results with every posting whose boilerplate mentions one.
            if !keywords.isEmpty && !JobFilters.matchesKeywords(title, keywords) { continue }

            let locationName = jsonString(jsonDict(job["location"])["name"])
            var company = jsonString(job["company_name"])
            if company.isEmpty { company = titleCased(slug: slug) }
            var url = jsonString(job["absolute_url"])
            if url.isEmpty { url = "https://boards.greenhouse.io/\(slug)/jobs/\(jobID)" }

            results.append(NormalizedJob(
                source: "greenhouse",
                externalId: externalID,
                title: title,
                company: company,
                location: locationName,
                url: url,
                description: JobFilters.cleanDescription(jsonString(job["content"])),
                tags: jsonArray(job["departments"]).map { jsonString(jsonDict($0)["name"]) },
                datePosted: jsonString(job["updated_at"]),
                isRemote: locationName.lowercased().contains("remote")))
        }
        return results
    }

    // MARK: - Lever

    private static func fetchLeverBoard(slug: String, keywords: [String],
                                        excludePatterns: [NSRegularExpression],
                                        knownIDs: Set<String>) async throws -> [NormalizedJob] {
        guard let url = URL(string: "https://api.lever.co/v0/postings/\(slug)") else { return [] }
        do {
            let response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 30)
            if SourceAuthError.isAuthFailure(response.status) {
                throw SourceAuthError(source: id, status: response.status)
            }
            guard response.status == 200 else { return [] }
            return try parseLeverBoard(data: response.data, slug: slug, keywords: keywords,
                                       excludePatterns: excludePatterns, knownIDs: knownIDs)
        } catch let authError as SourceAuthError {
            throw authError
        } catch {
            return []
        }
    }

    static func parseLeverBoard(data: Data, slug: String, keywords: [String],
                                excludePatterns: [NSRegularExpression],
                                knownIDs: Set<String>) throws -> [NormalizedJob] {
        let postings = (try JSONSerialization.jsonObject(with: data)) as? [Any] ?? []
        var results: [NormalizedJob] = []
        for case let posting as [String: Any] in postings {
            let title = jsonString(posting["text"])
            if JobFilters.matchesExclude(title, excludePatterns) { continue }

            let postingID = jsonString(posting["id"])
            let externalID = "lv-\(slug)-\(postingID)"
            if knownIDs.contains(externalID) { continue }

            var rawDescription = jsonString(posting["descriptionPlain"])
            if posting["descriptionPlain"] == nil { rawDescription = jsonString(posting["description"]) }
            let description = JobFilters.cleanDescription(rawDescription)
            let categories = jsonDict(posting["categories"])
            let locationName = jsonString(categories["location"])

            // Lever matches keywords against title AND description (Python
            // parity — an empty keyword list matches nothing here).
            if !JobFilters.matchesKeywords("\(title) \(description)", keywords) { continue }

            var url = jsonString(posting["hostedUrl"])
            if url.isEmpty { url = "https://jobs.lever.co/\(slug)/\(postingID)" }

            results.append(NormalizedJob(
                source: "lever",
                externalId: externalID,
                title: title,
                company: titleCased(slug: slug),
                location: locationName,
                url: url,
                description: description,
                tags: [jsonString(categories["team"]), jsonString(categories["department"])],
                datePosted: "",
                isRemote: locationName.lowercased().contains("remote")))
        }
        return results
    }
}
