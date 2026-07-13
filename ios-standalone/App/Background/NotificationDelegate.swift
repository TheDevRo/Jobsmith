import Foundation
import UserNotifications
import JobsmithKit

/// Handles taps and action buttons on notifications.
///
/// Two things were broken before this existed:
///   * Notifications wrote `userInfo["deepLink"]` but nothing ever read it, so
///     tapping one just opened the app to wherever you left it.
///   * There was no way to answer a reminder without opening the app.
///
/// The action buttons are the point. A reminder that makes you launch the app,
/// find the job, and pick from a menu is a reminder you'll dismiss — and then the
/// funnel stays empty. Answering from the lock screen makes recording an outcome
/// almost free, which is the only way the data ever gets collected.
@MainActor
final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    private let model: AppModel

    init(model: AppModel) {
        self.model = model
        super.init()
    }

    /// Show reminders even while the app is in the foreground — otherwise an
    /// interview alert is silently swallowed by the app being open.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let info = response.notification.request.content.userInfo
        let applicationId = info[NotificationManager.Reminder.applicationKey] as? String

        switch response.actionIdentifier {
        case NotificationManager.Reminder.heardBack:
            record(applicationId, .screening)
        case NotificationManager.Reminder.rejected:
            record(applicationId, .rejected)
        case NotificationManager.Reminder.stillWaiting:
            // Deliberately records nothing: "still waiting" is the state it's
            // already in, and writing an event would just pad the history. Push
            // the nudge out a week so it asks again instead of going quiet.
            snooze(applicationId, days: 7)
        case UNNotificationDefaultActionIdentifier:
            // A plain tap opens the app; route it at the job if we know one.
            openJob(for: applicationId)
        default:
            break
        }
    }

    private func record(_ applicationId: String?, _ outcome: ApplicationOutcome) {
        guard let applicationId,
              let application = try? model.applicationStore.application(id: applicationId)
        else { return }
        model.setOutcome(jobId: application.jobId, outcome)
        NotificationManager.rescheduleReminders(model: model)
    }

    private func snooze(_ applicationId: String?, days: Int) {
        guard let applicationId else { return }
        let next = Date().addingTimeInterval(TimeInterval(days * 86_400))
        try? model.applicationStore.setSchedule(
            id: applicationId, followUpAt: ApplicationStore.isoMs(next))
        model.refresh()
        NotificationManager.rescheduleReminders(model: model)
        Task { await model.syncNow() }
    }

    private func openJob(for applicationId: String?) {
        guard let applicationId,
              let application = try? model.applicationStore.application(id: applicationId)
        else { return }
        model.deepLinkedJobId = application.jobId
    }
}
