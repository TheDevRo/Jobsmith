import Foundation

/// One AI-recommended job title with a short rationale.
public struct TitleSuggestion: Equatable, Sendable, Identifiable {
    public var title: String
    public var reason: String
    public var id: String { title.lowercased() }

    public init(title: String, reason: String) {
        self.title = title
        self.reason = reason
    }
}

/// A single answer to a direction question, shaped for the prompt's
/// "- Label: value" preference lines. Blank values are dropped.
public struct TitlePreference: Equatable, Sendable {
    public var label: String
    public var value: String
    public init(label: String, value: String) {
        self.label = label
        self.value = value
    }
}

/// Port of desktop `ai_engine.suggest_job_titles`: recommend job-board search
/// titles from the profile plus the user's optional direction answers.
public enum TitleSuggestionService {
    public static func suggest(profile: Profile, preferences: [TitlePreference],
                               config: AppConfig, engine: AIEngine) async throws -> [TitleSuggestion] {
        let lines = preferences.compactMap { pref -> String? in
            let value = pref.value.trimmingCharacters(in: .whitespacesAndNewlines)
            return value.isEmpty ? nil : "- \(pref.label): \(value)"
        }
        let answerLines = lines.isEmpty ? "- (no preferences given)" : lines.joined(separator: "\n")

        let prompt = PromptRegistry.render("suggest_job_titles", [
            "answer_lines": answerLines,
            "profile_summary": Directives.profileSummary(profile),
        ], config: config)

        // Strong tier: title suggestions want the best model. Falls back to the
        // configured strong/on-device model per the router.
        let request = CompletionRequest(user: prompt, tier: .strong,
                                        temperature: config.ai.temperature, maxTokens: 1500)
        let text = try await engine.complete(request, config: config.ai)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return parse(text)
    }

    /// Parse `{"titles":[{"title","reason"}]}` with the same salvage + dedup as
    /// desktop: direct JSON, then an embedded-object regex; bare strings allowed.
    public static func parse(_ text: String) -> [TitleSuggestion] {
        var object = LenientJSON.decodeObject(text)
        if object == nil,
           let groups = Rx.first("\\{.*\\}", in: text, options: [.dotMatchesLineSeparators]),
           let raw = groups[0] {
            object = LenientJSON.decodeObject(raw)
        }
        guard let data = object, let items = data["titles"] as? [Any] else { return [] }

        var suggestions: [TitleSuggestion] = []
        var seen = Set<String>()
        for item in items {
            let title: String
            let reason: String
            if let string = item as? String {
                title = string; reason = ""
            } else if let dict = item as? [String: Any] {
                title = dict["title"] as? String ?? ""
                reason = dict["reason"] as? String ?? ""
            } else {
                continue
            }
            let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
            let key = trimmed.lowercased()
            guard !trimmed.isEmpty, !seen.contains(key) else { continue }
            seen.insert(key)
            suggestions.append(TitleSuggestion(
                title: trimmed,
                reason: reason.trimmingCharacters(in: .whitespacesAndNewlines)))
        }
        return suggestions
    }
}
