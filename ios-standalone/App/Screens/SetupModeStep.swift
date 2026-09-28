import SwiftUI
import JobsmithKit

/// Setup Assistant step 0 — "How should Jobsmith think?": Local (Apple
/// Intelligence everywhere), Cloud (a provider preset or any OpenAI-compatible
/// server) or Advanced (the full AI form). Twin of the desktop wizard's step 0.
///
/// Nothing is tested or saved while the user fills it in. Continue is the one
/// shared exit: a real 1-token chat ping to the chosen Writing model, then the
/// AI settings are saved in one write, so the profile import that follows
/// already uses them.
struct SetupModeStep: View {
    @Environment(AppModel.self) private var model
    /// Called after a save (or "Set up AI later"): the wizard moves on, the
    /// Settings entry ("Change setup…") pops back.
    let onDone: () -> Void

    enum Mode: String { case local, cloud, advanced }

    @State private var mode: Mode?
    @State private var localAvailable = AppleOnDeviceEngine.isAvailable
    @State private var localReason = AppleOnDeviceEngine.unavailableReason
    @State private var localDownloads = true
    // Cloud
    @State private var provider = ""        // preset name, "custom", or "" (none yet)
    @State private var baseURL = ""
    @State private var apiKey = ""
    @State private var models: [String] = []
    @State private var listError: String?
    @State private var listing = false
    @State private var writing = ""
    @State private var scoring = ""         // "" = same as Writing
    @State private var helpers = ""         // "" = same as Writing
    @State private var cloudQuickMatch = false
    @State private var showPicker = false
    // Shared exit
    @State private var testing = false
    @State private var failure: String?
    @State private var failureDetail = ""
    @State private var showDetails = false
    @State private var askCellular = false
    @State private var canContinueAnyway = false

    static let custom = "custom"
    private var preset: AIProviderPreset? { AIProviderPreset.all.first { $0.name == provider } }
    private var quickMB: Int { Int((Double(QuickMatchModel.sizeBytes) / 1_000_000).rounded()) }
    private var localMB: Int { Int((Double(QuickMatchModel.sizeBytes + NLIModel.sizeBytes) / 1_000_000).rounded()) }
    private var chatModels: [String] { models.filter { !AIProviderPreset.isNonChat($0) } }

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section {
                    card(.local, title: "Local",
                         body: "Runs privately on this device with Apple Intelligence. Free, nothing to set up. Writing quality is basic; you can connect a cloud provider later for stronger résumés.",
                         enabled: localAvailable, reason: localAvailable ? nil : localReason)
                    if !localAvailable {
                        Button("Check again") {
                            localAvailable = AppleOnDeviceEngine.isAvailable
                            localReason = AppleOnDeviceEngine.unavailableReason
                        }
                    }
                    card(.cloud, title: "Cloud",
                         body: "Use an AI provider such as OpenAI, Anthropic or OpenRouter. You bring the server address and an API key. Your provider may charge per use.",
                         enabled: true, reason: nil)
                    card(.advanced, title: "Advanced",
                         body: "Full control: any server, a different model per task, and local model switches.",
                         enabled: true, reason: nil)
                } header: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("How should Jobsmith think?")
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(.primary)
                            .textCase(nil)
                        Text("You can change this any time in Settings → AI.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .textCase(nil)
                    }
                    .padding(.bottom, 6)
                }
                switch mode {
                case .local: localSection
                case .cloud: cloudSections
                case .advanced: advancedSection
                case nil: EmptyView()
                }
            }
            exitBar
            Button {
                Task { await finish() }
            } label: {
                Group {
                    if testing {
                        HStack(spacing: 8) { ProgressView(); Text("Testing…") }
                    } else {
                        Text("Continue").fontWeight(.semibold)
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.ember)
            .disabled(testing)
            .padding(.horizontal, 16)
            Button("Set up AI later") { onDone() }
                .accessibilityIdentifier("setup.later")
                .padding(.vertical, 8)
        }
        .sheet(isPresented: $showPicker) {
            ModelPickerSheet(models: models, selection: $writing)
        }
        .confirmationDialog("Download \(downloadMB) MB on cellular?", isPresented: $askCellular,
                            titleVisibility: .visible) {
            Button("Download now") { startDownloads(); onDone() }
            Button("Wait for Wi-Fi") { onDone() }
        }
        .onAppear(perform: prefill)
    }

    // MARK: Cards

    private func card(_ m: Mode, title: String, body: String, enabled: Bool, reason: String?) -> some View {
        Button {
            mode = m
            failure = nil
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: mode == m ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(enabled ? Theme.ember : .secondary)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.headline).foregroundStyle(enabled ? .primary : .secondary)
                    Text(body).font(.footnote).foregroundStyle(.secondary)
                    if let reason {
                        Text(reason)
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(.red)
                            .accessibilityIdentifier("setup.local.reason")
                    }
                }
            }
        }
        .disabled(!enabled)
        .accessibilityIdentifier("setup.card.\(m.rawValue)")
        .accessibilityAddTraits(mode == m ? .isSelected : [])
    }

    // MARK: Local

    private var localSection: some View {
        Section {
            Toggle(isOn: $localDownloads) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Recommended").font(.caption.weight(.semibold)).foregroundStyle(.green)
                    Text("Download Quick match and Local match (about \(localMB) MB) for faster job scoring and smarter Apply Assist.")
                }
            }
        }
    }

    // MARK: Cloud

    @ViewBuilder
    private var cloudSections: some View {
        Section {
            Picker("Provider", selection: $provider) {
                Text("Choose…").tag("")
                ForEach(AIProviderPreset.all) { Text($0.name).tag($0.name) }
                Text("Custom").tag(Self.custom)
            }
            .onChange(of: provider) { _, new in providerChanged(new) }
            .accessibilityIdentifier("setup.provider")
            if let preset {
                LabeledContent("Server address", value: baseURL)
                Button("Edit address") {
                    keepAddress = true
                    provider = Self.custom
                }
                if let url = URL(string: preset.keyURL) {
                    Link("Get an API key", destination: url)
                }
            } else if provider == Self.custom {
                TextField("https://your-server/v1", text: $baseURL)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .onSubmit { baseURL = AIProviderPreset.normalize(baseURL) }
                    .accessibilityLabel("Server address")
            }
            if !provider.isEmpty {
                SecureField(provider == Self.custom ? "API key (optional)" : "API key", text: $apiKey)
                    .accessibilityLabel("API key")
            }
        } footer: {
            if provider == Self.custom {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Any OpenAI-compatible server, including one you run yourself.")
                    if AIProviderPreset.lacksV1(baseURL) {
                        Text("This address does not end in /v1. Most servers need it; check your server’s docs.")
                            .foregroundStyle(.orange)
                    }
                }
            }
        }
        // List models once there is something to list with: a key for a preset,
        // an address for Custom. Keyed on the inputs, so typing cancels the
        // previous attempt (a debounce with no timer bookkeeping).
        .task(id: "\(provider)|\(baseURL)|\(apiKey)") { await listModelsDebounced() }

        if !provider.isEmpty {
            Section {
                if !models.isEmpty {
                    Button {
                        showPicker = true
                    } label: {
                        LabeledContent("Writing model", value: writing.isEmpty ? "Choose…" : writing)
                    }
                    .accessibilityIdentifier("setup.writing")
                } else if listError != nil {
                    TextField("Model ID", text: $writing)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                if listing { HStack(spacing: 8) { ProgressView(); Text("Loading models…") } }
                if let listError {
                    Text(listError).font(.footnote).foregroundStyle(.secondary)
                }
                if !chatModels.isEmpty {
                    DisclosureGroup("Use different models for scoring") {
                        Picker("Scoring", selection: $scoring) {
                            Text("Same as Writing model").tag("")
                            ForEach(chatModels, id: \.self) { Text($0).tag($0) }
                        }
                        Picker("Quick helpers", selection: $helpers) {
                            Text("Same as Writing model").tag("")
                            ForEach(chatModels, id: \.self) { Text($0).tag($0) }
                        }
                    }
                }
                Toggle("Also download Quick match (about \(quickMB) MB) for faster, free job scoring.",
                       isOn: $cloudQuickMatch)
            } header: {
                Eyebrow(text: "Writing model")
            } footer: {
                Text("Writes your résumés and cover letters. Pick from the models your provider lists.")
            }
        }
    }

    @State private var keepAddress = false

    private func providerChanged(_ new: String) {
        if let p = AIProviderPreset.all.first(where: { $0.name == new }) {
            baseURL = p.baseURL
        } else if !keepAddress {
            baseURL = ""  // Custom starts empty
        }
        if !keepAddress { apiKey = "" }  // keys are per provider
        keepAddress = false
        models = []
        listError = nil
        writing = ""; scoring = ""; helpers = ""
    }

    private func listModelsDebounced() async {
        let url = AIProviderPreset.normalize(baseURL)
        guard !url.isEmpty, provider == Self.custom || !apiKey.isEmpty else { return }
        try? await Task.sleep(for: .milliseconds(700))
        guard !Task.isCancelled else { return }
        listing = true
        defer { listing = false }
        var probe = model.config.ai
        probe.baseURL = url
        probe.apiKey = apiKey
        do {
            let ids = try await model.aiEngine.listModels(config: probe)
            guard !Task.isCancelled else { return }
            models = ids.filter { $0 != AIConfig.onDeviceModelID }
            listError = models.isEmpty ? "This server did not list any models. Type the model ID instead." : nil
        } catch {
            guard !Task.isCancelled else { return }
            models = []
            listError = AIErrorMapper.describe(error, baseURL: url).message + ". Type the model ID instead."
        }
    }

    // MARK: Advanced

    private var advancedSection: some View {
        Section {
            NavigationLink("Open the full AI form") {
                AIConnectionSettingsView(showChangeSetup: false)
            }
        } footer: {
            Text("Any server, a model per task (Apple Intelligence included), and the Quick match / Local match switches. Come back here and tap Continue to test it.")
        }
    }

    // MARK: Shared exit

    /// Outside the Form so the result of Continue is always on screen.
    @ViewBuilder
    private var exitBar: some View {
        if let failure {
            VStack(alignment: .leading, spacing: 6) {
                Label(failure, systemImage: "xmark.octagon.fill")
                    .foregroundStyle(.red)
                    .font(.callout)
                    .accessibilityIdentifier("setup.failure")
                HStack(spacing: 16) {
                    if !failureDetail.isEmpty {
                        Button(showDetails ? "Hide details" : "Show details") { showDetails.toggle() }
                    }
                    if canContinueAnyway {
                        Button("Continue anyway") { Task { await finish(anyway: true) } }
                    }
                }
                .font(.callout)
                if showDetails {
                    Text(failureDetail).font(.caption.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(4)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 20)
            .padding(.top, 8)
        }
    }

    private func prefill() {
        let ai = model.config.ai
        switch model.config.setupMode {
        case Mode.local.rawValue where localAvailable:
            mode = .local
        case Mode.cloud.rawValue where !ai.provider.isEmpty:
            mode = .cloud
            keepAddress = true
            provider = ai.provider
            baseURL = ai.baseURL
            apiKey = ai.apiKey
            writing = ai.strongModel
        case Mode.advanced.rawValue:
            mode = .advanced
        default:
            break
        }
    }

    /// The AI settings this step would save, built from the form at the moment
    /// of saving (never from state captured earlier).
    private func chosenAI() -> AIConfig? {
        guard let mode else { return nil }
        var ai = model.config.ai
        let s = AIConfig.onDeviceModelID
        switch mode {
        case .local:
            ai.strongModel = s
            ai.fastModel = localDownloads ? AIConfig.localMatchModelID : s
            ai.utilityModel = s
            if localDownloads { ai.nliBetaEnabled = true }
        case .cloud:
            let w = writing.trimmingCharacters(in: .whitespaces)
            ai.provider = provider
            ai.baseURL = AIProviderPreset.normalize(baseURL)
            ai.apiKey = apiKey.trimmingCharacters(in: .whitespaces)
            ai.strongModel = w
            ai.fastModel = cloudQuickMatch ? AIConfig.localMatchModelID : scoring
            ai.utilityModel = helpers.isEmpty ? (scoring.isEmpty ? "" : w) : helpers
        case .advanced:
            break  // the full form already saved itself
        }
        return ai
    }

    private func finish(anyway: Bool = false) async {
        guard let mode, let ai = chosenAI() else {
            failure = "Choose Local, Cloud or Advanced, or set up AI later."
            failureDetail = ""
            canContinueAnyway = false
            return
        }
        guard !ai.strongModel.isEmpty else {
            failure = mode == .advanced
                ? "The Writing tier is empty. Open the full AI form and pick a Writing model."
                : "Pick a Writing model first."
            failureDetail = ""
            canContinueAnyway = false
            return
        }
        if !anyway {
            testing = true
            defer { testing = false }
            do {
                try await model.aiEngine.pingChat(model: ai.strongModel, config: ai)
            } catch {
                let d = AIErrorMapper.describe(error, baseURL: ai.baseURL,
                                               onDevice: ai.strongModel == AIConfig.onDeviceModelID)
                failure = d.message
                failureDetail = String(describing: error)
                canContinueAnyway = true
                return
            }
        }
        failure = nil
        await model.saveConfigNow { config in
            config.ai = ai
            config.setupMode = mode.rawValue
        }
        guard wantsDownloads else { onDone(); return }
        if DownloadPolicy.isExpensiveNetwork {
            askCellular = true  // ask once; the dialog moves on either way
        } else {
            startDownloads()
            onDone()
        }
    }

    private var wantsDownloads: Bool {
        (mode == .local && localDownloads) || (mode == .cloud && cloudQuickMatch)
    }

    private var downloadMB: Int { mode == .local ? localMB : quickMB }

    private func startDownloads() {
        NLIModelStore.quickMatch.install()
        if mode == .local { NLIModelStore.shared.install() }
    }
}

/// The Cloud Writing-model list: searchable, non-chat ids hidden unless
/// "Show all models" is on. Nothing is preselected.
struct ModelPickerSheet: View {
    let models: [String]
    @Binding var selection: String
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var showAll = false

    private var shown: [String] {
        models.filter { (showAll || !AIProviderPreset.isNonChat($0))
            && (query.isEmpty || $0.localizedCaseInsensitiveContains(query)) }
    }

    var body: some View {
        NavigationStack {
            List {
                Toggle("Show all models", isOn: $showAll)
                ForEach(shown, id: \.self) { id in
                    Button {
                        selection = id
                        dismiss()
                    } label: {
                        HStack {
                            Text(id).foregroundStyle(.primary)
                            Spacer()
                            if id == selection { Image(systemName: "checkmark").foregroundStyle(.tint) }
                        }
                    }
                }
            }
            .searchable(text: $query, prompt: "Search models")
            .navigationTitle("Writing model")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
    }
}
