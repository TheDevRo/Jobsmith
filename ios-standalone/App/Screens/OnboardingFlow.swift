import SwiftUI
import UniformTypeIdentifiers
import JobsmithKit

/// First-run setup: connect the AI first (profile extraction needs it),
/// then import a resume or LinkedIn profile, review, pick sources. Every
/// step is skippable — the app works progressively.
struct OnboardingFlow: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    enum Step { case welcome, ai, resume, profile, companies, sources }
    @State private var step: Step = .welcome

    var body: some View {
        NavigationStack {
            Group {
                switch step {
                case .welcome: welcome
                case .ai: aiStep
                case .resume: ResumeImportStep(onDone: { step = .profile })
                case .profile: profileStep
                case .companies: companiesStep
                case .sources: sourcesStep
                }
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if step != .welcome {
                        Button {
                            back()
                        } label: {
                            Label("Back", systemImage: "chevron.backward")
                        }
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button(step == .sources ? "Done" : "Skip") { advance() }
                }
            }
        }
    }

    private func back() {
        switch step {
        case .welcome: break
        case .ai: step = .welcome
        case .resume: step = .ai
        case .profile: step = .resume
        case .companies: step = .profile
        case .sources: step = .companies
        }
    }

    private func advance() {
        switch step {
        case .welcome: step = .ai
        case .ai: step = .resume
        case .resume: step = .profile
        case .profile: step = .companies
        case .companies: step = .sources
        case .sources: dismiss()
        }
    }

    private var welcome: some View {
        VStack(spacing: 20) {
            Spacer()
            Image(systemName: "hammer.fill")
                .font(.system(size: 52))
                .foregroundStyle(Theme.ember)
            Text("Jobsmith")
                .font(.largeTitle.weight(.bold))
            Text("Fetch jobs from a dozen boards, score them against your resume with your own AI, forge tailored documents, and apply — all from your phone, no server required.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 28)
            Spacer()
            Button {
                advance()
            } label: {
                Text("Set up")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 6)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.ember)
            .padding(.horizontal, 24)
            .padding(.bottom, 24)
        }
    }

    private var profileStep: some View {
        VStack(spacing: 0) {
            stepHeader("Review your profile",
                       "Check what the import found — this is the only source of truth the AI writes from.")
            ProfileEditorView()
            Button {
                advance()
            } label: {
                Text("Looks right")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.ember)
            .padding(16)
        }
    }

    private var aiStep: some View {
        VStack(spacing: 0) {
            stepHeader("Connect your AI",
                       "LM Studio on your network, any OpenAI-compatible provider, or Apple's on-device model. This powers the profile import on the next step.")
            AISettingsView()
            Button {
                advance()
            } label: {
                Text("Continue")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.ember)
            .padding(16)
        }
    }

    private var companiesStep: some View {
        VStack(spacing: 0) {
            stepHeader("Who do you want to work for?",
                       "Follow specific companies and Jobsmith pulls their latest openings — just type a name, no board slugs to hunt down. Optional, and you can change it later.")
            Form { CompanyFollowControls() }
            Button {
                advance()
            } label: {
                Text("Continue")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.ember)
            .padding(16)
        }
    }

    private var sourcesStep: some View {
        VStack(spacing: 0) {
            stepHeader("Where should jobs come from?",
                       "Keywords, locations, and boards. You can change all of this later in Settings.")
            SearchSettingsView()
            Button {
                dismiss()
            } label: {
                Text("Start scouting")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.ember)
            .padding(16)
        }
    }

    private func stepHeader(_ title: String, _ subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.title3.weight(.semibold))
            Text(subtitle).font(.callout).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }
}

/// Import a resume file (PDF/DOCX/TXT), pasted resume text, or pasted
/// LinkedIn profile text; the AI extracts a profile for review. Skippable —
/// manual entry always works.
struct ResumeImportStep: View {
    @Environment(AppModel.self) private var model
    let onDone: () -> Void

    enum ImportKind: String, CaseIterable {
        case resume = "Resume"
        case linkedin = "LinkedIn profile"
    }

    @State private var kind: ImportKind = .resume
    @State private var showFilePicker = false
    @State private var showLinkedInSignIn = false
    @State private var pastedText = ""
    @State private var profileLink = ""
    @State private var parsing = false
    @State private var warnings: [String] = []
    @State private var errorMessage: String?

    var body: some View {
        VStack(spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Import your profile")
                    .font(.title3.weight(.semibold))
                Text(kind == .resume
                     ? "The AI reads your resume and fills in your profile — nothing is fabricated, and the file never leaves your devices."
                     : "Sign in once and your profile is read automatically, or paste your public profile link.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 20)
            .padding(.top, 12)

            Picker("Source", selection: $kind) {
                ForEach(ImportKind.allCases, id: \.self) { kind in
                    Text(kind.rawValue).tag(kind)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 20)
            .disabled(parsing)

            if kind == .resume {
                Button {
                    showFilePicker = true
                } label: {
                    Label("Choose a file (PDF, DOCX, TXT)", systemImage: "doc.badge.arrow.up")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                }
                .buttonStyle(.bordered)
                .padding(.horizontal, 20)
                .disabled(parsing)

                HStack {
                    VStack { Divider() }
                    Text("or paste").font(.caption).foregroundStyle(.secondary)
                    VStack { Divider() }
                }
                .padding(.horizontal, 20)

                TextEditor(text: $pastedText)
                    .font(.footnote)
                    .frame(minHeight: 140)
                    .padding(8)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color.primary.opacity(0.04)))
                    .padding(.horizontal, 20)
            } else {
                Button {
                    showLinkedInSignIn = true
                } label: {
                    Label("Sign in with LinkedIn", systemImage: "person.crop.circle.badge.checkmark")
                        .fontWeight(.semibold)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.ember)
                .padding(.horizontal, 20)
                .disabled(parsing)

                HStack {
                    VStack { Divider() }
                    Text("or use your profile link").font(.caption).foregroundStyle(.secondary)
                    VStack { Divider() }
                }
                .padding(.horizontal, 20)

                TextField("linkedin.com/in/yourname", text: $profileLink)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .padding(12)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color.primary.opacity(0.04)))
                    .padding(.horizontal, 20)
                    .disabled(parsing)
            }

            if let errorMessage {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .padding(.horizontal, 20)
            }
            ForEach(warnings, id: \.self) { warning in
                Label(warning, systemImage: "info.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 20)
            }

            if kind == .resume {
                Button {
                    Task { await parse(text: pastedText) }
                } label: {
                    actionLabel("Extract profile", busy: "Reading resume…")
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.ember)
                .padding(.horizontal, 20)
                .disabled(parsing || currentInputEmpty)
            } else {
                Button {
                    Task { await importFromLink() }
                } label: {
                    actionLabel("Import from link", busy: "Reading profile…")
                }
                .buttonStyle(.bordered)
                .padding(.horizontal, 20)
                .disabled(parsing || currentInputEmpty)
            }

            Spacer()
        }
        .fileImporter(isPresented: $showFilePicker,
                      allowedContentTypes: [.pdf, .plainText,
                                            UTType("org.openxmlformats.wordprocessingml.document") ?? .data]) { result in
            guard case .success(let url) = result else { return }
            Task { await importFile(url) }
        }
        .sheet(isPresented: $showLinkedInSignIn) {
            LinkedInSignInSheet { text, cookie in
                if let cookie, !cookie.isEmpty {
                    model.saveConfig { $0.apiKeys.linkedInCookie = cookie }
                }
                Task { await parse(text: text) }
            }
        }
    }

    @ViewBuilder
    private func actionLabel(_ idle: String, busy: String) -> some View {
        if parsing {
            HStack(spacing: 8) { ProgressView(); Text(busy) }
                .frame(maxWidth: .infinity)
        } else {
            Text(idle)
                .fontWeight(.semibold)
                .frame(maxWidth: .infinity)
        }
    }

    private var currentInputEmpty: Bool {
        kind == .resume
            ? pastedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            : profileLink.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private func importFromLink() async {
        parsing = true
        defer { parsing = false }
        errorMessage = nil
        do {
            let text = try await LinkedInProfileFetcher.fetchPublicProfile(profileLink)
            await parse(text: text)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func importFile(_ url: URL) async {
        parsing = true
        defer { parsing = false }
        errorMessage = nil
        do {
            let secured = url.startAccessingSecurityScopedResource()
            defer { if secured { url.stopAccessingSecurityScopedResource() } }
            let data = try Data(contentsOf: url)
            let text = try ResumeTextExtractor.extract(filename: url.lastPathComponent, data: data)
            await parse(text: text)
        } catch {
            errorMessage = "Couldn't read that file: \(error.localizedDescription)"
        }
    }

    private func parse(text: String) async {
        parsing = true
        defer { parsing = false }
        errorMessage = nil
        let result = await ResumeProfileParser.parse(
            text: text, config: model.config, engine: model.aiEngine,
            promptKey: kind == .linkedin ? "linkedin_import" : "resume_parse")
        warnings = result.warnings
        if result.profile.isEmpty {
            errorMessage = "The AI couldn't extract a profile. Is your AI connected (previous step)? You can also skip and fill the profile manually."
            return
        }
        // Await the write: onDone() moves straight to the profile review,
        // which reads the config on appear.
        await model.saveConfigNow { $0.profile = result.profile }
        onDone()
    }
}
