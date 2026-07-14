import Foundation

/// The "I asked for these jobs to be scored" flag, and nothing more.
///
/// A Score-all run needs almost no state to survive being interrupted, because
/// each score is committed to the database the moment it lands — so the work
/// remaining is always derivable: it's the jobs that are still unscored. All
/// that has to outlive the run is the user's *intent* — that they asked for a
/// batch, over which set, up to what cap — which is small enough to live in
/// UserDefaults and cheap enough to write from a background-task expiration
/// handler.
enum ScoringIntent {
    /// Which pool the run was scoring: the Inbox's untriaged jobs, or the
    /// Pipeline's shortlisted ones. The pool is re-derived on resume rather than
    /// stored, so jobs triaged in the meantime are handled correctly.
    enum Candidates: String {
        case inbox
        case pipeline
    }

    struct Intent {
        let cap: Int
        let candidates: Candidates
    }

    private static let pendingKey = "jobsmith.scoring.pending"
    private static let capKey = "jobsmith.scoring.cap"
    private static let candidatesKey = "jobsmith.scoring.candidates"

    static func save(cap: Int, candidates: Candidates,
                     _ defaults: UserDefaults = .standard) {
        defaults.set(true, forKey: pendingKey)
        defaults.set(cap, forKey: capKey)
        defaults.set(candidates.rawValue, forKey: candidatesKey)
    }

    static func clear(_ defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: pendingKey)
        defaults.removeObject(forKey: capKey)
        defaults.removeObject(forKey: candidatesKey)
    }

    static func isPendingIn(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: pendingKey)
    }

    static var isPending: Bool { isPendingIn() }

    static func pendingIn(_ defaults: UserDefaults = .standard) -> Intent? {
        guard defaults.bool(forKey: pendingKey) else { return nil }
        let cap = defaults.integer(forKey: capKey)
        guard cap > 0 else { return nil }
        let candidates = Candidates(rawValue: defaults.string(forKey: candidatesKey) ?? "") ?? .inbox
        return Intent(cap: cap, candidates: candidates)
    }

    static var pending: Intent? { pendingIn() }
}
