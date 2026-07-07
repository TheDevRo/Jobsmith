import SwiftUI
import JobsmithKit

@main
struct JobsmithStandaloneApp: App {
    @State private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let model = AppModel()
        _model = State(initialValue: model)
        // BGTask registration must happen before the app finishes launching.
        BackgroundScheduler.register(model: model)
        NotificationManager.requestProvisionalAuthorization()
    }

    var body: some Scene {
        WindowGroup {
            RootTabView()
                .environment(model)
                .tint(Theme.ember)
                .onChange(of: scenePhase) { _, phase in
                    switch phase {
                    case .background:
                        BackgroundScheduler.scheduleNext()
                    case .active:
                        // Pick up jobs saved by the share extension while
                        // the app was backgrounded.
                        model.refresh()
                    default:
                        break
                    }
                }
        }
    }
}

struct RootTabView: View {
    @Environment(AppModel.self) private var model

    @State private var showOnboarding = false

    var body: some View {
        TabView {
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
        .onChange(of: scenePhase) { _, phase in
            if phase == .active && model.pendingApplyJob != nil {
                showApplyPrompt = true
            }
        }
        .task {
            // Give ConfigStore a beat to load, then gate on an empty profile.
            try? await Task.sleep(for: .milliseconds(300))
            if model.config.profile.isEmpty && !CommandLine.arguments.contains("-SkipOnboarding") {
                showOnboarding = true
            }
        }
        .sheet(isPresented: $showOnboarding) {
            OnboardingFlow()
                .environment(model)
        }
    }

    @State private var showApplyPrompt = false
    @Environment(\.scenePhase) private var scenePhase
}
