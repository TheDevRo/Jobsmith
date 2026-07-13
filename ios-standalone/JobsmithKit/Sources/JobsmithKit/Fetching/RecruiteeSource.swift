import Foundation

/// Recruitee public career-site API. Companies come from
/// search.recruiteeCompanies (the <company> part of <company>.recruitee.com).
/// One request per company: the offers endpoint returns full descriptions.
public struct RecruiteeSource: JobSource {
    public static let id = "recruitee"
    public static let timeout: Duration = .seconds(120)

    static let companyConcurrency = 4
    static let internalBudget: Duration = .seconds(100)
    static let headers = ["User-Agent": "Jobsmith/1.0"]

    public init() {}

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let keywords = config.search.keywords
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)
        let companies = config.search.recruiteeCompanies.filter { !$0.isEmpty && $0 != "example-company" }
        guard !companies.isEmpty else { return [] }

        let deadline = ContinuousClock.now + Self.internalBudget
        let limiter = AsyncLimiter(Self.companyConcurrency)
        var results: [NormalizedJob] = []

        // Throwing group: a 401/403 means the career site is gated, not empty —
        // that must reach the user, not look like "no jobs" (see REL-02).
        try await withThrowingTaskGroup(of: [NormalizedJob].self) { group in
            for company in companies {
                group.addTask {
                    await limiter.acquire()
                    if ContinuousClock.now >= deadline {
                        await limiter.release()
                        return []
                    }
                    do {
                        let jobs = try await Self.fetchCompany(company: company, keywords: keywords,
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

    private static func fetchCompany(company: String, keywords: [String],
                                     excludePatterns: [NSRegularExpression]) async throws -> [NormalizedJob] {
        guard let url = URL(string: "https://\(company).recruitee.com/api/offers/") else { return [] }
        do {
            let response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 30)
            if SourceAuthError.isAuthFailure(response.status) {
                throw SourceAuthError(source: id, status: response.status)
            }
            guard response.status == 200 else { return [] }
            return try parse(data: response.data, company: company, keywords: keywords,
                             excludePatterns: excludePatterns)
        } catch let authError as SourceAuthError {
            throw authError
        } catch {
            return []
        }
    }

    static func parse(data: Data, company: String, keywords: [String],
                      excludePatterns: [NSRegularExpression]) throws -> [NormalizedJob] {
        let root = jsonDict(try JSONSerialization.jsonObject(with: data))
        var results: [NormalizedJob] = []
        for case let offer as [String: Any] in jsonArray(root["offers"]) {
            // The public endpoint should only return published offers, but be
            // defensive — drafts/closed offers are not applyable.
            let status = jsonString(offer["status"])
            if !status.isEmpty && status != "published" { continue }

            let title = jsonString(offer["title"])
            if JobFilters.matchesExclude(title, excludePatterns) { continue }
            if !keywords.isEmpty && !JobFilters.matchesKeywords(title, keywords) { continue }

            var location = jsonString(offer["location"])
            if location.isEmpty {
                location = [offer["city"], offer["country"]]
                    .map { jsonString($0) }.filter { !$0.isEmpty }
                    .joined(separator: ", ")
            }
            let isRemote = jsonBool(offer["remote"]) || location.lowercased().contains("remote")
            let (salaryMin, salaryMax, salaryPeriod) = parseSalary(offer["salary"])
            let offerID = jsonString(offer["id"])
            let slug = jsonString(offer["slug"])
            var url = jsonString(offer["careers_url"])
            if url.isEmpty { url = "https://\(company).recruitee.com/o/\(slug)" }

            var tags = jsonArray(offer["tags"]).map { jsonString($0) }.filter { !$0.isEmpty }
            let department = jsonString(offer["department"])
            if !department.isEmpty { tags.insert(department, at: 0) }

            var rawDate = jsonString(offer["published_at"])
            if rawDate.isEmpty { rawDate = jsonString(offer["created_at"]) }

            var companyName = jsonString(offer["company_name"])
            if companyName.isEmpty { companyName = titleCased(slug: company) }

            results.append(NormalizedJob(
                source: "recruitee",
                externalId: "recruitee-\(company)-\(offerID)",
                title: title,
                company: companyName,
                location: !location.isEmpty ? location : (isRemote ? "Remote" : ""),
                url: url,
                description: JobFilters.cleanDescription(jsonString(offer["description"])),
                salaryMin: salaryMin,
                salaryMax: salaryMax,
                salaryPeriod: salaryPeriod,
                tags: tags,
                datePosted: toISO(rawDate),
                isRemote: isRemote,
                isEasyApply: false,
                applyType: ApplyTypeDetector.detect(source: "recruitee", url: url)))
        }
        return results
    }

    /// Normalize Recruitee's "2026-06-22 13:37:27 UTC" timestamps to ISO 8601
    /// so parsePostedDate (and the max-age filter) can read them.
    static func toISO(_ timestamp: String) -> String {
        let s = timestamp.trimmingCharacters(in: .whitespacesAndNewlines)
        guard s.hasSuffix(" UTC") else { return s }
        return String(s.dropLast(4)).trimmingCharacters(in: .whitespaces)
            .replacingOccurrences(of: " ", with: "T") + "+00:00"
    }

    /// Recruitee salary object: {min, max, period, currency} with string
    /// min/max. Only yearly and hourly periods are emitted; monthly figures
    /// are dropped rather than mislabeled.
    static func parseSalary(_ salary: Any?) -> (Int?, Int?, String) {
        guard let dict = salary as? [String: Any] else { return (nil, nil, "unknown") }
        let periodRaw = jsonString(dict["period"]).lowercased()
        let period: String
        if periodRaw.hasPrefix("hour") { period = "hourly" }
        else if periodRaw.hasPrefix("year") || periodRaw.hasPrefix("annual") { period = "annual" }
        else { return (nil, nil, "unknown") }

        // Empty/zero values count as missing; an unparseable value voids the
        // whole salary (Python's ValueError path).
        func value(_ raw: Any?) -> (parsed: Bool, value: Int?) {
            if raw == nil || raw is NSNull { return (true, nil) }
            if let s = raw as? String {
                if s.isEmpty { return (true, nil) }
                guard let d = Double(s) else { return (false, nil) }
                return (true, Int(d))
            }
            if let n = raw as? NSNumber {
                return (true, n.doubleValue == 0 ? nil : n.intValue)
            }
            return (false, nil)
        }
        let minResult = value(dict["min"])
        let maxResult = value(dict["max"])
        guard minResult.parsed && maxResult.parsed else { return (nil, nil, "unknown") }
        if minResult.value == nil && maxResult.value == nil { return (nil, nil, "unknown") }
        return (minResult.value, maxResult.value, period)
    }
}
