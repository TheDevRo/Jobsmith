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
        let strongFits = (try? await model.jobStore.jobs(triage: "new"))?
            .filter { ($0.fitScore ?? 0) >= 75 }.count ?? 0

        let content = UNMutableNotificationContent()
        content.title = "\(summary.inserted) new job\(summary.inserted == 1 ? "" : "s")"
        content.body = strongFits > 0
            ? "\(strongFits) look like strong fits. Open the Inbox to triage."
            : "Fresh listings are waiting in your Inbox."
        content.sound = .default
        content.userInfo = ["deepLink": "jobsmith-standalone://inbox"]

        let request = UNNotificationRequest(identifier: "new-jobs-\(UUID().uuidString)",
                                            content: content, trigger: nil)
        try? await UNUserNotificationCenter.current().add(request)
    }
}
