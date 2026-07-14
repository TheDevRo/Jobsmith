import SwiftUI
import WebKit

/// In-app LinkedIn sign-in that ends with the user's own profile text: the
/// user logs in normally in a web view, we navigate to their profile
/// (/in/me/), wait for it to render, and hand back the visible text (for
/// the linkedin_import extraction) plus the li_at session cookie.
struct LinkedInSignInSheet: View {
    @Environment(\.dismiss) private var dismiss
    /// (profileText, liAtCookie, jsessionIdCookie)
    let onComplete: (String, String?, String?) -> Void

    @State private var phase: Phase = .signIn
    enum Phase: Equatable {
        case signIn, loadingProfile, extracting
    }

    var body: some View {
        NavigationStack {
            LinkedInWebView(phase: $phase) { text, cookie, jsession in
                dismiss()
                onComplete(text, cookie, jsession)
            }
            .overlay {
                if phase != .signIn {
                    VStack(spacing: 10) {
                        ProgressView()
                        Text(phase == .loadingProfile
                             ? "Opening your profile…"
                             : "Reading your profile…")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    .padding(24)
                    .background(RoundedRectangle(cornerRadius: 16).fill(.thickMaterial))
                }
            }
            .navigationTitle("Sign in to LinkedIn")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}

private struct LinkedInWebView: UIViewRepresentable {
    @Binding var phase: LinkedInSignInSheet.Phase
    /// (profileText, liAtCookie, jsessionIdCookie)
    let onExtracted: (String, String?, String?) -> Void

    func makeUIView(context: Context) -> WKWebView {
        let webView = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        webView.navigationDelegate = context.coordinator
        context.coordinator.webView = webView
        webView.load(URLRequest(url: URL(string: "https://www.linkedin.com/login")!))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        private let parent: LinkedInWebView
        weak var webView: WKWebView?
        private var openedProfile = false
        private var finished = false

        init(_ parent: LinkedInWebView) {
            self.parent = parent
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            guard !finished else { return }
            let path = webView.url?.path.lowercased() ?? ""

            if path.hasPrefix("/in/") {
                // The profile page — give lazy sections a beat, then extract.
                parent.phase = .extracting
                extractText(attempt: 0)
                return
            }

            // Anywhere else (feed, checkpoint done, etc.): once the session
            // cookie exists, the login is complete — jump to the profile.
            guard !openedProfile else { return }
            sessionCookies { [weak self] liAt, _ in
                guard let self, liAt != nil, !self.openedProfile else { return }
                self.openedProfile = true
                self.parent.phase = .loadingProfile
                webView.load(URLRequest(url: URL(string: "https://www.linkedin.com/in/me/")!))
            }
        }

        private func extractText(attempt: Int) {
            guard let webView, !finished else { return }
            // Nudge lazy content, then read the rendered text.
            webView.evaluateJavaScript("window.scrollTo(0, document.body.scrollHeight);",
                                       completionHandler: nil)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) { [weak self] in
                guard let self, let webView = self.webView, !self.finished else { return }
                webView.evaluateJavaScript("document.body.innerText") { result, _ in
                    let text = (result as? String) ?? ""
                    if text.count > 800 || attempt >= 5 {
                        self.finished = true
                        self.sessionCookies { liAt, jsession in
                            self.onDone(text: text, cookie: liAt, jsession: jsession)
                        }
                    } else {
                        self.extractText(attempt: attempt + 1)
                    }
                }
            }
        }

        private func onDone(text: String, cookie: String?, jsession: String?) {
            DispatchQueue.main.async {
                self.parent.onExtracted(text, cookie, jsession)
            }
        }

        /// The two cookies an authenticated LinkedIn session needs: the
        /// persistent `li_at`, and the session `JSESSIONID` whose value the
        /// Voyager API reads back as its `csrf-token`.
        private func sessionCookies(_ completion: @escaping (_ liAt: String?, _ jsession: String?) -> Void) {
            guard let webView else { completion(nil, nil); return }
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
                let liAt = cookies.first { $0.name == "li_at" }?.value
                let jsession = cookies.first { $0.name == "JSESSIONID" }?.value
                completion(liAt, jsession)
            }
        }
    }
}
