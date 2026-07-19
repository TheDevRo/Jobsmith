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

    @StateObject private var controller = ApplyWebController()
    @State private var status = ""
    @State private var statusVisible = false
    @State private var statusHideTask: Task<Void, Never>?
    @State private var busy = false
    @State private var autofillTask: Task<Void, Never>?
    @State private var rows: [ApplyFieldRow] = []
    @State private var showPanel = false
    @State private var didStart = false
    @State private var showLinkedInSignIn = false

    // Workday one-tap auth: the on-page auth state ("create"|"signin"|"none"),
    // the tenant host, whether this device already knows an account for it, and
    // an in-flight guard.
    @State private var workdayState = "none"
    @State private var workdayTenant = ""
    @State private var workdayKnown = false
    @State private var workdayBusy = false
    // Debounces the Workday re-check driven by SPA URL changes (see the
    // controller.pageURL onChange below).
    @State private var workdayRecheckTask: Task<Void, Never>?

    private var jobURL: URL? { URL(string: job.url) }

    private var workdayEmail: String { model.config.apiKeys.workdayEmail }
    private var workdayPassword: String { model.config.apiKeys.workdayPassword }
    private var workdayConfigured: Bool { !workdayEmail.isEmpty && !workdayPassword.isEmpty }
    private var showWorkdayBanner: Bool { workdayState == "signin" || workdayState == "create" }

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

    /// The Keychain-stored session as injectable cookies. Enough on its own
    /// only when the browser's persistent jar already holds LinkedIn's device
    /// cookies from an earlier sign-in — see `handleLinkedInSignIn`.
    private var storedLinkedInCookies: [HTTPCookie] {
        var cookies: [HTTPCookie] = []
        if let value = storedLinkedInCookie,
           let cookie = ApplyWebController.linkedInSessionCookie(value: value) {
            cookies.append(cookie)
        }
        if let value = storedLinkedInJSessionId,
           let cookie = ApplyWebController.linkedInJSessionCookie(value: value) {
            cookies.append(cookie)
        }
        return cookies
    }

    /// LinkedIn browsing works on `li_at` alone, but Easy Apply's Voyager POST
    /// needs a live `JSESSIONID` too. Prompt a (fresh) sign-in when either is
    /// missing rather than loading a half-authenticated session whose Apply
    /// button silently 401s. LinkedIn bouncing the loaded posting to its
    /// authwall also counts: it means the stored session (if any) is stale.
    private var needsLinkedInSignIn: Bool {
        isLinkedIn && (storedLinkedInCookie == nil || storedLinkedInJSessionId == nil
                       || controller.hitAuthwall)
    }

    var body: some View {
        // No NavigationStack: ATS forms (LinkedIn Easy Apply's modal especially)
        // need every point of viewport height, so the only chrome is a single
        // slim toolbar and a transient status line that appears during autofill.
        VStack(spacing: 0) {
            if jobURL != nil {
                ApplyWebView(controller: controller)
                    .overlay(alignment: .top) {
                        if controller.isLoading {
                            ProgressView(value: min(max(controller.estimatedProgress, 0.05), 1))
                                .progressViewStyle(.linear)
                        }
                    }
            } else {
                ContentUnavailableView("No application URL",
                                       systemImage: "link.badge.plus")
            }
            if needsLinkedInSignIn {
                linkedInSignInBanner
            }
            if showWorkdayBanner {
                workdayBanner
            }
            bottomBar
        }
        // Let the keyboard cover the toolbar instead of squeezing the layout:
        // the web view manages its own keyboard insets, so this hands the form
        // the full remaining height while typing (Safari behaves the same way).
        .ignoresSafeArea(.keyboard, edges: .bottom)
        .task {
            guard !didStart, let url = jobURL else { return }
            didStart = true
            controller.start(url: url,
                             cookies: isLinkedIn ? storedLinkedInCookies : [])
            showStatus("Load the form, then tap Autofill.", autoHideAfter: 6)
        }
        .onDisappear {
            // A mapping run left in flight would keep burning an LLM round
            // (and battery) after the user gave up on the page.
            autofillTask?.cancel()
        }
        .onChange(of: controller.hasPopup) { _, opened in
            if opened {
                showStatus("The site opened a new window — back returns to the posting.",
                           autoHideAfter: 6)
            }
        }
        .onChange(of: controller.isLoading) { _, loading in
            // Once a page settles, re-check whether it's a Workday auth wall so
            // the one-tap banner can appear (or disappear after a sign-in).
            if !loading { Task { await refreshWorkdayState() } }
        }
        .onChange(of: controller.pageURL) { _, _ in
            // SPA route changes (Workday is a React SPA) never toggle isLoading,
            // so the load-complete hook above misses the auth wall. Real loads
            // fire this KVO several times mid-flight — skip those; the isLoading
            // hook covers them once the page settles.
            guard !controller.isLoading else { return }
            workdayRecheckTask?.cancel()
            workdayRecheckTask = Task {
                await refreshWorkdayState()
                // Workday renders the form a beat after the URL changes; one
                // delayed re-check catches it.
                try? await Task.sleep(nanoseconds: 800_000_000)
                guard !Task.isCancelled else { return }
                await refreshWorkdayState()
            }
        }
        .onChange(of: controller.loadError) { _, error in
            // A failed load otherwise looks like a silently blank page; name
            // the failure and leave it up — Reload and Safari are the way out.
            if let error {
                showStatus("Couldn't load the page: \(error)")
            } else if statusVisible, status.hasPrefix("Couldn't load") {
                withAnimation(.easeIn(duration: 0.2)) { statusVisible = false }
            }
        }
        .sheet(isPresented: $showLinkedInSignIn) {
            LinkedInSignInSheet { result in
                handleLinkedInSignIn(result)
            }
        }
        .sheet(isPresented: $showPanel) {
            ApplyFallbackPanel(job: job, rows: rows)
                .presentationDetents([.medium, .large])
        }
    }

    private var bottomBar: some View {
        VStack(spacing: 4) {
            if statusVisible {
                Text(status)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(2)
                    .padding(.horizontal, 6)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            HStack(spacing: 0) {
                barButton("xmark", "Close") { dismiss() }
                barButton("chevron.backward", "Back") { controller.goBack() }
                    .disabled(!controller.canGoBack && !controller.hasPopup)
                barButton("chevron.forward", "Forward") { controller.goForward() }
                    .disabled(!controller.canGoForward)
                barButton("arrow.clockwise", "Reload") { controller.reload() }

                Spacer(minLength: 4)

                Button {
                    // While a run is in flight the same button cancels it —
                    // a slow AI endpoint must never pin the browser for
                    // minutes with no way out.
                    if busy { autofillTask?.cancel() } else { autofill() }
                } label: {
                    Label(busy ? "Cancel" : "Autofill",
                          systemImage: busy ? "xmark.circle" : "wand.and.stars")
                        .font(.callout.weight(.semibold))
                        .padding(.vertical, 7)
                        .padding(.horizontal, 2)
                }
                .buttonStyle(.borderedProminent)
                .disabled(URL(string: job.url) == nil)

                if !rows.isEmpty {
                    barButton("list.clipboard", "Answers") { showPanel = true }
                }
                barButton("safari", "Open in Safari") {
                    // Escape hatch: reCAPTCHA v2 and some ATS flows can't be
                    // completed inside a WKWebView, so hand the CURRENT page
                    // (not just the original posting) off to real Safari.
                    if let url = controller.currentURL ?? URL(string: job.url) {
                        UIApplication.shared.open(url)
                    }
                }
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(.bar)
    }

    private func barButton(_ systemImage: String, _ label: String,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.title3)
                .frame(width: 43, height: 40)
                .contentShape(Rectangle())
        }
        .accessibilityLabel(label)
    }

    /// Show the transient status line; optionally auto-hide it. The line only
    /// occupies toolbar height while it has something to say — permanent chrome
    /// costs viewport the form needs.
    private func showStatus(_ text: String, autoHideAfter seconds: Double? = nil) {
        status = text
        statusHideTask?.cancel()
        statusHideTask = nil
        withAnimation(.easeOut(duration: 0.2)) { statusVisible = true }
        if let seconds {
            statusHideTask = Task {
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                guard !Task.isCancelled else { return }
                withAnimation(.easeIn(duration: 0.2)) { statusVisible = false }
            }
        }
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
    /// onboarding writes) and reload the posting behind the fresh session —
    /// injecting the sheet's COMPLETE cookie jar, not just the two we keep:
    /// `li_at` landing in a jar without its companion device cookies
    /// (bcookie/bscookie/lidc/…) fails LinkedIn's session check and
    /// 302-loops, which the loop recovery then "fixes" by discarding the
    /// brand-new session — making sign-in appear to do nothing. The extra
    /// cookies live only in the browser's own jar, never in the Keychain.
    private func handleLinkedInSignIn(_ result: LinkedInSignInResult) {
        guard let cookie = result.liAt, !cookie.isEmpty else { return }
        model.saveConfig {
            $0.apiKeys.linkedInCookie = cookie
            $0.apiKeys.linkedInJSessionId = result.jsessionId ?? ""
        }
        if let url = jobURL {
            controller.start(url: url, cookies: result.cookies)
        }
    }

    // MARK: - Workday one-tap auth

    private var workdayBanner: some View {
        // The registry wins over the DOM heuristic for the label: a known tenant
        // is always "Sign in", even if Workday defaulted to the create form.
        let creating = workdayState == "create" && !workdayKnown
        return HStack(spacing: 10) {
            Image(systemName: "building.2")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(creating ? "Create your Workday account" : "Sign in to Workday")
                    .font(.footnote.weight(.semibold))
                Text(workdayConfigured
                     ? "Uses your Workday email + password from Settings."
                     : "Add your Workday email + password in Settings first.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer(minLength: 8)
            Button(creating ? "Create" : "Sign in") {
                Task { await doWorkdayAuth(creating: creating) }
            }
            .font(.footnote.weight(.semibold))
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
            .disabled(!workdayConfigured || workdayBusy)
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.thinMaterial)
    }

    private func refreshWorkdayState() async {
        let host = (controller.currentURL ?? jobURL)?.host?.lowercased() ?? ""
        // Workday tenants live on both suffixes (wd5.myworkdayjobs.com and
        // wd1.myworkdaysite.com style career sites).
        guard host.hasSuffix("myworkdayjobs.com") || host.hasSuffix("myworkdaysite.com") else {
            workdayState = "none"
            return
        }
        do {
            let (state, tenant) = try await controller.workdayAuthState()
            workdayState = state
            workdayTenant = tenant.isEmpty ? host : tenant
            let known = (try? AtsAccountStore(model.database).get(workdayTenant)) ?? nil
            workdayKnown = known != nil
        } catch {
            workdayState = "none"
        }
    }

    private func doWorkdayAuth(creating: Bool) async {
        guard workdayConfigured else {
            showStatus("Add your Workday email + password in Settings.")
            return
        }
        workdayBusy = true
        showStatus(creating ? "Creating your Workday account…" : "Signing in to Workday…")
        do {
            let out = try await controller.workdayAuth(email: workdayEmail, password: workdayPassword)
            let ok = out["ok"] as? Bool ?? false
            let action = out["action"] as? String ?? ""
            let message = out["message"] as? String
            let pending = out["pending"] as? Bool ?? false
            if ok {
                recordWorkdayAccount(action: action, pending: pending)
                showStatus(message ?? (action == "account_created" ? "Account created." : "Signed in."),
                           autoHideAfter: 5)
                // The page navigates past the auth wall; re-check after it lands.
                try? await Task.sleep(nanoseconds: 1_200_000_000)
                await refreshWorkdayState()
            } else {
                showStatus(message ?? "Workday sign-in failed.")
            }
        } catch {
            showStatus("Workday error: \(error.localizedDescription)")
        }
        workdayBusy = false
    }

    /// Remember the tenant in the synced registry (never a password).
    private func recordWorkdayAccount(action: String, pending: Bool) {
        guard !workdayTenant.isEmpty else { return }
        let store = AtsAccountStore(model.database)
        if action == "account_created" {
            try? store.upsert(tenantHost: workdayTenant, email: workdayEmail,
                              status: pending ? AtsAccount.statusPending : AtsAccount.statusActive)
        } else {
            let existing = (try? store.get(workdayTenant)) ?? nil
            if existing == nil {
                try? store.upsert(tenantHost: workdayTenant, email: workdayEmail)
            } else {
                try? store.markSignedIn(workdayTenant)
            }
        }
    }

    // MARK: - Autofill flow

    private func autofill() {
        busy = true
        showStatus("Scanning form…")
        autofillTask = Task {
            defer { busy = false }
            do {
                let snap = try await controller.snapshot()
                guard !snap.fields.isEmpty else {
                    showStatus("No form fields found — the page may still be loading, "
                               + "behind a login, or not a web form.", autoHideAfter: 10)
                    return
                }
                showStatus("Mapping \(snap.fields.count) field\(snap.fields.count == 1 ? "" : "s")…")
                let mapping = await model.mapApplyFields(snap.fields, job: job)
                try Task.checkCancellation()
                var items = Self.buildFillItems(descriptors: snap.fields, values: mapping.values,
                                                documents: uploadDocuments(for: mapping.values))
                showStatus("Filling…")
                // Phase 2 — shrink the snapshot→fill window. Mapping above can
                // take seconds (LLM), during which an SPA form may re-render and
                // drop the data-jobsmith-fid stamps snapshot.js applied. Re-run
                // snapshot to re-stamp the current nodes under the same stable
                // field_ids, then refresh each item's selector from the fresh
                // descriptors (matched by frame + field_id) so fill.js targets
                // live nodes.
                if let fresh = try? await controller.snapshot() {
                    var freshByKey: [String: [String: Any]] = [:]
                    for f in fresh.fields {
                        if let fid = f["field_id"] as? String {
                            freshByKey["\(f["_frame"] as? Int ?? 0)|\(fid)"] = f
                        }
                    }
                    var origByFid: [String: [String: Any]] = [:]
                    for d in snap.fields {
                        if let fid = d["field_id"] as? String { origByFid[fid] = d }
                    }
                    for i in items.indices {
                        guard let fid = items[i]["field_id"] as? String else { continue }
                        let frame = items[i]["_frame"] as? Int ?? 0
                        guard let f = freshByKey["\(frame)|\(fid)"],
                              let sel = f["_selector"] as? String else { continue }
                        // Only refresh when the fresh descriptor still
                        // describes the same field — index-derived ids
                        // (field_12) can land on a DIFFERENT element when the
                        // DOM changed between the two snapshots, and silently
                        // retargeting would fill the wrong box.
                        if let orig = origByFid[fid] {
                            guard f["name"] as? String == orig["name"] as? String,
                                  f["label"] as? String == orig["label"] as? String,
                                  f["field_type"] as? String == orig["field_type"] as? String
                            else { continue }
                        }
                        items[i]["selector"] = sel
                    }
                }
                try Task.checkCancellation()
                let fillResults = try await controller.fill(items: items)
                rows = Self.buildRows(descriptors: snap.fields, values: mapping.values,
                                      fillResults: fillResults)
                let filled = fillResults.filter { ($0["status"] as? String) == "filled" }.count
                let review = fillResults.filter { ($0["status"] as? String) == "low_confidence" }.count
                let pendingUploads = rows.contains { $0.isUpload && !$0.wasFilled }
                var status = "Filled \(filled + review) of \(items.count)."
                if review > 0 { status += " \(review) to double-check." }
                if mapping.llmError != nil {
                    status += " The AI engine couldn't be reached — some answers were left blank."
                }
                status += pendingUploads
                    ? " Review the page, attach documents, then submit."
                    : " Review the page, then submit."
                showStatus(status, autoHideAfter: mapping.llmError == nil ? 8 : 14)
                showPanel = true
            } catch is CancellationError {
                showStatus("Autofill cancelled.", autoHideAfter: 4)
            } catch {
                showStatus("Autofill failed: \(error.localizedDescription)", autoHideAfter: 10)
            }
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
        let preferred = model.config.honesty.documentFormat
        var docs: [String: ApplyUploadDocument] = [:]
        for token in kinds {
            guard let kind = FileVault.Kind(rawValue: token) else { continue }
            // The vault holds whatever format the job was tailored under; if
            // the setting changed since, fall back to the other format rather
            // than silently skipping the upload.
            var format = preferred
            var data = FileVault.read(jobId: job.id, kind: kind, format: format)
            if data == nil {
                let alt: FileVault.Format = preferred == .docx ? .pdf : .docx
                if let altData = FileVault.read(jobId: job.id, kind: kind, format: alt) {
                    data = altData
                    format = alt
                }
            }
            guard let data else { continue }
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
                // Which frame the field was snapshotted in (0 = main) — fill()
                // routes the item back into that frame's script world.
                "_frame": d["_frame"] as? Int ?? 0,
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
    /// "low_confidence" fields WERE set on the page (an unsure pick or a
    /// partial multi-select) — counting them as unfilled made users re-type
    /// values that were already there.
    var wasFilled: Bool { fillStatus == "filled" || fillStatus == "low_confidence" }
    var needsReview: Bool { fillStatus == "low_confidence" }

    /// Lower sorts first: unattached uploads and required-but-unfilled need
    /// attention. An upload fill.js managed to attach is resolved.
    var attentionRank: Int {
        if isUpload { return wasFilled ? 3 : 0 }
        if required && !wasFilled { return 1 }
        if !wasFilled || needsReview { return 2 }
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

    /// Subframes seen navigating, per web view. ATS vendors (Greenhouse embeds,
    /// iCIMS, Taleo) render the whole application form inside a — usually
    /// cross-origin — iframe, which plain `evaluateJavaScript` (main frame
    /// only) can never see; these captured `WKFrameInfo`s let snapshot/fill
    /// target those frames too. Keyed by frame request URL so a frame that
    /// re-navigates replaces its stale info; cleared when the owning view's
    /// main frame navigates.
    private var subframes: [(view: WKWebView, url: String, frame: WKFrameInfo)] = []
    private let maxSubframes = 24

    /// The frames the LAST snapshot pulled fields from, by the `_frame` index
    /// stamped on each descriptor (0 / nil = main frame). fill() routes each
    /// item back to the frame its field was found in.
    private var snapshotFrames: [WKFrameInfo?] = [nil]

    // Published navigation state so the toolbar can enable/disable controls and
    // show load progress — users tapping into a half-loaded form is a top cause
    // of "the button did nothing".
    @Published private(set) var isLoading = false
    @Published private(set) var estimatedProgress: Double = 0
    @Published private(set) var canGoBack = false
    @Published private(set) var canGoForward = false
    /// The current URL, republished via KVO. Unlike `isLoading`, `\.url` fires
    /// for pushState/same-document navigations — the signal that a React SPA
    /// (Workday) routed to its auth wall without a real page load.
    @Published private(set) var pageURL: URL?
    /// True while a `window.open` child is mounted; the back button closes it
    /// (the page's own `window.close()` is the only other way out).
    @Published private(set) var hasPopup = false

    /// A human-readable load failure ("blank page" is the alternative — the
    /// user needs to know the page failed and that Reload is the way out).
    @Published private(set) var loadError: String?

    /// True after LinkedIn tried to bounce an already-rendered posting to its
    /// /authwall (its bot check flags WKWebView guests as "scraping" and
    /// redirects a page that loaded fine). The redirect is cancelled so the
    /// posting stays visible; this flag lets the view offer sign-in, which
    /// also covers a stored-but-stale li_at session. Also set when a stale
    /// stored session makes LinkedIn 302-loop the initial load (see
    /// recoverFromLinkedInRedirectLoop).
    @Published private(set) var hitAuthwall = false

    /// True when `start` injected stored LinkedIn session cookies — the
    /// precondition for the redirect-loop recovery below (a pure guest load
    /// that loops has nothing to clear, so retrying can't help it).
    private var injectedLinkedInSession = false

    /// One-shot guard so a posting that loops even as a guest surfaces the
    /// error instead of reloading forever.
    private var didRetryAsGuestAfterRedirectLoop = false

    /// Set once a linkedin.com /jobs/ page finishes rendering — the signal
    /// that a later /authwall navigation is the client-side bounce (cancel it)
    /// rather than a server-side wall on a page we never got (allow it, or
    /// the user sees a blank view instead of the wall).
    private var renderedLinkedInJobPage = false

    /// Content-process kills already auto-reloaded, so a repeat means the page
    /// genuinely can't run on this device — stop looping and tell the user.
    private var didRecoverFromProcessKill = false

    /// KVO tokens for the mounted web view; re-created on every mount so the
    /// toolbar always reflects the view the user is actually looking at.
    private var observations: [NSKeyValueObservation] = []

    /// The URL `start` was given, so Reload can recover a web view whose
    /// provisional load failed (url == nil, where reload() is a no-op).
    private var initialURL: URL?

    /// A recent mobile-Safari UA. WKWebView's default UA omits the "Safari"
    /// token and version, which Google reCAPTCHA v2 and some ATS bot checks
    /// treat as suspicious — failing the "I'm not a robot" challenge or refusing
    /// to render it. Presenting a Safari-like UA makes the embedded browser look
    /// like the system browser so those flows behave.
    static let safariUserAgent =
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        + "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"

    /// The URL currently shown in the active web view (post-redirect), so
    /// "Open in Safari" hands off the page the user is actually looking at.
    var currentURL: URL? { activeWebView.url }

    override init() {
        let config = WKWebViewConfiguration()
        // ATS redirect shims often call window.open AFTER async validation —
        // outside the user-gesture window — which WebKit blocks silently by
        // default. The tapped Apply button then appears to do nothing.
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
        config.allowsInlineMediaPlayback = true
        webView = WKWebView(frame: .zero, configuration: config)
        activeWebView = webView
        super.init()
        configure(webView)
        mount(webView)
    }

    /// Settings every mounted web view (primary or popup child) needs.
    private func configure(_ view: WKWebView) {
        view.allowsBackForwardNavigationGestures = true
        view.customUserAgent = Self.safariUserAgent
        // Long-press link previews swallow the tap that follows and fight the
        // page's own press handlers; an application form never needs them.
        view.allowsLinkPreview = false
        // Long forms: let a downward swipe put the keyboard away instead of
        // leaving it covering half the remaining viewport.
        view.scrollView.keyboardDismissMode = .interactive
        view.uiDelegate = self
        view.navigationDelegate = self
    }

    /// Show `view` filling the container, replacing whatever was mounted, and
    /// route autofill at it.
    private func mount(_ view: WKWebView) {
        activeWebView = view
        hasPopup = view !== webView
        containerView.subviews.forEach { $0.removeFromSuperview() }
        view.translatesAutoresizingMaskIntoConstraints = false
        containerView.addSubview(view)
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: containerView.topAnchor),
            view.bottomAnchor.constraint(equalTo: containerView.bottomAnchor),
            view.leadingAnchor.constraint(equalTo: containerView.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: containerView.trailingAnchor),
        ])
        observe(view)
    }

    /// Mirror the mounted view's navigation state into the published props.
    /// WebKit fires these on the main thread; the Task hop keeps the writes
    /// inside this class's MainActor isolation without assuming it.
    private func observe(_ view: WKWebView) {
        observations = [
            view.observe(\.estimatedProgress, options: [.initial, .new]) { [weak self] wv, _ in
                let value = wv.estimatedProgress
                Task { @MainActor in self?.estimatedProgress = value }
            },
            view.observe(\.isLoading, options: [.initial, .new]) { [weak self] wv, _ in
                let value = wv.isLoading
                Task { @MainActor in self?.isLoading = value }
            },
            view.observe(\.canGoBack, options: [.initial, .new]) { [weak self] wv, _ in
                let value = wv.canGoBack
                Task { @MainActor in self?.canGoBack = value }
            },
            view.observe(\.canGoForward, options: [.initial, .new]) { [weak self] wv, _ in
                let value = wv.canGoForward
                Task { @MainActor in self?.canGoForward = value }
            },
            view.observe(\.url, options: [.initial, .new]) { [weak self] wv, _ in
                let value = wv.url
                Task { @MainActor in self?.pageURL = value }
            },
        ]
    }

    // MARK: Toolbar navigation

    /// Back, with popup awareness: a `window.open` child with no history of its
    /// own is closed (returning to the page that opened it) — otherwise a popup
    /// the site never closes traps the user.
    func goBack() {
        if activeWebView.canGoBack {
            activeWebView.goBack()
        } else if activeWebView !== webView {
            closePopup()
        }
    }

    func goForward() {
        activeWebView.goForward()
    }

    func reload() {
        if activeWebView.url != nil {
            activeWebView.reload()
        } else if let url = initialURL {
            load(url)
        }
    }

    /// Dismiss the mounted popup child and re-show what was underneath.
    func closePopup() {
        guard activeWebView !== webView,
              let idx = childWebViews.firstIndex(of: activeWebView) else { return }
        childWebViews.remove(at: idx)
        mount(childWebViews.last ?? webView)
    }

    /// LinkedIn's external Apply button (and many ATS redirect shims) hand off
    /// to the company site via `window.open` / `target="_blank"`. WKWebView
    /// routes those here and drops them without a UI delegate.
    ///
    /// Two shapes, both answered with a real child web view (created with the
    /// passed configuration, as WKWebView requires):
    ///  - carries a URL → load it in the child; the back button (or the
    ///    page's window.close) returns to the opener, which stays alive.
    ///  - blank/`about:blank` then-navigate → the opener opens an empty window
    ///    and later sets `location` on the returned handle. Returning `nil`
    ///    drops that flow. The child stays hidden until it actually loads
    ///    something, so popunders never blank the screen.
    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil else { return nil }
        let url = navigationAction.request.url
        NSLog("[Apply] window.open → %@", url?.absoluteString ?? "(blank)")

        if url == nil || url?.absoluteString == "about:blank" {
            let child = makeChildWebView(configuration)
            // A blank child painted via opener document.write never navigates,
            // so the first-navigation mount below never fires — watch for
            // content so the window the site "opened" actually appears.
            if let child { watchBlankPopupForContent(child) }
            return child
        }
        guard let url else { return nil }
        if url.scheme == "http" || url.scheme == "https" {
            // Load in a real child so the OPENER stays alive underneath —
            // OAuth popups (Apply with LinkedIn/Indeed) post their result back
            // through window.opener, which loading in place would destroy.
            if let child = makeChildWebView(configuration) {
                child.load(navigationAction.request)
                return child
            }
            webView.load(navigationAction.request)  // child cap hit — degrade in place
        } else if UIApplication.shared.canOpenURL(url) {
            // Non-web schemes (mailto:, tel:, …) can't render in the web view.
            UIApplication.shared.open(url)
        }
        return nil
    }

    /// Create a `window.open` child but do NOT show it yet: analytics
    /// popunders open about:blank windows they never navigate, and mounting
    /// one immediately replaces the visible page with a blank view ("the page
    /// doesn't show up at all"). The child is mounted on its first real
    /// navigation (see decidePolicyFor) — a window that never loads anything
    /// is never shown.
    private func makeChildWebView(_ configuration: WKWebViewConfiguration) -> WKWebView? {
        if childWebViews.count >= maxChildWebViews {
            // Evict the oldest background child rather than refuse — refusing
            // degrades into loading over the opener, which kills OAuth popups
            // that post back through window.opener.
            guard let idx = childWebViews.firstIndex(where: { $0 !== activeWebView }) else {
                return nil
            }
            childWebViews.remove(at: idx)
        }
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = true
        let child = WKWebView(frame: .zero, configuration: configuration)
        configure(child)
        childWebViews.append(child)
        return child
    }

    /// Poll a blank `window.open` child briefly: a child the opener paints
    /// with document.write holds content but no URL, and without this it would
    /// stay invisible forever — the tapped button that opened it looks dead.
    /// Real popunders stay blank and are never shown.
    private func watchBlankPopupForContent(_ child: WKWebView) {
        Task { @MainActor [weak self, weak child] in
            for delay: UInt64 in [900_000_000, 2_400_000_000] {
                try? await Task.sleep(nanoseconds: delay)
                guard let self, let child, self.childWebViews.contains(child) else { return }
                guard child !== self.activeWebView,
                      child.url == nil || child.url?.absoluteString == "about:blank"
                else { return }  // it navigated — decidePolicyFor mounts it
                let js = "!!(document.body && (document.body.children.length"
                    + " || (document.body.textContent || '').trim().length))"
                let has = ((try? await child.evaluateJavaScript(js)) as? Bool) ?? false
                if has {
                    self.mount(child)
                    return
                }
            }
        }
    }

    /// A child that scripts `window.close()` — drop it, and if it was the one
    /// on screen, re-show what was underneath (the last child that actually
    /// loaded something, or the primary posting).
    func webViewDidClose(_ webView: WKWebView) {
        guard let idx = childWebViews.firstIndex(of: webView) else { return }
        childWebViews.remove(at: idx)
        if activeWebView === webView {
            mount(childWebViews.last(where: { $0.url != nil }) ?? self.webView)
        }
    }

    // MARK: WKUIDelegate — JS dialogs. Without these handlers WebKit drops the
    // dialog silently: alert() vanishes and confirm() returns false, so any
    // ATS submit flow gated on "Are you sure?" dead-ends with a button that
    // looks broken.

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        presentDialog(message: message,
                      actions: [("OK", .default, { completionHandler() })],
                      fallback: completionHandler)
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        presentDialog(message: message,
                      actions: [("Cancel", .cancel, { completionHandler(false) }),
                                ("OK", .default, { completionHandler(true) })],
                      fallback: { completionHandler(false) })
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        guard let presenter = presenterViewController else {
            completionHandler(nil)
            return
        }
        let alert = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
        alert.addTextField { $0.text = defaultText }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in
            completionHandler(nil)
        })
        alert.addAction(UIAlertAction(title: "OK", style: .default) { [weak alert] _ in
            completionHandler(alert?.textFields?.first?.text ?? "")
        })
        presenter.present(alert, animated: true)
    }

    /// Present a JS dialog as a native alert. The completion handler MUST fire
    /// exactly once no matter what — WebKit blocks the page's JS thread until
    /// it does — hence the fallback when there's nothing to present from.
    private func presentDialog(message: String,
                               actions: [(String, UIAlertAction.Style, () -> Void)],
                               fallback: () -> Void) {
        guard let presenter = presenterViewController else {
            fallback()
            return
        }
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        for (title, style, handler) in actions {
            alert.addAction(UIAlertAction(title: title, style: style) { _ in handler() })
        }
        presenter.present(alert, animated: true)
    }

    /// The top-most presented view controller (the fullScreenCover hosting this
    /// browser, or a sheet above it) — where a UIAlertController can present.
    private var presenterViewController: UIViewController? {
        guard var top = containerView.window?.rootViewController else { return nil }
        while let presented = top.presentedViewController { top = presented }
        return top
    }

    // MARK: WKNavigationDelegate — allow web navigations (incl. cross-origin
    // apply redirects) and log, so a dropped handoff is diagnosable.

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        NSLog("[Apply] navigate → %@", url.absoluteString)
        trackFrame(navigationAction, in: webView)
        // LinkedIn's guest job pages render fully, then a bot-detection script
        // decides the embedded browser is "scraping" and JS-redirects to the
        // /authwall signup page — replacing a posting the user was already
        // reading. Cancel that hop and keep the rendered page; the sign-in
        // banner (via hitAuthwall) is the sanctioned way behind the wall.
        if renderedLinkedInJobPage,
           url.host?.lowercased().hasSuffix("linkedin.com") == true,
           url.path.hasPrefix("/authwall") {
            NSLog("[Apply] cancelled authwall bounce (posting already rendered)")
            hitAuthwall = true
            decisionHandler(.cancel)
            return
        }
        // Non-web schemes (mailto:, tel:, ATS companion-app links) can't render
        // in a web view — .allow just fails the provisional navigation and the
        // tapped link looks dead. Cancel and hand them to the system instead.
        let webSchemes: Set<String> = ["http", "https", "about", "blob", "data", "javascript", "file"]
        if webSchemes.contains(url.scheme?.lowercased() ?? "") {
            // A window.open child's first real navigation is the moment it
            // stops being a potential popunder and becomes the page the user
            // should see — show it now (created hidden in makeChildWebView).
            if webView !== activeWebView, childWebViews.contains(webView),
               url.scheme == "http" || url.scheme == "https" {
                mount(webView)
            }
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
            if UIApplication.shared.canOpenURL(url) {
                UIApplication.shared.open(url)
            }
        }
    }

    /// Record subframe navigations so snapshot/fill can reach into iframes,
    /// and reset the record when a view's main frame navigates away.
    private func trackFrame(_ navigationAction: WKNavigationAction, in webView: WKWebView) {
        guard let frame = navigationAction.targetFrame else { return }
        if frame.isMainFrame {
            subframes.removeAll { $0.view === webView }
            return
        }
        guard let url = navigationAction.request.url,
              url.scheme == "http" || url.scheme == "https" else { return }
        let key = url.absoluteString
        subframes.removeAll { $0.view === webView && $0.url == key }
        subframes.append((view: webView, url: key, frame: frame))
        if subframes.count > maxSubframes { subframes.removeFirst() }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        NSLog("[Apply] provisional navigation failed: %@", error.localizedDescription)
        if recoverFromLinkedInRedirectLoop(webView, error) { return }
        // A hidden window.open child (popunder) failing its load must not
        // flash "Couldn't load the page" over the healthy visible page.
        guard webView === activeWebView else { return }
        reportLoadFailure(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        NSLog("[Apply] navigation failed: %@", error.localizedDescription)
        guard webView === activeWebView else { return }
        reportLoadFailure(error)
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        loadError = nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        NSLog("[Apply] didFinish → %@ (child: %d)",
              webView.url?.absoluteString ?? "nil", webView !== self.webView ? 1 : 0)
        if let url = webView.url,
           url.host?.lowercased().hasSuffix("linkedin.com") == true,
           url.path.hasPrefix("/jobs/") {
            renderedLinkedInJobPage = true
        }
    }

    /// iOS kills the WebKit content process under memory pressure (heavy pages
    /// like LinkedIn, especially inside a fullScreenCover), which leaves a
    /// permanently blank web view — "the page doesn't show up at all". Reload
    /// once automatically; if it dies again, surface it instead of looping.
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        NSLog("[Apply] web content process terminated")
        if didRecoverFromProcessKill {
            loadError = "This page keeps crashing the browser engine. "
                + "Try Open in Safari."
            return
        }
        didRecoverFromProcessKill = true
        webView.reload()
    }

    /// Publish a load failure the toolbar can show. Cancellations are routine
    /// (every cancelled scheme handoff and mid-load tap produces one) and the
    /// page is usually still usable, so they don't count.
    private func reportLoadFailure(_ error: Error) {
        let nsError = error as NSError
        guard nsError.code != NSURLErrorCancelled,
              nsError.code != 102 else { return }  // WebKitErrorFrameLoadInterrupted
        loadError = nsError.localizedDescription
    }

    /// Inject the given LinkedIn session cookies (if any) into the web view's
    /// cookie store, then load — so LinkedIn postings render, and can Easy
    /// Apply, behind the user's own session instead of the logged-out wall.
    /// A fresh sign-in passes the complete jar (LinkedIn validates `li_at`
    /// against its device cookies; see `handleLinkedInSignIn`); later opens
    /// pass just the Keychain pair atop the browser's persisted jar. All are
    /// scoped to `.linkedin.com`, so they're never sent to other ATS hosts.
    func start(url: URL, cookies: [HTTPCookie] = []) {
        initialURL = url
        renderedLinkedInJobPage = false
        hitAuthwall = false
        didRetryAsGuestAfterRedirectLoop = false
        injectedLinkedInSession = !cookies.isEmpty
        guard !cookies.isEmpty else { load(url); return }
        setCookies(cookies, thenLoad: url)
    }

    /// A stale stored `li_at` makes LinkedIn 302-loop the very first load
    /// (posting → authwall/checkpoint → posting …, because the cookie is
    /// neither a valid guest nor a valid session) until CFNetwork fails with
    /// NSURLErrorHTTPTooManyRedirects — nothing ever renders, so the
    /// rendered-page authwall cancel above never gets a chance. Recover by
    /// clearing the injected session cookies and retrying once as a guest
    /// (the search-referer guest path is the known-good one), and raise
    /// `hitAuthwall` so the banner offers a fresh sign-in for the dead
    /// session. Returns true when recovery was started (skip the error UI).
    private func recoverFromLinkedInRedirectLoop(_ failedView: WKWebView,
                                                 _ error: Error) -> Bool {
        let nsError = error as NSError
        guard nsError.code == NSURLErrorHTTPTooManyRedirects,
              failedView === webView,
              injectedLinkedInSession, !didRetryAsGuestAfterRedirectLoop,
              let url = initialURL,
              url.host?.lowercased().hasSuffix("linkedin.com") == true
        else { return false }
        didRetryAsGuestAfterRedirectLoop = true
        injectedLinkedInSession = false
        hitAuthwall = true
        NSLog("[Apply] LinkedIn redirect loop with stored session — clearing cookies, retrying as guest")
        Task { @MainActor in
            let store = webView.configuration.websiteDataStore.httpCookieStore
            let stale = await store.allCookies().filter {
                $0.domain.lowercased().hasSuffix("linkedin.com")
                    && ($0.name == "li_at" || $0.name == "JSESSIONID")
            }
            for cookie in stale { await store.deleteCookie(cookie) }
            self.load(url)
        }
        return true
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
        var request = URLRequest(url: url)
        // LinkedIn's guest authwall spares search-engine visitors: with no
        // referer, its bot check hides the (fully served) posting and bounces
        // to the signup wall; a search referer renders it like a Google hit.
        // Signed-in sessions are unaffected.
        if url.host?.lowercased().hasSuffix("linkedin.com") == true {
            request.setValue("https://www.google.com/", forHTTPHeaderField: "Referer")
        }
        webView.load(request)
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

    /// Inject snapshot.js in the main frame AND every tracked iframe of the
    /// active web view; its IIFE returns `{ url, fields }` per frame. Merged
    /// fields carry a `_frame` index (0 = main) so fill() can route each item
    /// back to the frame it was found in — ATS forms frequently live in a
    /// cross-origin iframe the main frame can't see. Targets the active web
    /// view so autofill scans the visible page (including an external-apply
    /// child), not the posting hidden behind it.
    func snapshot() async throws -> (url: String, fields: [[String: Any]]) {
        var fields: [[String: Any]] = []
        var seenFids = Set<String>()
        func merge(_ raw: [[String: Any]], frameIndex: Int) {
            for var f in raw {
                guard var fid = f["field_id"] as? String else { continue }
                // The same id can exist in two frames ("email" in main +
                // iframe) — suffix the duplicate; `_selector` still holds the
                // original stamp, which is resolved inside its own frame.
                if seenFids.contains(fid) {
                    fid = "\(fid)__f\(frameIndex)"
                    f["field_id"] = fid
                }
                seenFids.insert(fid)
                f["_frame"] = frameIndex
                fields.append(f)
            }
        }
        let result = try await activeWebView.evaluateJavaScript(ApplyScripts.snapshot)
        let dict = result as? [String: Any] ?? [:]
        merge(dict["fields"] as? [[String: Any]] ?? [], frameIndex: 0)
        snapshotFrames = [nil]
        var seenFrameURLs = Set<String>()
        for entry in subframes where entry.view === activeWebView {
            guard !seenFrameURLs.contains(entry.url) else { continue }
            // A stale frame (navigated away, removed) just throws — skip it.
            guard let raw = try? await activeWebView.evaluateJavaScript(
                    ApplyScripts.snapshot, in: entry.frame, contentWorld: .page),
                  let d = raw as? [String: Any],
                  let fs = d["fields"] as? [[String: Any]], !fs.isEmpty else { continue }
            seenFrameURLs.insert(entry.url)
            snapshotFrames.append(entry.frame)
            merge(fs, frameIndex: snapshotFrames.count - 1)
        }
        return (dict["url"] as? String ?? "", fields)
    }

    /// Register fill.js, then invoke the async global with the merged items —
    /// grouped by the frame each field was snapshotted in, since the script
    /// world is per frame. Upload items arrive with `file_b64` (a string
    /// survives the JS bridge; a Data/byte-array does not) — decode each back
    /// into the `file_bytes` Uint8Array that fill.js's DataTransfer attachment
    /// path expects.
    func fill(items: [[String: Any]]) async throws -> [[String: Any]] {
        var byFrame: [Int: [[String: Any]]] = [:]
        for item in items {
            byFrame[item["_frame"] as? Int ?? 0, default: []].append(item)
        }
        var results: [[String: Any]] = []
        for (frameIdx, group) in byFrame.sorted(by: { $0.key < $1.key }) {
            let frame: WKFrameInfo? =
                frameIdx < snapshotFrames.count ? snapshotFrames[frameIdx] : nil
            do {
                results += try await fill(items: group, in: frame)
            } catch where frame != nil {
                // The iframe went away between snapshot and fill — report its
                // fields instead of sinking the whole run.
                results += group.map {
                    ["field_id": $0["field_id"] as? String ?? "",
                     "status": "not_found",
                     "message": "the form's frame reloaded — tap Autofill again"]
                }
            }
        }
        return results
    }

    private func fill(items: [[String: Any]], in frame: WKFrameInfo?) async throws -> [[String: Any]] {
        _ = try await activeWebView.evaluateJavaScript(
            ApplyScripts.fill, in: frame, contentWorld: .page)
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
            in: frame,
            contentWorld: .page)
        let dict = out as? [String: Any] ?? [:]
        return dict["results"] as? [[String: Any]] ?? []
    }

    /// Register workday_auth.js and read the on-page auth state
    /// ("create" | "signin" | "none") plus the tenant host.
    func workdayAuthState() async throws -> (state: String, tenantHost: String) {
        _ = try await activeWebView.evaluateJavaScript(ApplyScripts.workdayAuth)
        let out = try await activeWebView.callAsyncJavaScript(
            "return window.__jobsmithWorkdayAuthState();",
            arguments: [:],
            contentWorld: .page)
        let dict = out as? [String: Any] ?? [:]
        return (dict["state"] as? String ?? "none",
                (dict["tenantHost"] as? String ?? "").lowercased())
    }

    /// Fill + submit the Workday auth form. The password is passed as a bound
    /// argument (never interpolated into the script string), the same way
    /// document bytes are passed to fill.js.
    func workdayAuth(email: String, password: String) async throws -> [String: Any] {
        _ = try await activeWebView.evaluateJavaScript(ApplyScripts.workdayAuth)
        let out = try await activeWebView.callAsyncJavaScript(
            "return await window.__jobsmithWorkdayAuth(email, password);",
            arguments: ["email": email, "password": password],
            contentWorld: .page)
        return out as? [String: Any] ?? [:]
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
    static let workdayAuth = load("workday_auth")

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
                    // Why the fill didn't take (or needs a second look),
                    // straight from fill.js — without this a failed upload is
                    // undiagnosable on-device.
                    if !row.wasFilled || row.needsReview,
                       let message = row.fillMessage, !message.isEmpty {
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
        if row.needsReview { return "filled — double-check it" }
        if row.wasFilled { return "filled" }
        if row.required { return "required, not filled" }
        return "optional, not filled"
    }

    private func statusDot(_ row: ApplyFieldRow) -> some View {
        let color: Color = row.isUpload ? (row.wasFilled ? .green : .orange)
            : row.needsReview ? .yellow
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
        // Prefer the current format setting, but fall back to the format the
        // job was actually tailored under — flipping DOCX↔PDF in Settings must
        // not make an existing document read as "not tailored yet".
        var format = model.config.honesty.documentFormat
        var read = FileVault.read(jobId: job.id, kind: kind, format: format)
        if read == nil {
            let alt: FileVault.Format = format == .docx ? .pdf : .docx
            if let altData = FileVault.read(jobId: job.id, kind: kind, format: alt) {
                read = altData
                format = alt
            }
        }
        guard let data = read else {
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
