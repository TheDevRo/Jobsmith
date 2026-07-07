import Foundation

/// Global pacing for LinkedIn requests — port of Python `_DetailThrottle`.
///
/// Serializes request *starts* a minimum interval apart, and on 429 pushes
/// the shared cooldown out so every worker backs off — without this, one
/// worker sleeping on a 429 just means the others keep hammering and inherit
/// the rate limit.
public actor LinkedInThrottler {
    private let spacing: Double
    private let jitter: Double
    private var nextAt: Double = 0

    /// - Parameters:
    ///   - spacing: minimum seconds between request starts.
    ///   - jitter: extra random 0..jitter seconds added after each start
    ///     (Python adds `random.uniform(0, 0.3)` for detail requests).
    public init(spacing: Double, jitter: Double = 0) {
        self.spacing = spacing
        self.jitter = jitter
    }

    /// Wait until this caller may start a request. The next slot is reserved
    /// *before* sleeping so concurrent waiters queue up spacing apart, which
    /// is what Python's lock-held sleep achieved.
    public func wait() async {
        let now = Self.now()
        let start = max(now, nextAt)
        nextAt = start + spacing + (jitter > 0 ? Double.random(in: 0...jitter) : 0)
        let delay = start - now
        if delay > 0 {
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
        }
    }

    /// Push the next allowed request start at least `seconds` out
    /// (shared exponential backoff on 429).
    public func backoff(_ seconds: Double) {
        nextAt = max(nextAt, Self.now() + seconds)
    }

    private static func now() -> Double {
        ProcessInfo.processInfo.systemUptime
    }
}

/// Wall-clock budget for a fetch phase — port of the Python deadline logic
/// (`_TOTAL_BUDGET` / `_SEARCH_PHASE_BUDGET` / `_DETAIL_PHASE_BUDGET`).
/// The orchestrator cancels the source outright when it overruns, discarding
/// everything collected, so each phase checks its budget and bails early
/// with partial results instead.
public struct TimeBudget: Sendable {
    private let deadline: Double

    public init(seconds: Double) {
        deadline = ProcessInfo.processInfo.systemUptime + seconds
    }

    public var remaining: Double {
        max(0, deadline - ProcessInfo.processInfo.systemUptime)
    }

    public var isExpired: Bool { remaining <= 0 }
}
