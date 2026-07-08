import SwiftUI
import JobsmithKit

/// "Help me pick" — AI-suggested job titles for the search keywords. Mirrors
/// the desktop "✨ Suggest titles with AI" flow: a few optional direction
/// questions, then a selectable list of titles to add.
struct TitleSuggestSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    /// Keywords already in the search box, so suggestions can flag duplicates.
    let existing: [String]
    /// Chosen titles to append to the keywords.
    let onAdd: ([String]) -> Void

    private static let directions = [
        "Same kind of role as my recent experience",
        "A step up in seniority",
        "A pivot into a different specialty",
        "A move into people management",
        "Open to anything",
    ]
    private static let seniorities = [
        "No preference", "Entry / junior", "Mid-level",
        "Senior", "Lead / staff", "Manager / director",
    ]

    @State private var direction = directions[0]
    @State private var focus = ""
    @State private var seniority = seniorities[0]
    @State private var avoid = ""

    @State private var loading = false
    @State private var suggestions: [TitleSuggestion] = []
    @State private var selected: Set<String> = []
    @State private var errorMessage: String?

    private var profileReady: Bool {
        let p = model.config.profile
        return !(p.summary.isEmpty && p.skills.isEmpty && p.experience.isEmpty)
    }

    var body: some View {
        NavigationStack {
            Form {
                if suggestions.isEmpty {
                    questions
                } else {
                    results
                }
            }
            .navigationTitle("Suggest titles")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                if !suggestions.isEmpty {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Add \(selected.count)") {
                            onAdd(Array(selected))
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
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        } else {
            Section {
                Picker("Next role", selection: $direction) {
                    ForEach(Self.directions, id: \.self) { Text($0) }
                }
                TextField("Skills to use (optional)", text: $focus, axis: .vertical)
                Picker("Seniority", selection: $seniority) {
                    ForEach(Self.seniorities, id: \.self) { Text($0) }
                }
                TextField("Anything to avoid? (optional)", text: $avoid, axis: .vertical)
            } header: {
                Eyebrow(text: "A few optional questions")
            } footer: {
                Text("Every field is optional — they steer the suggestions toward where you want to go.")
            }
            Section {
                Button {
                    Task { await run() }
                } label: {
                    if loading {
                        HStack(spacing: 8) { ProgressView(); Text("Thinking…") }
                    } else {
                        Label("Suggest titles", systemImage: "sparkles")
                    }
                }
                .disabled(loading)
            }
        }
        if let errorMessage {
            Section {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.red)
            }
        }
    }

    @ViewBuilder private var results: some View {
        Section {
            ForEach(suggestions) { row($0) }
        } header: {
            Eyebrow(text: "\(selected.count) of \(suggestions.count) selected")
        }
        Section {
            Button {
                withAnimation { suggestions = []; selected = []; errorMessage = nil }
            } label: {
                Label("Refine preferences", systemImage: "arrow.uturn.backward")
            }
        }
    }

    private func row(_ suggestion: TitleSuggestion) -> some View {
        let already = existing.contains { $0.caseInsensitiveCompare(suggestion.title) == .orderedSame }
        let isSelected = selected.contains(suggestion.title)
        return Button {
            if isSelected { selected.remove(suggestion.title) } else { selected.insert(suggestion.title) }
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(isSelected ? Theme.ember : .secondary)
                    .font(.title3)
                VStack(alignment: .leading, spacing: 2) {
                    Text(suggestion.title)
                        .font(.callout.weight(.medium))
                        .foregroundStyle(.primary)
                    if already {
                        Text("Already in your keywords")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    } else if !suggestion.reason.isEmpty {
                        Text(suggestion.reason)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .buttonStyle(.plain)
    }

    private func run() async {
        loading = true
        errorMessage = nil
        defer { loading = false }
        let prefs = [
            TitlePreference(label: "Direction", value: direction),
            TitlePreference(label: "Skills to use", value: focus),
            TitlePreference(label: "Seniority", value: seniority),
            TitlePreference(label: "Avoid", value: avoid),
        ]
        do {
            let result = try await TitleSuggestionService.suggest(
                profile: model.config.profile, preferences: prefs,
                config: model.config, engine: model.aiEngine)
            guard !result.isEmpty else {
                errorMessage = "The AI returned no usable titles — try again."
                return
            }
            withAnimation {
                suggestions = result
                // Preselect everything not already in the keyword list.
                selected = Set(result.map(\.title).filter { title in
                    !existing.contains { $0.caseInsensitiveCompare(title) == .orderedSame }
                })
            }
        } catch {
            errorMessage = "Couldn't get suggestions: \(error.localizedDescription)"
        }
    }
}
