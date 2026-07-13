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

    // MARK: reminders

    /// A reminder isn't just a nudge — it's the outcome-capture flow.
    ///
    /// The funnel is only as good as the data in it, and the data only exists if
    /// recording it is nearly free. So the reminder carries its answers: long-press
    /// it and say "Heard back" / "Rejected" / "Still waiting" straight from the
    /// lock screen, without opening the app. That is the whole point of putting
    /// reminders on the phone rather than the desktop.
    enum Reminder {
        static let category = "OUTCOME_REMINDER"
        static let heardBack = "OUTCOME_HEARD_BACK"
        static let rejected = "OUTCOME_REJECTED"
        static let stillWaiting = "OUTCOME_STILL_WAITING"
        /// userInfo key carrying the application id the actions apply to.
        static let applicationKey = "applicationId"

        static var actions: UNNotificationCategory {
            UNNotificationCategory(
                identifier: category,
                actions: [
                    UNNotificationAction(identifier: heardBack, title: "Heard back", options: []),
                    UNNotificationAction(identifier: rejected, title: "Rejected", options: []),
                    UNNotificationAction(identifier: stillWaiting, title: "Still waiting", options: []),
                ],
                intentIdentifiers: [])
        }
    }

    /// Prefix for every reminder request id, so rescheduling can clear the old
    /// ones without touching the fetch notifications.
    private static let reminderPrefix = "reminder-"

    static func registerCategories() {
        UNUserNotificationCenter.current().setNotificationCategories([Reminder.actions])
    }

    /// Rebuild every scheduled reminder from the database.
    ///
    /// Wholesale rather than incremental: dates change on either device and
    /// arrive by sync, so there is no reliable local "what changed" signal — and
    /// a stale reminder for an application you already heard back on is worse
    /// than a missing one. Cheap: a job search has tens of applications, not
    /// thousands.
    static func rescheduleReminders(model: AppModel) {
        let center = UNUserNotificationCenter.current()
        center.getPendingNotificationRequests { pending in
            let stale = pending.map(\.identifier).filter { $0.hasPrefix(reminderPrefix) }
            center.removePendingNotificationRequests(withIdentifiers: stale)

            guard let scheduled = try? model.applicationStore.scheduled() else { return }
            for application in scheduled {
                let job = try? model.jobStore.job(id: application.jobId)
                let what = job.map { "\($0.title) at \($0.company)" } ?? "your application"

                if let followUp = application.followUpAt {
                    add(id: "\(reminderPrefix)followup-\(application.id)",
                        title: "Follow up?",
                        body: "No word yet on \(what). Heard anything?",
                        at: followUp, applicationId: application.id)
                }
                if let interview = application.interviewAt {
                    add(id: "\(reminderPrefix)interview-\(application.id)",
                        title: "Interview tomorrow",
                        body: "\(what) — you're interviewing soon.",
                        // A day's notice is the point; firing as it starts is useless.
                        at: interview, applicationId: application.id, offsetDays: -1)
                }
            }
        }
    }

    private static func add(id: String, title: String, body: String, at iso: String,
                            applicationId: String, offsetDays: Int = 0) {
        guard let date = ApplicationStore.parseEventDate(iso)?
            .addingTimeInterval(TimeInterval(offsetDays * 86_400)),
              date > Date()  // never schedule into the past — it fires immediately
        else { return }

        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.categoryIdentifier = Reminder.category
        content.userInfo = [
            Reminder.applicationKey: applicationId,
            "deepLink": "jobsmith-standalone://application/\(applicationId)",
        ]

        let parts = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: date)
        let trigger = UNCalendarNotificationTrigger(dateMatching: parts, repeats: false)
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: id, content: content, trigger: trigger))
    }
}
