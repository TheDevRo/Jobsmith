import Foundation
import SwiftSoup

/// Shared filter/normalization logic ported from Python `job_sources/__init__.py`:
/// exclude/keyword matching, posted-date parsing, salary normalization,
/// description cleanup, and the global safety-net filter.
public enum JobFilters {
    /// 40h/wk * 52wk — hourly -> annual conversion happens only at comparison
    /// time; stored salaries stay raw.
    public static let annualHours = 2080

    // MARK: - Exclude keywords

    /// Compile exclude keywords to word-boundary regexes. `(?<!\w)`/`(?!\w)`
    /// instead of `\b` so keywords that start or end with non-word chars
    /// ("TS/SCI", "C++") still anchor correctly, and a plain substring check
    /// like "SC" cannot drop "Cisco".
    public static func compileExcludes(_ keywords: [String]) -> [NSRegularExpression] {
        keywords.compactMap { keyword in
            let kw = keyword.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !kw.isEmpty else { return nil }
            let pattern = "(?<!\\w)" + NSRegularExpression.escapedPattern(for: kw) + "(?!\\w)"
            return try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
        }
    }

    public static func matchesExclude(_ text: String, _ patterns: [NSRegularExpression]) -> Bool {
        guard !text.isEmpty else { return false }
        let range = NSRange(text.startIndex..., in: text)
        return patterns.contains { $0.firstMatch(in: text, range: range) != nil }
    }

    /// True if `text` matches any configured search keyword: whole phrase, or
    /// (for multi-word keywords) every token appearing somewhere in the text.
    public static func matchesKeywords(_ text: String, _ keywords: [String]) -> Bool {
        guard !text.isEmpty else { return false }
        let t = text.lowercased()
        for keyword in keywords {
            let k = keyword.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
            if k.isEmpty { continue }
            if t.contains(k) { return true }
            let tokens = k.split(separator: " ").map(String.init)
            if tokens.count > 1 && tokens.allSatisfy({ t.contains($0) }) { return true }
        }
        return false
    }

    // MARK: - Dates

    private static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let isoPlain = ISO8601DateFormatter()

    private static func utcFormatter(_ format: String) -> DateFormatter {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = format
        return f
    }
    // Naive ISO variants are treated as UTC, matching the Python port.
    private static let isoNaiveFractional = utcFormatter("yyyy-MM-dd'T'HH:mm:ss.SSSSSS")
    private static let isoNaive = utcFormatter("yyyy-MM-dd'T'HH:mm:ss")
    private static let isoNaiveSpace = utcFormatter("yyyy-MM-dd HH:mm:ss")
    private static let isoDateOnly = utcFormatter("yyyy-MM-dd")
    private static let rfc822Numeric = utcFormatter("EEE, dd MMM yyyy HH:mm:ss Z")
    private static let rfc822Named = utcFormatter("EEE, dd MMM yyyy HH:mm:ss zzz")
    private static let rfc822NoDay = utcFormatter("dd MMM yyyy HH:mm:ss Z")

    /// Parse the heterogeneous date_posted formats sources emit: unix epochs
    /// (Arbeitnow; seconds or milliseconds), ISO 8601 with/without Z or zone,
    /// and RFC 822 (WeWorkRemotely RSS). Returns nil when unparseable —
    /// callers must treat nil as "unknown, let it through".
    public static func parsePostedDate(_ value: String) -> Date? {
        let s = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if s.count >= 9, s.allSatisfy(\.isNumber) {
            guard let raw = Double(s) else { return nil }
            // 12+ digits can only be a millisecond epoch.
            return Date(timeIntervalSince1970: s.count >= 12 ? raw / 1000 : raw)
        }
        for iso in [isoFractional, isoPlain] {
            if let d = iso.date(from: s) { return d }
        }
        for f in [isoNaiveFractional, isoNaive, isoNaiveSpace, isoDateOnly,
                  rfc822Numeric, rfc822Named, rfc822NoDay] {
            if let d = f.date(from: s) { return d }
        }
        return nil
    }

    // MARK: - Salary

    /// Return "hourly" | "annual" | "unknown" from free-text salary phrasing.
    public static func detectPayPeriod(_ text: String?) -> String {
        guard let text, !text.isEmpty else { return "unknown" }
        let hourly = "(?:/\\s*(?:hr|hour)\\b|\\bper\\s+hour\\b|\\bhourly\\b|\\ban?\\s+hour\\b)"
        let annual = "(?:/\\s*(?:yr|year|annum)\\b|\\bper\\s+(?:year|annum)\\b|\\b(?:yearly|annually)\\b)"
        if text.range(of: hourly, options: [.regularExpression, .caseInsensitive]) != nil { return "hourly" }
        if text.range(of: annual, options: [.regularExpression, .caseInsensitive]) != nil { return "annual" }
        return "unknown"
    }

    /// Heuristic fallback when no period text is available: a bare 25 is
    /// almost certainly hourly, 80000 annual. Cutoff at 1000.
    public static func inferPeriod(fromAmount amount: Int?) -> String {
        guard let amount else { return "unknown" }
        return amount < 1000 ? "hourly" : "annual"
    }

    public static func normalizeToAnnual(_ amount: Int?, period: String) -> Int? {
        guard let amount else { return nil }
        return period == "hourly" ? amount * annualHours : amount
    }

    // MARK: - Description cleanup

    /// Shared description sanitizer used across all job sources. Steps:
    /// entity decode -> <br> to newlines -> strip tags -> percent-decode if
    /// the string still looks URL-encoded -> collapse whitespace. Idempotent.
    public static func cleanDescription(_ text: String?) -> String {
        guard let text, !text.isEmpty else { return "" }
        var s = (try? Entities.unescape(text)) ?? text
        s = s.replacingOccurrences(of: "<br\\s*/?>", with: "\n",
                                   options: [.regularExpression, .caseInsensitive])
        s = s.replacingOccurrences(of: "<[^>]+>", with: " ", options: .regularExpression)
        // Some sources (esp. RSS feeds) double-encode entities; one more pass.
        if s.contains("&") && s.contains(";") {
            s = (try? Entities.unescape(s)) ?? s
        }
        // Only unquote if the string genuinely looks URL-encoded, and reject
        // decodes that introduce control chars (likely false positive).
        if s.range(of: "%[0-9A-Fa-f]{2}", options: .regularExpression) != nil,
           let decoded = s.removingPercentEncoding,
           !decoded.unicodeScalars.contains(where: { $0.value < 9 }) {
            s = decoded
        }
        s = s.replacingOccurrences(of: "[ \\t]+", with: " ", options: .regularExpression)
        s = s.replacingOccurrences(of: "\\n{3,}", with: "\n\n", options: .regularExpression)
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Global safety-net filter

    /// Final filter applied to ALL jobs from ALL sources — port of
    /// `_passes_global_filters`. Catches anything that slipped through
    /// individual source filters.
    public static func passesGlobalFilters(_ job: NormalizedJob, search: SearchConfig,
                                           now: Date = Date()) -> Bool {
        let excludePatterns = compileExcludes(search.excludeKeywords)
        return passesGlobalFilters(job, search: search, excludePatterns: excludePatterns, now: now)
    }

    static func passesGlobalFilters(_ job: NormalizedJob, search: SearchConfig,
                                    excludePatterns: [NSRegularExpression],
                                    now: Date = Date()) -> Bool {
        let title = job.title.lowercased()
        let jobLocation = job.location.lowercased()

        // Exclude keywords — word-boundary match on title and company.
        if matchesExclude(job.title, excludePatterns) || matchesExclude(job.company, excludePatterns) {
            return false
        }

        // Max-age — only when date_posted is parseable; relative strings
        // ("Posted 3 days ago") pass through unchecked. +1 day grace for
        // timezone/rounding differences across sources.
        if let maxAgeDays = search.maxAgeDays, maxAgeDays != 0,
           let posted = parsePostedDate(job.datePosted) {
            let ageDays = now.timeIntervalSince(posted) / 86400
            if ageDays > Double(maxAgeDays) + 1 { return false }
        }

        // Min-salary — lenient: uses the job's upper bound and only drops jobs
        // that *state* a salary below the floor. No salary data = pass.
        if let minSalary = search.minSalary, minSalary != 0 {
            var amount = job.salaryMax
            if amount == nil || amount == 0 { amount = job.salaryMin }
            if let amount, amount != 0 {
                var period = job.salaryPeriod ?? "unknown"
                if period.isEmpty || period == "unknown" {
                    period = inferPeriod(fromAmount: amount)
                }
                if let annual = normalizeToAnnual(amount, period: period), annual < minSalary {
                    return false
                }
            }
        }

        // Location — only if locations are configured.
        let locations = search.locations
        if !locations.isEmpty {
            // LinkedIn cards sometimes omit location; if it's still empty let
            // it through — LinkedIn's own filter already approved it.
            if jobLocation.isEmpty && job.source == "linkedin" { return true }

            let isRemote = job.isRemote || jobLocation.contains("remote") || title.contains("remote")
            let hasRemoteConfig = locations.contains {
                $0.trimmingCharacters(in: .whitespaces).lowercased() == "remote"
            }
            if isRemote && hasRemoteConfig { return true }

            for loc in locations {
                let clean = loc.trimmingCharacters(in: .whitespaces).lowercased()
                if clean.isEmpty || clean == "remote" { continue }
                if jobLocation.contains(clean) { return true }
            }

            // RemoteOK and WeWorkRemotely are inherently remote — always pass.
            if job.source == "remoteok" || job.source == "weworkremotely" { return true }

            return false
        }

        return true
    }

    /// Apply the global filter to a batch, compiling exclude patterns once.
    public static func applyGlobalFilters(_ jobs: [NormalizedJob],
                                          search: SearchConfig,
                                          now: Date = Date()) -> [NormalizedJob] {
        let patterns = compileExcludes(search.excludeKeywords)
        return jobs.filter {
            passesGlobalFilters($0, search: search, excludePatterns: patterns, now: now)
        }
    }
}
