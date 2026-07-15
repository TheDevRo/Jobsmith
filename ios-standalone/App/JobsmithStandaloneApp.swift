import SwiftUI
import UserNotifications
import JobsmithKit

@main
struct JobsmithStandaloneApp: App {
    @State private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase

    /// Retained for the app's lifetime: UNUserNotificationCenter holds its
    /// delegate weakly, so a local would be deallocated and the action buttons
    /// would silently stop working.
    @State private var notificationDelegate: NotificationDelegate

    init() {
        let model = AppModel()
        _model = State(initialValue: model)
        let delegate = NotificationDelegate(model: model)
        _notificationDelegate = State(initialValue: delegate)
        // BGTask registration must happen before the app finishes launching.
        BackgroundScheduler.register(model: model)
        NotificationManager.requestProvisionalAuthorization()
        NotificationManager.registerCategories()
        UNUserNotificationCenter.current().delegate = delegate
        // The Lock Screen Stop button: StopRunIntent executes in this process
        // and reaches the model through the bridge (the widget target only
        // references the intent type, never this closure).
        RunControlBridge.onStop = { model.stopActiveRun() }
    }

    var body: some Scene {
        WindowGroup {
            RootTabView()
                .environment(model)
                .tint(Theme.ember)
                .onChange(of: scenePhase) { _, phase in
                    switch phase {
                    case .background:
                        model.stopAutoSync()
                        BackgroundScheduler.scheduleNext()
                    case .active:
                        // Pick up jobs saved by the share extension while
                        // the app was backgrounded, then resume foreground
                        // auto-sync (an immediate catch-up cycle + polling).
                        model.refresh()
                        model.startAutoSync()
                        // Finish whatever the last background window cut short.
                        // Returning to the app is the earliest — and by far the
                        // most likely — moment to get a long stretch of
                        // execution, so it beats waiting on iOS to grant a
                        // BGProcessingTask.
                        Task {
                            await model.resumeInterruptedSearch()
                            model.resumeScoringIfNeeded()
                            // With the resume attempts settled, the flags are
                            // accurate — end any Live Activity left over from
                            // a run that no longer exists (e.g. the app was
                            // killed and the run since retired).
                            LiveActivityController.shared.reconcile(model: model)
                        }
                        // Dates change on either device and arrive by sync, so
                        // rebuild the schedule whenever we come back.
                        NotificationManager.rescheduleReminders(model: model)
                    default:
                        break
                    }
                }
                .task { model.startAutoSync() }  // cover the initial launch
        }
    }
}

struct RootTabView: View {
    @Environment(AppModel.self) private var model

    @State private var showOnboarding = false

    var body: some View {
        tabs
            .onShake { model.requestUndo() }
            // The system's own phrasing, and for the same reason: a shake is a
            // gesture you can make without meaning to, so it asks rather than acts.
            .alert("Undo?", isPresented: Binding(
                get: { model.pendingUndo != nil },
                set: { if !$0 { model.pendingUndo = nil } }
            )) {
                Button("Undo") { model.performUndo() }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(model.pendingUndo?.label ?? "")
            }
    }

    private var tabs: some View {
        @Bindable var model = model
        return TabView {
            InboxView()
                .tabItem { Label("Inbox", systemImage: "tray.full") }
                .badge(model.stats.newInInbox)

            PipelineView()
                .tabItem { Label("Pipeline", systemImage: "list.bullet.rectangle") }

            ActivityView()
                .tabItem { Label("Activity", systemImage: "chart.bar") }

            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .alert("Something went wrong", isPresented: Binding(
            get: { model.lastError != nil },
            set: { if !$0 { model.lastError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.lastError ?? "")
        }
        .confirmationDialog(
            "Did you submit the application for \(model.pendingApplyJob?.title ?? "")?",
            isPresented: $showApplyPrompt,
            titleVisibility: .visible
        ) {
            Button("Yes, I applied") { model.resolvePendingApply(applied: true) }
            Button("Not yet — keep it in the pipeline") { model.resolvePendingApply(applied: false) }
            Button("Still working on it", role: .cancel) {}
        }
        .task {
            // Give ConfigStore a beat to load, then gate on an empty profile.
            try? await Task.sleep(for: .milliseconds(300))
            // -SkipOnboarding is a UI-test hook, and the UI tests run against
            // the Debug build — a shipped binary always shows the wizard on a
            // fresh profile.
            #if DEBUG
            let skipOnboarding = CommandLine.arguments.contains("-SkipOnboarding")
            #else
            let skipOnboarding = false
            #endif
            if model.config.profile.isEmpty && !skipOnboarding {
                showOnboarding = true
            }
        }
        .sheet(isPresented: $showOnboarding) {
            OnboardingFlow()
                .environment(model)
        }
        .fullScreenCover(item: $model.applyBrowserJob, onDismiss: {
            // Closing the Apply browser is the signal to ask what happened.
            if model.pendingApplyJob != nil { showApplyPrompt = true }
        }) { job in
            ApplyBrowserView(job: job)
                .environment(model)
        }
    }

    @State private var showApplyPrompt = false
}
