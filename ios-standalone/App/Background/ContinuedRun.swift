import Foundation
import BackgroundTasks
import OSLog
import UIKit

/// Keeps a user-started run (a search or a Score-all) executing after the app
/// is backgrounded, via `BGContinuedProcessingTask` on iOS 26+.
///
/// The pre-26 story: leaving the app closes a ~30s continued-execution window,
/// the run parks, and a discretionary `BGProcessingTask` (or the next
/// foreground) finishes it — correct, but "the scoring stopped when I left".
/// A continued-processing task is the sanctioned fix: the system keeps the
/// process running with a visible progress pill and a user-facing cancel, for
/// as long as conditions allow. It is still best-effort — the scheduler can
/// expire it under pressure — so everything here is opportunistic: expiration
/// cancels the run's task, which parks it through the exact machinery that
/// already exists, and older iOS never reaches this code at all.
///
/// Both engines benefit, which is the point: the process staying alive covers
/// on-device FoundationModels inference just as well as a LAN LM Studio call.
@MainActor
enum ContinuedRun {
    enum Kind: Hashable {
        case search
        case scoring
    }

    /// Diagnostics sink — wired to the Activity feed at app startup, so what
    /// iOS did with a background run (accepted, granted, declined, reclaimed)
    /// is readable on the phone itself, without Xcode attached.
    static var onEvent: ((_ event: String, _ detail: String) -> Void)?

    /// True while the scheduler has actually granted us a running continued
    /// task. The `beginBackgroundTask` expiration handlers consult this: their
    /// ~30s window closing is meaningless while the continued task holds the
    /// process open, so they must not cancel the run out from under it.
    static var isKeepingAlive: Bool {
        guard #available(iOS 26.0, *) else { return false }
        return ContinuedRunCoordinator.shared.isKeepingAlive
    }

    /// Announce a run and ask the system to keep it alive past backgrounding.
    /// `onExpire` is called (on the main actor) if the scheduler reclaims the
    /// task — it should cancel the run's work, which parks it for resume.
    /// No-op before iOS 26 or when the app isn't foregrounded (a continued
    /// task can only be submitted on behalf of the foreground app).
    static func begin(_ kind: Kind, total: Int, onExpire: @escaping @MainActor () -> Void) {
        guard #available(iOS 26.0, *) else { return }
        ContinuedRunCoordinator.shared.begin(kind, total: total, onExpire: onExpire)
    }

    static func progress(_ kind: Kind, done: Int, total: Int) {
        guard #available(iOS 26.0, *) else { return }
        ContinuedRunCoordinator.shared.progress(kind, done: done, total: total)
    }

    /// The run finished, parked, or was stopped — release the keep-alive once
    /// no other run still needs it. `finished` distinguishes actual completion
    /// from a park: the system pill fills to 100% and reports success only for
    /// the former, so an interrupted run can't masquerade as "complete".
    static func end(_ kind: Kind, finished: Bool = true) {
        guard #available(iOS 26.0, *) else { return }
        ContinuedRunCoordinator.shared.end(kind, finished: finished)
    }
}

@available(iOS 26.0, *)
@MainActor
final class ContinuedRunCoordinator {
    static let shared = ContinuedRunCoordinator()
    private init() {}

    /// `log stream --predicate 'subsystem == "com.thedevro.jobsmith.standalone"'`
    /// is the way to watch a run survive (or not survive) backgrounding.
    private static let log = Logger(subsystem: "com.thedevro.jobsmith.standalone",
                                    category: "ContinuedRun")

    /// The wildcard family declared in Info.plist
    /// (`BGTaskSchedulerPermittedIdentifiers` carries `<family>.*`). Each
    /// submission registers a fresh concrete identifier under it: registering
    /// the same identifier twice kills the app, and unlike the refresh and
    /// processing tiers, continued-processing registrations are exempt from
    /// the register-before-launch rule — so a unique id per run is both
    /// required and allowed.
    private static let family = "com.thedevro.jobsmith.standalone.continued"

    private struct RunState {
        var done: Int
        var total: Int
        var onExpire: @MainActor () -> Void
    }

    private var runs: [ContinuedRun.Kind: RunState] = [:]
    /// Submitted and waiting for the scheduler to start it. Kept so a run that
    /// ends before the grant arrives can withdraw the request.
    private var pendingID: String?
    private var task: BGContinuedProcessingTask?
    /// `setTaskCompleted` must be called exactly once, and expiration races
    /// normal completion — same discipline as BackgroundScheduler.
    private var completion: Completion?
    /// Heartbeat that inches the progress bar forward *within* a job. The
    /// scheduler expires tasks that appear stalled first, and one real tick
    /// per job (an LLM call is 30–90s) reads as exactly that. WWDC25: "tasks
    /// that do not report any progress will be expired."
    private var ticker: Task<Void, Never>?

    var isKeepingAlive: Bool { task != nil }

    func begin(_ kind: ContinuedRun.Kind, total: Int,
               onExpire: @escaping @MainActor () -> Void) {
        runs[kind] = RunState(done: 0, total: total, onExpire: onExpire)

        if let task {
            // A second run joining a live window (search chaining into
            // scoring, or overlapping runs) rides the existing task rather
            // than submitting a new one — which also wouldn't be possible
            // once backgrounded.
            task.updateTitle(title(), subtitle: subtitle())
            pushProgress()
            return
        }
        guard pendingID == nil else { return }
        // Submission is only valid on behalf of the foregrounded app; a run
        // started from a BGProcessingTask window just skips the keep-alive.
        guard UIApplication.shared.applicationState == .active else { return }

        let id = Self.family + "." + UUID().uuidString
        let registered = BGTaskScheduler.shared.register(
            forTaskWithIdentifier: id, using: nil
        ) { task in
            let continued = task as! BGContinuedProcessingTask
            Task { @MainActor in
                ContinuedRunCoordinator.shared.adopt(continued)
            }
        }
        guard registered else {
            Self.log.error("register(\(id, privacy: .public)) returned false — check BGTaskSchedulerPermittedIdentifiers")
            return
        }

        let request = BGContinuedProcessingTaskRequest(
            identifier: id, title: title(), subtitle: subtitle())
        // Queue rather than fail: the run's own work is already underway in
        // the app — a grant that arrives a little late still covers the
        // remainder, and no grant at all just leaves the pre-26 behavior.
        request.strategy = .queue
        do {
            try BGTaskScheduler.shared.submit(request)
            pendingID = id
            Self.log.notice("submitted continued task \(id, privacy: .public)")
        } catch {
            // Not permitted / too many pending: the run continues exactly as
            // it would on iOS 17 — park on the 30s window, resume later.
            Self.log.error("submit failed: \((error as NSError).domain, privacy: .public) \((error as NSError).code) — \(error.localizedDescription, privacy: .public)")
            ContinuedRun.onEvent?("bg_run_denied", Self.explainSubmitError(error))
        }
    }

    /// Turn a scheduler error into something the user can act on from the
    /// Activity feed.
    private static func explainSubmitError(_ error: Error) -> String {
        switch BGTaskScheduler.Error.Code(rawValue: (error as NSError).code) {
        case .notPermitted:
            return "iOS declined to keep this run alive in the background — "
                + "check that Background App Refresh is on for Jobsmith "
                + "(Settings → Apps → Jobsmith) and Low Power Mode is off."
        case .tooManyPendingTaskRequests:
            return "iOS declined the background run: too many pending requests."
        case .unavailable:
            return "Background tasks are unavailable here (expected in the Simulator)."
        default:
            return "iOS declined the background run: \(error.localizedDescription)"
        }
    }

    func progress(_ kind: ContinuedRun.Kind, done: Int, total: Int) {
        guard var run = runs[kind] else { return }
        run.done = done
        run.total = total
        runs[kind] = run
        Self.log.notice("progress \(String(describing: kind), privacy: .public) \(done)/\(total) (task granted: \(self.task != nil))")
        pushProgress()
    }

    func end(_ kind: ContinuedRun.Kind, finished: Bool) {
        runs.removeValue(forKey: kind)
        if let task, runs.isEmpty {
            Self.log.notice("run ended (finished: \(finished)) — completing continued task")
            ticker?.cancel()
            ticker = nil
            // A parked run must not fill the bar and claim success — the
            // system UI reads that as "all done" while jobs remain unscored.
            if finished {
                task.progress.completedUnitCount = task.progress.totalUnitCount
            }
            completion?.finish(task, success: finished)
            self.task = nil
            completion = nil
        } else if let task {
            task.updateTitle(title(), subtitle: subtitle())
        } else if let pendingID, runs.isEmpty {
            // Ended before the scheduler got around to starting us — withdraw
            // the request so a stale pill can't appear later.
            BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: pendingID)
            self.pendingID = nil
        }
    }

    private func adopt(_ task: BGContinuedProcessingTask) {
        pendingID = nil
        Self.log.notice("granted continued task \(task.identifier, privacy: .public)")
        guard !runs.isEmpty else {
            // The run it was meant to cover already finished or parked.
            task.setTaskCompleted(success: true)
            return
        }
        ContinuedRun.onEvent?("bg_run_granted",
                              "iOS is keeping this run alive in the background")
        let completion = Completion()
        self.task = task
        self.completion = completion
        task.updateTitle(title(), subtitle: subtitle())
        pushProgress()
        // Heartbeat between real ticks — see `ticker`.
        ticker = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                self?.tick()
            }
        }
        task.expirationHandler = {
            Task { @MainActor in
                Self.log.warning("continued task expired — parking covered runs")
                ContinuedRun.onEvent?("bg_run_expired",
                                      "iOS reclaimed the background run — it will resume "
                                      + "when you return to the app")
                // Park every covered run through its own cancel path — the
                // same route the 30s window takes pre-26 — then report done
                // promptly; the scheduler's patience after expiration is
                // short. The runs' own completion paths will call `end`,
                // which the Completion lock turns into a no-op.
                let coordinator = ContinuedRunCoordinator.shared
                coordinator.ticker?.cancel()
                coordinator.ticker = nil
                for run in coordinator.runs.values {
                    run.onExpire()
                }
                if let task = coordinator.task {
                    coordinator.completion?.finish(task, success: false)
                }
                // Drop the reference too: `isKeepingAlive` must read false the
                // moment the grant is gone (the scoring backoff loop consults
                // it to decide between retrying and parking).
                coordinator.task = nil
                coordinator.completion = nil
            }
        }
    }

    /// Progress is the contract: continued tasks must report it, and the
    /// scheduler expires tasks that appear stalled first. Summing the covered
    /// runs keeps one honest monotonic bar across a search→score chain. The
    /// scale is ×100 so the heartbeat can move *within* a job — one real tick
    /// per 30–90s LLM call reads as a stalled task otherwise.
    private func pushProgress() {
        guard let task else { return }
        let total = max(runs.values.reduce(0) { $0 + $1.total }, 1)
        let done = min(runs.values.reduce(0) { $0 + $1.done }, total)
        task.progress.totalUnitCount = Int64(total * 100)
        // Only ever move forward — the heartbeat may have advanced the bar
        // partway into the job that just finished.
        let base = Int64(done * 100)
        if task.progress.completedUnitCount < base {
            task.progress.completedUnitCount = base
        }
    }

    /// One heartbeat: creep toward (but never past) the end of the job in
    /// flight, ~1% every 2s. A job slower than ~3 minutes parks the bar at
    /// 99% of itself — at that point looking stalled is simply the truth.
    private func tick() {
        guard let task else { return }
        let total = max(runs.values.reduce(0) { $0 + $1.total }, 1)
        let done = min(runs.values.reduce(0) { $0 + $1.done }, total)
        let cap = min(Int64(done * 100 + 99), Int64(total * 100))
        if task.progress.completedUnitCount < cap {
            task.progress.completedUnitCount += 1
        }
    }

    private func title() -> String {
        let kinds = Set(runs.keys)
        if kinds.contains(.search) && kinds.contains(.scoring) {
            return "Searching & scoring jobs"
        }
        if kinds.contains(.search) { return "Searching for jobs" }
        return "Scoring jobs"
    }

    private func subtitle() -> String {
        "Jobsmith finishes this even if you leave"
    }

    private final class Completion: @unchecked Sendable {
        private let lock = NSLock()
        private var done = false

        func finish(_ task: BGTask, success: Bool) {
            lock.lock()
            let alreadyDone = done
            done = true
            lock.unlock()
            guard !alreadyDone else { return }
            task.setTaskCompleted(success: success)
        }
    }
}
