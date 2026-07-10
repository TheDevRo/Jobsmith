import Foundation
import BackgroundTasks
import JobsmithKit

/// Opportunistic background fetching — the mobile replacement for the
/// desktop's n8n scheduled workflows. Two tiers:
///  - refresh: cheap JSON-API sources only, no LinkedIn, no LLM (~20s budget)
///  - processing: LinkedIn + batch scoring, when iOS grants a long window
enum BackgroundScheduler {
    static let refreshID = "com.thedevro.jobsmith.standalone.refresh"
    static let processingID = "com.thedevro.jobsmith.standalone.processing"

    /// Sources cheap enough for a BGAppRefreshTask window.
    static let refreshSources = ["remoteok", "weworkremotely", "arbeitnow",
                                 "greenhouse", "ashby", "workable", "recruitee",
                                 "adzuna", "usajobs"]

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

    private static func handleRefresh(_ task: BGAppRefreshTask, model: AppModel) {
        scheduleNext()
        let work = Task {
            let summary = await runFetch(model: model, includeLinkedIn: false)
            await NotificationManager.notifyNewJobs(summary: summary, model: model)
            task.setTaskCompleted(success: true)
        }
        task.expirationHandler = { work.cancel() }
    }

    private static func handleProcessing(_ task: BGProcessingTask, model: AppModel) {
        scheduleNext()
        let work = Task {
            let summary = await runFetch(model: model, includeLinkedIn: true)
            await NotificationManager.notifyNewJobs(summary: summary, model: model)
            task.setTaskCompleted(success: true)
        }
        task.expirationHandler = { work.cancel() }
    }

    private static func runFetch(model: AppModel, includeLinkedIn: Bool) async -> FetchSummary {
        let config = await ConfigStore.shared.reload()
        var sources = refreshSources.filter { config.search.enabledSources.contains($0) }
        if includeLinkedIn && config.search.enabledSources.contains("linkedin") {
            sources.append("linkedin")
        }
        let jobStore = await model.jobStore
        return await FetchPipeline().run(config: config, sources: sources, jobStore: jobStore)
    }
}
