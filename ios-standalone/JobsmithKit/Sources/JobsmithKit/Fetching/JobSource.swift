import Foundation

/// A job-board fetcher. Mirrors the Python `job_sources` module contract:
/// each source returns normalized jobs and may throw `SourceBlockedError`
/// when the site is bot-blocking us.
public protocol JobSource: Sendable {
    static var id: String { get }
    /// Per-source budget enforced by FetchPipeline (Python _SOURCE_TIMEOUTS).
    static var timeout: Duration { get }
    func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob]
}

/// Raised when a site is bot-blocking us and no results are obtainable, so
/// the pipeline can distinguish "blocked" from "genuinely zero new jobs".
public struct SourceBlockedError: Error, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
}

// MARK: - JSON coercion helpers (Python-dict-style leniency)

func jsonString(_ value: Any?) -> String {
    if let s = value as? String { return s }
    if let n = value as? NSNumber { return n.stringValue }
    return ""
}

func jsonInt(_ value: Any?) -> Int? {
    // NSNull must not coerce; NSNumber covers Int/Double/Bool-backed values.
    if value is NSNull { return nil }
    if let n = value as? NSNumber { return n.intValue }
    if let s = value as? String, let d = Double(s) { return Int(d) }
    return nil
}

func jsonBool(_ value: Any?) -> Bool {
    if let b = value as? Bool { return b }
    if let n = value as? NSNumber { return n.boolValue }
    return false
}

func jsonDict(_ value: Any?) -> [String: Any] {
    value as? [String: Any] ?? [:]
}

func jsonArray(_ value: Any?) -> [Any] {
    value as? [Any] ?? []
}

/// Python `slug.replace("-", " ").title()`.
func titleCased(slug: String) -> String {
    slug.replacingOccurrences(of: "-", with: " ").capitalized
}
