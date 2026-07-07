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

    static func scheduleNext() {
        let refresh = BGAppRefreshTaskRequest(identifier: refreshID)
        refresh.earliestBeginDate = Date(timeIntervalSinceNow: 4 * 3600)
        try? BGTaskScheduler.shared.submit(refresh)

        let processing = BGProcessingTaskRequest(identifier: processingID)
        processing.earliestBeginDate = Date(timeIntervalSinceNow: 12 * 3600)
        processing.requiresNetworkConnectivity = true
        try? BGTaskScheduler.shared.submit(processing)
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
