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
    /// The model declined this specific job (on-device guardrails, content too
    /// long, unsupported language). Deterministic — a retry gets the same
    /// answer — and job-specific, so a batch skips it and keeps going.
    case refused(String)

    public var errorDescription: String? {
        switch self {
        case .engineUnavailable(let detail):
            return "The AI endpoint could not be reached: \(detail)"
        case .interrupted(let detail):
            return "Scoring was interrupted: \(detail)"
        case .unparseableResponse(let raw):
            return "The AI response contained no score. Raw: \(raw)"
        case .refused(let detail):
            return detail
        }
    }
}

/// Port of `ai_engine.score_job_fit` including its full fallback chain:
/// JSON parse → embedded-object salvage → "score": N regex → any 0-100
/// number, with one retry at temperature 0.3 when the call fails. Unlike the
/// Python original it never invents a `0` — an unreachable engine or an
/// unsalvageable response throws `ScoringError`.
public enum ScoringService {
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
            // A decline is deterministic and about THIS job — no retry, and
            // the caller keeps the batch going without it.
            if let aiError = error as? AIEngineError, case .refused(let detail) = aiError {
                throw ScoringError.refused(detail)
            }
            // Retry once at low temperature (strict JSON parse only). The
            // retry normally escalates to the strong model — but never across
            // the on-device boundary: when the user routed scoring on-device,
            // escalating to .strong would silently send the job to the cloud
            // model instead, which is exactly what they opted out of.
            var retry = request
            retry.tier = config.ai.usesOnDevice(for: .fast) ? .fast : .strong
            retry.temperature = 0.3
            do {
                let retryText = try await engine.complete(retry, config: config.ai)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if let data = LenientJSON.decodeObject(retryText),
                   let score = LenientJSON.doubleValue(data["score"]) {
                    return FitResult(score: score,
                                     reasoning: data["reasoning"] as? String ?? "",
                                     matchReportJSON: ScoreResponseParser.sanitizedMatchReportJSON(data))
                }
            } catch let retryError where TransientNetwork.isTransient(retryError) {
                throw ScoringError.interrupted(String(describing: retryError))
            } catch {
                // Fall through — the retry failed for its own reasons, but the
                // original error is the one worth reporting.
            }
            throw ScoringError.engineUnavailable(String(describing: error))
        }

        // Full fallback chain lives in ScoreResponseParser — shared with the
        // desktop via the cross-language conformance fixtures. Do not inline
        // parsing steps here; add them to the parser (and to ai_engine.py).
        if let parsed = ScoreResponseParser.parse(text) {
            return FitResult(score: parsed.score,
                             reasoning: parsed.reasoning,
                             matchReportJSON: parsed.matchReportJSON)
        }
        throw ScoringError.unparseableResponse(String(text.prefix(200)))
    }

    /// Kept as thin aliases — the implementations moved to ScoreResponseParser
    /// so the cross-language host tool can compile them without this file's
    /// Job/Profile/AIEngine dependencies.
    static func sanitizeMatchReport(_ data: [String: Any]) -> [String: Any]? {
        ScoreResponseParser.sanitizeMatchReport(data)
    }

    static func sanitizedMatchReportJSON(_ data: [String: Any]) -> String? {
        ScoreResponseParser.sanitizedMatchReportJSON(data)
    }
}
