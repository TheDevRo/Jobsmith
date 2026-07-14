import SwiftUI
import WebKit
import UIKit
import JobsmithKit

/// In-app "Apply browser": loads the posting in a WKWebView, injects the
/// bundled snapshot.js/fill.js, maps the form's fields on-device via
/// `AppModel.mapApplyFields`, and fills them. A fallback panel offers
/// tap-to-copy values and document export for anything the injector can't set
/// (file inputs, unfilled required fields) — replacing the old Safari Web
/// Extension, which couldn't run inside an in-app browser and required
/// per-site permission on real Safari.
struct ApplyBrowserView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let job: Job

    @State private var controller = ApplyWebController()
    @State private var status = "Load the form, then tap Autofill."
    @State private var busy = false
    @State private var rows: [ApplyFieldRow] = []
    @State private var showPanel = false
    @State private var didStart = false
    @State private var showLinkedInSignIn = false

    private var jobURL: URL? { URL(string: job.url) }

    private var isLinkedIn: Bool {
        jobURL?.host?.lowercased().hasSuffix("linkedin.com") ?? false
    }

    /// The stored `li_at` session cookie, or nil if the user hasn't signed in.
    private var storedLinkedInCookie: String? {
        let cookie = model.config.apiKeys.linkedInCookie
        return cookie.isEmpty ? nil : cookie
    }

    /// The stored `JSESSIONID`, or nil. Needed (in addition to `li_at`) for
    /// LinkedIn's authenticated actions like Easy Apply — see `APIKeys`.
    private var storedLinkedInJSessionId: String? {
        let cookie = model.config.apiKeys.linkedInJSessionId
        return cookie.isEmpty ? nil : cookie
    }

    /// LinkedIn browsing works on `li_at` alone, but Easy Apply's Voyager POST
    /// needs a live `JSESSIONID` too. Prompt a (fresh) sign-in when either is
    /// missing rather than loading a half-authenticated session whose Apply
    /// button silently 401s.
    private var needsLinkedInSignIn: Bool {
        isLinkedIn && (storedLinkedInCookie == nil || storedLinkedInJSessionId == nil)
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if jobURL != nil {
                    ApplyWebView(controller: controller)
                } else {
                    ContentUnavailableView("No application URL",
                                           systemImage: "link.badge.plus")
                }
                if needsLinkedInSignIn {
                    linkedInSignInBanner
                }
                bottomBar
            }
            .navigationTitle(job.company.isEmpty ? "Apply" : job.company)
            .navigationBarTitleDisplayMode(.inline)
            .task {
                guard !didStart, let url = jobURL else { return }
                didStart = true
                controller.start(url: url,
                                 liAtCookie: isLinkedIn ? storedLinkedInCookie : nil,
                                 jsessionId: isLinkedIn ? storedLinkedInJSessionId : nil)
            }
            .sheet(isPresented: $showLinkedInSignIn) {
                LinkedInSignInSheet { _, cookie, jsession in
                    handleLinkedInSignIn(cookie, jsession)
                }
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Done") { dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    if let url = URL(string: job.url) {
                        Button {
                            UIApplication.shared.open(url)
                        } label: {
                            Image(systemName: "safari")
                        }
                        .accessibilityLabel("Open in Safari")
                    }
                }
            }
            .sheet(isPresented: $showPanel) {
                ApplyFallbackPanel(job: job, rows: rows)
                    .presentationDetents([.medium, .large])
            }
        }
    }

    private var bottomBar: some View {
        VStack(spacing: 8) {
            Text(status)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .lineLimit(2)
            HStack(spacing: 12) {
                Button(action: autofill) {
                    Label(busy ? "Working…" : "Autofill", systemImage: "wand.and.stars")
                        .font(.callout.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                }
                .buttonStyle(.borderedProminent)
                .disabled(busy || URL(string: job.url) == nil)

                if !rows.isEmpty {
                    Button {
                        showPanel = true
                    } label: {
                        Label("Answers", systemImage: "list.clipboard")
                            .font(.callout.weight(.semibold))
                            .padding(.vertical, 10)
                            .padding(.horizontal, 6)
                    }
                    .buttonStyle(.bordered)
                }
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 10)
        .background(.bar)
    }

    private var linkedInSignInBanner: some View {
        HStack(spacing: 10) {
            Image(systemName: "person.crop.circle.badge.checkmark")
                .foregroundStyle(.secondary)
            Text("Sign in to see this LinkedIn posting behind your own session.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Button("Sign in") { showLinkedInSignIn = true }
                .font(.footnote.weight(.semibold))
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.thinMaterial)
    }

    /// Persist the captured `li_at` + `JSESSIONID` cookies (same store
    /// onboarding writes) and reload the posting so it renders — and can Easy
    /// Apply — behind the fresh session.
    private func handleLinkedInSignIn(_ cookie: String?, _ jsession: String?) {
        guard let cookie, !cookie.isEmpty else { return }
        model.saveConfig {
            $0.apiKeys.linkedInCookie = cookie
            $0.apiKeys.linkedInJSessionId = jsession ?? ""
        }
        if let url = jobURL {
            controller.start(url: url, liAtCookie: cookie, jsessionId: jsession)
        }
    }

    // MARK: - Autofill flow

    private func autofill() {
        busy = true
        status = "Scanning form…"
        Task {
            do {
                let snap = try await controller.snapshot()
                guard !snap.fields.isEmpty else {
                    status = "No form fields found — the page may still be loading, "
                        + "behind a login, or not a web form."
                    busy = false
                    return
                }
                status = "Mapping \(snap.fields.count) field\(snap.fields.count == 1 ? "" : "s")…"
                let values = await model.mapApplyFields(snap.fields, job: job)
                var items = Self.buildFillItems(descriptors: snap.fields, values: values,
                                                documents: uploadDocuments(for: values))
                status = "Filling…"
                // Phase 2 — shrink the snapshot→fill window. Mapping above can
                // take seconds (LLM), during which an SPA form may re-render and
                // drop the data-jobsmith-fid stamps snapshot.js applied. Re-run
                // snapshot to re-stamp the current nodes under the same stable
                // field_ids, then refresh each item's selector from the fresh
                // descriptors (matched by field_id) so fill.js targets live nodes.
                if let fresh = try? await controller.snapshot() {
                    var freshSelectors: [String: String] = [:]
                    for f in fresh.fields {
                        if let fid = f["field_id"] as? String,
                           let sel = f["_selector"] as? String {
                            freshSelectors[fid] = sel
                        }
                    }
                    for i in items.indices {
                        if let fid = items[i]["field_id"] as? String,
                           let sel = freshSelectors[fid] {
                            items[i]["selector"] = sel
                        }
                    }
                }
                let fillResults = try await controller.fill(items: items)
                rows = Self.buildRows(descriptors: snap.fields, values: values,
                                      fillResults: fillResults)
                let filled = fillResults.filter { ($0["status"] as? String) == "filled" }.count
                let pendingUploads = rows.contains { $0.isUpload && !$0.wasFilled }
                status = pendingUploads
                    ? "Filled \(filled) of \(items.count). Review the page, attach documents, then submit."
                    : "Filled \(filled) of \(items.count). Review the page, then submit."
                showPanel = true
            } catch {
                status = "Autofill failed: \(error.localizedDescription)"
            }
            busy = false
        }
    }

    /// Read the tailored documents referenced by the mapping's upload items
    /// ("resume"/"cover_letter" kind tokens in `value`) out of FileVault, so
    /// fill.js can attach them to `<input type=file>` the same way the desktop
    /// extension does. Base64 keeps the payload a plain string through
    /// `callAsyncJavaScript`; the fill wrapper decodes it back to bytes.
    private func uploadDocuments(for values: [[String: Any]]) -> [String: ApplyUploadDocument] {
        let kinds = Set(values.compactMap { v -> String? in
            guard (v["action"] as? String) == "upload" else { return nil }
            return v["value"] as? String
        })
        let format = model.config.honesty.documentFormat
        var docs: [String: ApplyUploadDocument] = [:]
        for token in kinds {
            guard let kind = FileVault.Kind(rawValue: token),
                  let data = FileVault.read(jobId: job.id, kind: kind, format: format) else {
                continue
            }
            docs[token] = ApplyUploadDocument(
                base64: data.base64EncodedString(),
                name: FileVault.exportFilename(name: model.config.profile.fullName,
                                               company: job.company,
                                               kind: kind, format: format),
                mime: format.mime)
        }
        return docs
    }

    // MARK: - Merge helpers (ported from SafariExt/Resources/sidepanel.js)

    /// Join each mapped `FieldValue` with its snapshot descriptor by `field_id`
    /// into the fill-payload shape `fill.js` expects. `options` is always an
    /// array (never null) so it survives `callAsyncJavaScript` serialization.
    /// `documents` (kind token → tailored document) hydrates upload items with
    /// the file payload, mirroring the extension side panel's `bytesFor`.
    static func buildFillItems(descriptors: [[String: Any]],
                               values: [[String: Any]],
                               documents: [String: ApplyUploadDocument] = [:]) -> [[String: Any]] {
        var descById: [String: [String: Any]] = [:]
        for d in descriptors {
            if let fid = d["field_id"] as? String { descById[fid] = d }
        }
        var items: [[String: Any]] = []
        for v in values {
            guard let fid = v["field_id"] as? String else { continue }
            let d = descById[fid] ?? [:]
            var item: [String: Any] = [
                "field_id": fid,
                "selector": d["_selector"] as? String ?? "",
                // Fallback selector (id/name) so fill.js can re-locate the field
                // if an SPA re-render dropped the injected data-jobsmith-fid.
                "human_selector": d["_human_selector"] as? String ?? "",
                "name": d["name"] as? String ?? "",
                "value": v["value"] as? String ?? "",
                "action": v["action"] as? String ?? "fill",
                "field_type": d["field_type"] as? String ?? "text",
                "confidence": (v["confidence"] as? Double) ?? 1.0,
                "source": v["source"] as? String ?? "",
                "options": d["options"] as? [String] ?? [],
                "required": d["required"] as? Bool ?? false,
                "_combobox": d["_combobox"] as? Bool ?? false,
            ]
            if (v["action"] as? String) == "upload",
               let doc = documents[v["value"] as? String ?? ""] {
                item["file_b64"] = doc.base64
                item["file_name"] = doc.name
                item["file_mime"] = doc.mime
            }
            items.append(item)
        }
        return items
    }

    /// Build the fallback-panel rows from descriptors + mapped values + the
    /// per-field fill outcome. Unfilled/required and file uploads sort first.
    static func buildRows(descriptors: [[String: Any]],
                          values: [[String: Any]],
                          fillResults: [[String: Any]]) -> [ApplyFieldRow] {
        var descById: [String: [String: Any]] = [:]
        for d in descriptors {
            if let fid = d["field_id"] as? String { descById[fid] = d }
        }
        var statusById: [String: String] = [:]
        var messageById: [String: String] = [:]
        for r in fillResults {
            if let fid = r["field_id"] as? String {
                statusById[fid] = r["status"] as? String
                messageById[fid] = r["message"] as? String
            }
        }
        var out: [ApplyFieldRow] = []
        for v in values {
            guard let fid = v["field_id"] as? String else { continue }
            let d = descById[fid] ?? [:]
            let label = (d["label"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                ?? (d["name"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                ?? fid
            out.append(ApplyFieldRow(
                id: fid,
                label: label,
                value: v["value"] as? String ?? "",
                source: v["source"] as? String ?? "",
                action: v["action"] as? String ?? "fill",
                fillStatus: statusById[fid],
                required: d["required"] as? Bool ?? false,
                fillMessage: messageById[fid]))
        }
        // Attention-first ordering: unresolved before resolved.
        return out.sorted { $0.attentionRank < $1.attentionRank }
    }
}

/// A tailored document staged for in-page attachment, base64-encoded so it
/// crosses the `callAsyncJavaScript` bridge as a string.
struct ApplyUploadDocument {
    let base64: String
    let name: String
    let mime: String
}

/// One field's outcome, shown in the fallback panel.
struct ApplyFieldRow: Identifiable {
    let id: String
    let label: String
    let value: String
    let source: String
    let action: String
    let fillStatus: String?
    let required: Bool
    /// fill.js's per-field diagnostic ("attached X → #resume", "this uploader
    /// rejects scripted files — …"), so a failed fill is explainable on-device.
    var fillMessage: String? = nil

    var isUpload: Bool { action == "upload" }
    var isSkipped: Bool { action == "skip" }
    var copyable: Bool { !isUpload && !isSkipped && !value.isEmpty }
    var wasFilled: Bool { fillStatus == "filled" }

    /// Lower sorts first: unattached uploads and required-but-unfilled need
    /// attention. An upload fill.js managed to attach is resolved.
    var attentionRank: Int {
        if isUpload { return wasFilled ? 3 : 0 }
        if required && !wasFilled { return 1 }
        if !wasFilled { return 2 }
        return 3
    }
}

// MARK: - WKWebView driver

/// Owns the WKWebView and exposes async snapshot/fill helpers. Kept out of the
/// SwiftUI view so the injected-script calls read cleanly.
@MainActor
final class ApplyWebController: NSObject, ObservableObject, WKUIDelegate, WKNavigationDelegate {
    /// The primary web view showing the posting. `SwiftUI` renders
    /// `containerView`; the primary (or a child spawned by an external-apply
    /// window.open) is mounted inside it, so a handoff can be shown in place.
    let webView: WKWebView
    let containerView = UIView()

    /// The web view autofill currently targets — the primary, or the child an
    /// external-apply handoff navigated into (so Autofill works on the visible
    /// ATS form, not the hidden posting behind it).
    private(set) var activeWebView: WKWebView

    /// Child web views spawned by `window.open`. Capped so a misbehaving opener
    /// can't spawn unbounded web views.
    private var childWebViews: [WKWebView] = []
    private let maxChildWebViews = 4

    override init() {
        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.allowsBackForwardNavigationGestures = true
        activeWebView = webView
        super.init()
        webView.uiDelegate = self
        webView.navigationDelegate = self
        mount(webView)
    }

    /// Show `view` filling the container, replacing whatever was mounted, and
    /// route autofill at it.
    private func mount(_ view: WKWebView) {
        activeWebView = view
        containerView.subviews.forEach { $0.removeFromSuperview() }
        view.translatesAutoresizingMaskIntoConstraints = false
        containerView.addSubview(view)
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: containerView.topAnchor),
            view.bottomAnchor.constraint(equalTo: containerView.bottomAnchor),
            view.leadingAnchor.constraint(equalTo: containerView.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: containerView.trailingAnchor),
        ])
    }

    /// LinkedIn's external Apply button (and many ATS redirect shims) hand off
    /// to the company site via `window.open` / `target="_blank"`. WKWebView
    /// routes those here and drops them without a UI delegate.
    ///
    /// Two shapes:
    ///  - carries a URL → load it in the opener (swipe-back returns to the
    ///    posting), which is enough for most `target="_blank"` links.
    ///  - blank/`about:blank` then-navigate → the opener opens an empty window
    ///    and later sets `location` on the returned handle. Returning `nil`
    ///    drops that flow, so hand back a real child web view (created with the
    ///    passed configuration, as WKWebView requires) presented in place.
    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil else { return nil }
        let url = navigationAction.request.url

        if url == nil || url?.absoluteString == "about:blank" {
            return makeChildWebView(configuration)
        }
        guard let url else { return nil }
        if url.scheme == "http" || url.scheme == "https" {
            webView.load(navigationAction.request)
        } else if UIApplication.shared.canOpenURL(url) {
            // Non-web schemes (mailto:, tel:, …) can't render in the web view.
            UIApplication.shared.open(url)
        }
        return nil
    }

    private func makeChildWebView(_ configuration: WKWebViewConfiguration) -> WKWebView? {
        guard childWebViews.count < maxChildWebViews else { return nil }
        let child = WKWebView(frame: .zero, configuration: configuration)
        child.allowsBackForwardNavigationGestures = true
        child.uiDelegate = self
        child.navigationDelegate = self
        childWebViews.append(child)
        mount(child)
        return child
    }

    /// A child that scripts `window.close()` — pop it and re-show whatever was
    /// underneath (the previous child, or the primary posting).
    func webViewDidClose(_ webView: WKWebView) {
        guard let idx = childWebViews.firstIndex(of: webView) else { return }
        childWebViews.remove(at: idx)
        mount(childWebViews.last ?? self.webView)
    }

    // MARK: WKNavigationDelegate — allow (incl. cross-origin apply redirects)
    // and log, so a dropped handoff is diagnosable.

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url {
            NSLog("[Apply] navigate → %@", url.absoluteString)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        NSLog("[Apply] provisional navigation failed: %@", error.localizedDescription)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        NSLog("[Apply] navigation failed: %@", error.localizedDescription)
    }

    /// Inject the stored LinkedIn session cookies (if any) into the web view's
    /// cookie store, then load — so LinkedIn postings render, and can Easy
    /// Apply, behind the user's own session instead of the logged-out wall.
    /// `li_at` alone renders logged-in but the Easy-Apply POST needs a live
    /// `JSESSIONID` (its value is LinkedIn's csrf-token). Both are scoped to
    /// `.linkedin.com`, so they're never sent to other ATS hosts.
    func start(url: URL, liAtCookie: String?, jsessionId: String?) {
        var cookies: [HTTPCookie] = []
        if let liAtCookie, !liAtCookie.isEmpty,
           let cookie = Self.linkedInSessionCookie(value: liAtCookie) {
            cookies.append(cookie)
        }
        if let jsessionId, !jsessionId.isEmpty,
           let cookie = Self.linkedInJSessionCookie(value: jsessionId) {
            cookies.append(cookie)
        }
        guard !cookies.isEmpty else { load(url); return }
        setCookies(cookies, thenLoad: url)
    }

    /// Set each cookie in turn (the store's completion fires per-cookie), then
    /// load — so the request goes out with every cookie already in place.
    private func setCookies(_ cookies: [HTTPCookie], thenLoad url: URL) {
        let store = webView.configuration.websiteDataStore.httpCookieStore
        var remaining = cookies
        func next() {
            guard let cookie = remaining.first else { load(url); return }
            remaining.removeFirst()
            store.setCookie(cookie) { next() }
        }
        next()
    }

    func load(_ url: URL) {
        if activeWebView !== webView { mount(webView) }
        webView.load(URLRequest(url: url))
    }

    static func linkedInSessionCookie(value: String) -> HTTPCookie? {
        HTTPCookie(properties: [
            .domain: ".linkedin.com",
            .path: "/",
            .name: "li_at",
            .value: value,
            .secure: "TRUE",
        ])
    }

    static func linkedInJSessionCookie(value: String) -> HTTPCookie? {
        HTTPCookie(properties: [
            .domain: ".linkedin.com",
            .path: "/",
            .name: "JSESSIONID",
            .value: value,
            .secure: "TRUE",
        ])
    }

    /// Inject snapshot.js; its IIFE returns `{ url, fields }` directly. Targets
    /// the active web view so autofill scans the visible page (including an
    /// external-apply child), not the posting hidden behind it.
    func snapshot() async throws -> (url: String, fields: [[String: Any]]) {
        let result = try await activeWebView.evaluateJavaScript(ApplyScripts.snapshot)
        let dict = result as? [String: Any] ?? [:]
        return (dict["url"] as? String ?? "",
                dict["fields"] as? [[String: Any]] ?? [])
    }

    /// Register fill.js, then invoke the async global with the merged items.
    /// Upload items arrive with `file_b64` (a string survives the JS bridge;
    /// a Data/byte-array does not) — decode each back into the `file_bytes`
    /// Uint8Array that fill.js's DataTransfer attachment path expects.
    func fill(items: [[String: Any]]) async throws -> [[String: Any]] {
        _ = try await activeWebView.evaluateJavaScript(ApplyScripts.fill)
        let out = try await activeWebView.callAsyncJavaScript(
            """
            for (const it of (items || [])) {
                if (!it.file_b64) continue;
                const bin = atob(it.file_b64);
                const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                it.file_bytes = bytes;
                delete it.file_b64;
            }
            return await window.__jobsmithFillAndHighlight(items, {});
            """,
            arguments: ["items": items],
            contentWorld: .page)
        let dict = out as? [String: Any] ?? [:]
        return dict["results"] as? [[String: Any]] ?? []
    }
}

private struct ApplyWebView: UIViewRepresentable {
    let controller: ApplyWebController

    // The controller mounts the primary (or an external-apply child) web view
    // inside this container, so a window.open handoff swaps in place.
    func makeUIView(context: Context) -> UIView { controller.containerView }

    func updateUIView(_ uiView: UIView, context: Context) {}
}

/// Lazily-loaded bundled autofill scripts (single-sourced from
/// extension/src/common; see the header of each JS file).
enum ApplyScripts {
    static let snapshot = load("snapshot")
    static let fill = load("fill")

    private static func load(_ name: String) -> String {
        guard let url = Bundle.main.url(forResource: name, withExtension: "js"),
              let source = try? String(contentsOf: url, encoding: .utf8) else {
            assertionFailure("Missing bundled \(name).js resource")
            return ""
        }
        return source
    }
}

// MARK: - Fallback panel

/// Tap-to-copy answers + document export for fields the in-page autofill can't
/// set (file inputs are OS-picker-only in WKWebView) or didn't reach.
private struct ApplyFallbackPanel: View {
    @Environment(\.dismiss) private var dismiss
    let job: Job
    let rows: [ApplyFieldRow]

    @State private var copiedId: String?

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ApplyDocumentTile(job: job, kind: .resume, title: "Résumé")
                    ApplyDocumentTile(job: job, kind: .coverLetter, title: "Cover letter")
                } header: {
                    Text("Documents")
                } footer: {
                    Text("Autofill attaches these to the form's file fields when it can. "
                         + "If one didn't stick, tap the field on the page, choose Files, "
                         + "then pick the exported document.")
                }

                Section("Answers — tap to copy") {
                    ForEach(rows) { row in
                        answerRow(row)
                    }
                }
            }
            .navigationTitle("Application kit")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    @ViewBuilder
    private func answerRow(_ row: ApplyFieldRow) -> some View {
        let displayValue = row.isSkipped ? "(skipped)"
            : (row.value.isEmpty ? "(empty)" : row.value)
        Button {
            guard row.copyable else { return }
            UIPasteboard.general.string = row.value
            copiedId = row.id
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                if copiedId == row.id { copiedId = nil }
            }
        } label: {
            HStack(alignment: .top, spacing: 10) {
                statusDot(row)
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.label)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                    Text(displayValue)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    // Why the fill didn't take, straight from fill.js —
                    // without this a failed upload is undiagnosable on-device.
                    if !row.wasFilled, let message = row.fillMessage, !message.isEmpty {
                        Text(message)
                            .font(.caption2)
                            .foregroundStyle(.orange)
                            .lineLimit(3)
                    }
                }
                Spacer(minLength: 8)
                if copiedId == row.id {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .accessibilityHidden(true)
                } else if row.copyable {
                    Image(systemName: "doc.on.doc")
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }
            }
        }
        .disabled(!row.copyable)
        // The dot's color is the only visual carrier of field status, so fold
        // it into the row's spoken label rather than leaving it color-only.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel({
            var parts = "\(row.label), \(statusText(row)). \(displayValue)"
            if !row.wasFilled, let message = row.fillMessage, !message.isEmpty {
                parts += ". \(message)"
            }
            return parts
        }())
        .accessibilityHint(row.copyable
                           ? (copiedId == row.id ? "Copied" : "Copies the answer to the clipboard")
                           : "")
    }

    /// The words behind the status dot's color.
    private func statusText(_ row: ApplyFieldRow) -> String {
        if row.isUpload { return row.wasFilled ? "file attached" : "file upload needed" }
        if row.wasFilled { return "filled" }
        if row.required { return "required, not filled" }
        return "optional, not filled"
    }

    private func statusDot(_ row: ApplyFieldRow) -> some View {
        let color: Color = row.isUpload ? (row.wasFilled ? .green : .orange)
            : row.wasFilled ? .green
            : row.required ? .red
            : .secondary
        return Circle()
            .fill(color)
            .frame(width: 8, height: 8)
            .padding(.top, 6)
            .accessibilityHidden(true)
    }
}

/// A tailored DOCX offered via the share sheet ("Save to Files") so the OS file
/// picker's Recents surfaces it in one tap on the ATS upload control.
private struct ApplyDocumentTile: View {
    @Environment(AppModel.self) private var model
    let job: Job
    let kind: FileVault.Kind
    let title: String

    private var fileURL: URL? {
        let format = model.config.honesty.documentFormat
        guard let data = FileVault.read(jobId: job.id, kind: kind, format: format) else {
            return nil
        }
        let base = job.company.isEmpty ? "Jobsmith" : job.company
        let safe = base.components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { !$0.isEmpty }.joined(separator: "-")
        let suffix = kind == .resume ? "Resume" : "CoverLetter"
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("\(safe)-\(suffix).\(format.rawValue)")
        do {
            try data.write(to: url, options: .atomic)
            return url
        } catch {
            return nil
        }
    }

    var body: some View {
        if let url = fileURL {
            ShareLink(item: url) {
                Label(title, systemImage: "doc.fill")
            }
        } else {
            Label {
                Text("\(title) — not tailored yet")
                    .foregroundStyle(.secondary)
            } icon: {
                Image(systemName: "doc")
                    .foregroundStyle(.secondary)
            }
        }
    }
}
