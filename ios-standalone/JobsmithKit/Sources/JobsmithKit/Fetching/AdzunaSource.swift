import Foundation

/// Adzuna API. Requires apiKeys.adzunaAppID/adzunaAppKey; missing keys skip
/// the source. Keyword × location combinations run concurrently (bounded);
/// each combination paginates up to 3 pages sequentially.
public struct AdzunaSource: JobSource {
    public static let id = "adzuna"
    public static let timeout: Duration = .seconds(120)

    static let baseURL = "https://api.adzuna.com/v1/api/jobs/us/search"
    // Adzuna's free tier rate-limits aggressively — 5 concurrent requests
    // trips 429s within seconds.
    static let concurrency = 2

    public init() {}

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let appID = config.apiKeys.adzunaAppID
        let appKey = config.apiKeys.adzunaAppKey
        guard !appID.isEmpty, !appKey.isEmpty else { return [] }

        let keywords = config.search.keywords
        // A blank `where` searches the whole US. Default an unset locations
        // list to one blank combo (mirrors the desktop's `[""]` default) so
        // Adzuna still runs when the user hasn't set a location.
        let locations = config.search.locations.isEmpty ? [""] : config.search.locations
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)
        let maxAge = config.search.maxAgeDays ?? 7
        let limiter = AsyncLimiter(Self.concurrency)

        let combos = keywords.flatMap { kw in locations.map { (kw, $0) } }
        var collected: [NormalizedJob] = []
        // Throwing group: a bad app id/key fails identically for every combo, so
        // the first SourceAuthError aborts the run and surfaces to the user
        // instead of masquerading as "Adzuna had no jobs today".
        try await withThrowingTaskGroup(of: [NormalizedJob].self) { group in
            for (keyword, location) in combos {
                group.addTask {
                    try await Self.fetchCombo(keyword: keyword, location: location,
                                              appID: appID, appKey: appKey, maxAge: maxAge,
                                              excludePatterns: excludePatterns, limiter: limiter)
                }
            }
            for try await jobs in group { collected += jobs }
        }

        // Combos deduplicate on Adzuna's numeric id (shared seen-set in Python).
        var seenIDs = Set<String>()
        return collected.filter { seenIDs.insert($0.externalId).inserted }
    }

    private static func fetchCombo(keyword: String, location: String,
                                   appID: String, appKey: String, maxAge: Int,
                                   excludePatterns: [NSRegularExpression],
                                   limiter: AsyncLimiter) async throws -> [NormalizedJob] {
        var jobs: [NormalizedJob] = []
        var seenIDs = Set<String>()
        // Adzuna's `where` is a geographic filter — "Remote" isn't a place, so
        // it geocodes to nothing and returns zero results. Map Remote/blank to
        // a nationwide search (the same normalization USAJobsSource applies);
        // the global remote/location filter narrows the results afterward.
        let whereParam = location.trimmingCharacters(in: .whitespaces)
            .lowercased() == "remote" ? "" : location
        for pageNum in 1...3 {
            guard let url = HTTPClient.url("\(baseURL)/\(pageNum)", query: [
                ("app_id", appID),
                ("app_key", appKey),
                ("what", keyword),
                ("where", whereParam),
                ("max_days_old", String(maxAge)),
                ("results_per_page", "50"),
                ("content-type", "application/json"),
            ]) else { break }
            do {
                await limiter.acquire()
                let response: HTTPClient.Response
                do {
                    response = try await HTTPClient.fetchWithRetries(url, timeout: 30)
                    await limiter.release()
                } catch {
                    await limiter.release()
                    throw error
                }
                if SourceAuthError.isAuthFailure(response.status) {
                    throw SourceAuthError(source: id, status: response.status)
                }
                guard response.status == 200 else { break }
                let page = try parsePage(data: response.data, excludePatterns: excludePatterns,
                                         seenIDs: &seenIDs)
                jobs += page.jobs
                if page.rawCount == 0 { break }
            } catch let authError as SourceAuthError {
                throw authError
            } catch {
                break
            }
        }
        return jobs
    }

    /// Parse one Adzuna results page. `rawCount` is the unfiltered result
    /// count, which ends pagination when zero.
    static func parsePage(data: Data, excludePatterns: [NSRegularExpression],
                          seenIDs: inout Set<String>) throws -> (jobs: [NormalizedJob], rawCount: Int) {
        let root = jsonDict(try JSONSerialization.jsonObject(with: data))
        let results = jsonArray(root["results"])
        var jobs: [NormalizedJob] = []
        for case let item as [String: Any] in results {
            let extID = jsonString(item["id"])
            if seenIDs.contains(extID) { continue }
            seenIDs.insert(extID)

            let title = jsonString(item["title"])
            if JobFilters.matchesExclude(title, excludePatterns) { continue }

            let company = jsonString(jsonDict(item["company"])["display_name"])
            let location = jsonString(jsonDict(item["location"])["display_name"])
            let tags: [String]
            if item["category"] != nil, !(item["category"] is NSNull) {
                tags = jsonString(jsonDict(item["category"])["tag"])
                    .split(separator: ",").map(String.init)
            } else {
                tags = []
            }

            jobs.append(NormalizedJob(
                source: "adzuna",
                externalId: extID,
                title: title,
                company: company,
                location: location,
                url: jsonString(item["redirect_url"]),
                description: JobFilters.cleanDescription(jsonString(item["description"])),
                salaryMin: jsonInt(item["salary_min"]),
                salaryMax: jsonInt(item["salary_max"]),
                salaryPeriod: "annual",
                tags: tags,
                datePosted: jsonString(item["created"]),
                isRemote: title.lowercased().contains("remote")
                    || location.lowercased().contains("remote")))
        }
        return (jobs, results.count)
    }
}
