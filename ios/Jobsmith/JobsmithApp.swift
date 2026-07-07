import SwiftUI

@main
struct JobsmithApp: App {
    @StateObject private var config = ServerConfig()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(config)
        }
    }
}

struct RootView: View {
    @EnvironmentObject private var config: ServerConfig

    var body: some View {
        if let serverURL = config.serverURL {
            // .id(serverURL) recreates the whole webview stack when the
            // server changes, so DashboardContainerView can own its
            // WebViewStore for one fixed server URL.
            DashboardContainerView(serverURL: serverURL)
                .id(serverURL)
        } else {
            ServerSetupView(isFirstRun: true)
        }
    }
}
