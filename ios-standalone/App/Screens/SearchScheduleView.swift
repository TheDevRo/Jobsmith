import SwiftUI
import JobsmithKit

/// Settings → Background search: opt into recurring, opportunistic job fetches
/// and pick a cadence. The scheduling engine lives in `BackgroundScheduler`;
/// this is just the control surface. Runs are fetch-only (no AI scoring) and
/// notify through `NotificationManager` when new jobs arrive.
struct SearchScheduleView: View {
    @State private var enabled = BackgroundScheduler.isEnabled()
    @State private var intervalHours = BackgroundScheduler.intervalHours()

    var body: some View {
        Form {
            Section {
                Toggle("Automatic background search", isOn: $enabled)
                    .onChange(of: enabled) { _, on in
                        BackgroundScheduler.setEnabled(on)
                        if on {
                            BackgroundScheduler.scheduleNext()
                        } else {
                            BackgroundScheduler.cancelScheduled()
                        }
                    }
            } footer: {
                Text("Jobsmith fetches fresh listings from your enabled sources on its own, "
                     + "even when the app is closed, and notifies you when there's something new.")
            }

            Section {
                Picker("Search about every", selection: $intervalHours) {
                    Text("4 hours").tag(4)
                    Text("12 hours").tag(12)
                    Text("Once a day").tag(24)
                }
                .onChange(of: intervalHours) { _, hours in
                    BackgroundScheduler.setIntervalHours(hours)
                    // Re-arm the next run with the new cadence.
                    if BackgroundScheduler.isEnabled() { BackgroundScheduler.scheduleNext() }
                }
                .disabled(!enabled)
            } header: {
                Text("Cadence")
            } footer: {
                Text("iOS decides the exact moment to run based on battery, network, and how "
                     + "you use the app, so this is a target rather than an exact schedule. "
                     + "Background searches only fetch listings — scoring still happens when "
                     + "you open the app.")
            }
        }
        .navigationTitle("Background search")
        .onAppear {
            // Re-sync from persisted prefs: a NavigationLink initializes this
            // view's @State eagerly (before the row is tapped), so without this
            // a reopened screen could show a stale toggle/cadence.
            enabled = BackgroundScheduler.isEnabled()
            intervalHours = BackgroundScheduler.intervalHours()
        }
    }
}
