import Foundation
import ActivityKit
import AppIntents

/// The one Live Activity Jobsmith runs: a "run" that is searching, scoring,
/// or both in sequence. One activity per run — the card morphs between phases
/// rather than stacking a second activity.
///
/// Compiled into BOTH the app and the widget extension (the two processes
/// must agree on the wire format ActivityKit serializes between them).
struct JobRunAttributes: ActivityAttributes {
    enum Phase: String, Codable, Hashable {
        case searching, scoring, paused, done
    }

    struct ContentState: Codable, Hashable {
        var phase: Phase
        /// Sources finished (searching) or jobs scored (scoring).
        var completed: Int
        var total: Int
        /// Running count of jobs found; the headline numeral while searching.
        var jobsFound: Int
        /// "Searching 9 job boards" / "Scoring matches" / "Search complete".
        var title: String
        /// One supporting line: "6 of 9 boards · LinkedIn still working…".
        var detail: String

        var fraction: Double {
            guard total > 0 else { return 0 }
            return min(max(Double(completed) / Double(total), 0), 1)
        }
    }

    var startedAt: Date
}

/// How the Lock Screen Stop button reaches the app. `LiveActivityIntent`
/// executes in the app's own process, where the app has installed `onStop`
/// at launch; in the widget process the closure is never set — and never
/// called, since the widget only *references* the intent type.
@MainActor
enum RunControlBridge {
    static var onStop: (() -> Void)?
}

struct StopRunIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Stop the current run"
    /// Lock-screen-button plumbing, not a user-searchable action.
    static var isDiscoverable: Bool = false

    func perform() async throws -> some IntentResult {
        await MainActor.run { RunControlBridge.onStop?() }
        return .result()
    }
}
