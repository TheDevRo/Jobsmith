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

/// Why a job could not be scored. Distinct from a low score: callers must not
/// persist a fit score when one of these is thrown, or a dead endpoint would
/// permanently brand every unscored job as a `0` (indistinguishable from a real
/// bad fit).
public enum ScoringError: Error, LocalizedError {
    /// Both the initial call and the low-temperature retry failed.
    case engineUnavailable(String)
    /// The call was cut off — the app was suspended mid-request, the task was
    /// cancelled, the endpoint dropped off the network. Nothing is wrong with the
    /// job or the model, so a batch that hits this *pauses* and resumes later
    /// rather than reporting a failure and giving up on the remaining jobs.
    case interrupted(String)
    /// The model answered, but no score could be salvaged from its output.
    case unparseableResponse(String)

    public var errorDescription: String? {
        switch self {
        case .engineUnavailable(let detail):
            return "The AI endpoint could not be reached: \(detail)"
        case .interrupted(let detail):
            return "Scoring was interrupted: \(detail)"
        case .unparseableResponse(let raw):
            return "The AI response contained no score. Raw: \(raw)"
        }
    }
}

/// Port of `ai_engine.score_job_fit` including its full fallback chain:
/// JSON parse → embedded-object salvage → "score": N regex → any 0-100
/// number, with one retry at temperature 0.3 when the call fails. Unlike the
/// Python original it never invents a `0` — an unreachable engine or an
/// unsalvageable response throws `ScoringError`.
public enum ScoringService {
    static let titleAlignments: Set<String> = ["strong", "partial", "weak"]

    public static func score(job: Job, profile: Profile, config: AppConfig,
                             engine: AIEngine) async throws -> FitResult {
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
            // A cut-off call gets no retry: the app is being suspended or the
            // endpoint has gone out of reach, and a second request would die the
            // same way. Surface it as `interrupted` so the batch pauses here and
            // picks this job up again later, instead of treating it as a dead
            // endpoint and abandoning every job behind it.
            if TransientNetwork.isTransient(error) {
                throw ScoringError.interrupted(String(describing: error))
            }
            // Retry once at low temperature (strict JSON parse only).
            var retry = request
            retry.tier = .strong
            retry.temperature = 0.3
            do {
                let retryText = try await engine.complete(retry, config: config.ai)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if let data = LenientJSON.decodeObject(retryText),
                   let score = LenientJSON.doubleValue(data["score"]) {
                    return FitResult(score: score,
                                     reasoning: data["reasoning"] as? String ?? "",
                                     matchReportJSON: sanitizedMatchReportJSON(data))
                }
            } catch let retryError where TransientNetwork.isTransient(retryError) {
                throw ScoringError.interrupted(String(describing: retryError))
            } catch {
                // Fall through — the retry failed for its own reasons, but the
                // original error is the one worth reporting.
            }
            throw ScoringError.engineUnavailable(String(describing: error))
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
        throw ScoringError.unparseableResponse(String(text.prefix(200)))
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
