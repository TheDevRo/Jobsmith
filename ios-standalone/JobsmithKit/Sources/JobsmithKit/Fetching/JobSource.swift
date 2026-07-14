import Foundation

/// One incremental delivery from a source: jobs collected so far that the
/// pipeline may persist immediately, plus an opaque resume cursor describing
/// where the source got to.
///
/// The cursor is source-private JSON — only LinkedIn, whose fetch runs for
/// minutes and pages through search results, produces one. The pipeline stores
/// it verbatim and hands it back on the next attempt.
public typealias SourceCheckpoint = @Sendable (_ jobs: [NormalizedJob], _ cursor: String?) async -> Void

/// A job-board fetcher. Mirrors the Python `job_sources` module contract:
/// each source returns normalized jobs and may throw `SourceBlockedError`
/// when the site is bot-blocking us.
public protocol JobSource: Sendable {
    static var id: String { get }
    /// Per-source budget enforced by FetchPipeline (Python _SOURCE_TIMEOUTS).
    static var timeout: Duration { get }
    func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob]

    /// Fetch, handing results to `onCheckpoint` as they are collected rather
    /// than only at the end, and resuming from `cursor` when one is supplied.
    ///
    /// The default implementation satisfies this for every single-request
    /// source. Only a source that can be interrupted partway with work worth
    /// keeping — today just LinkedIn — needs to implement it itself.
    func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>,
                   resumeCursor cursor: String?,
                   onCheckpoint: @escaping SourceCheckpoint) async throws -> [NormalizedJob]
}

public extension JobSource {
    /// A source that finishes in one request has nothing to checkpoint: it
    /// either completes, delivering everything at once, or fails with nothing
    /// worth keeping. Resuming it just means running it again, so the cursor is
    /// ignored.
    func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>,
                   resumeCursor cursor: String?,
                   onCheckpoint: @escaping SourceCheckpoint) async throws -> [NormalizedJob] {
        let jobs = try await fetchJobs(config: config, knownExternalIDs: knownExternalIDs)
        await onCheckpoint(jobs, nil)
        return jobs
    }
}

/// Raised when a site is bot-blocking us and no results are obtainable, so
/// the pipeline can distinguish "blocked" from "genuinely zero new jobs".
public struct SourceBlockedError: Error, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
}

/// Raised when a source was cut off mid-fetch by something a retry will not
/// reproduce — the app being suspended, the network dropping, the task being
/// cancelled. Distinct from `failed`: whatever the source already checkpointed
/// is saved, and the run resumes from its cursor rather than reporting an error.
public struct SourceInterruptedError: Error, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
}

/// Raised when a source rejects our credentials (401) or refuses the request
/// outright (403). Without it, a wrong API key reads to the pipeline exactly
/// like "no jobs today" — and after three such runs the source is quietly
/// demoted to `suspect` while the user is never told to fix the key.
public struct SourceAuthError: Error, Sendable {
    public let source: String
    public let status: Int

    public init(source: String, status: Int) {
        self.source = source
        self.status = status
    }

    /// Whether an HTTP status means "your credentials are the problem".
    public static func isAuthFailure(_ status: Int) -> Bool {
        status == 401 || status == 403
    }
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
