import Foundation
import BackgroundTasks
import UIKit
import JobsmithKit

/// Opportunistic background fetching — the mobile replacement for the
/// desktop's n8n scheduled workflows. Two tiers:
///  - refresh: single-request JSON feeds only, no LinkedIn, no LLM. iOS grants
///    a `BGAppRefreshTask` about 30s, so sources are capped at `refreshBudget`.
///  - processing: the fan-out boards (greenhouse/ashby/workable/recruitee) and
///    the keyed APIs (adzuna/usajobs), plus LinkedIn — all of which allow
///    themselves 60–300s and can only finish in a long `BGProcessingTask` window.
///
/// The processing tier does double duty. Besides the *scheduled* run the user
/// opts into, it is where a **user-initiated** search goes to finish when it
/// couldn't fit in the 30 seconds iOS grants a backgrounded app. That
/// continuation is not part of the recurring-search feature and deliberately
/// ignores its opt-in: the user asked for this search, in the foreground,
/// moments ago.
enum BackgroundScheduler {
    static let refreshID = "com.thedevro.jobsmith.standalone.refresh"
    static let processingID = "com.thedevro.jobsmith.standalone.processing"

    /// Sources cheap enough for a BGAppRefreshTask window: one HTTP request
    /// each, no per-board fan-out, no credentials.
    static let refreshSources = ["remoteok", "weworkremotely", "arbeitnow"]

    /// Everything the refresh tier can't finish in ~30s. Runs in the processing
    /// tier, which iOS schedules sparingly but grants minutes for.
    static let processingSources = refreshSources + ["greenhouse", "ashby", "workable",
                                                     "recruitee", "adzuna", "usajobs"]

    /// Per-source ceiling in the refresh tier, well under the ~30s iOS allows,
    /// so a straggler can't eat the window that the upsert still needs.
    static let refreshBudget: Duration = .seconds(8)

    static func register(model: AppModel) {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshID, using: nil) { task in
            handleRefresh(task as! BGAppRefreshTask, model: model)
        }
        BGTaskScheduler.shared.register(forTaskWithIdentifier: processingID, using: nil) { task in
            handleProcessing(task as! BGProcessingTask, model: model)
        }
    }

    // MARK: - User preferences (UserDefaults-backed)

    private static let enabledKey = "jobsmith.bgsearch.enabled"
    private static let intervalHoursKey = "jobsmith.bgsearch.intervalHours"
    private static let scoreInBackgroundKey = "jobsmith.bgsearch.score"
    /// Set while a user-initiated search is parked mid-run. Read at schedule
    /// time so re-arming the recurring tier can't overwrite the continuation
    /// request with one that begins a day from now.
    private static let continuationKey = "jobsmith.search.continuationPending"

    /// Whether recurring background search is on. Off by default — the user
    /// opts in from Settings → Background search.
    static func isEnabled(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: enabledKey)
    }
    static func setEnabled(_ on: Bool, _ defaults: UserDefaults = .standard) {
        defaults.set(on, forKey: enabledKey)
    }

    /// Desired cadence in hours. iOS treats this as an *earliest* begin time,
    /// not a guarantee — runs happen opportunistically. Defaults to 12h.
    static func intervalHours(_ defaults: UserDefaults = .standard) -> Int {
        defaults.object(forKey: intervalHoursKey) as? Int ?? 12
    }
    static func setIntervalHours(_ hours: Int, _ defaults: UserDefaults = .standard) {
        defaults.set(hours, forKey: intervalHoursKey)
    }

    /// Whether a background run should also score what it finds. Off by default:
    /// it only works when the AI endpoint is reachable from wherever the phone
    /// happens to be, which for a self-hosted LM Studio means "at home".
    static func scoresInBackground(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: scoreInBackgroundKey)
    }
    static func setScoresInBackground(_ on: Bool, _ defaults: UserDefaults = .standard) {
        defaults.set(on, forKey: scoreInBackgroundKey)
    }

    static func hasContinuation(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: continuationKey)
    }
    static func clearContinuation(_ defaults: UserDefaults = .standard) {
        defaults.set(false, forKey: continuationKey)
    }

    // MARK: - Scheduling

    /// Ask iOS to run us again as soon as it reasonably can, to finish a search
    /// that ran out of foreground window. Unlike the scheduled tiers this has no
    /// `earliestBeginDate` — there is nothing to wait for, the work is already
    /// half done.
    static func requestContinuation(_ defaults: UserDefaults = .standard) {
        defaults.set(true, forKey: continuationKey)
        submitProcessing(immediate: true)
    }

    /// Arm the next background runs at the user's cadence — a no-op when
    /// recurring search is disabled. The cheap refresh tier is aimed at the
    /// chosen interval; the deeper LinkedIn tier runs on a looser cadence
    /// (2× the interval) since iOS grants those long windows sparingly.
    static func scheduleNext() {
        // A parked search outranks the recurring cadence. Both tiers share one
        // identifier apiece, and submitting replaces whatever was pending — so
        // without this, backgrounding the app would quietly push the
        // continuation out to the next scheduled slot, hours or days away.
        if hasContinuation() {
            submitProcessing(immediate: true)
            return
        }
        guard isEnabled() else { return }
        let hours = Double(intervalHours())

        let refresh = BGAppRefreshTaskRequest(identifier: refreshID)
        refresh.earliestBeginDate = Date(timeIntervalSinceNow: hours * 3600)
        try? BGTaskScheduler.shared.submit(refresh)

        submitProcessing(immediate: false, afterHours: hours * 2)
    }

    private static func submitProcessing(immediate: Bool, afterHours: Double = 0) {
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: processingID)
        let request = BGProcessingTaskRequest(identifier: processingID)
        request.requiresNetworkConnectivity = true
        request.earliestBeginDate = immediate ? nil : Date(timeIntervalSinceNow: afterHours * 3600)
        try? BGTaskScheduler.shared.submit(request)
    }

    /// Clear any pending scheduled runs — used when the user turns recurring
    /// search off so a previously submitted request can't still fire. A parked
    /// continuation is left alone: it belongs to a search the user started by
    /// hand, not to the recurring feature being switched off.
    static func cancelScheduled() {
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: refreshID)
        guard !hasContinuation() else { return }
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: processingID)
    }

    /// `setTaskCompleted` must be called exactly once — and the expiration
    /// handler races the work's own completion, so a plain flag isn't enough.
    /// Without this, an expiring task only cancelled its work and never reported
    /// back, which iOS charges against the app's future background budget.
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

    /// Re-arm the next run, or bail out if the user has since turned recurring
    /// search off. `cancelScheduled()` only drops requests iOS hasn't committed
    /// to yet — once it has decided to launch us, the cancel is a no-op and the
    /// task still arrives here, potentially many hours after the toggle was
    /// flipped (the processing tier's earliest begin date is already 2× the
    /// cadence out, and iOS may sit on it well past that). Enforcing the
    /// preference at run time, not just at schedule time, is what stops a stale
    /// request from fetching behind the user's back. Reporting success — not
    /// failure — keeps a task we simply declined to run from being charged
    /// against the app's future background budget.
    private static func shouldRunScheduled(_ task: BGTask) -> Bool {
        guard isEnabled() else {
            cancelScheduled()
            task.setTaskCompleted(success: true)
            return false
        }
        scheduleNext()
        return true
    }

    private static func handleRefresh(_ task: BGAppRefreshTask, model: AppModel) {
        guard shouldRunScheduled(task) else { return }
        let completion = Completion()
        let work = Task {
            let summary = await runFetch(model: model, sources: refreshSources,
                                         includeLinkedIn: false, budget: refreshBudget)
            await NotificationManager.notifyNewJobs(summary: summary, model: model)
            completion.finish(task, success: !Task.isCancelled)
        }
        task.expirationHandler = {
            work.cancel()
            completion.finish(task, success: false)
        }
    }

    /// The long window. Finishing a parked user-initiated search comes first and
    /// runs whether or not recurring search is switched on; only if there is
    /// nothing to resume does this fall through to the scheduled tier.
    private static func handleProcessing(_ task: BGProcessingTask, model: AppModel) {
        let completion = Completion()
        let work = Task { @MainActor in
            let resumed = await model.resumeInterruptedSearch()

            if resumed {
                // Still unfinished after a full processing window: park it again
                // and ask for another. iOS grants these sparingly, but each pass
                // makes progress, and everything collected is already saved.
                // (The completion notification is `runSearch`'s job — it has the
                // real summary, and we are by definition backgrounded here.)
                if model.isSearchPaused {
                    requestContinuation()
                } else {
                    clearContinuation()
                }
            } else {
                clearContinuation()
                guard isEnabled() else {
                    cancelScheduled()
                    completion.finish(task, success: true)
                    return
                }
                scheduleNext()
                let summary = await runFetch(model: model, sources: processingSources,
                                             includeLinkedIn: true, budget: nil)
                await NotificationManager.notifyNewJobs(summary: summary, model: model)
            }

            await scoreInBackgroundIfEnabled(model: model)
            completion.finish(task, success: !Task.isCancelled)
        }
        task.expirationHandler = {
            work.cancel()
            completion.finish(task, success: false)
        }
    }

    /// Score what the run turned up, when the user has asked for it *and* the
    /// endpoint is actually reachable from wherever the phone is right now.
    ///
    /// The reachability probe is the point: a self-hosted endpoint (LM Studio on
    /// a laptop) is only on the LAN while the user is home. Without the probe,
    /// every background run away from home would burn its window on calls that
    /// cannot succeed. With it, we skip silently and try again next time.
    private static func scoreInBackgroundIfEnabled(model: AppModel) async {
        guard scoresInBackground(), !Task.isCancelled else { return }
        guard await model.isAIEndpointReachable() else { return }
        await model.scoreAllAndWait(cap: model.config.ai.scoreAllCap)
    }

    private static func runFetch(model: AppModel, sources tierSources: [String],
                                 includeLinkedIn: Bool,
                                 budget: Duration?) async -> FetchSummary {
        let config = await ConfigStore.shared.reload()
        let enabled = Set(SourceRegistry.enabledIDs(for: config))
        var sources = tierSources.filter { enabled.contains($0) }
        if includeLinkedIn && enabled.contains(LinkedInSource.id) {
            sources.append(LinkedInSource.id)
        }
        // An empty list means "every registered source" to FetchPipeline — not
        // what we want when this tier's sources are all disabled.
        guard !sources.isEmpty else { return FetchSummary() }
        let jobStore = await model.jobStore
        return await FetchPipeline().run(config: config, sources: sources,
                                         jobStore: jobStore, timeoutBudget: budget)
    }
}
