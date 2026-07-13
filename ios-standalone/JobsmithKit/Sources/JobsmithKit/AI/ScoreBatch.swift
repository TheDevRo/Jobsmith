import Foundation

/// The candidate-selection half of "Score all", lifted out of `AppModel` so it
/// is testable without a `@MainActor` app model: which jobs count as unscored,
/// and how many of them one run may touch.
public enum ScoreBatch {
    /// A job is "unscored" when it has no fit score at all. A stored `0` also
    /// qualifies — historically a failed score wrote one (see `ScoringError`),
    /// so those jobs are re-offered rather than left branded a bad fit.
    public static func isUnscored(_ job: Job) -> Bool {
        (job.fitScore ?? 0) <= 0
    }

    public static func unscored(_ jobs: [Job]) -> [Job] {
        jobs.filter(isUnscored)
    }

    /// The jobs one "Score all" run may process: the candidates, truncated to
    /// the configured hard cap (`config.ai.scoreAllCap`). A non-positive cap
    /// yields an empty batch — a run can never fan out unbounded.
    public static func plan(candidates: [Job], cap: Int) -> [Job] {
        Array(candidates.prefix(max(0, cap)))
    }
}
