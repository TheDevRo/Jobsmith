import SwiftUI
import JobsmithKit

/// AI endpoint + per-task model configuration.
///
/// Each task tier is assigned its own model — an endpoint model or Apple's
/// on-device model — so the user can see (and control) exactly what runs
/// where. On-device is just another option in each dropdown, not a separate
/// engine mode.
///
/// State note: the fields are `@State` mirrors saved to `model.saveConfig`
/// shortly after the last change (debounced) and again on disappear, rather
/// than bindings straight into `model.config.ai`: `test()` probes the endpoint
/// with the typed values, and writing through on every keystroke would persist
/// half-typed URLs and keys. Killing the app mid-edit loses at most the last
/// half second.
struct AIConnectionSettingsView: View {
    @Environment(AppModel.self) private var model
    /// Off when this form IS the setup wizard's Advanced path (no loop back).
    var showChangeSetup = true
    @State private var showSetup = false
    @State private var baseURL = ""
    @State private var apiKey = ""
    @State private var strongModel = ""
    @State private var fastModel = ""
    @State private var utilityModel = ""
    @State private var testing = false
    @State private var status: ConnectionStatus?
    @State private var showSavePrompt = false
    @State private var presetName = ""
    /// NavigationLink builds this destination eagerly, and a never-shown
    /// instance can still fire `onDisappear` — which would flush these empty
    /// defaults over the real endpoint, key and models. Only an instance that
    /// actually appeared (and so loaded from config) may save.
    @State private var hasAppeared = false
    @ObservedObject private var localModel = NLIModelStore.shared
    @ObservedObject private var quickModel = NLIModelStore.quickMatch
    @State private var saveTask: Task<Void, Never>?
    /// The Scoring model to go back to when Quick match is switched off.
    @AppStorage("quickMatchPreviousFastModel") private var previousFast = ""
    @State private var cellularAsk: NLIModelStore?
    @State private var confirmDelete: NLIModelStore?

    private var availableModels: [String] { status?.models ?? [] }
    private var onDeviceAvailable: Bool { AppleOnDeviceEngine.isAvailable }
    /// Any pickable model exists (endpoint list or on-device) — otherwise we
    /// fall back to free-text entry so an offline user can still type a name.
    private var hasPickableModels: Bool { !availableModels.isEmpty || onDeviceAvailable }

    /// Probe the endpoint with the CURRENT field values (not yet saved), so
    /// the user tests exactly what they typed.
    private func test() async {
        testing = true
        defer { testing = false }
        var probe = model.config.ai
        probe.baseURL = baseURL.trimmingCharacters(in: .whitespaces)
        probe.apiKey = apiKey
        let result = await OpenAICompatibleEngine().testConnection(config: probe)
        status = result  // no auto-pick: the user chooses every tier
    }

    /// Endpoint dropdown options: the live model list, plus the current value
    /// if the server no longer reports it (so the picker isn't blank).
    private func endpointOptions(current: String) -> [String] {
        var names = availableModels
        if !current.isEmpty, current != AIConfig.onDeviceModelID, current != AIConfig.localMatchModelID,
           !names.contains(current) {
            names.insert(current, at: 0)
        }
        return names
    }

    /// Where a tier's work would run given the CURRENT (unsaved) selections.
    private func whereRuns(_ tier: ModelTier) -> String {
        var ai = model.config.ai
        ai.strongModel = strongModel; ai.fastModel = fastModel; ai.utilityModel = utilityModel
        if tier == .fast, ai.usesLocalMatchModel {
            let writer = ScoreSource.llm(ai, .fast).label
            if quickModel.state == .ready {
                return "→ Scores every job on your device with Quick match, no AI calls. Jobs it can't judge go to Local match when it's on and downloaded, else your Writing model · \(writer). AI form-fill and essays use your Writing model."
            }
            return quickModel.isDownloading
                ? "→ Scoring uses your Writing model · \(writer) until Quick match finishes downloading."
                : "→ Scoring uses your Writing model · \(writer) — Quick match isn't downloaded. Download it under Quick match below."
        }
        if ai.usesOnDevice(for: tier) {
            return "→ Runs on your device: private, offline, free. A small model, so quality is below a good server model."
        }
        let name = ai.endpointModel(for: tier)
        return name == "local-model"
            ? "→ Runs on your endpoint. Test the connection to pick a model."
            : "→ Runs on your endpoint · \(name)."
    }

    var body: some View {
        Form {
            if showChangeSetup {
                Section {
                    Button("Change setup…") { showSetup = true }
                } footer: {
                    Text("Re-run the Local / Cloud / Advanced choice from first-time setup.")
                }
            }
            savedEndpointsSection
            endpointSection
            tierSection(tier: .strong, selection: $strongModel,
                        title: "Writing",
                        blurb: "Résumés and cover letters, and their revisions. Use your most capable model here.")
            tierSection(tier: .fast, selection: $fastModel, fallbackLabel: "Same as Writing model",
                        title: "Scoring",
                        blurb: "Rates each job's fit and maps application form fields. Runs often — a smaller or on-device model is usually fine.")
            tierSection(tier: .utility, selection: $utilityModel, fallbackLabel: "Same as Scoring model",
                        title: "Quick helpers",
                        blurb: "Salary-title lookup and picking which résumé sections to include. The lightest calls.")
            quickMatchSection
            localModelSection
            batchScoringSection
        }
        .navigationTitle("AI connection")
        .navigationDestination(isPresented: $showSetup) {
            SetupModeStep(onDone: { showSetup = false })
                .navigationTitle("Change setup")
        }
        .onChange(of: fastModel) { old, new in
            scheduleSave()
            // Picking Quick match (here or with its switch) starts its download and
            // remembers the Scoring model to restore. Not on load: re-opening this
            // screen must not restart a deleted download.
            guard hasAppeared, new == AIConfig.localMatchModelID, old != new,
                  model.config.ai.fastModel != new else { return }
            previousFast = old
            if quickModel.state != .ready { requestDownload(quickModel) }
        }
        .onChange(of: baseURL) { scheduleSave() }
        .onChange(of: apiKey) { scheduleSave() }
        .onChange(of: strongModel) { scheduleSave() }
        .onChange(of: utilityModel) { scheduleSave() }
        .confirmationDialog(cellularTitle, isPresented: Binding(
            get: { cellularAsk != nil }, set: { if !$0 { cellularAsk = nil } }
        ), titleVisibility: .visible) {
            Button("Download now") { cellularAsk?.install(); cellularAsk = nil }
            Button("Wait for Wi-Fi", role: .cancel) { cellularAsk = nil }
        }
        .confirmationDialog(deleteTitle, isPresented: Binding(
            get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } }
        ), titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                let store = confirmDelete
                confirmDelete = nil
                Task { await store?.delete() }
            }
        } message: {
            Text("It can be downloaded again later.")
        }
        .onAppear {
            hasAppeared = true
            baseURL = model.config.ai.baseURL
            apiKey = model.config.ai.apiKey
            strongModel = model.config.ai.strongModel
            fastModel = model.config.ai.fastModel
            utilityModel = model.config.ai.utilityModel
            // Populate the dropdowns quietly when an endpoint is configured.
            if !baseURL.trimmingCharacters(in: .whitespaces).isEmpty && status == nil {
                Task { await test() }
            }
        }
        .onDisappear {
            guard hasAppeared else { return }
            saveTask?.cancel()
            flush()
        }
    }

    /// Save a beat after the last edit, so a killed app keeps what was typed.
    private func scheduleSave() {
        guard hasAppeared else { return }
        saveTask?.cancel()
        saveTask = Task {
            try? await Task.sleep(for: .milliseconds(600))
            guard !Task.isCancelled else { return }
            flush()
        }
    }

    private func flush() {
        let (u, k, s, f, ut) = (baseURL, apiKey, strongModel, fastModel, utilityModel)
        model.saveConfig { config in
            config.ai.baseURL = u
            // Keep ai.provider honest when the address is edited here.
            config.ai.provider = AIProviderPreset.all.first { $0.baseURL == u }?.name
                ?? (u.isEmpty ? "" : "custom")
            config.ai.apiKey = k
            config.ai.strongModel = s
            config.ai.fastModel = f
            config.ai.utilityModel = ut
            // Per-tier models are now the single source of truth; retire
            // the legacy engine switch so it can't override them.
            config.ai.engine = .openAICompatible
            config.ai.preferOnDeviceForLightTasks = false
        }
    }

    /// Start a model download, asking first on cellular / metered networks.
    private func requestDownload(_ store: NLIModelStore) {
        if DownloadPolicy.isExpensiveNetwork { cellularAsk = store } else { store.install() }
    }

    private func name(of store: NLIModelStore) -> String { store === quickModel ? "Quick match" : "Local match" }

    private func megabytes(_ store: NLIModelStore) -> Int {
        let bytes = store === quickModel ? QuickMatchModel.sizeBytes : NLIModel.sizeBytes
        return Int((Double(bytes) / 1_000_000).rounded())
    }

    private var cellularTitle: String {
        cellularAsk.map { "Download \(megabytes($0)) MB on cellular?" } ?? ""
    }

    private var deleteTitle: String {
        confirmDelete.map { "Delete the \(name(of: $0)) model?" } ?? ""
    }

    /// The one-tap switcher. Selecting a preset fills the fields, persists the
    /// switch immediately (unlike the fields, which flush on disappear — a
    /// switch is an explicit "use this now"), and re-probes the endpoint so
    /// the model pickers refresh. Saving captures the CURRENT typed fields.
    private var savedEndpointsSection: some View {
        Section {
            ForEach(model.config.ai.savedEndpoints) { endpoint in
                Button {
                    applyPreset(endpoint)
                } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(endpoint.name)
                                .foregroundStyle(.primary)
                            Text(endpoint.baseURL)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                        Spacer()
                        if isActive(endpoint) {
                            Image(systemName: "checkmark")
                                .foregroundStyle(.tint)
                                .accessibilityLabel("Active")
                        }
                    }
                }
                .accessibilityLabel("Switch to \(endpoint.name)")
            }
            .onDelete { offsets in
                model.saveConfig { $0.ai.savedEndpoints.remove(atOffsets: offsets) }
            }
            Button {
                presetName = model.config.ai.savedEndpoints
                    .first { isActive($0) }?.name ?? suggestedPresetName
                showSavePrompt = true
            } label: {
                Label("Save current endpoint…", systemImage: "plus.circle")
            }
            .disabled(baseURL.trimmingCharacters(in: .whitespaces).isEmpty)
        } header: {
            Eyebrow(text: "Saved endpoints")
        } footer: {
            Text("Keep every server you use here — keys and model choices included — and switch between them in one tap. Saved only on this device.")
        }
        .alert("Save endpoint", isPresented: $showSavePrompt) {
            TextField("Name", text: $presetName)
            Button("Save") { savePreset() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Saving with an existing preset's name replaces it.")
        }
    }

    private func isActive(_ endpoint: AIConfig.SavedEndpoint) -> Bool {
        endpoint.baseURL == baseURL.trimmingCharacters(in: .whitespaces)
            && endpoint.apiKey == apiKey
    }

    /// Default preset name: the endpoint's host ("openrouter.ai").
    private var suggestedPresetName: String {
        URL(string: baseURL.trimmingCharacters(in: .whitespaces))?.host ?? ""
    }

    private func savePreset() {
        let name = presetName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        var snapshot = model.config.ai
        snapshot.baseURL = baseURL.trimmingCharacters(in: .whitespaces)
        snapshot.apiKey = apiKey
        snapshot.strongModel = strongModel
        snapshot.fastModel = fastModel
        snapshot.utilityModel = utilityModel
        model.saveConfig { config in
            if let i = config.ai.savedEndpoints.firstIndex(where: { $0.name == name }) {
                config.ai.savedEndpoints[i] = snapshot.capture(
                    name: name, id: config.ai.savedEndpoints[i].id)
            } else {
                config.ai.savedEndpoints.append(snapshot.capture(name: name))
            }
        }
    }

    private func applyPreset(_ endpoint: AIConfig.SavedEndpoint) {
        model.saveConfig { $0.ai.apply(endpoint) }
        let ai = model.config.ai
        baseURL = ai.baseURL
        apiKey = ai.apiKey
        strongModel = ai.strongModel
        fastModel = ai.fastModel
        utilityModel = ai.utilityModel
        status = nil
        Task { await test() }
    }

    private var endpointSection: some View {
        Section {
            // Shortcut: fill the address from a provider preset (still editable).
            Menu {
                ForEach(AIProviderPreset.all) { p in
                    Button(p.name) { baseURL = p.baseURL; status = nil }
                }
            } label: {
                Label("Provider presets", systemImage: "list.bullet")
            }
            TextField("https://your-server/v1", text: $baseURL)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .accessibilityLabel("Endpoint URL")
            SecureField("API key (optional for your own server)", text: $apiKey)
                .accessibilityLabel("API key")
            Button {
                Task { await test() }
            } label: {
                if testing {
                    HStack(spacing: 8) { ProgressView(); Text("Testing…") }
                } else {
                    Label("Test connection", systemImage: "bolt.horizontal")
                }
            }
            .disabled(testing || baseURL.trimmingCharacters(in: .whitespaces).isEmpty)
            if let status {
                // Success/failure is carried by the symbol and the sentence, not
                // by the green/red tint alone.
                if status.connected {
                    Label("Connected — \(status.models.count) model\(status.models.count == 1 ? "" : "s") available",
                          systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .font(.callout)
                } else {
                    Label(status.error ?? "Connection failed",
                          systemImage: "xmark.octagon.fill")
                        .foregroundStyle(.red)
                        .font(.callout)
                        .accessibilityLabel("Connection failed. \(status.error ?? "")")
                }
            }
        } header: {
            Eyebrow(text: "OpenAI-compatible endpoint")
        } footer: {
            if onDeviceAvailable {
                Text("Any OpenAI-compatible server, hosted or your own. You can also assign Apple's on-device model to any task below — it's private, offline, and free.")
            } else {
                Text("Any OpenAI-compatible server, hosted or your own. (Apple's on-device model would appear as an option below on an iOS 26 Apple Intelligence device.)")
            }
        }
    }

    /// One task tier: a labeled model picker plus a footer that spells out
    /// what the tier does and where the current selection sends its data.
    private func tierSection(tier: ModelTier, selection: Binding<String>,
                             fallbackLabel: String? = nil,
                             title: String, blurb: String) -> some View {
        Section {
            // The fast tier always has a pickable option: the local match model.
            if hasPickableModels || tier == .fast {
                Picker("Model", selection: selection) {
                    if let fallbackLabel {
                        Text(fallbackLabel).tag("")
                    }
                    if onDeviceAvailable {
                        Text("Apple Intelligence").tag(AIConfig.onDeviceModelID)
                    }
                    if tier == .fast {
                        Text("Quick match").tag(AIConfig.localMatchModelID)
                    }
                    ForEach(endpointOptions(current: selection.wrappedValue), id: \.self) { name in
                        Text(name).tag(name)
                    }
                }
                .accessibilityLabel("\(title) model")
            }
            if !hasPickableModels, selection.wrappedValue != AIConfig.localMatchModelID {
                TextField(fallbackLabel ?? "Model name", text: selection)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityLabel("\(title) model")
            }
        } header: {
            Eyebrow(text: title)
        } footer: {
            VStack(alignment: .leading, spacing: 4) {
                Text(blurb)
                if tier == .strong, strongModel.trimmingCharacters(in: .whitespaces).isEmpty {
                    Label("No Writing model — profile import, résumés and cover letters won't work until you pick one.",
                          systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                } else {
                    Text(whereRuns(tier)).fontWeight(.medium)
                }
            }
        }
    }

    /// Progress, Stop, Retry and Delete for one downloadable model. Delete only
    /// when the model is on disk, and always behind a confirmation.
    @ViewBuilder
    private func downloadRows(_ store: NLIModelStore, status: String, canDownload: Bool) -> some View {
        HStack {
            Text("Model")
            Spacer()
            Text(status).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
        if case .downloading(let p) = store.state {
            ProgressView(value: p)
            Button("Stop download") { store.cancel() }
        }
        if case .failed(let message) = store.state {
            Text(message).font(.footnote).foregroundStyle(.red)
        }
        if canDownload, !store.isDownloading, store.state != .ready {
            Button(store.state == .notInstalled ? "Download" : "Retry download") { requestDownload(store) }
        }
        if store.state == .ready {
            Button("Delete \(name(of: store)) model", role: .destructive) { confirmDelete = store }
        }
    }

    /// Quick match: one switch that IS the Scoring choice (fastModel =
    /// local-match-model); off restores the previous Scoring model.
    private var quickMatchSection: some View {
        Section {
            Toggle("Quick match (on-device, free)", isOn: Binding(
                get: { fastModel == AIConfig.localMatchModelID },
                set: { on in
                    if on {
                        if fastModel != AIConfig.localMatchModelID { previousFast = fastModel }
                        fastModel = AIConfig.localMatchModelID
                    } else {
                        fastModel = previousFast == AIConfig.localMatchModelID ? "" : previousFast
                        quickModel.cancel()
                    }
                }
            ))
            if fastModel == AIConfig.localMatchModelID {
                Toggle("Run Quick match on the Neural Engine (experimental)", isOn: Binding(
                    get: { model.config.ai.triageUseNeuralEngine },
                    set: { on in model.saveConfig { $0.ai.triageUseNeuralEngine = on } }
                ))
            }
            downloadRows(quickModel, status: quickModelStatus,
                         canDownload: fastModel == AIConfig.localMatchModelID)
        } header: {
            Eyebrow(text: "Quick match")
        } footer: {
            Text("Scores every job on this device in a fraction of a second, from how well your profile covers each requirement. Jobs it can't judge go to your Writing model. One-time download of \(ByteCountFormatter.string(fromByteCount: QuickMatchModel.sizeBytes, countStyle: .file)); it keeps downloading if you leave this screen.")
        }
    }

    /// Local match: saved immediately like the batch cap, and turning it on
    /// starts the one-time download.
    private var localModelSection: some View {
        Section {
            Toggle("Local match (beta)", isOn: Binding(
                get: { model.config.ai.nliBetaEnabled },
                set: { on in
                    model.saveConfig { $0.ai.nliBetaEnabled = on }
                    if on { requestDownload(localModel) } else { localModel.cancel() }
                }
            ))
            if model.config.ai.nliBetaEnabled {
                Toggle("Run on the Neural Engine (experimental)", isOn: Binding(
                    get: { model.config.ai.nliUseNeuralEngine },
                    set: { on in model.saveConfig { $0.ai.nliUseNeuralEngine = on } }
                ))
                Toggle("Refine top matches with Local match", isOn: Binding(
                    get: { model.config.ai.triageRefine },
                    set: { on in model.saveConfig { $0.ai.triageRefine = on } }
                ))
            }
            downloadRows(localModel, status: localModelStatus, canDownload: model.config.ai.nliBetaEnabled)
        } header: {
            Eyebrow(text: "Local match")
        } footer: {
            Text("Jobsmith’s own on-device model — separate from Apple Intelligence. It checks your profile against each requirement: it fills application forms only from your profile (options, profile values, years of experience), and scores jobs whenever your AI model fails (unreachable, misconfigured, or out of quota). With Quick match on, it also judges the jobs Quick match can't, and “Refine top matches” re-checks the top 15% of each run. Every score is labeled with what produced it: “Quick match”, “Local match”, “Apple Intelligence”, or your endpoint’s model name. Essay questions still use your AI model and are marked as drafts. One-time download of \(ByteCountFormatter.string(fromByteCount: NLIModel.sizeBytes, countStyle: .file)); it keeps downloading if you leave this screen or the app.")
        }
    }

    private var quickModelStatus: String {
        switch quickModel.state {
        case .notInstalled: return "Not downloaded"
        case .downloading(let p): return "Downloading \(Int(p * 100))%"
        case .ready: return "Ready"
        case .failed: return "Download failed"
        }
    }

    private var localModelStatus: String {
        switch localModel.state {
        case .notInstalled: return model.config.ai.nliBetaEnabled ? "Not downloaded" : "Off"
        case .downloading(let p): return "Downloading \(Int(p * 100))%"
        case .ready: return model.config.ai.nliBetaEnabled ? "Ready" : "Downloaded (off)"
        case .failed: return "Download failed"
        }
    }

    private var batchScoringSection: some View {
        Section {
            Stepper(value: Binding(
                get: { model.config.ai.scoreAllCap },
                set: { v in model.saveConfig { $0.ai.scoreAllCap = v } }
            ), in: 5...200, step: 5) {
                Text("Score-all limit: \(model.config.ai.scoreAllCap)")
            }
        } header: {
            Eyebrow(text: "Batch scoring")
        } footer: {
            Text("The default number of jobs a “Score” run processes in one tap. When more are unscored, “Score all” can still score every one — this cap just keeps the default tap in check. You can Stop a run at any time.")
        }
    }
}
