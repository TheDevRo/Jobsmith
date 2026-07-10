import Foundation
import UserNotifications
import JobsmithKit

enum NotificationManager {
    /// Provisional authorization: notifications arrive quietly in
    /// Notification Center without an upfront permission dialog.
    static func requestProvisionalAuthorization() {
        UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .badge, .sound, .provisional]) { _, _ in }
    }

    /// One summary notification per background run, only when there's news.
    static func notifyNewJobs(summary: FetchSummary, model: AppModel) async {
        guard summary.inserted > 0 else { return }
        let strongFits = await strongFitCount(model: model)

        let content = UNMutableNotificationContent()
        content.title = "\(summary.inserted) new job\(summary.inserted == 1 ? "" : "s")"
        content.body = strongFits > 0
            ? "\(strongFits) look like strong fits. Open the Inbox to scout."
            : "Fresh listings are waiting in your Inbox."
        content.sound = .default
        content.userInfo = ["deepLink": "jobsmith-standalone://inbox"]

        let request = UNNotificationRequest(identifier: "new-jobs-\(UUID().uuidString)",
                                            content: content, trigger: nil)
        try? await UNUserNotificationCenter.current().add(request)
    }

    /// Completion notification for a manual search the user kicked off and then
    /// left the app during. Unlike `notifyNewJobs`, this *always* posts — even
    /// with zero new jobs — so a search run in the background gets closure.
    static func notifySearchComplete(summary: FetchSummary, model: AppModel) async {
        let content = UNMutableNotificationContent()
        content.title = summary.inserted > 0
            ? "Search complete · \(summary.inserted) new job\(summary.inserted == 1 ? "" : "s")"
            : "Search complete · no new jobs"
        if summary.inserted > 0 {
            let strongFits = await strongFitCount(model: model)
            content.body = strongFits > 0
                ? "\(strongFits) look like strong fits. Open the Inbox to scout."
                : "Fresh listings are waiting in your Inbox."
        } else {
            content.body = "Nothing new from your sources this time."
        }
        content.sound = .default
        content.userInfo = ["deepLink": "jobsmith-standalone://inbox"]

        let request = UNNotificationRequest(identifier: "search-done-\(UUID().uuidString)",
                                            content: content, trigger: nil)
        try? await UNUserNotificationCenter.current().add(request)
    }

    /// Count of untriaged jobs that scored as strong fits (≥75).
    private static func strongFitCount(model: AppModel) async -> Int {
        (try? await model.jobStore.jobs(triage: "new"))?
            .filter { ($0.fitScore ?? 0) >= 75 }.count ?? 0
    }
}
