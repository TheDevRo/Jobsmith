import Foundation
import WebKit

struct IdentifiedURL: Identifiable {
    let id = UUID()
    let url: URL
}

/// Owns the WKWebView and implements its delegates. Mirrors the behavior the
/// Tauri desktop shell gets from `IS_DESKTOP_SHELL` in frontend/js/core.js —
/// but natively: the dashboard stays inside the webview, and anything that
/// leaves the server's origin (job postings, Apply Assist launch pages)
/// opens in Safari, where the Jobsmith Assist extension can run.
@MainActor
final class WebViewStore: NSObject, ObservableObject {
    @Published var isLoading = false
    @Published var loadErrorMessage: String?
    @Published var externalURL: IdentifiedURL?
    @Published var shareFileURL: IdentifiedURL?

    let webView: WKWebView
    private let serverURL: URL

    init(serverURL: URL) {
        self.serverURL = serverURL

        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        // Distinct token (NOT "JobsmithDesktop": that makes the frontend route
        // external links through the backend's /api/system/open-url, which
        // would open the browser on the *server* machine, not this phone).
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
        configuration.applicationNameForUserAgent = "JobsmithiOS/\(version)"

        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()

        #if DEBUG
        if #available(iOS 16.4, *) {
            webView.isInspectable = true
        }
        #endif

        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .automatic

        let refresh = UIRefreshControl()
        refresh.addTarget(self, action: #selector(pullToRefresh(_:)), for: .valueChanged)
        webView.scrollView.refreshControl = refresh
    }

    func loadDashboard() {
        loadErrorMessage = nil
        isLoading = true
        webView.load(URLRequest(url: serverURL, timeoutInterval: 15))
    }

    @objc private func pullToRefresh(_ sender: UIRefreshControl) {
        if webView.url != nil {
            webView.reload()
        } else {
            loadDashboard()
        }
    }

    private func isSameOrigin(_ url: URL) -> Bool {
        guard let host = url.host else { return false }
        let serverPort = serverURL.port ?? (serverURL.scheme == "https" ? 443 : 80)
        let urlPort = url.port ?? (url.scheme == "https" ? 443 : 80)
        return host.caseInsensitiveCompare(serverURL.host ?? "") == .orderedSame && urlPort == serverPort
    }

    private func openExternally(_ url: URL) {
        guard url.scheme == "http" || url.scheme == "https" else {
            UIApplication.shared.open(url)
            return
        }
        externalURL = IdentifiedURL(url: url)
    }

    private func endRefreshing() {
        webView.scrollView.refreshControl?.endRefreshing()
    }
}

// MARK: - WKNavigationDelegate

extension WebViewStore: WKNavigationDelegate {
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }

        // <a download> links (tailored resume / cover letter DOCX).
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
            return
        }

        // target="_blank" without a target frame.
        if navigationAction.targetFrame == nil {
            openExternally(url)
            decisionHandler(.cancel)
            return
        }

        // Main-frame navigation off the server's origin: job postings,
        // assist launch pages → Safari. The dashboard itself never
        // legitimately navigates the main frame off-origin.
        if navigationAction.targetFrame?.isMainFrame == true,
           (url.scheme == "http" || url.scheme == "https"),
           !isSameOrigin(url) {
            openExternally(url)
            decisionHandler(.cancel)
            return
        }

        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if !navigationResponse.canShowMIMEType {
            decisionHandler(.download)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        isLoading = false
        loadErrorMessage = nil
        endRefreshing()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        handleLoadFailure(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handleLoadFailure(error)
    }

    private func handleLoadFailure(_ error: Error) {
        endRefreshing()
        isLoading = false
        let nsError = error as NSError
        // Back/forward swipes and our own .cancel decisions surface as
        // NSURLErrorCancelled — not real failures.
        if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled { return }
        // WebKitErrorFrameLoadInterruptedByPolicyChange (102): our own
        // .cancel / .download policy decisions, not a real failure.
        if nsError.domain == "WebKitErrorDomain" && nsError.code == 102 { return }
        // Only show the full-screen error when nothing is rendered yet;
        // a failed sub-navigation on a live dashboard is recoverable in-page.
        if webView.url == nil || loadErrorMessage != nil || !webView.canGoBack {
            loadErrorMessage = nsError.localizedDescription
        }
    }
}

// MARK: - WKUIDelegate

extension WebViewStore: WKUIDelegate {
    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            openExternally(url)
        }
        return nil
    }

    // The frontend uses confirm() for destructive actions (bulk delete etc.).
    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        presentAlert(message: message, cancellable: true) { confirmed in
            completionHandler(confirmed)
        }
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        presentAlert(message: message, cancellable: false) { _ in
            completionHandler()
        }
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = UIAlertController(title: "Jobsmith", message: prompt, preferredStyle: .alert)
        alert.addTextField { $0.text = defaultText }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(nil) })
        alert.addAction(UIAlertAction(title: "OK", style: .default) { [weak alert] _ in
            completionHandler(alert?.textFields?.first?.text ?? "")
        })
        Self.topViewController()?.present(alert, animated: true)
    }

    private func presentAlert(message: String, cancellable: Bool, completion: @escaping (Bool) -> Void) {
        let alert = UIAlertController(title: "Jobsmith", message: message, preferredStyle: .alert)
        if cancellable {
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completion(false) })
        }
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completion(true) })
        if let top = Self.topViewController() {
            top.present(alert, animated: true)
        } else {
            completion(!cancellable)
        }
    }

    static func topViewController() -> UIViewController? {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        guard let root = scenes.flatMap(\.windows).first(where: \.isKeyWindow)?.rootViewController else {
            return nil
        }
        var top = root
        while let presented = top.presentedViewController {
            top = presented
        }
        return top
    }
}

// MARK: - WKDownloadDelegate

extension WebViewStore: WKDownloadDelegate {
    private static var destinations = [ObjectIdentifier: URL]()

    func download(_ download: WKDownload,
                  decideDestinationUsing response: URLResponse,
                  suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("Downloads-\(UUID().uuidString)", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let destination = dir.appendingPathComponent(suggestedFilename)
            Self.destinations[ObjectIdentifier(download)] = destination
            completionHandler(destination)
        } catch {
            completionHandler(nil)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        endRefreshing()
        if let fileURL = Self.destinations.removeValue(forKey: ObjectIdentifier(download)) {
            shareFileURL = IdentifiedURL(url: fileURL)
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        Self.destinations.removeValue(forKey: ObjectIdentifier(download))
        endRefreshing()
        presentAlert(message: "Download failed: \(error.localizedDescription)", cancellable: false) { _ in }
    }
}
