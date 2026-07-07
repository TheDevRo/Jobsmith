import CryptoKit
import Foundation
import GRDB

/// A market salary estimate — stored as JSON in the jobs table's
/// `salaryEstimate` column. All numbers come from real external data sources
/// (Adzuna histogram primary, BLS OEWS secondary); the LLM only canonicalizes
/// the job title and extracts a seniority signal so the API queries return
/// tighter matches — it is NEVER allowed to invent salary numbers.
public struct SalaryEstimate: Codable, Equatable, Sendable {
    public var p25: Int
    public var p50: Int?
    public var p75: Int
    public var currency: String
    /// "high" | "medium" | "low"
    public var confidence: String
    /// "adzuna" | "bls_oews"
    public var source: String
    public var canonicalTitle: String?
    public var seniority: String?
}

/// Raised when an external salary data source rejects us for rate-limit
/// reasons (Python `QuotaExceeded`). Callers should stop the batch cleanly,
/// preserving whatever estimates were already persisted.
public struct SalaryQuotaExceededError: Error, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
}

/// LLM role classification result — port of the `classify_job_role` dict.
public struct RoleClassification: Codable, Equatable, Sendable {
    public var canonicalTitle: String
    public var seniority: String?
    public var socCode: String?
    public var socTitle: String?

    enum CodingKeys: String, CodingKey {
        case canonicalTitle = "canonical_title"
        case seniority
        case socCode = "soc_code"
        case socTitle = "soc_title"
    }
}

/// Port of `salary_estimator.py`. Strategy:
///   1. LLM (utility tier) canonicalizes the title and classifies seniority,
///      cached by hash(title|desc) in `ai_cache`.
///   2. Adzuna histogram for canonical title + location; p25/p75 become the
///      range. Empty local histograms retry nationally with a confidence
///      downgrade.
///   3. BLS OEWS by SOC + MSA as fallback.
///   4. Lookups cached 30 days so re-running the estimator across the queue
///      doesn't burn the daily API quota.
public struct SalaryEstimator: Sendable {
    static let cacheTTLDays = 30

    public init() {}

    // MARK: - Public entry point

    public func estimate(job: Job, config: AppConfig, engine: (any AIEngine)?,
                         database: AppDatabase?) async throws -> SalaryEstimate? {
        let title = job.title.trimmingCharacters(in: .whitespacesAndNewlines)
        let location = job.location.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return nil }

        // 1. Canonicalize role (LLM only — no salary data passes through here)
        let classification = await Self.classifyJobRole(
            title: title, description: job.description, config: config,
            engine: engine, database: database)

        // 2. Adzuna histogram (primary)
        let appID = config.apiKeys.adzunaAppID
        let appKey = config.apiKeys.adzunaAppKey
        let country = "us"

        var market: MarketData?
        if !appID.isEmpty && !appKey.isEmpty {
            market = try await Self.lookupAdzunaHistogram(
                what: classification.canonicalTitle, where: location,
                country: country, appID: appID, appKey: appKey, database: database)
            // Some niche city strings return an empty histogram even though
            // the title is well-represented nationally. Retry without `where`
            // so we still get *some* signal — with a confidence bump down,
            // because the number is real but doesn't reflect the local market.
            if market == nil && !location.isEmpty {
                market = try await Self.lookupAdzunaHistogram(
                    what: classification.canonicalTitle, where: "",
                    country: country, appID: appID, appKey: appKey, database: database)
                if var national = market {
                    if national.confidence == "high" {
                        national.confidence = "medium"
                    } else if national.confidence == "medium" {
                        national.confidence = "low"
                    }
                    market = national
                }
            }
        }

        // 3. BLS fallback (optional)
        if market == nil, let soc = classification.socCode {
            let blsKey = config.apiKeys.blsRegistrationKey
            market = await Self.lookupBLSOEWS(
                socCode: soc, msaCode: Self.locationToMSA(location),
                apiKey: blsKey.isEmpty ? nil : blsKey)
        }

        guard let market else { return nil }

        var estimate = SalaryEstimate(
            p25: market.p25, p50: market.p50, p75: market.p75,
            currency: "USD", confidence: market.confidence, source: market.source,
            canonicalTitle: classification.canonicalTitle,
            seniority: classification.seniority)

        // Apply seniority adjustment when the LLM signals junior/senior.
        // The histogram already mixes levels, so a small scalar nudges the
        // range toward where this specific posting sits in that distribution —
        // a multiplicative shift on top of real percentiles, NOT an invented
        // number.
        let multiplier = Self.seniorityMultiplier(classification.seniority)
        if multiplier != 1.0 {
            estimate.p25 = Int(Double(estimate.p25) * multiplier)
            estimate.p50 = estimate.p50.map { Int(Double($0) * multiplier) }
            estimate.p75 = Int(Double(estimate.p75) * multiplier)
        }

        return estimate
    }

    // MARK: - Seniority

    static let seniorityMultipliers: [String: Double] = [
        "intern": 0.55,
        "entry": 0.80,
        "junior": 0.85,
        "mid": 1.00,
        "senior": 1.20,
        "staff": 1.45,
        "principal": 1.70,
        "manager": 1.25,
        "director": 1.55,
    ]

    static func seniorityMultiplier(_ seniority: String?) -> Double {
        guard let seniority, !seniority.isEmpty else { return 1.0 }
        return seniorityMultipliers[seniority.lowercased()] ?? 1.0
    }

    // MARK: - LLM role classification (no salary numbers — just canonicalization)

    static let socHints = """
    Common SOC examples (use the closest match; reply with the 6-digit code in NN-NNNN form):
    - 15-1252 Software Developers
    - 15-1244 Network and Computer Systems Administrators
    - 15-1212 Information Security Analysts
    - 15-1232 Computer User Support Specialists
    - 15-1299 Computer Occupations, All Other
    - 15-2051 Data Scientists
    - 13-1082 Project Management Specialists
    - 11-3021 Computer and Information Systems Managers
    - 41-3091 Sales Representatives, Services
    - 25-1199 Postsecondary Teachers, All Other
    """

    /// Canonicalize the role + extract seniority + SOC code via the utility
    /// LLM, with regex fallbacks when no engine is available or the call
    /// fails. Cached in `ai_cache` by hash(title|desc).
    public static func classifyJobRole(title: String, description: String,
                                       config: AppConfig, engine: (any AIEngine)?,
                                       database: AppDatabase?) async -> RoleClassification {
        let descSlice = String(description.prefix(1500))
        let keyMaterial = title.lowercased()
            .trimmingCharacters(in: .whitespacesAndNewlines) + "|" + descSlice
        let cacheKey = "soc:" + String(sha256Hex(keyMaterial).prefix(24))

        if let cached = await AICache.get(database, key: cacheKey, maxAgeDays: cacheTTLDays),
           let parsed = try? JSONDecoder().decode(RoleClassification.self, from: Data(cached.utf8)) {
            return parsed
        }

        var result = RoleClassification(
            canonicalTitle: fallbackCanonicalTitle(title),
            seniority: fallbackSeniority(title),
            socCode: nil,
            socTitle: nil)

        if let engine {
            let prompt = PromptRegistry.render("classify_job_role", [
                "soc_hints": socHints,
                "job_title": title,
                "job_description": descSlice,
            ], config: config)
            do {
                let text = try await engine.complete(
                    CompletionRequest(user: prompt, tier: .utility,
                                      temperature: 0.1, maxTokens: 200),
                    config: config.ai)
                if let parsed = parseClassificationJSON(text) {
                    if let v = nonEmpty(parsed["canonical_title"]) { result.canonicalTitle = v }
                    if let v = nonEmpty(parsed["seniority"]) { result.seniority = v }
                    if let v = nonEmpty(parsed["soc_code"]) { result.socCode = v }
                    if let v = nonEmpty(parsed["soc_title"]) { result.socTitle = v }
                    // Normalize SOC: must look like "NN-NNNN"
                    if let soc = result.socCode,
                       let range = soc.range(of: "\\b(\\d{2}-\\d{4})\\b",
                                             options: .regularExpression) {
                        result.socCode = String(soc[range])
                    } else {
                        result.socCode = nil
                    }
                }
            } catch {
                // Keep the regex fallbacks.
            }
        }

        if let data = try? JSONEncoder().encode(result),
           let json = String(data: data, encoding: .utf8) {
            await AICache.set(database, key: cacheKey, value: json)
        }
        return result
    }

    private static func nonEmpty(_ value: Any?) -> String? {
        guard let s = value as? String else { return nil }
        let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// Parse the LLM response as JSON, falling back to the first {...} blob.
    private static func parseClassificationJSON(_ text: String) -> [String: Any]? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if let obj = try? JSONSerialization.jsonObject(with: Data(trimmed.utf8)) as? [String: Any] {
            return obj
        }
        guard let range = trimmed.range(of: "\\{[\\s\\S]*\\}", options: .regularExpression) else {
            return nil
        }
        return try? JSONSerialization.jsonObject(
            with: Data(String(trimmed[range]).utf8)) as? [String: Any]
    }

    private static let seniorityPrefixRegex = try! NSRegularExpression(
        pattern: "\\b(senior|sr\\.?|junior|jr\\.?|staff|principal|lead|chief|head\\s+of|vp|director|manager|intern|entry[-\\s]?level)\\b",
        options: [.caseInsensitive])

    /// Strip seniority/level prefixes off a job title for use as a search
    /// query — port of `_fallback_canonical_title`.
    static func fallbackCanonicalTitle(_ title: String) -> String {
        let ns = title as NSString
        var cleaned = seniorityPrefixRegex.stringByReplacingMatches(
            in: title, range: NSRange(location: 0, length: ns.length), withTemplate: "")
        cleaned = cleaned.replacingOccurrences(
            of: "[\\(\\[\\{].*?[\\)\\]\\}]", with: "", options: .regularExpression)
        cleaned = cleaned.replacingOccurrences(
            of: "\\s+", with: " ", options: .regularExpression)
        cleaned = cleaned.trimmingCharacters(in: CharacterSet(charactersIn: " -,/|"))
        return cleaned.isEmpty ? title : cleaned
    }

    /// Port of `_fallback_seniority` — keyword order matters.
    static func fallbackSeniority(_ title: String) -> String {
        let t = title.lowercased()
        let pairs: [(String, String)] = [
            ("intern", "intern"),
            ("entry", "entry"),
            ("junior", "junior"),
            (" jr", "junior"),
            ("staff", "staff"),
            ("principal", "principal"),
            ("senior", "senior"),
            (" sr", "senior"),
            ("director", "director"),
            ("manager", "manager"),
            ("lead", "senior"),
        ]
        for (keyword, level) in pairs where t.contains(keyword) {
            return level
        }
        return "mid"
    }

    // MARK: - Adzuna histogram lookup (primary source)

    /// Intermediate lookup payload cached in `ai_cache` (pre-seniority
    /// adjustment, matching the Python cache-before-mutation behavior).
    struct MarketData: Codable, Equatable, Sendable {
        var p25: Int
        var p50: Int?
        var p75: Int
        var confidence: String
        var source: String
        var sampleSize: Int?
    }

    static let adzunaHistogramBase = "https://api.adzuna.com/v1/api/jobs"

    /// Fetch the Adzuna salary histogram for `what` in `where` and derive a
    /// range. Adzuna returns {histogram: {bucket_start: count, ...}, ...};
    /// we compute count-weighted p25/p50/p75. Cached 30 days under
    /// "adzuna:{country}:{what}:{where}". Throws quota-exceeded on 429/403.
    static func lookupAdzunaHistogram(what: String, where location: String,
                                      country: String, appID: String, appKey: String,
                                      database: AppDatabase?) async throws -> MarketData? {
        guard !what.isEmpty else { return nil }

        let cacheKey = "adzuna:\(country):\(what.lowercased()):\(location.isEmpty ? "any" : location.lowercased())"
        if let cached = await AICache.get(database, key: cacheKey, maxAgeDays: cacheTTLDays),
           let parsed = try? JSONDecoder().decode(MarketData.self, from: Data(cached.utf8)) {
            return parsed
        }

        var query: [(String, String)] = [
            ("app_id", appID),
            ("app_key", appKey),
            ("what", what),
        ]
        if !location.isEmpty { query.append(("where", location)) }
        guard let url = HTTPClient.url("\(adzunaHistogramBase)/\(country)/histogram",
                                       query: query) else { return nil }

        // Single attempt: a retried 429 would just burn more quota.
        let response = try await HTTPClient.fetchWithRetries(url, timeout: 15, retries: 0)
        if response.status == 429 || response.status == 403 {
            throw SalaryQuotaExceededError("adzuna \(response.status): \(response.text.prefix(200))")
        }
        guard response.status < 400 else { return nil }

        let root = jsonDict(try? JSONSerialization.jsonObject(with: response.data))
        let histogram = jsonDict(root["histogram"])
        guard !histogram.isEmpty else { return nil }

        let (p25, p50, p75) = percentilesFromHistogram(histogram)
        guard let p25, let p75, p25 != 0, p75 != 0 else { return nil }

        let sampleSize = histogram.values.compactMap { jsonInt($0) }.reduce(0, +)
        var confidence = "low"
        if sampleSize >= 200 {
            confidence = "high"
        } else if sampleSize >= 50 {
            confidence = "medium"
        }

        let payload = MarketData(p25: Int(p25), p50: p50.map { Int($0) }, p75: Int(p75),
                                 confidence: confidence, source: "adzuna",
                                 sampleSize: sampleSize)
        if let data = try? JSONEncoder().encode(payload),
           let json = String(data: data, encoding: .utf8) {
            await AICache.set(database, key: cacheKey, value: json)
        }
        return payload
    }

    /// Compute count-weighted p25/p50/p75 from an Adzuna histogram — exact
    /// port of `_percentiles_from_histogram`. Buckets ({bucket_start: count})
    /// are treated as their lower bound; percentiles walk the cumulative
    /// distribution and linearly interpolate inside the bucket containing
    /// the target rank.
    static func percentilesFromHistogram(_ histogram: [String: Any])
        -> (Double?, Double?, Double?) {
        var buckets: [(lo: Double, count: Int)] = []
        for (key, value) in histogram {
            guard let lo = Double(key), let count = jsonInt(value) else {
                return (nil, nil, nil)
            }
            if count > 0 { buckets.append((lo, count)) }
        }
        buckets.sort { $0.lo < $1.lo }
        guard !buckets.isEmpty else { return (nil, nil, nil) }

        let total = buckets.reduce(0) { $0 + $1.count }
        guard total > 0 else { return (nil, nil, nil) }

        func pct(_ p: Double) -> Double {
            let target = Double(total) * p
            var cum = 0.0
            for (i, bucket) in buckets.enumerated() {
                if cum + Double(bucket.count) >= target {
                    let nextLo = i + 1 < buckets.count ? buckets[i + 1].lo : bucket.lo * 1.1
                    // Linear interpolation within the bucket
                    let frac = bucket.count > 0 ? (target - cum) / Double(bucket.count) : 0
                    return bucket.lo + frac * (nextLo - bucket.lo)
                }
                cum += Double(bucket.count)
            }
            return buckets[buckets.count - 1].lo
        }

        return (pct(0.25), pct(0.50), pct(0.75))
    }

    // MARK: - BLS OEWS lookup (secondary; optional)

    static let blsAPIURL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

    /// BLS OEWS series: OEU{N|M}{areacode}000000{occupation_code}{datatype}
    /// datatype 12 = 75th pctile, 11 = median, 10 = 25th. We pull 10/11/12.
    static let blsDatatypes: [(label: String, code: String)] = [
        ("p25", "10"), ("p50", "11"), ("p75", "12"),
    ]

    /// BLS OEWS percentile lookup — port of `lookup_bls_oews`. socCode like
    /// "15-1252"; msaCode is a 7-digit BLS MSA code or nil for national.
    static func lookupBLSOEWS(socCode: String, msaCode: String?,
                              apiKey: String?) async -> MarketData? {
        guard !socCode.isEmpty else { return nil }

        var occ = socCode.replacingOccurrences(of: "-", with: "")
        if occ.count < 6 { occ = String(repeating: "0", count: 6 - occ.count) + occ }

        let prefix: String
        if let msaCode {
            prefix = "OEUM\(msaCode)000000"
        } else {
            prefix = "OEUN0000000000"  // national series prefix for OEWS
        }
        let seriesIDs = blsDatatypes.map { prefix + occ + $0.code }

        var body: [String: Any] = ["seriesid": seriesIDs]
        if let apiKey { body["registrationkey"] = apiKey }

        var request = URLRequest(url: URL(string: blsAPIURL)!, timeoutInterval: 15)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        let data: Data
        do {
            let (received, urlResponse) = try await HTTPClient.session.data(for: request)
            let status = (urlResponse as? HTTPURLResponse)?.statusCode ?? 0
            guard status < 400 else { return nil }
            data = received
        } catch {
            return nil
        }

        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              jsonString(root["status"]) == "REQUEST_SUCCEEDED" else {
            return nil
        }

        var pcts: [String: Double] = [:]
        let series = jsonArray(jsonDict(root["Results"])["series"])
        for case let entry as [String: Any] in series {
            let sid = jsonString(entry["seriesID"])
            for (label, code) in blsDatatypes where sid.hasSuffix(code) {
                let points = jsonArray(entry["data"])
                if let first = points.first as? [String: Any] {
                    let raw = jsonString(first["value"])
                    if let value = Double(raw) { pcts[label] = value }
                }
                break
            }
        }

        guard let p25 = pcts["p25"], let p75 = pcts["p75"], p25 != 0, p75 != 0 else {
            return nil
        }

        return MarketData(p25: Int(p25), p50: pcts["p50"].map { Int($0) }, p75: Int(p75),
                          confidence: msaCode != nil ? "high" : "medium",
                          source: "bls_oews", sampleSize: nil)
    }

    /// Minimal BLS MSA mapping — covers common metros plus the user's home
    /// market. Anything else falls through to national.
    static let msaCodes: [String: String] = [
        "denver": "1974000",
        "boulder": "1474000",
        "colorado springs": "1782000",
        "new york": "3562000",
        "san francisco": "4194000",
        "san jose": "4194000",
        "los angeles": "3108000",
        "seattle": "4274000",
        "boston": "1471650",
        "chicago": "1697600",
        "austin": "1242000",
        "dallas": "1910000",
        "houston": "2642000",
        "atlanta": "1206200",
        "washington": "4790000",
        "philadelphia": "3798000",
        "phoenix": "3806000",
        "miami": "3310000",
        "minneapolis": "3346000",
        "san diego": "4174000",
        "portland": "3890000",
    ]

    /// Map a free-text location string to a BLS MSA code, or nil for national.
    static func locationToMSA(_ location: String) -> String? {
        guard !location.isEmpty else { return nil }
        let t = location.lowercased()
        for (needle, code) in msaCodes where t.contains(needle) {
            return code
        }
        return nil
    }
}

// MARK: - ai_cache table access

/// TTL'd string cache backed by the `ai_cache` table (key/value/createdAt).
enum AICache {
    private static let iso = ISO8601DateFormatter()

    static func get(_ database: AppDatabase?, key: String, maxAgeDays: Int?) async -> String? {
        guard let database else { return nil }
        let row: (value: String, createdAt: String)? = try? await database.writer.read { db in
            guard let r = try Row.fetchOne(
                db, sql: "SELECT value, createdAt FROM ai_cache WHERE key = ?",
                arguments: [key]) else { return nil }
            return (r["value"], r["createdAt"])
        }
        guard let row else { return nil }
        if let maxAgeDays {
            guard let created = iso.date(from: row.createdAt),
                  Date().timeIntervalSince(created) <= Double(maxAgeDays) * 86400 else {
                return nil
            }
        }
        return row.value
    }

    static func set(_ database: AppDatabase?, key: String, value: String) async {
        guard let database else { return }
        let createdAt = iso.string(from: Date())
        try? await database.writer.write { db in
            try db.execute(
                sql: "INSERT OR REPLACE INTO ai_cache (key, value, createdAt) VALUES (?, ?, ?)",
                arguments: [key, value, createdAt])
        }
    }
}

// MARK: - SHA-256

private func sha256Hex(_ text: String) -> String {
    SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
}
