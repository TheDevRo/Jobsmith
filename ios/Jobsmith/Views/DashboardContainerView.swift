import SwiftUI

/// Full-screen dashboard: the real Jobsmith web frontend in a WKWebView.
/// External links open in an in-app Safari view (where the Jobsmith Assist
/// Safari extension can run); DOCX downloads surface a share sheet.
struct DashboardContainerView: View {
    @EnvironmentObject private var config: ServerConfig
    @StateObject private var webStore: WebViewStore
    @State private var showServerSheet = false
    @State private var didStartLoading = false

    init(serverURL: URL) {
        _webStore = StateObject(wrappedValue: WebViewStore(serverURL: serverURL))
    }

    var body: some View {
        // Full-bleed like Safari: the page paints behind the status bar and
        // home indicator; contentInsetAdjustmentBehavior keeps content clear
        // of the notch.
        WebViewContainer(store: webStore)
            .ignoresSafeArea()
            .overlay {
                if let message = webStore.loadErrorMessage {
                    LoadErrorView(
                        message: message,
                        retry: { webStore.loadDashboard() },
                        changeServer: { showServerSheet = true }
                    )
                }
            }
            .onAppear {
                guard !didStartLoading else { return }
                didStartLoading = true
                webStore.loadDashboard()
            }
            .onShake { showServerSheet = true }
            .fullScreenCover(item: $webStore.externalURL) { item in
                SafariView(url: item.url)
                    .ignoresSafeArea()
            }
            .sheet(item: $webStore.shareFileURL) { item in
                ShareSheet(items: [item.url])
            }
            .sheet(isPresented: $showServerSheet) {
                ServerSetupView(isFirstRun: false)
                    .environmentObject(config)
            }
    }
}

private struct LoadErrorView: View {
    let message: String
    let retry: () -> Void
    let changeServer: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 44))
                .foregroundStyle(.secondary)
            Text("Can't reach your Jobsmith server")
                .font(.headline)
            Text(message)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            HStack(spacing: 12) {
                Button(action: retry) {
                    Label("Retry", systemImage: "arrow.clockwise")
                }
                .buttonStyle(.borderedProminent)
                Button(action: changeServer) {
                    Label("Change Server", systemImage: "server.rack")
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(uiColor: .systemBackground))
    }
}
