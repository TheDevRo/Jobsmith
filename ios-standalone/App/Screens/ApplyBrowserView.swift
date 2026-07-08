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

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if jobURL != nil {
                    ApplyWebView(controller: controller)
                } else {
                    ContentUnavailableView("No application URL",
                                           systemImage: "link.badge.plus")
                }
                if isLinkedIn && storedLinkedInCookie == nil {
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
                                 liAtCookie: isLinkedIn ? storedLinkedInCookie : nil)
            }
            .sheet(isPresented: $showLinkedInSignIn) {
                LinkedInSignInSheet { _, cookie in handleLinkedInSignIn(cookie) }
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

    /// Persist the captured `li_at` cookie (same store onboarding writes) and
    /// reload the posting so it renders behind the fresh session.
    private func handleLinkedInSignIn(_ cookie: String?) {
        guard let cookie, !cookie.isEmpty else { return }
        model.saveConfig { $0.apiKeys.linkedInCookie = cookie }
        if let url = jobURL {
            controller.start(url: url, liAtCookie: cookie)
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
                let items = Self.buildFillItems(descriptors: snap.fields, values: values)
                status = "Filling…"
                let fillResults = try await controller.fill(items: items)
                rows = Self.buildRows(descriptors: snap.fields, values: values,
                                      fillResults: fillResults)
                let filled = fillResults.filter { ($0["status"] as? String) == "filled" }.count
                status = "Filled \(filled) of \(items.count). Review the page, attach documents, then submit."
                showPanel = true
            } catch {
                status = "Autofill failed: \(error.localizedDescription)"
            }
            busy = false
        }
    }

    // MARK: - Merge helpers (ported from SafariExt/Resources/sidepanel.js)

    /// Join each mapped `FieldValue` with its snapshot descriptor by `field_id`
    /// into the fill-payload shape `fill.js` expects. `options` is always an
    /// array (never null) so it survives `callAsyncJavaScript` serialization.
    static func buildFillItems(descriptors: [[String: Any]],
                               values: [[String: Any]]) -> [[String: Any]] {
        var descById: [String: [String: Any]] = [:]
        for d in descriptors {
            if let fid = d["field_id"] as? String { descById[fid] = d }
        }
        var items: [[String: Any]] = []
        for v in values {
            guard let fid = v["field_id"] as? String else { continue }
            let d = descById[fid] ?? [:]
            let item: [String: Any] = [
                "field_id": fid,
                "selector": d["_selector"] as? String ?? "",
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
        for r in fillResults {
            if let fid = r["field_id"] as? String {
                statusById[fid] = r["status"] as? String
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
                required: d["required"] as? Bool ?? false))
        }
        // Attention-first ordering: unresolved before resolved.
        return out.sorted { $0.attentionRank < $1.attentionRank }
    }
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

    var isUpload: Bool { action == "upload" }
    var isSkipped: Bool { action == "skip" }
    var copyable: Bool { !isUpload && !isSkipped && !value.isEmpty }
    var wasFilled: Bool { fillStatus == "filled" }

    /// Lower sorts first: uploads and required-but-unfilled need attention.
    var attentionRank: Int {
        if isUpload { return 0 }
        if required && !wasFilled { return 1 }
        if !wasFilled { return 2 }
        return 3
    }
}

// MARK: - WKWebView driver

/// Owns the WKWebView and exposes async snapshot/fill helpers. Kept out of the
/// SwiftUI view so the injected-script calls read cleanly.
@MainActor
final class ApplyWebController: ObservableObject {
    let webView: WKWebView

    init() {
        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.allowsBackForwardNavigationGestures = true
    }

    /// Inject the stored LinkedIn `li_at` session cookie (if any) into the web
    /// view's cookie store, then load — so LinkedIn postings render behind the
    /// user's own session instead of the logged-out wall. The cookie is scoped
    /// to `.linkedin.com`, so it's never sent to other ATS hosts.
    func start(url: URL, liAtCookie: String?) {
        guard let liAtCookie, !liAtCookie.isEmpty,
              let cookie = Self.linkedInSessionCookie(value: liAtCookie) else {
            load(url)
            return
        }
        webView.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) { [weak self] in
            self?.load(url)
        }
    }

    func load(_ url: URL) {
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

    /// Inject snapshot.js; its IIFE returns `{ url, fields }` directly.
    func snapshot() async throws -> (url: String, fields: [[String: Any]]) {
        let result = try await webView.evaluateJavaScript(ApplyScripts.snapshot)
        let dict = result as? [String: Any] ?? [:]
        return (dict["url"] as? String ?? "",
                dict["fields"] as? [[String: Any]] ?? [])
    }

    /// Register fill.js, then invoke the async global with the merged items.
    func fill(items: [[String: Any]]) async throws -> [[String: Any]] {
        _ = try await webView.evaluateJavaScript(ApplyScripts.fill)
        let out = try await webView.callAsyncJavaScript(
            "return await window.__jobsmithFillAndHighlight(items, {});",
            arguments: ["items": items],
            contentWorld: .page)
        let dict = out as? [String: Any] ?? [:]
        return dict["results"] as? [[String: Any]] ?? []
    }
}

private struct ApplyWebView: UIViewRepresentable {
    let controller: ApplyWebController

    func makeUIView(context: Context) -> WKWebView { controller.webView }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
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
                    Text("Tap the file field on the page, choose Files, then pick the "
                         + "exported document. WKWebView can't attach files for you.")
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
                }
                Spacer(minLength: 8)
                if copiedId == row.id {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                } else if row.copyable {
                    Image(systemName: "doc.on.doc")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .disabled(!row.copyable)
    }

    private func statusDot(_ row: ApplyFieldRow) -> some View {
        let color: Color = row.isUpload ? .orange
            : row.wasFilled ? .green
            : row.required ? .red
            : .secondary
        return Circle()
            .fill(color)
            .frame(width: 8, height: 8)
            .padding(.top, 6)
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
