import SwiftUI
import JobsmithKit

/// Form sections for following companies by *name* instead of ATS slug: a board
/// finder (type a name → probe its live board) and an AI company suggester, plus
/// the list of companies currently watched. Drops into both the Search & sources
/// settings screen and the onboarding "Who do you want to work for?" step.
struct CompanyFollowControls: View {
    @Environment(AppModel.self) private var model
    @State private var activeSheet: ActiveSheet?

    private enum ActiveSheet: Int, Identifiable {
        case finder, suggest
        var id: Int { rawValue }
    }

    var body: some View {
        Group {
            Section {
                Button {
                    activeSheet = .finder
                } label: {
                    Label("Find a company's board", systemImage: "magnifyingglass")
                }
                Button {
                    activeSheet = .suggest
                } label: {
                    Label("Suggest companies to follow", systemImage: "sparkles")
                }
                // Anchor the sheet to a leaf Button — `.sheet` on a Form Section
                // silently no-ops. One .sheet(item:); two isPresented sheets on
                // the same view would clobber each other.
                .sheet(item: $activeSheet) { sheet in
                    switch sheet {
                    case .finder:
                        BoardFinderSheet { matches in addBoards(matches) }
                            .environment(model)
                    case .suggest:
                        CompanySuggestSheet { matches in addBoards(matches) }
                            .environment(model)
                    }
                }
            } header: {
                Eyebrow(text: "Companies to follow")
            } footer: {
                Text("Type a company name — we find its live job board (Greenhouse, Lever, Ashby, Workable, Recruitee) so you never need to know a slug.")
            }

            if !watched.isEmpty {
                Section {
                    ForEach(watched) { entry in
                        HStack {
                            VStack(alignment: .leading, spacing: 1) {
                                Text(entry.slug).font(.callout)
                                Text(entry.ats.label).font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button(role: .destructive) {
                                remove(entry.ats, entry.slug)
                            } label: {
                                Image(systemName: "minus.circle.fill").foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                } header: {
                    Eyebrow(text: "Watching \(watched.count)")
                }
            }
        }
    }

    // MARK: - Watched list

    private struct WatchEntry: Identifiable {
        let ats: BoardDetector.ATS
        let slug: String
        var id: String { "\(ats.rawValue):\(slug)" }
    }

    private var watched: [WatchEntry] {
        BoardDetector.ATS.allCases.flatMap { ats in
            model.config.search[keyPath: ats.keyPath]
                .filter { $0 != "example-company" }
                .map { WatchEntry(ats: ats, slug: $0) }
        }
    }

    /// Merge discovered boards into the right slug arrays (dedup) and enable the
    /// sources that fetch them.
    private func addBoards(_ matches: [BoardDetector.BoardMatch]) {
        guard !matches.isEmpty else { return }
        model.saveConfig { config in
            for match in matches {
                if !config.search[keyPath: match.ats.keyPath].contains(match.slug) {
                    config.search[keyPath: match.ats.keyPath].append(match.slug)
                }
                config.search.enabledSources.insert(match.ats.enabledSourceID)
            }
        }
    }

    private func remove(_ ats: BoardDetector.ATS, _ slug: String) {
        model.saveConfig { $0.search[keyPath: ats.keyPath].removeAll { $0 == slug } }
    }
}

/// Type a company name → probe every ATS → tap the live boards to follow.
struct BoardFinderSheet: View {
    @Environment(\.dismiss) private var dismiss
    let onAdd: ([BoardDetector.BoardMatch]) -> Void

    @State private var company = ""
    @State private var loading = false
    @State private var matches: [BoardDetector.BoardMatch] = []
    @State private var searched = false
    @State private var added: Set<String> = []
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Company name (e.g. Stripe)", text: $company)
                        .textInputAutocapitalization(.words)
                        .autocorrectionDisabled()
                        .onSubmit { Task { await search() } }
                    Button {
                        Task { await search() }
                    } label: {
                        if loading {
                            HStack(spacing: 8) { ProgressView(); Text("Searching…") }
                        } else {
                            Label("Find boards", systemImage: "magnifyingglass")
                        }
                    }
                    .disabled(loading || company.trimmingCharacters(in: .whitespaces).isEmpty)
                } footer: {
                    Text("We probe each ATS's public job board for this company — no slug needed.")
                }

                if let errorMessage {
                    Section {
                        Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                            .font(.callout).foregroundStyle(.red)
                    }
                }

                if !matches.isEmpty {
                    Section {
                        ForEach(matches) { match in row(match) }
                    } header: {
                        Eyebrow(text: "Live boards")
                    }
                } else if searched && !loading {
                    Section {
                        Label("No public board found for that name. Try the company's shorter brand name, or add a slug manually below.",
                              systemImage: "questionmark.circle")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Find a company")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    private func row(_ match: BoardDetector.BoardMatch) -> some View {
        let isAdded = added.contains(match.id)
        return Button {
            onAdd([match])
            added.insert(match.id)
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(match.companyName ?? match.slug)
                        .font(.callout.weight(.medium)).foregroundStyle(.primary)
                    Text("\(match.ats.label) · \(match.jobs) open \(match.jobs == 1 ? "role" : "roles")")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: isAdded ? "checkmark.circle.fill" : "plus.circle")
                    .foregroundStyle(isAdded ? Theme.ember : .secondary)
                    .font(.title3)
            }
        }
        .buttonStyle(.plain)
        .disabled(isAdded)
    }

    private func search() async {
        let name = company.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        loading = true
        errorMessage = nil
        defer { loading = false; searched = true }
        let found = await BoardDetector.detectBoards(company: name)
        withAnimation { matches = found }
    }
}

/// "Who do you want to work for?" — AI company suggestions steered by direction
/// chips, then validated against live boards so only real companies survive.
struct CompanySuggestSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let onAdd: ([BoardDetector.BoardMatch]) -> Void

    private static let directions = [
        "Fortune 500", "High-growth startups", "Remote-first",
        "Big Tech / FAANG-adjacent", "Well-funded scale-ups",
        "Mission-driven / nonprofit", "Agencies & consultancies",
    ]

    @State private var picked: Set<String> = []
    @State private var loading = false
    @State private var results: [SuggestedCompany] = []
    @State private var selected: Set<String> = []
    @State private var searched = false
    @State private var errorMessage: String?

    private var profileReady: Bool {
        let p = model.config.profile
        return !(p.summary.isEmpty && p.skills.isEmpty && p.experience.isEmpty)
    }

    var body: some View {
        NavigationStack {
            Form {
                if results.isEmpty {
                    questions
                } else {
                    resultsList
                }
            }
            .navigationTitle("Suggest companies")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                if !results.isEmpty {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Add \(selectedBoardCount)") {
                            onAdd(selectedBoards)
                            dismiss()
                        }
                        .disabled(selected.isEmpty)
                    }
                }
            }
        }
    }

    @ViewBuilder private var questions: some View {
        if !profileReady {
            Section {
                Label("Add a summary, skills, or experience to your profile first — suggestions are based on it.",
                      systemImage: "person.crop.circle.badge.exclamationmark")
                    .font(.callout).foregroundStyle(.secondary)
            }
        } else {
            Section {
                ForEach(Self.directions, id: \.self) { chip in
                    Button {
                        if picked.contains(chip) { picked.remove(chip) } else { picked.insert(chip) }
                    } label: {
                        HStack {
                            Text(chip).foregroundStyle(.primary)
                            Spacer()
                            if picked.contains(chip) {
                                Image(systemName: "checkmark").foregroundStyle(Theme.ember)
                            }
                        }
                    }
                    .buttonStyle(.plain)
                }
            } header: {
                Eyebrow(text: "Who do you want to work for?")
            } footer: {
                Text("Optional — pick any that fit. Every suggestion is checked against a live job board before it's shown.")
            }
            Section {
                Button {
                    Task { await run() }
                } label: {
                    if loading {
                        HStack(spacing: 8) { ProgressView(); Text("Finding companies…") }
                    } else {
                        Label("Suggest companies", systemImage: "sparkles")
                    }
                }
                .disabled(loading)
            }
        }
        if let errorMessage {
            Section {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.red)
            }
        }
    }

    @ViewBuilder private var resultsList: some View {
        Section {
            ForEach(results) { row($0) }
        } header: {
            Eyebrow(text: "\(selected.count) of \(results.count) selected")
        }
        Section {
            Button {
                withAnimation { results = []; selected = []; errorMessage = nil; searched = false }
            } label: {
                Label("Refine", systemImage: "arrow.uturn.backward")
            }
        }
    }

    private func row(_ company: SuggestedCompany) -> some View {
        let isSelected = selected.contains(company.id)
        let boardSummary = company.boards
            .map { "\($0.ats.label) · \($0.jobs)" }
            .joined(separator: "   ")
        return Button {
            if isSelected { selected.remove(company.id) } else { selected.insert(company.id) }
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(isSelected ? Theme.ember : .secondary)
                    .font(.title3)
                VStack(alignment: .leading, spacing: 2) {
                    Text(company.suggestion.name)
                        .font(.callout.weight(.medium)).foregroundStyle(.primary)
                    if !company.suggestion.why.isEmpty {
                        Text(company.suggestion.why).font(.caption).foregroundStyle(.secondary)
                    }
                    Text(boardSummary).font(.caption2).foregroundStyle(.tertiary)
                }
            }
        }
        .buttonStyle(.plain)
    }

    private var selectedBoards: [BoardDetector.BoardMatch] {
        results.filter { selected.contains($0.id) }.flatMap(\.boards)
    }
    private var selectedBoardCount: Int { selectedBoards.count }

    private func run() async {
        loading = true
        errorMessage = nil
        defer { loading = false; searched = true }
        let prefs = picked.isEmpty ? [] : [TitlePreference(label: "Target companies", value: picked.sorted().joined(separator: ", "))]
        let excluding = BoardDetector.ATS.allCases.flatMap { model.config.search[keyPath: $0.keyPath] }
        do {
            let suggestions = try await CompanySuggestionService.suggest(
                profile: model.config.profile, preferences: prefs, excluding: excluding,
                config: model.config, engine: model.aiEngine)
            guard !suggestions.isEmpty else {
                errorMessage = "The AI returned no usable companies — try again."
                return
            }
            let validated = await CompanySuggestionService.validate(suggestions)
            guard !validated.isEmpty else {
                errorMessage = "None of the suggestions had a reachable job board right now. Try again or use the board finder."
                return
            }
            withAnimation {
                results = validated
                selected = Set(validated.map(\.id))
            }
        } catch {
            errorMessage = "Couldn't get suggestions: \(error.localizedDescription)"
        }
    }
}
