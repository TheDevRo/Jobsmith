import Foundation

/// Workable public widget API. Accounts come from search.workableAccounts
/// (the <account> part of apply.workable.com/<account>). One request per
/// account: ?details=true returns every published job's description inline.
public struct WorkableSource: JobSource {
    public static let id = "workable"
    public static let timeout: Duration = .seconds(120)

    static let accountConcurrency = 4
    static let internalBudget: Duration = .seconds(100)
    static let headers = ["User-Agent": "Jobsmith/1.0"]

    public init() {}

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let keywords = config.search.keywords
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)
        let accounts = config.search.workableAccounts.filter { !$0.isEmpty && $0 != "example-company" }
        guard !accounts.isEmpty else { return [] }

        let deadline = ContinuousClock.now + Self.internalBudget
        let limiter = AsyncLimiter(Self.accountConcurrency)
        var results: [NormalizedJob] = []

        // Throwing group: a 401/403 means the account is gated, not empty — that
        // must reach the user rather than looking like "no jobs" (see REL-02).
        try await withThrowingTaskGroup(of: [NormalizedJob].self) { group in
            for account in accounts {
                group.addTask {
                    await limiter.acquire()
                    if ContinuousClock.now >= deadline {
                        await limiter.release()
                        return []
                    }
                    do {
                        let jobs = try await Self.fetchAccount(account: account, keywords: keywords,
                                                               excludePatterns: excludePatterns)
                        await limiter.release()
                        return jobs
                    } catch {
                        await limiter.release()
                        throw error
                    }
                }
            }
            for try await jobs in group { results += jobs }
        }
        return results
    }

    private static func fetchAccount(account: String, keywords: [String],
                                     excludePatterns: [NSRegularExpression]) async throws -> [NormalizedJob] {
        guard let url = URL(string:
            "https://apply.workable.com/api/v1/widget/accounts/\(account)?details=true")
        else { return [] }
        do {
            let response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 30)
            if SourceAuthError.isAuthFailure(response.status) {
                throw SourceAuthError(source: id, status: response.status)
            }
            guard response.status == 200 else { return [] }
            return try parse(data: response.data, account: account, keywords: keywords,
                             excludePatterns: excludePatterns)
        } catch let authError as SourceAuthError {
            throw authError
        } catch {
            return []
        }
    }

    static func parse(data: Data, account: String, keywords: [String],
                      excludePatterns: [NSRegularExpression]) throws -> [NormalizedJob] {
        let root = jsonDict(try JSONSerialization.jsonObject(with: data))
        var company = jsonString(root["name"])
        if company.isEmpty { company = titleCased(slug: account) }

        var results: [NormalizedJob] = []
        for case let job as [String: Any] in jsonArray(root["jobs"]) {
            let title = jsonString(job["title"])
            if JobFilters.matchesExclude(title, excludePatterns) { continue }
            if !keywords.isEmpty && !JobFilters.matchesKeywords(title, keywords) { continue }

            let location = jobLocation(job)
            let isRemote = jsonBool(job["telecommuting"]) || jsonBool(job["remote"])
                || location.lowercased().contains("remote")
            var shortcode = jsonString(job["shortcode"])
            if shortcode.isEmpty { shortcode = jsonString(job["code"]) }
            if shortcode.isEmpty { shortcode = jsonString(job["id"]) }
            var url = jsonString(job["url"])
            if url.isEmpty { url = jsonString(job["shortlink"]) }
            if url.isEmpty { url = "https://apply.workable.com/\(account)/j/\(shortcode)/" }
            var datePosted = jsonString(job["published_on"])
            if datePosted.isEmpty { datePosted = jsonString(job["created_at"]) }

            results.append(NormalizedJob(
                source: "workable",
                externalId: "workable-\(account)-\(shortcode)",
                title: title,
                company: company,
                location: !location.isEmpty ? location : (isRemote ? "Remote" : ""),
                url: url,
                description: JobFilters.cleanDescription(jsonString(job["description"])),
                tags: [jsonString(job["department"]), jsonString(job["function"])].filter { !$0.isEmpty },
                datePosted: datePosted,
                isRemote: isRemote,
                isEasyApply: false,
                applyType: ApplyTypeDetector.detect(source: "workable", url: url)))
        }
        return results
    }

    /// Display location from top-level city/state/country, falling back to
    /// the first entry of the locations array.
    static func jobLocation(_ job: [String: Any]) -> String {
        let parts = [job["city"], job["state"], job["country"]]
            .map { jsonString($0) }.filter { !$0.isEmpty }
        if !parts.isEmpty { return parts.joined(separator: ", ") }
        if let first = jsonArray(job["locations"]).first as? [String: Any] {
            return [first["city"], first["region"], first["country"]]
                .map { jsonString($0) }.filter { !$0.isEmpty }
                .joined(separator: ", ")
        }
        return ""
    }
}
