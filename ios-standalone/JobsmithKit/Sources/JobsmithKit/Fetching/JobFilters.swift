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

    /// Best-effort extraction of a *stated* pay rate from free-text description
    /// prose — the piece structured feeds (JSON-LD `baseSalary`, Adzuna, etc.)
    /// already provide, but that job cards and RSS bodies bury in text like
    /// "Compensation: $25–30/hr" or "$120,000 - $150,000 a year".
    ///
    /// Deliberately conservative so it never invents a salary from stray "$"
    /// amounts: it only accepts a figure carrying a period cue (hourly/annual
    /// phrasing, a "k" suffix, or a five-figure magnitude), skips money that
    /// reads as a bonus/equity/401(k) match, and rejects anything outside sane
    /// bounds (hourly 2–500, annual 10k–2M). Returns nil when nothing qualifies.
    public static func parseSalaryFromText(_ text: String?) -> (min: Int?, max: Int?, period: String)? {
        guard let text, !text.isEmpty else { return nil }
        let ns = text as NSString
        let full = NSRange(location: 0, length: ns.length)

        // A dollar amount: "$" then digits (optional thousands commas/decimals)
        // and an optional adjacent "k"/"K" multiplier. The "k" must not lead into
        // a word, so "$120 knights" is not misread as $120,000.
        let mult = "(?:([kK])(?![A-Za-z]))?"
        let amount = "\\$\\s*([0-9][0-9,]*(?:\\.[0-9]+)?)" + mult
        let sep = "\\s*(?:[-–—]|\\bto\\b)\\s*"
        // Ranges are strictly more specific, so try them first: "$120k – $150k".
        let rangePattern = amount + sep + "\\$?\\s*([0-9][0-9,]*(?:\\.[0-9]+)?)" + mult

        for (pattern, ranged) in [(rangePattern, true), (amount, false)] {
            guard let re = try? NSRegularExpression(pattern: pattern,
                                                    options: [.caseInsensitive]) else { continue }
            for m in re.matches(in: text, range: full) {
                if let hit = evaluateSalaryMatch(m, ns: ns, ranged: ranged) { return hit }
            }
        }
        return nil
    }

    private static func evaluateSalaryMatch(_ m: NSTextCheckingResult, ns: NSString,
                                            ranged: Bool) -> (min: Int?, max: Int?, period: String)? {
        func group(_ i: Int) -> String? {
            guard i < m.numberOfRanges else { return nil }
            let r = m.range(at: i)
            return r.location == NSNotFound ? nil : ns.substring(with: r)
        }
        guard let aStr = group(1) else { return nil }
        let aK = group(2) != nil
        let bStr = ranged ? group(3) : nil
        let bK = ranged ? (group(4) != nil) : false
        // A "k" on either end of a range applies to both shorthand ends
        // ("$120–150k"), but never re-scales a value that already reads big.
        let anyK = aK || bK
        guard let minV = money(aStr, hasK: aK, promoteShorthand: ranged && anyK) else { return nil }
        var maxV: Int? = bStr.flatMap { money($0, hasK: bK, promoteShorthand: ranged && anyK) }

        // Period: prefer explicit phrasing in a window that spans the match plus
        // a little trailing text ("$25/hr", "$120,000 a year").
        let start = max(0, m.range.location - 4)
        let end = min(ns.length, m.range.location + m.range.length + 24)
        let window = ns.substring(with: NSRange(location: start, length: end - start))
        let lower = window.lowercased()
        for noise in ["bonus", "equity", "401", "403b", "stipend", "reimburs", "budget", "credit"] {
            if lower.contains(noise) { return nil }
        }

        var period = detectPayPeriod(window)
        if period == "unknown" {
            // No "per hour"/"a year" cue: trust magnitude, but never guess on a
            // bare small "$25" — too easily a gift card or fee.
            if anyK || minV >= 1000 { period = "annual" } else { return nil }
        }

        let bounds = period == "hourly" ? 2...500 : 10_000...2_000_000
        guard bounds.contains(minV) else { return nil }
        if let mx = maxV, !bounds.contains(mx) { maxV = nil }
        if let mx = maxV, mx <= minV { maxV = nil }
        return (minV, maxV, period)
    }

    /// Parse the profile's free-text desired salary ("$85k", "40/hr",
    /// "120,000", "$80k–$100k") into an annual floor for the pay filter.
    ///
    /// Unlike `parseSalaryFromText`, which digs money out of description prose
    /// and must be paranoid about bonuses and gift cards, this reads a field
    /// the user labeled "desired salary" — so no "$" or period cue is required
    /// and the first number wins (a range's lower end is the floor). Period
    /// falls back to magnitude ("40" reads hourly, "85000" annual). Returns
    /// nil when no plausible salary is present ("negotiable").
    public static func parseDesiredAnnualSalary(_ text: String?) -> Int? {
        guard let text, !text.isEmpty else { return nil }
        let pattern = "([0-9][0-9,]*(?:\\.[0-9]+)?)\\s*([kK](?![A-Za-z]))?"
        let ns = text as NSString
        guard let re = try? NSRegularExpression(pattern: pattern),
              let m = re.firstMatch(in: text, range: NSRange(location: 0, length: ns.length)),
              let amount = money(ns.substring(with: m.range(at: 1)),
                                 hasK: m.range(at: 2).location != NSNotFound,
                                 promoteShorthand: false) else { return nil }
        var period = detectPayPeriod(text)
        if period == "unknown" { period = inferPeriod(fromAmount: amount) }
        let bounds = period == "hourly" ? 2...500 : 10_000...2_000_000
        guard bounds.contains(amount) else { return nil }
        return normalizeToAnnual(amount, period: period)
    }

    /// A job's stated pay, annualized — nil when the posting states no salary
    /// or states one with an unknown period (annualizing a bare number is
    /// unreliable; see the min-salary comment in `passesGlobalFilters`).
    /// Prefers the upper bound, matching the lenient fetch-time gate.
    public static func statedAnnualPay(salaryMin: Int?, salaryMax: Int?,
                                       salaryPeriod: String?) -> Int? {
        var amount = salaryMax
        if amount == nil || amount == 0 { amount = salaryMin }
        guard let amount, amount != 0 else { return nil }
        let period = (salaryPeriod ?? "unknown").lowercased()
        guard period == "hourly" || period == "annual" else { return nil }
        return normalizeToAnnual(amount, period: period)
    }

    /// Parse a captured amount string ("120,000", "25.50", "120") into whole
    /// dollars, applying the "k" multiplier when present — or, for a shorthand
    /// range end like the "150" in "$120–150k", when the sibling carried it.
    private static func money(_ s: String, hasK: Bool, promoteShorthand: Bool) -> Int? {
        guard var v = Double(s.replacingOccurrences(of: ",", with: "")) else { return nil }
        if hasK || (promoteShorthand && v < 1000) { v *= 1000 }
        guard v.isFinite, v >= 0, v < 1e9 else { return nil }
        return Int(v.rounded())
    }

    // MARK: - Description cleanup

    /// Shared description sanitizer used across all job sources. Decodes
    /// entities, then maps HTML structure to plain text so it stays readable:
    /// `<br>`/list items/block closers (`</p>`, `</div>`, headings, …) become
    /// line breaks and bullets while inline tags collapse away. Percent-decodes
    /// URL-encoded bodies, tidies whitespace, and is idempotent on clean text.
    public static func cleanDescription(_ text: String?) -> String {
        guard let text, !text.isEmpty else { return "" }
        var s = (try? Entities.unescape(text)) ?? text

        // Structure-preserving tag handling before the blanket strip: keep
        // paragraph and list boundaries as line breaks / bullets.
        s = s.replacingOccurrences(of: "<br\\s*/?>", with: "\n",
                                   options: [.regularExpression, .caseInsensitive])
        // Bullet marker on the opener; the matching </li> closer below supplies
        // the single line break, so items don't end up double-spaced.
        s = s.replacingOccurrences(of: "<li[^>]*>", with: "• ",
                                   options: [.regularExpression, .caseInsensitive])
        s = s.replacingOccurrences(
            of: "</(?:p|div|section|article|ul|ol|li|tr|h[1-6]|blockquote)\\s*>",
            with: "\n", options: [.regularExpression, .caseInsensitive])
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
        // Drop bullets an empty <li> left behind, then tidy line edges.
        s = s.replacingOccurrences(of: "•[ \\t]*(?=\\n|$)", with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: "[ \\t]+\\n", with: "\n", options: .regularExpression)
        s = s.replacingOccurrences(of: "\\n[ \\t]+", with: "\n", options: .regularExpression)
        s = s.replacingOccurrences(of: "\\n{3,}", with: "\n\n", options: .regularExpression)
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Global safety-net filter

    /// Sources whose server-side search already did (possibly fuzzy) keyword
    /// relevance matching — e.g. "SRE" surfacing "Site Reliability Engineer".
    /// Re-applying a strict local gate would drop legitimate matches, so they
    /// skip the global keyword safety net; every in-Swift-filtered source gets
    /// it as a backstop against a silently-broken parser.
    static let serverSideKeywordSources: Set<String> = ["adzuna", "usajobs", "indeed", "linkedin"]

    /// Sources that constrain results by an authoritative server-side location
    /// param (one query per configured location). A job they return with an
    /// empty location was still location-filtered by the API, so an empty
    /// location is trusted rather than dropped. Per-board ATS sources
    /// (greenhouse/ashby/workable/recruitee) don't location-filter, so they
    /// stay subject to the location check.
    static let locationTrustedSources: Set<String> = ["linkedin", "indeed", "adzuna", "usajobs"]

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

        // Keyword gate — the safety net the in-Swift per-source filters were the
        // only line of defense for. Skipped for server-side-search sources whose
        // API already did (possibly fuzzy) relevance matching; applied to every
        // other source so a broken parser can't smuggle unrelated postings past
        // us. The haystack unions every field any source matches on, so this
        // never drops a job that a source's own keyword filter would have kept.
        if !search.keywords.isEmpty, !serverSideKeywordSources.contains(job.source) {
            let haystack = [job.title, job.company, job.tags.joined(separator: " "), job.description]
                .joined(separator: " ")
            if !matchesKeywords(haystack, search.keywords) { return false }
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
        // that *state* a salary below the floor with a KNOWN pay period. When the
        // period is unknown we don't guess: annualizing a bare number is
        // unreliable (a $990/wk stipend inferred hourly becomes ~$2M and clears
        // any floor), so ambiguous salaries pass — same posture as "no salary =
        // pass".
        if let minSalary = search.minSalary, minSalary != 0,
           let annual = statedAnnualPay(salaryMin: job.salaryMin, salaryMax: job.salaryMax,
                                        salaryPeriod: job.salaryPeriod),
           annual < minSalary {
            return false
        }

        // Location — only if locations are configured.
        let locations = search.locations
        if !locations.isEmpty {
            // Sources that constrain results by an authoritative server-side
            // location param sometimes still return a job with an empty location
            // string. Trust the API's filtering rather than drop it — this
            // applies to every server-side-location source, not just LinkedIn.
            if jobLocation.isEmpty && locationTrustedSources.contains(job.source) { return true }

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
