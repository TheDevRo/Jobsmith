import SwiftUI
import JobsmithKit

/// AI endpoint + per-task model configuration.
///
/// Each task tier is assigned its own model — an endpoint model or Apple's
/// on-device model — so the user can see (and control) exactly what runs
/// where. On-device is just another option in each dropdown, not a separate
/// engine mode.
///
/// State note: the fields are `@State` mirrors flushed to `model.saveConfig`
/// on disappear, rather than bindings straight into `model.config.ai`. That is
/// deliberate — `test()` probes the endpoint with the *typed but unsaved*
/// values, and writing through on every keystroke would persist (and
/// re-validate) half-typed URLs and keys.
struct AIConnectionSettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var baseURL = ""
    @State private var apiKey = ""
    @State private var strongModel = ""
    @State private var fastModel = ""
    @State private var utilityModel = ""
    @State private var testing = false
    @State private var status: ConnectionStatus?

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
        status = result
        // Preselect the first model so the strong dropdown never sits blank.
        if result.connected, strongModel.isEmpty, let first = result.models.first {
            strongModel = first
        }
    }

    /// Endpoint dropdown options: the live model list, plus the current value
    /// if the server no longer reports it (so the picker isn't blank).
    private func endpointOptions(current: String) -> [String] {
        var names = availableModels
        if !current.isEmpty, current != AIConfig.onDeviceModelID, !names.contains(current) {
            names.insert(current, at: 0)
        }
        return names
    }

    /// Where a tier's work would run given the CURRENT (unsaved) selections.
    private func whereRuns(_ tier: ModelTier) -> String {
        var ai = model.config.ai
        ai.strongModel = strongModel; ai.fastModel = fastModel; ai.utilityModel = utilityModel
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
            endpointSection
            tierSection(tier: .strong, selection: $strongModel,
                        title: "Resume & cover letters",
                        blurb: "Writes your full tailored documents and revisions. Use your most capable model here.")
            tierSection(tier: .fast, selection: $fastModel, fallbackLabel: "Same as Resume model",
                        title: "Scoring & form-fill",
                        blurb: "Rates each job's fit and maps application form fields. Runs often — a smaller or on-device model is usually fine.")
            tierSection(tier: .utility, selection: $utilityModel, fallbackLabel: "Same as Scoring model",
                        title: "Quick helpers",
                        blurb: "Salary-title lookup and picking which résumé sections to include. The lightest calls.")
            batchScoringSection
        }
        .navigationTitle("AI connection")
        .onAppear {
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
            let (u, k, s, f, ut) = (baseURL, apiKey, strongModel, fastModel, utilityModel)
            model.saveConfig { config in
                config.ai.baseURL = u
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
    }

    private var endpointSection: some View {
        Section {
            TextField("http://192.168.1.7:1234/v1", text: $baseURL)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .accessibilityLabel("Endpoint URL")
            SecureField("API key (blank for LM Studio)", text: $apiKey)
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
                Text("LM Studio, Ollama, OpenRouter, or any chat-completions server. You can also assign Apple's on-device model to any task below — it's private, offline, and free.")
            } else {
                Text("LM Studio, Ollama, OpenRouter, or any chat-completions server. (Apple's on-device model would appear as an option below on an iOS 26 Apple Intelligence device.)")
            }
        }
    }

    /// One task tier: a labeled model picker plus a footer that spells out
    /// what the tier does and where the current selection sends its data.
    private func tierSection(tier: ModelTier, selection: Binding<String>,
                             fallbackLabel: String? = nil,
                             title: String, blurb: String) -> some View {
        Section {
            if hasPickableModels {
                Picker("Model", selection: selection) {
                    if let fallbackLabel {
                        Text(fallbackLabel).tag("")
                    }
                    if onDeviceAvailable {
                        Text("Apple On-Device").tag(AIConfig.onDeviceModelID)
                    }
                    ForEach(endpointOptions(current: selection.wrappedValue), id: \.self) { name in
                        Text(name).tag(name)
                    }
                }
                .accessibilityLabel("\(title) model")
            } else {
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
                Text(whereRuns(tier)).fontWeight(.medium)
            }
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
