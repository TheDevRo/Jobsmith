import Foundation
import ActivityKit
import UIKit
import JobsmithKit

/// Bridges the run state AppModel already publishes (fetch progress, scoring
/// counts, pause flags) onto the one JobRun Live Activity. Presentation of
/// truth that is already being computed — no new bookkeeping of its own.
///
/// Two iOS physics rules shape everything here:
/// - `Activity.request` only works in the foreground. A run started by a
///   background task simply gets no Live Activity (its completion notification
///   still fires); a run started in the foreground keeps its activity updated
///   from the background just fine.
/// - The activity displays work, it doesn't grant CPU time. When the app's
///   background window closes mid-run, the card flips to `.paused` and a
///   `staleDate` greys it if the system never grants another window — the
///   activity can't silently lie about being live.
@MainActor
final class LiveActivityController {
    static let shared = LiveActivityController()

    private var activity: Activity<JobRunAttributes>?
    /// A completion state lingers briefly before the activity is ended, so a
    /// scoring phase that starts right after a search (the background
    /// search-then-score chain) morphs the card instead of losing it.
    private var pendingEnd: Task<Void, Never>?
    private var lastPushed = Date.distantPast
    private var lastPhase: JobRunAttributes.Phase?
    private var lastCompleted = -1

    private init() {}

    // MARK: - Search

    func searchStarted(sourcesTotal: Int) {
        startOrMorph(.init(
            phase: .searching, kind: .search, completed: 0, total: sourcesTotal, jobsFound: 0,
            title: searchTitle(total: sourcesTotal), detail: "Starting search…"))
    }

    func searchProgress(_ p: FetchProgress) {
        push(.init(
            phase: .searching, kind: .search, completed: p.sourcesDone, total: p.sourcesTotal,
            jobsFound: p.jobsFound,
            title: searchTitle(total: p.sourcesTotal),
            detail: "\(p.sourcesDone) of \(p.sourcesTotal) boards done"),
            throttled: true)
    }

    func searchPaused(_ p: FetchProgress?) {
        let remaining = p.map { max($0.sourcesTotal - $0.sourcesDone, $0.interrupted.count) } ?? 0
        push(.init(
            phase: .paused, kind: .search, completed: p?.sourcesDone ?? 0, total: p?.sourcesTotal ?? 1,
            jobsFound: p?.jobsFound ?? 0,
            title: "Search paused",
            detail: remaining > 0
                ? "\(remaining) board\(remaining == 1 ? "" : "s") left — resumes in the background"
                : "Resumes in the background"),
            throttled: false)
    }

    func searchCompleted(newJobs: Int, stopped: Bool) {
        scheduleEnd(.init(
            phase: .done, kind: .search, completed: 1, total: 1, jobsFound: newJobs,
            title: stopped ? "Search stopped" : "Search complete",
            detail: stopped
                ? "Kept everything found so far"
                : "\(newJobs) new job\(newJobs == 1 ? "" : "s") in your Inbox"))
    }

    // MARK: - Scoring

    func scoringStarted(total: Int) {
        startOrMorph(.init(
            phase: .scoring, kind: .scoring, completed: 0, total: total, jobsFound: 0,
            title: "Scoring matches", detail: "0 of \(total) scored"))
    }

    func scoringProgress(done: Int, total: Int) {
        push(.init(
            phase: .scoring, kind: .scoring, completed: done, total: total, jobsFound: 0,
            title: "Scoring matches", detail: "\(done) of \(total) scored"),
            throttled: true)
    }

    func scoringPaused(done: Int, total: Int) {
        push(.init(
            phase: .paused, kind: .scoring, completed: done, total: total, jobsFound: 0,
            title: "Scoring paused",
            detail: "Finishes when the AI endpoint is reachable"),
            throttled: false)
    }

    func scoringEnded(done: Int, total: Int, failed: Bool) {
        scheduleEnd(.init(
            phase: .done, kind: .scoring, completed: done, total: max(total, 1), jobsFound: done,
            title: failed ? "Scoring stopped" : "Scoring complete",
            detail: "Scored \(done) job\(done == 1 ? "" : "s")"))
    }

    // MARK: - Lifecycle

    /// Ends a leftover activity when nothing is actually running or parked —
    /// e.g. the app was killed mid-run and the run was since retired. Also
    /// adopts an activity from a previous process so updates land on it.
    func reconcile(model: AppModel) {
        adoptIfNeeded()
        guard activity != nil else { return }
        let parkedSearch = ((try? model.searchRunStore.activeRun()).map { !$0.isFinished }) ?? false
        let busy = model.isFetching || model.isScoringAll
            || model.isSearchPaused || model.isScoringPaused
            || parkedSearch || ScoringIntent.isPending
        if !busy && pendingEnd == nil {
            endNow()
        }
    }

    /// Immediate teardown for a deliberate stop of a parked (not running) run.
    func endNow() {
        pendingEnd?.cancel()
        pendingEnd = nil
        guard let activity else { return }
        self.activity = nil
        Task { await activity.end(nil, dismissalPolicy: .immediate) }
    }

    // MARK: - Plumbing

    private func adoptIfNeeded() {
        if activity == nil {
            activity = Activity<JobRunAttributes>.activities.first {
                $0.activityState == .active
            }
        }
    }

    private func searchTitle(total: Int) -> String {
        "Searching \(total) job board\(total == 1 ? "" : "s")"
    }

    private func startOrMorph(_ state: JobRunAttributes.ContentState) {
        pendingEnd?.cancel()
        pendingEnd = nil
        adoptIfNeeded()
        if activity != nil {
            push(state, throttled: false)
            return
        }
        // Requesting is foreground-only; a background-started run just runs
        // without a Live Activity and notifies on completion as before.
        guard ActivityAuthorizationInfo().areActivitiesEnabled,
              UIApplication.shared.applicationState == .active else { return }
        activity = try? Activity.request(
            attributes: JobRunAttributes(startedAt: Date()),
            content: ActivityContent(state: state, staleDate: staleDate()))
        lastPushed = Date()
        lastPhase = state.phase
        lastCompleted = state.completed
    }

    /// Update the live card. Throttled pushes skip cosmetic-only changes that
    /// arrive within 2s of the last one (LinkedIn delivers jobs in a stream);
    /// phase changes and per-source/per-job completions always land.
    private func push(_ state: JobRunAttributes.ContentState, throttled: Bool) {
        guard let activity else { return }
        let significant = state.phase != lastPhase || state.completed != lastCompleted
        if throttled && !significant && Date().timeIntervalSince(lastPushed) < 2 { return }
        lastPushed = Date()
        lastPhase = state.phase
        lastCompleted = state.completed
        let content = ActivityContent(state: state, staleDate: staleDate())
        Task { await activity.update(content) }
    }

    /// A short leash while live: if the process dies and nothing resumes the
    /// run, the system dims the card rather than showing a frozen "Searching…".
    private func staleDate() -> Date {
        Date().addingTimeInterval(180)
    }

    private func scheduleEnd(_ state: JobRunAttributes.ContentState) {
        push(state, throttled: false)
        pendingEnd?.cancel()
        pendingEnd = Task { [weak self] in
            // The grace window in which a chained scoring phase may morph the
            // card instead (background search-then-score).
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            await self?.finish(state)
        }
    }

    private func finish(_ state: JobRunAttributes.ContentState) async {
        pendingEnd = nil
        guard let activity else { return }
        self.activity = nil
        // The result stays readable on the Lock Screen for a while, then
        // clears itself — no lingering stale card to swipe away days later.
        await activity.end(
            ActivityContent(state: state, staleDate: nil),
            dismissalPolicy: .after(.now + 15 * 60))
    }
}
