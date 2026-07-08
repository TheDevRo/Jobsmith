import SwiftUI
import JobsmithKit

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var showSetupFlow = false
    @State private var showDeletePostings = false
    @State private var showDeleteAllData = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    NavigationLink {
                        ProfileEditorView()
                    } label: {
                        row("Profile", system: "person.crop.circle",
                            detail: model.config.profile.fullName.isEmpty
                                ? "Not set up" : model.config.profile.fullName)
                    }
                    NavigationLink {
                        SearchSettingsView()
                    } label: {
                        row("Search & sources", system: "magnifyingglass",
                            detail: model.config.search.keywords.isEmpty
                                ? "No keywords" : model.config.search.keywords.joined(separator: ", "))
                    }
                    NavigationLink {
                        AISettingsView()
                    } label: {
                        row("AI connection", system: "cpu",
                            detail: model.config.ai.baseURL)
                    }
                    Button {
                        showSetupFlow = true
                    } label: {
                        row("Run setup assistant", system: "wand.and.rays", detail: nil)
                    }
                    .foregroundStyle(.primary)
                } header: {
                    Eyebrow(text: "Setup")
                }

                Section {
                    formatPicker
                    honestyPicker
                    stylePicker
                    experienceLimit
                } header: {
                    Eyebrow(text: "Documents")
                } footer: {
                    Text("Résumés and cover letters are generated in your chosen format. Limiting work history keeps only the roles most relevant to each job (pinned roles are always included). Honesty controls how much latitude the AI takes when tailoring — from reorder-only to invented experience. Fabricated is at your own risk.")
                }

                Section {
                    Button(role: .destructive) {
                        showDeletePostings = true
                    } label: {
                        Label("Delete all tracked postings", systemImage: "trash")
                    }
                    Button(role: .destructive) {
                        showDeleteAllData = true
                    } label: {
                        Label("Delete all data", systemImage: "exclamationmark.triangle")
                    }
                } header: {
                    Eyebrow(text: "Danger zone")
                } footer: {
                    Text("Deleting postings clears your inbox and pipeline and their tailored documents, but keeps your profile and settings. Deleting all data resets the app to a clean install.")
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Settings")
            .sheet(isPresented: $showSetupFlow) {
                OnboardingFlow()
                    .environment(model)
            }
            .confirmationDialog("Delete all tracked postings?",
                                isPresented: $showDeletePostings, titleVisibility: .visible) {
                Button("Delete all postings", role: .destructive) {
                    model.deleteAllTrackedPostings()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Removes every job in your inbox and pipeline and their tailored documents. Your profile, settings, and saved answers are kept. This can't be undone.")
            }
            .confirmationDialog("Delete all data?",
                                isPresented: $showDeleteAllData, titleVisibility: .visible) {
                Button("Erase everything", role: .destructive) {
                    model.deleteAllData()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Erases all postings, documents, saved answers, and your profile and settings — resetting the app to a clean install. This can't be undone.")
            }
        }
    }

    private var formatPicker: some View {
        Picker(selection: Binding(
            get: { model.config.honesty.documentFormat },
            set: { format in model.saveConfig { $0.honesty.documentFormat = format } }
        )) {
            ForEach(FileVault.Format.allCases, id: \.self) { format in
                Text(format.label).tag(format)
            }
        } label: {
            row("File format", system: "doc", detail: nil)
        }
    }

    /// Cap the number of work-history entries the AI includes (null = all,
    /// 1–20 otherwise), mirroring the desktop `max_resume_experience_entries`.
    @ViewBuilder
    private var experienceLimit: some View {
        let limit = model.config.honesty.maxResumeExperienceEntries
        Toggle(isOn: Binding(
            get: { limit != nil },
            set: { on in model.saveConfig { $0.honesty.maxResumeExperienceEntries = on ? (limit ?? 5) : nil } }
        )) {
            Label("Limit work history", systemImage: "briefcase")
        }
        if let current = limit {
            Stepper(value: Binding(
                get: { current },
                set: { value in model.saveConfig { $0.honesty.maxResumeExperienceEntries = value } }
            ), in: 1...20) {
                row("Most relevant roles", system: "list.number", detail: "\(current)")
            }
        }
    }

    private var honestyPicker: some View {
        Picker(selection: Binding(
            get: { model.config.honesty.level },
            set: { level in model.saveConfig { $0.honesty.level = level } }
        )) {
            ForEach(HonestyConfig.Level.allCases, id: \.self) { level in
                Text(level.rawValue.capitalized).tag(level)
            }
        } label: {
            row("Honesty level", system: "checkmark.seal", detail: nil)
        }
    }

    private var stylePicker: some View {
        Picker(selection: Binding(
            get: { model.config.honesty.resumeStyle },
            set: { style in model.saveConfig { $0.honesty.resumeStyle = style } }
        )) {
            ForEach(HonestyConfig.Style.allCases, id: \.self) { style in
                Text(style.rawValue.capitalized).tag(style)
            }
        } label: {
            row("Resume style", system: "doc.richtext", detail: nil)
        }
    }

    private func row(_ title: String, system: String, detail: String?) -> some View {
        HStack {
            Label(title, systemImage: system)
            if let detail {
                Spacer()
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }
}

/// Search keywords, locations, filters, and per-source toggles.
struct SearchSettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var showTitleSuggest = false
    @State private var keywords = ""
    @State private var locations = ""
    @State private var excludes = ""
    @State private var greenhouseBoards = ""
    @State private var leverCompanies = ""
    @State private var adzunaAppID = ""
    @State private var adzunaAppKey = ""
    @State private var usajobsEmail = ""
    @State private var usajobsKey = ""
    @State private var blsKey = ""

    var body: some View {
        Form {
            Section {
                TextField("software engineer, backend developer", text: $keywords, axis: .vertical)
                Button {
                    showTitleSuggest = true
                } label: {
                    Label("Help me pick", systemImage: "sparkles")
                }
            } header: {
                Eyebrow(text: "Keywords (comma-separated)")
            } footer: {
                Text("Not sure what to search? Let the AI suggest job titles from your profile.")
            }
            Section {
                TextField("Remote, Denver", text: $locations, axis: .vertical)
            } header: {
                Eyebrow(text: "Locations")
            }
            Section {
                TextField("director, principal", text: $excludes, axis: .vertical)
            } header: {
                Eyebrow(text: "Exclude keywords")
            }
            Section {
                ForEach(sourceList, id: \.0) { id, label in
                    Toggle(label, isOn: Binding(
                        get: { model.config.search.enabledSources.contains(id) },
                        set: { on in
                            model.saveConfig { config in
                                if on { config.search.enabledSources.insert(id) }
                                else { config.search.enabledSources.remove(id) }
                            }
                        }
                    ))
                }
            } header: {
                Eyebrow(text: "Sources")
            } footer: {
                Text("Most sources just work. Adzuna and USAJobs need a free API key — fields appear below when you turn them on.")
            }
            if model.config.search.enabledSources.contains("adzuna") {
                Section {
                    TextField("App ID", text: $adzunaAppID)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("App key", text: $adzunaAppKey)
                } header: {
                    Eyebrow(text: "Adzuna keys")
                } footer: {
                    Text("Free at developer.adzuna.com. Also powers salary estimates on job pages.")
                }
            }
            if model.config.search.enabledSources.contains("usajobs") {
                Section {
                    TextField("Registered email", text: $usajobsEmail)
                        .keyboardType(.emailAddress)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("API key", text: $usajobsKey)
                } header: {
                    Eyebrow(text: "USAJobs keys")
                } footer: {
                    Text("Free at developer.usajobs.gov.")
                }
            }
            CompanyFollowControls()
            Section {
                TextField("stripe, airbnb", text: $greenhouseBoards, axis: .vertical)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Eyebrow(text: "Greenhouse slugs (manual)")
            } footer: {
                Text("Advanced: enter Greenhouse slugs directly. Most people should use the company finder above instead.")
            }
            Section {
                TextField("openai", text: $leverCompanies, axis: .vertical)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Eyebrow(text: "Lever slugs (manual)")
            }
            Section {
                SecureField("BLS registration key", text: $blsKey)
            } header: {
                Eyebrow(text: "Salary estimates (optional)")
            } footer: {
                Text("Fallback wage data for salary estimates when Adzuna has none. Free at data.bls.gov/registrationEngine.")
            }
        }
        .navigationTitle("Search & sources")
        .sheet(isPresented: $showTitleSuggest) {
            TitleSuggestSheet(existing: split(keywords)) { titles in
                addTitles(titles)
            }
            .environment(model)
        }
        .onAppear {
            let search = model.config.search
            keywords = search.keywords.joined(separator: ", ")
            locations = search.locations.joined(separator: ", ")
            excludes = search.excludeKeywords.joined(separator: ", ")
            greenhouseBoards = search.greenhouseBoards.joined(separator: ", ")
            leverCompanies = search.leverCompanies.joined(separator: ", ")
            let keys = model.config.apiKeys
            adzunaAppID = keys.adzunaAppID
            adzunaAppKey = keys.adzunaAppKey
            usajobsEmail = keys.usajobsEmail
            usajobsKey = keys.usajobsAPIKey
            blsKey = keys.blsRegistrationKey
        }
        .onDisappear { save() }
    }

    private var sourceList: [(String, String)] {
        [("remoteok", "RemoteOK"), ("weworkremotely", "WeWorkRemotely"),
         ("arbeitnow", "Arbeitnow"), ("greenhouse", "Greenhouse & Lever boards"),
         ("ashby", "Ashby boards"), ("workable", "Workable"),
         ("recruitee", "Recruitee"), ("adzuna", "Adzuna (API key)"),
         ("usajobs", "USAJobs (API key)"), ("linkedin", "LinkedIn")]
    }

    private func split(_ text: String) -> [String] {
        text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    /// Merge AI-suggested titles into the keyword field, skipping duplicates,
    /// and persist immediately so the choice survives even without leaving the
    /// screen.
    private func addTitles(_ titles: [String]) {
        var current = split(keywords)
        let seen = Set(current.map { $0.lowercased() })
        for title in titles where !seen.contains(title.lowercased()) {
            current.append(title)
        }
        keywords = current.joined(separator: ", ")
        save()
    }

    private func save() {
        let k = split(keywords), l = split(locations), e = split(excludes)
        let gh = split(greenhouseBoards), lv = split(leverCompanies)
        let (aID, aKey) = (adzunaAppID.trimmingCharacters(in: .whitespaces),
                           adzunaAppKey.trimmingCharacters(in: .whitespaces))
        let (uEmail, uKey) = (usajobsEmail.trimmingCharacters(in: .whitespaces),
                              usajobsKey.trimmingCharacters(in: .whitespaces))
        let bls = blsKey.trimmingCharacters(in: .whitespaces)
        model.saveConfig { config in
            config.search.keywords = k
            config.search.locations = l
            config.search.excludeKeywords = e
            config.search.greenhouseBoards = gh
            config.search.leverCompanies = lv
            config.apiKeys.adzunaAppID = aID
            config.apiKeys.adzunaAppKey = aKey
            config.apiKeys.usajobsEmail = uEmail
            config.apiKeys.usajobsAPIKey = uKey
            config.apiKeys.blsRegistrationKey = bls
        }
    }
}

/// AI endpoint + per-task model configuration.
///
/// Each task tier is assigned its own model — an endpoint model or Apple's
/// on-device model — so the user can see (and control) exactly what runs
/// where. On-device is just another option in each dropdown, not a separate
/// engine mode.
struct AISettingsView: View {
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
            SecureField("API key (blank for LM Studio)", text: $apiKey)
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
            } else {
                TextField(fallbackLabel ?? "Model name", text: selection)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
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
            Text("The most jobs a single “Score all” run will process — a hard ceiling on how many AI calls one tap can trigger. You can also Stop a run at any time.")
        }
    }
}
