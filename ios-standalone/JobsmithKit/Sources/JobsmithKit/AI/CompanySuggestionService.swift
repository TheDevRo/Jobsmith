import Foundation

/// One AI-recommended company with a short rationale.
public struct CompanySuggestion: Equatable, Sendable, Identifiable {
    public var name: String
    public var why: String
    public var id: String { name.lowercased() }

    public init(name: String, why: String) {
        self.name = name
        self.why = why
    }
}

/// An AI-suggested company paired with the live ATS boards found for it. Only
/// companies with at least one reachable board survive validation.
public struct SuggestedCompany: Equatable, Sendable, Identifiable {
    public var suggestion: CompanySuggestion
    public var boards: [BoardDetector.BoardMatch]
    public var id: String { suggestion.id }

    public init(suggestion: CompanySuggestion, boards: [BoardDetector.BoardMatch]) {
        self.suggestion = suggestion
        self.boards = boards
    }
}

/// Port of desktop `ai_engine.suggest_companies` + the `/sources/suggest-companies`
/// validation step: recommend companies to follow from the profile plus optional
/// direction preferences, then keep only those with a live board so a
/// hallucinated name costs nothing.
public enum CompanySuggestionService {

    /// Ask the AI for companies. Reuses `TitlePreference` for the "- Label: value"
    /// direction lines and the `suggest_companies` prompt's `{directions}` block.
    public static func suggest(profile: Profile, preferences: [TitlePreference], excluding: [String],
                               config: AppConfig, engine: AIEngine) async throws -> [CompanySuggestion] {
        let lines = preferences.compactMap { pref -> String? in
            let value = pref.value.trimmingCharacters(in: .whitespacesAndNewlines)
            return value.isEmpty ? nil : "- \(pref.label): \(value)"
        }
        let directions = lines.isEmpty ? "- (no preferences given)" : lines.joined(separator: "\n")
        let keywords = config.search.keywords.isEmpty ? "(none)" : config.search.keywords.joined(separator: ", ")
        let excluded = excluding.isEmpty ? "(none)" : excluding.joined(separator: ", ")

        let prompt = PromptRegistry.render("suggest_companies", [
            "directions": directions,
            "profile_summary": Directives.profileSummary(profile),
            "keywords": keywords,
            "liked": "(none yet)",
            "excluded": excluded,
        ], config: config)

        let request = CompletionRequest(user: prompt, tier: .strong,
                                        temperature: config.ai.temperature, maxTokens: 1500)
        let text = try await engine.complete(request, config: config.ai)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return parse(text, excluding: excluding)
    }

    /// Parse `{"companies":[{"name","why"}]}` with the same salvage + dedup as
    /// `TitleSuggestionService.parse`: direct JSON, then an embedded-object regex;
    /// bare strings allowed. Drops anything already in `excluding`.
    public static func parse(_ text: String, excluding: [String] = []) -> [CompanySuggestion] {
        var object = LenientJSON.decodeObject(text)
        if object == nil,
           let groups = Rx.first("\\{.*\\}", in: text, options: [.dotMatchesLineSeparators]),
           let raw = groups[0] {
            object = LenientJSON.decodeObject(raw)
        }
        guard let data = object, let items = data["companies"] as? [Any] else { return [] }

        var seen = Set(excluding.map { $0.lowercased() })
        var suggestions: [CompanySuggestion] = []
        for item in items {
            let name: String
            let why: String
            if let string = item as? String {
                name = string; why = ""
            } else if let dict = item as? [String: Any] {
                name = dict["name"] as? String ?? ""
                why = dict["why"] as? String ?? ""
            } else {
                continue
            }
            let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
            let key = trimmed.lowercased()
            guard !trimmed.isEmpty, !seen.contains(key) else { continue }
            seen.insert(key)
            suggestions.append(CompanySuggestion(
                name: trimmed, why: why.trimmingCharacters(in: .whitespacesAndNewlines)))
        }
        return suggestions
    }

    /// Validate suggestions against live board probes; keep only companies with a
    /// board that has open jobs. Bounded so a batch doesn't fan out unbounded.
    public static func validate(_ suggestions: [CompanySuggestion]) async -> [SuggestedCompany] {
        let limiter = AsyncLimiter(4)
        var validated: [SuggestedCompany] = []
        await withTaskGroup(of: SuggestedCompany?.self) { group in
            for suggestion in suggestions {
                group.addTask {
                    await limiter.acquire()
                    defer { Task { await limiter.release() } }
                    let boards = await BoardDetector.detectBoards(company: suggestion.name)
                        .filter { $0.jobs > 0 }
                    return boards.isEmpty ? nil : SuggestedCompany(suggestion: suggestion, boards: boards)
                }
            }
            for await result in group {
                if let result { validated.append(result) }
            }
        }
        // Preserve the AI's most-relevant-first ordering.
        let order = Dictionary(uniqueKeysWithValues: suggestions.enumerated().map { ($0.element.id, $0.offset) })
        return validated.sorted { (order[$0.id] ?? 0) < (order[$1.id] ?? 0) }
    }
}
