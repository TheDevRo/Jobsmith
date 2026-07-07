import Foundation

/// RemoteOK JSON API. RemoteOK aggressively blocks automated requests, so
/// this source uses browser-like headers and surfaces their anti-bot
/// responses (403, HTML instead of JSON) as SourceBlockedError.
public struct RemoteOKSource: JobSource {
    public static let id = "remoteok"
    public static let timeout: Duration = .seconds(60)

    static let apiURL = "https://remoteok.com/api"

    public init() {}

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let keywords = config.search.keywords
        guard !keywords.isEmpty else { return [] }
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)

        let headers = [
            "User-Agent": HTTPClient.browserUserAgent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://remoteok.com/",
            "Origin": "https://remoteok.com",
        ]

        guard let url = URL(string: Self.apiURL) else { return [] }
        let response: HTTPClient.Response
        do {
            response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 30)
        } catch let error as URLError {
            if error.code == .cancelled { throw error }
            return []
        }
        if response.status == 403 {
            throw SourceBlockedError("RemoteOK returned 403 — blocking automated requests")
        }
        guard response.status == 200 else { return [] }
        return try Self.parse(body: response.data, keywords: keywords,
                              excludePatterns: excludePatterns)
    }

    /// Parse the API body. Throws SourceBlockedError when the body is HTML
    /// (RemoteOK's soft block); returns [] on other invalid JSON.
    static func parse(body: Data, keywords: [String],
                      excludePatterns: [NSRegularExpression]) throws -> [NormalizedJob] {
        let text = String(decoding: body, as: UTF8.self)
        if text.prefix(200).lowercased().contains("<html") {
            throw SourceBlockedError("RemoteOK returned HTML instead of JSON — likely blocked")
        }
        guard let data = try? JSONSerialization.jsonObject(with: body) as? [Any] else { return [] }

        // First element is metadata — skip it.
        let raw = data.count > 1 ? Array(data.dropFirst()) : []
        var results: [NormalizedJob] = []
        for case let item as [String: Any] in raw {
            let title = jsonString(item["position"])
            let tags = jsonArray(item["tags"]).map { jsonString($0) }
            let haystack = "\(title) \(jsonString(item["company"])) \(tags.joined(separator: " ")) \(jsonString(item["description"]))"
            if !JobFilters.matchesKeywords(haystack, keywords) { continue }
            if JobFilters.matchesExclude(title, excludePatterns) { continue }

            var slug = jsonString(item["slug"])
            if slug.isEmpty { slug = jsonString(item["id"]) }
            var url = jsonString(item["url"])
            if url.isEmpty {
                url = slug.isEmpty ? "" : "https://remoteok.com/remote-jobs/\(slug)"
            }
            // Prefer the direct apply link when present.
            var applyURL = jsonString(item["apply_url"])
            if applyURL.isEmpty { applyURL = url }

            let location = item["location"] == nil ? "Remote" : jsonString(item["location"])

            results.append(NormalizedJob(
                source: "remoteok",
                externalId: jsonString(item["id"]),
                title: title,
                company: jsonString(item["company"]),
                location: location,
                url: applyURL,
                description: JobFilters.cleanDescription(jsonString(item["description"])),
                salaryMin: jsonInt(item["salary_min"]),
                salaryMax: jsonInt(item["salary_max"]),
                salaryPeriod: "annual",
                tags: tags,
                datePosted: jsonString(item["date"]),
                isRemote: true))
        }
        return results
    }
}
