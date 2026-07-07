import Foundation

/// Result of scoring one job against the profile.
public struct FitResult: Equatable, Sendable {
    public var score: Double
    public var reasoning: String
    /// Structured skill/keyword gap breakdown as JSON, or nil when the model
    /// output couldn't be parsed beyond a bare score.
    public var matchReportJSON: String?

    public init(score: Double, reasoning: String, matchReportJSON: String? = nil) {
        self.score = score; self.reasoning = reasoning
        self.matchReportJSON = matchReportJSON
    }
}

/// Port of `ai_engine.score_job_fit` including its full fallback chain:
/// JSON parse → embedded-object salvage → "score": N regex → any 0-100
/// number → 0.0, with one retry at temperature 0.3 when the call fails.
public enum ScoringService {
    static let titleAlignments: Set<String> = ["strong", "partial", "weak"]

    public static func score(job: Job, profile: Profile, config: AppConfig,
                             engine: AIEngine) async -> FitResult {
        let prompt = PromptRegistry.render("score_job_fit", [
            "job_title": job.title,
            "job_company": job.company,
            "job_description": String(job.description.prefix(3000)),
            "profile_summary": Directives.profileSummary(profile),
        ], config: config)

        // Scoring is a classify-and-rate task, not document generation: it
        // rides the `fast` tier (which falls back to the strong model when no
        // dedicated fast model is set). This keeps the Settings label honest —
        // "Scoring & form-fill" lives on the Fast tier — and lets a user route
        // scoring on-device by assigning the fast tier to the on-device model.
        let request = CompletionRequest(user: prompt, tier: .fast,
                                        temperature: config.ai.temperature, maxTokens: 1200)
        let text: String
        do {
            text = try await engine.complete(request, config: config.ai)
                .trimmingCharacters(in: .whitespacesAndNewlines)
        } catch {
            // Retry once at low temperature (strict JSON parse only).
            var retry = request
            retry.tier = .strong
            retry.temperature = 0.3
            if let retryText = try? await engine.complete(retry, config: config.ai)
                .trimmingCharacters(in: .whitespacesAndNewlines),
               let data = LenientJSON.decodeObject(retryText),
               let score = LenientJSON.doubleValue(data["score"]) {
                return FitResult(score: score,
                                 reasoning: data["reasoning"] as? String ?? "",
                                 matchReportJSON: sanitizedMatchReportJSON(data))
            }
            return FitResult(score: 0.0, reasoning: "AI error: \(String(describing: error))")
        }

        // Try parsing as JSON first
        if let data = LenientJSON.decodeObject(text),
           let score = LenientJSON.doubleValue(data["score"]) {
            return FitResult(score: score,
                             reasoning: data["reasoning"] as? String ?? "",
                             matchReportJSON: sanitizedMatchReportJSON(data))
        }
        // Salvage attempt — models often wrap JSON in prose or code fences
        if let groups = Rx.first("\\{.*\\}", in: text, options: [.dotMatchesLineSeparators]),
           let raw = groups[0],
           let data = LenientJSON.decodeObject(raw),
           let score = LenientJSON.doubleValue(data["score"]) {
            return FitResult(score: score,
                             reasoning: data["reasoning"] as? String ?? "",
                             matchReportJSON: sanitizedMatchReportJSON(data))
        }
        // Regex fallback 1 — look for a number after "score"
        if let groups = Rx.first("\"score\"\\s*:\\s*(\\d+)", in: text),
           let digits = groups[1], let score = Double(digits) {
            let reasoning = Rx.first("\"reasoning\"\\s*:\\s*\"([^\"]+)\"", in: text)?[1] ?? text
            return FitResult(score: score, reasoning: reasoning)
        }
        // Regex fallback 2 — scan for any integer 0-100 in the text
        if let score = LenientJSON.firstNumber(in: text) {
            return FitResult(score: score,
                             reasoning: "(Score parsed from raw response) \(String(text.prefix(300)))")
        }
        return FitResult(score: 0.0,
                         reasoning: "ERROR: Could not parse score from LLM response. Raw: \(String(text.prefix(200)))")
    }

    /// Coerce the LLM's structured match output into a clean report; nil when
    /// nothing usable survives (callers treat that as score/reasoning only).
    static func sanitizeMatchReport(_ data: [String: Any]) -> [String: Any]? {
        func strList(_ key: String, _ cap: Int) -> [String] {
            guard let raw = data[key] as? [Any] else { return [] }
            var out: [String] = []
            for item in raw {
                if let s = item as? String {
                    let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { out.append(String(trimmed.prefix(80))) }
                }
                if out.count >= cap { break }
            }
            return out
        }

        var report: [String: Any] = [
            "matched_skills": strList("matched_skills", 12),
            "missing_skills": strList("missing_skills", 12),
            "matched_soft_skills": strList("matched_soft_skills", 8),
            "missing_soft_skills": strList("missing_soft_skills", 8),
            "keywords": strList("keywords", 15),
        ]
        if let alignment = data["title_alignment"] as? String, titleAlignments.contains(alignment) {
            report["title_alignment"] = alignment
        } else {
            report["title_alignment"] = NSNull()
        }

        let usable = ["matched_skills", "missing_skills", "keywords"]
            .contains { !(report[$0] as? [String] ?? []).isEmpty }
        return usable ? report : nil
    }

    static func sanitizedMatchReportJSON(_ data: [String: Any]) -> String? {
        guard let report = sanitizeMatchReport(data),
              let json = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]) else {
            return nil
        }
        return String(data: json, encoding: .utf8)
    }
}
