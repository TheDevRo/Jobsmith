import SwiftUI
import WebKit

/// Everything the sign-in sheet hands back: the rendered profile text (for
/// the linkedin_import extraction), the two cookies worth persisting, and the
/// complete linkedin.com cookie jar the login produced.
struct LinkedInSignInResult {
    let profileText: String
    let liAt: String?
    let jsessionId: String?
    /// Every linkedin.com cookie in the sheet's ephemeral store. The Apply
    /// browser must inject the FULL set: `li_at` arriving without its
    /// companion device cookies (bcookie/bscookie/lidc/…) fails LinkedIn's
    /// session check, which 302-loops the load instead of rendering it —
    /// making a fresh sign-in look exactly like a stale one.
    let cookies: [HTTPCookie]
}

/// In-app LinkedIn sign-in that ends with the user's own profile text: the
/// user logs in normally in a web view, we navigate to their profile
/// (/in/me/), wait for it to render, and hand back the visible text (for
/// the linkedin_import extraction) plus the session cookies.
struct LinkedInSignInSheet: View {
    @Environment(\.dismiss) private var dismiss
    let onComplete: (LinkedInSignInResult) -> Void

    @State private var phase: Phase = .signIn
    enum Phase: Equatable {
        case signIn, loadingProfile, extracting
    }

    var body: some View {
        NavigationStack {
            LinkedInWebView(phase: $phase) { result in
                dismiss()
                onComplete(result)
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
            .safeAreaInset(edge: .bottom) {
                Text("You sign in on linkedin.com itself. Your session never leaves this device: two cookies are kept in the Keychain and the rest lives only in the in-app Apply browser — used solely to show postings and Easy Apply as you.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 12)
                    .frame(maxWidth: .infinity)
                    .background(.bar)
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
    let onExtracted: (LinkedInSignInResult) -> Void

    func makeUIView(context: Context) -> WKWebView {
        // Ephemeral store: the LinkedIn session lives only for this sheet. The
        // two cookies we keep (li_at/JSESSIONID) are copied out to the Keychain
        // via the completion; when the view is torn down the store — and the
        // rest of the logged-in session — is discarded rather than lingering in
        // the app's shared cookie jar. The cookie read below still works: it
        // reads from this store's own httpCookieStore.
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: configuration)
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
            sessionCookies { [weak self] cookies in
                guard let self, cookies.contains(where: { $0.name == "li_at" }),
                      !self.openedProfile else { return }
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
                        self.sessionCookies { cookies in
                            self.onDone(text: text, cookies: cookies)
                        }
                    } else {
                        self.extractText(attempt: attempt + 1)
                    }
                }
            }
        }

        private func onDone(text: String, cookies: [HTTPCookie]) {
            DispatchQueue.main.async {
                self.parent.onExtracted(LinkedInSignInResult(
                    profileText: text,
                    liAt: cookies.first { $0.name == "li_at" }?.value,
                    jsessionId: cookies.first { $0.name == "JSESSIONID" }?.value,
                    cookies: cookies))
            }
        }

        /// The login's linkedin.com cookie jar. `li_at` is the persistent
        /// session; `JSESSIONID`'s value doubles as the Voyager `csrf-token`;
        /// the rest are the device cookies the session is validated against.
        private func sessionCookies(_ completion: @escaping ([HTTPCookie]) -> Void) {
            guard let webView else { completion([]); return }
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
                completion(cookies.filter {
                    $0.domain.lowercased().hasSuffix("linkedin.com")
                })
            }
        }
    }
}
