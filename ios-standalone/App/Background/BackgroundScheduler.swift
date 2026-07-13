import Foundation
import BackgroundTasks
import JobsmithKit

/// Opportunistic background fetching — the mobile replacement for the
/// desktop's n8n scheduled workflows. Two tiers:
///  - refresh: single-request JSON feeds only, no LinkedIn, no LLM. iOS grants
///    a `BGAppRefreshTask` about 30s, so sources are capped at `refreshBudget`.
///  - processing: the fan-out boards (greenhouse/ashby/workable/recruitee) and
///    the keyed APIs (adzuna/usajobs), plus LinkedIn — all of which allow
///    themselves 60–300s and can only finish in a long `BGProcessingTask` window.
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

    // MARK: - Scheduling

    /// Arm the next background runs at the user's cadence — a no-op when
    /// recurring search is disabled. The cheap refresh tier is aimed at the
    /// chosen interval; the deeper LinkedIn tier runs on a looser cadence
    /// (2× the interval) since iOS grants those long windows sparingly.
    static func scheduleNext() {
        guard isEnabled() else { return }
        let hours = Double(intervalHours())

        let refresh = BGAppRefreshTaskRequest(identifier: refreshID)
        refresh.earliestBeginDate = Date(timeIntervalSinceNow: hours * 3600)
        try? BGTaskScheduler.shared.submit(refresh)

        let processing = BGProcessingTaskRequest(identifier: processingID)
        processing.earliestBeginDate = Date(timeIntervalSinceNow: hours * 2 * 3600)
        processing.requiresNetworkConnectivity = true
        try? BGTaskScheduler.shared.submit(processing)
    }

    /// Clear any pending scheduled runs — used when the user turns recurring
    /// search off so a previously submitted request can't still fire.
    static func cancelScheduled() {
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: refreshID)
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

    private static func handleRefresh(_ task: BGAppRefreshTask, model: AppModel) {
        scheduleNext()
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

    private static func handleProcessing(_ task: BGProcessingTask, model: AppModel) {
        scheduleNext()
        let completion = Completion()
        let work = Task {
            let summary = await runFetch(model: model, sources: processingSources,
                                         includeLinkedIn: true, budget: nil)
            await NotificationManager.notifyNewJobs(summary: summary, model: model)
            completion.finish(task, success: !Task.isCancelled)
        }
        task.expirationHandler = {
            work.cancel()
            completion.finish(task, success: false)
        }
    }

    private static func runFetch(model: AppModel, sources tierSources: [String],
                                 includeLinkedIn: Bool,
                                 budget: Duration?) async -> FetchSummary {
        let config = await ConfigStore.shared.reload()
        var sources = tierSources.filter { config.search.enabledSources.contains($0) }
        if includeLinkedIn && config.search.enabledSources.contains("linkedin") {
            sources.append("linkedin")
        }
        // An empty list means "every registered source" to FetchPipeline — not
        // what we want when this tier's sources are all disabled.
        guard !sources.isEmpty else { return FetchSummary() }
        let jobStore = await model.jobStore
        return await FetchPipeline().run(config: config, sources: sources,
                                         jobStore: jobStore, timeoutBudget: budget)
    }
}
