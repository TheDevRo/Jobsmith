import Foundation

/// USAJobs API (official U.S. government board). Requires the free
/// apiKeys.usajobsEmail/usajobsAPIKey pair; missing keys skip the source.
public struct USAJobsSource: JobSource {
    public static let id = "usajobs"
    public static let timeout: Duration = .seconds(60)

    static let baseURL = "https://data.usajobs.gov/api/Search"
    static let concurrency = 5

    public init() {}

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let email = config.apiKeys.usajobsEmail
        let apiKey = config.apiKeys.usajobsAPIKey
        guard !email.isEmpty, !apiKey.isEmpty else { return [] }

        let keywords = config.search.keywords
        let locations = config.search.locations
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)
        let maxAge = config.search.maxAgeDays ?? 7
        let limiter = AsyncLimiter(Self.concurrency)

        let headers = [
            "Host": "data.usajobs.gov",
            "User-Agent": email,
            "Authorization-Key": apiKey,
        ]

        let combos = keywords.flatMap { kw in locations.map { (kw, $0) } }
        var collected: [NormalizedJob] = []
        // Throwing group: a wrong email/key pair fails every combo the same way,
        // so the first SourceAuthError aborts rather than reading as "no jobs".
        try await withThrowingTaskGroup(of: [NormalizedJob].self) { group in
            for (keyword, location) in combos {
                group.addTask {
                    try await Self.fetchCombo(keyword: keyword, location: location,
                                              maxAge: maxAge, headers: headers,
                                              excludePatterns: excludePatterns, limiter: limiter)
                }
            }
            for try await jobs in group { collected += jobs }
        }

        var seenIDs = Set<String>()
        return collected.filter { seenIDs.insert($0.externalId).inserted }
    }

    private static func fetchCombo(keyword: String, location: String, maxAge: Int,
                                   headers: [String: String],
                                   excludePatterns: [NSRegularExpression],
                                   limiter: AsyncLimiter) async throws -> [NormalizedJob] {
        let isRemoteQuery = location.lowercased() == "remote"
        var query: [(String, String)] = [
            ("Keyword", keyword),
            ("LocationName", isRemoteQuery ? "" : location),
            ("ResultsPerPage", "50"),
            ("DatePosted", String(maxAge)),
        ]
        if isRemoteQuery { query.append(("RemoteIndicator", "True")) }
        guard let url = HTTPClient.url(baseURL, query: query) else { return [] }
        do {
            await limiter.acquire()
            let response: HTTPClient.Response
            do {
                response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 30)
                await limiter.release()
            } catch {
                await limiter.release()
                throw error
            }
            if SourceAuthError.isAuthFailure(response.status) {
                throw SourceAuthError(source: id, status: response.status)
            }
            guard response.status == 200 else { return [] }
            var seen = Set<String>()
            return try parse(data: response.data, excludePatterns: excludePatterns, seenIDs: &seen)
        } catch let authError as SourceAuthError {
            throw authError
        } catch {
            return []
        }
    }

    static func parse(data: Data, excludePatterns: [NSRegularExpression],
                      seenIDs: inout Set<String>) throws -> [NormalizedJob] {
        let root = jsonDict(try JSONSerialization.jsonObject(with: data))
        let items = jsonArray(jsonDict(root["SearchResult"])["SearchResultItems"])
        var jobs: [NormalizedJob] = []
        for case let item as [String: Any] in items {
            let match = jsonDict(item["MatchedObjectDescriptor"])
            let extID = jsonString(match["PositionID"])
            if seenIDs.contains(extID) { continue }
            seenIDs.insert(extID)

            let title = jsonString(match["PositionTitle"])
            if JobFilters.matchesExclude(title, excludePatterns) { continue }

            let org = jsonString(match["OrganizationName"])
            let dept = jsonString(match["DepartmentName"])
            let company = (!dept.isEmpty && dept != org) ? "\(org) (\(dept))" : org

            let locs = jsonArray(match["PositionLocation"]).compactMap { $0 as? [String: Any] }
            let locStr = locs.prefix(3).map { jsonString($0["LocationName"]) }
                .joined(separator: ", ")

            var salaryMin: Int? = nil
            var salaryMax: Int? = nil
            if let remuneration = jsonArray(match["PositionRemuneration"]).first as? [String: Any] {
                salaryMin = jsonInt(remuneration["MinimumRange"])
                salaryMax = jsonInt(remuneration["MaximumRange"])
            }
            if salaryMin == 0 { salaryMin = nil }
            if salaryMax == 0 { salaryMax = nil }

            var description = ""
            let duties = jsonDict(jsonDict(match["UserArea"])["Details"])["MajorDuties"]
            if let list = duties as? [Any] {
                description = list.map { jsonString($0) }.joined(separator: " ")
            } else {
                description = jsonString(duties)
            }
            if description.isEmpty {
                description = jsonString(match["QualificationSummary"])
            }

            let isRemote = locs.contains {
                let name = jsonString($0["LocationName"]).lowercased()
                return name.contains("remote") || name.contains("negotiable")
            }

            jobs.append(NormalizedJob(
                source: "usajobs",
                externalId: extID,
                title: title,
                company: company,
                location: locStr,
                url: jsonString(match["PositionURI"]),
                description: JobFilters.cleanDescription(description),
                salaryMin: salaryMin,
                salaryMax: salaryMax,
                salaryPeriod: "annual",
                tags: ["government", "federal"],
                datePosted: jsonString(match["PublicationStartDate"]),
                isRemote: isRemote))
        }
        return jobs
    }
}
