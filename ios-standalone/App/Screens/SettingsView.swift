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
                        SearchScheduleView()
                    } label: {
                        row("Background search", system: "clock.arrow.2.circlepath",
                            detail: BackgroundScheduler.isEnabled() ? "On" : "Off")
                    }
                    NavigationLink {
                        AIConnectionSettingsView()
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
                    Text("Résumés and cover letters are generated in your chosen format and share one visual style, so a recruiter opening both sees a matched set. Every style stays single-column real text, so all of them parse cleanly through an ATS. Limiting work history keeps only the roles most relevant to each job (pinned roles are always included). Honesty controls how much latitude the AI takes when tailoring — from reorder-only to invented experience. Fabricated is at your own risk.")
                }

                Section {
                    NavigationLink {
                        SyncSettingsView()
                    } label: {
                        row("Sync", system: "arrow.triangle.2.circlepath",
                            detail: SyncManager.shared.isEnabled() ? "On" : "Off")
                    }
                } header: {
                    Eyebrow(text: "Sync")
                } footer: {
                    Text("Sync jobs, applications, and your profile across devices through a shared folder. Serverless — no account.")
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

    /// Style and accent used to be two menu rows that showed the user nothing.
    /// They're one row now, opening a picker with the sample resume on it; the
    /// row carries the resolved accent so the current choice is readable here.
    private var stylePicker: some View {
        let style = model.config.honesty.resumeStyle
        let accent = model.config.honesty.resumeAccent
        return NavigationLink {
            ResumeStyleView()
        } label: {
            HStack {
                Label("Resume style", systemImage: "doc.richtext")
                Spacer()
                if !style.isMonochrome, let hex = accent.hex {
                    Circle()
                        .fill(Color(hex6: hex))
                        .frame(width: 11, height: 11)
                        .overlay(Circle().strokeBorder(Color.black.opacity(0.15)))
                }
                Text(style.label)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
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
        // Spoken as "AI connection, http://…" rather than as two elements.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(detail.map { "\(title), \($0)" } ?? title)
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
            LinkedInSettingsSection()
            // All the credentialed sources' keys, in one place (UX-05).
            SourceKeysSettingsView(adzunaAppID: $adzunaAppID,
                                   adzunaAppKey: $adzunaAppKey,
                                   usajobsEmail: $usajobsEmail,
                                   usajobsKey: $usajobsKey,
                                   blsKey: $blsKey)
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

    /// LinkedIn is deliberately not here — it gets its own section, because its
    /// switch also decides whether the source exists at all (`LinkedInFeature`)
    /// and because that's where the account connection belongs.
    private var sourceList: [(String, String)] {
        [("remoteok", "RemoteOK"), ("weworkremotely", "WeWorkRemotely"),
         ("arbeitnow", "Arbeitnow"), ("greenhouse", "Greenhouse & Lever boards"),
         ("ashby", "Ashby boards"), ("workable", "Workable"),
         ("recruitee", "Recruitee"), ("adzuna", "Adzuna (API key)"),
         ("usajobs", "USAJobs (API key)")]
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

/// LinkedIn's own section: the switch that decides whether the source exists
/// for this user at all, plus the account connection that makes it work
/// properly. A build compiled without the feature (`JOBSMITH_NO_LINKEDIN`)
/// renders nothing here.
struct LinkedInSettingsSection: View {
    @Environment(AppModel.self) private var model
    @State private var showSignIn = false

    private var isOn: Bool { LinkedInFeature.isEnabled(model.config) }
    private var isConnected: Bool { LinkedInFeature.isAuthenticated(model.config) }

    var body: some View {
        if LinkedInFeature.isBuildEnabled {
            Section {
                Toggle("LinkedIn", isOn: Binding(
                    get: { isOn },
                    set: { on in
                        model.saveConfig { config in
                            config.search.linkedInEnabled = on
                            if on { config.search.enabledSources.insert(LinkedInSource.id) }
                            else { config.search.enabledSources.remove(LinkedInSource.id) }
                        }
                    }
                ))
                if isOn {
                    if isConnected {
                        Label("Account connected", systemImage: "checkmark.seal.fill")
                            .foregroundStyle(.secondary)
                        Button("Disconnect account", role: .destructive) {
                            model.saveConfig { $0.apiKeys.linkedInCookie = "" }
                        }
                    } else {
                        Button {
                            showSignIn = true
                        } label: {
                            Label("Connect your account for best results",
                                  systemImage: "person.crop.circle.badge.checkmark")
                        }
                    }
                }
            } header: {
                Eyebrow(text: "LinkedIn")
            } footer: {
                Text(isConnected
                     ? "Jobsmith searches LinkedIn as you. Your session stays in this device's Keychain and is never sent anywhere else — sign out here to remove it."
                     : "Signed in, Jobsmith searches LinkedIn as you — more results, far fewer rate limits. Without an account it falls back to LinkedIn's public pages. Turn LinkedIn off and Jobsmith never contacts it at all.")
            }
            .sheet(isPresented: $showSignIn) {
                LinkedInSignInSheet { _, cookie in
                    if let cookie, !cookie.isEmpty {
                        model.saveConfig { $0.apiKeys.linkedInCookie = cookie }
                    }
                }
            }
        }
    }
}
