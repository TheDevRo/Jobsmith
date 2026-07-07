import Foundation

/// Arbeitnow free job-board API (no auth). Remote/tech focus, paginated JSON.
public struct ArbeitnowSource: JobSource {
    public static let id = "arbeitnow"
    public static let timeout: Duration = .seconds(60)

    static let apiURL = "https://www.arbeitnow.com/api/job-board-api"

    public init() {}

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let keywords = config.search.keywords
        guard !keywords.isEmpty else { return [] }
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)

        let headers = [
            "User-Agent": HTTPClient.browserUserAgent,
            "Accept": "application/json",
        ]

        var results: [NormalizedJob] = []
        var seenSlugs = Set<String>()
        do {
            for page in 1...3 {
                guard let url = HTTPClient.url(Self.apiURL, query: [("page", String(page))]) else { break }
                let response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 30)
                guard response.status == 200 else { break }
                let parsed = try Self.parsePage(data: response.data, keywords: keywords,
                                                excludePatterns: excludePatterns,
                                                seenSlugs: &seenSlugs)
                results += parsed.jobs
                if parsed.rawCount == 0 || !parsed.hasNext { break }
            }
        } catch let error as URLError {
            _ = error
            return []
        }
        return results
    }

    static func parsePage(data: Data, keywords: [String],
                          excludePatterns: [NSRegularExpression],
                          seenSlugs: inout Set<String>)
        throws -> (jobs: [NormalizedJob], rawCount: Int, hasNext: Bool) {
        let root = jsonDict(try JSONSerialization.jsonObject(with: data))
        let items = jsonArray(root["data"])
        let hasNext = !(jsonDict(root["links"])["next"] is NSNull)
            && jsonDict(root["links"])["next"] != nil
        var jobs: [NormalizedJob] = []
        for case let item as [String: Any] in items {
            let slug = jsonString(item["slug"])
            if seenSlugs.contains(slug) { continue }
            seenSlugs.insert(slug)

            let title = jsonString(item["title"])
            let haystack = "\(title) \(jsonString(item["company_name"])) \(jsonString(item["description"]))"
            if !JobFilters.matchesKeywords(haystack, keywords) { continue }
            if JobFilters.matchesExclude(title, excludePatterns) { continue }

            let location = jsonString(item["location"])
            let isRemote = jsonBool(item["remote"]) || location.lowercased().contains("remote")

            var tags: [String] = []
            if let tagString = item["tags"] as? String {
                tags = tagString.split(separator: ",")
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                    .filter { !$0.isEmpty }
            } else if let tagList = item["tags"] as? [Any] {
                tags = tagList.map { jsonString($0) }
            }

            var url = jsonString(item["url"])
            if url.isEmpty { url = "https://www.arbeitnow.com/view/\(slug)" }

            jobs.append(NormalizedJob(
                source: "arbeitnow",
                externalId: slug,
                title: title,
                company: jsonString(item["company_name"]),
                location: !location.isEmpty ? location : (isRemote ? "Remote" : ""),
                url: url,
                description: JobFilters.cleanDescription(jsonString(item["description"])),
                tags: tags,
                datePosted: jsonString(item["created_at"]),
                isRemote: isRemote))
        }
        return (jobs, items.count, hasNext)
    }
}
