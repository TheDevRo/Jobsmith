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

/// Which engine produced a fit score, shown on the job so the local match model,
/// Apple Intelligence and an endpoint model are never confused. Stored as
/// `scored_by` inside the match report JSON (synced as-is; the desktop ignores it).
public enum ScoreSource: Equatable, Sendable {
    case quickMatch, localModel, appleIntelligence, endpoint(String)

    var tag: String {
        switch self {
        case .quickMatch: return "triage"
        case .localModel: return "local_model"
        case .appleIntelligence: return "apple_intelligence"
        case .endpoint(let model): return "endpoint:\(model)"
        }
    }

    init?(tag: String) {
        switch tag {
        case "triage": self = .quickMatch
        case "local_model": self = .localModel
        case "apple_intelligence": self = .appleIntelligence
        default:
            guard tag.hasPrefix("endpoint:") else { return nil }
            self = .endpoint(String(tag.dropFirst("endpoint:".count)))
        }
    }

    /// The source of a stored score; nil for scores saved before sources were recorded
    /// (except local-model scores, recognisable by their reasoning line).
    public static func of(matchReport: String?, reasoning: String?) -> ScoreSource? {
        if let data = matchReport?.data(using: .utf8),
           let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let tag = obj["scored_by"] as? String, let source = ScoreSource(tag: tag) {
            return source
        }
        return reasoning?.hasPrefix(LocalNLI.reasoningPrefix) == true ? .localModel : nil
    }

    /// Which engine an LLM call on `tier` goes to.
    public static func llm(_ ai: AIConfig, _ tier: ModelTier) -> ScoreSource {
        ai.usesOnDevice(for: tier) ? .appleIntelligence : .endpoint(ai.model(for: tier))
    }

    /// Which engine WILL score a job, shown before scoring. Mirrors
    /// `ScoringService.score`: when the local match model is picked for the fast
    /// tier and switched on, Quick match if it is installed, else the NLI model if
    /// that is; otherwise the fast-tier LLM (which resolves to the strong model
    /// when the local model is picked).
    public static func planned(config: AppConfig, localReady: Bool = NLIModel.isInstalled,
                               quickReady: Bool = QuickMatchModel.isInstalled) -> ScoreSource {
        guard prefersLocal(config) else { return .llm(config.ai, .fast) }
        return quickReady ? .quickMatch : localReady ? .localModel : .llm(config.ai, .fast)
    }

    /// Which engines WILL fill an Apply Assist form: leftover fields, and — when
    /// the local model answers those — the essay questions it hands to the LLM.
    /// Mirrors `FieldMapper`'s extractive pass.
    public static func plannedFill(config: AppConfig, localReady: Bool = NLIModel.isInstalled)
        -> (fields: ScoreSource, essays: ScoreSource?) {
        let llm = ScoreSource.llm(config.ai, .fast)
        return localReady && LocalNLI.enabled(config) ? (.localModel, llm) : (llm, nil)
    }

    static func prefersLocal(_ config: AppConfig) -> Bool {
        LocalNLI.enabled(config) && config.ai.usesLocalMatchModel
    }

    /// Whether a stored Quick match score came from a feed preview (no requirement lines).
    public static func previewOnly(matchReport: String?) -> Bool {
        guard let data = matchReport?.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return false }
        return obj["preview"] as? Bool == true
    }

    /// Seconds the stored score took, when recorded.
    public static func seconds(matchReport: String?) -> Double? {
        guard let data = matchReport?.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
        return (obj["score_seconds"] as? NSNumber)?.doubleValue
    }

    public var label: String {
        switch self {
        case .quickMatch: return "Quick match"
        case .localModel: return "Local match model"
        case .appleIntelligence: return "Apple Intelligence"
        // "local-model" is the placeholder id sent when no model is named; don't let it
        // read like the local match model.
        case .endpoint(let model): return model.isEmpty || model == "local-model" ? "AI endpoint" : model
        }
    }

    public var systemImage: String {
        switch self {
        case .quickMatch: return "bolt"
        case .localModel: return "checklist"
        case .appleIntelligence: return "apple.logo"
        case .endpoint: return "server.rack"
        }
    }
}

extension FitResult {
    /// This result with `source` recorded in its match report.
    func scored(by source: ScoreSource) -> FitResult { adding(["scored_by": source.tag]) }

    /// This result with how long scoring took, shown next to the source ("· 8.2 s").
    func timed(_ seconds: Double) -> FitResult { adding(["score_seconds": (seconds * 10).rounded() / 10]) }

    func adding(_ fields: [String: Any]) -> FitResult {
        var report: [String: Any] = [:]
        if let data = matchReportJSON?.data(using: .utf8),
           let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] { report = obj }
        report.merge(fields) { _, new in new }
        var out = self
        if let json = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]) {
            out.matchReportJSON = String(data: json, encoding: .utf8)
        }
        return out
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
    /// The Local match model is the chosen scorer but can't run at all (not
    /// downloaded, won't load), and the Writing-model fallback failed too.
    case localModelUnavailable(String)

    public var errorDescription: String? {
        switch self {
        case .localModelUnavailable(let detail):
            return detail
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
    /// With the Local AI model (beta) on and installed, it scores the job when
    /// the scoring LLM fails for any reason — unreachable or misconfigured
    /// endpoint, rate/usage limit, refusal, unreadable reply — or first, when
    /// it is picked as the fast-tier model (the LLM, i.e. the strong model, then
    /// scores only what it can't). The one exception is a cancelled task
    /// (the app being suspended, or Stop): that still pauses the batch.
    public static func score(job: Job, profile: Profile, config: AppConfig, engine: AIEngine,
                             nli: LocalNLI.Provider = LocalNLI.live,
                             quick: QuickMatchProvider = QuickMatchRuntime.live) async throws -> FitResult {
        let start = ContinuousClock.now
        let result = try await route(job: job, profile: profile, config: config, engine: engine, nli: nli, quick: quick)
        let elapsed = ContinuousClock.now - start
        return result.timed(Double(elapsed.components.seconds) + Double(elapsed.components.attoseconds) / 1e18)
    }

    private static func route(job: Job, profile: Profile, config: AppConfig, engine: AIEngine,
                              nli: LocalNLI.Provider, quick: QuickMatchProvider) async throws -> FitResult {
        let local = LocalNLI.enabled(config)
        let preferLocal = ScoreSource.prefersLocal(config)
        var localMiss: LocalMiss?
        if preferLocal {
            // Quick match first (every job, no LLM), then the detailed NLI model, then the LLM.
            let quickMiss: LocalMiss
            switch await quickScore(job: job, profile: profile, config: config, quick: quick) {
            case .success(let result): return result
            case .failure(let miss): quickMiss = miss
            }
            switch await localScore(job: job, profile: profile, config: config, nli: nli) {
            case .success(let result): return result
            case .failure(let miss): localMiss = miss == .notReady && quickMiss != .notReady ? quickMiss : miss
            }
        }
        do {
            return try await scoreWithLLM(job: job, profile: profile, config: config, engine: engine)
        } catch {
            if Task.isCancelled { throw error }
            if let localMiss {
                // The Local match model is the chosen scorer: say why IT didn't score, not
                // just that the Writing-model fallback failed.
                let fallback = "the Writing model fallback also failed (\((error as? LocalizedError)?.errorDescription ?? "\(error)"))"
                switch localMiss {
                case .nothingToJudge:
                    // About this one posting: skip it and keep the batch going.
                    throw ScoringError.refused("The Local match model found no requirements to check in this posting, and \(fallback).")
                case .notReady:
                    throw ScoringError.localModelUnavailable("The Local match model isn't downloaded yet (Settings → AI connection → Local match model), and \(fallback).")
                case .failed(let why):
                    throw ScoringError.localModelUnavailable("The Local match model couldn't run (\(why)), and \(fallback).")
                }
            }
            if !local { throw error }
            if case .success(let result) = await localScore(job: job, profile: profile, config: config, nli: nli) {
                return result
            }
            throw error
        }
    }

    /// Why the local model produced no score.
    enum LocalMiss: Error, Equatable {
        case notReady, nothingToJudge, failed(String)
    }

    private static func quickScore(job: Job, profile: Profile, config: AppConfig,
                                   quick: QuickMatchProvider) async -> Result<FitResult, LocalMiss> {
        guard let engine = await quick(config) else {
            if let why = await QuickMatchRuntime.shared.lastLoadError { return .failure(.failed(why)) }
            return .failure(.notReady)
        }
        do {
            let result = try await Task.detached(priority: .utility) {
                try engine.fitScore(job: job, profile: profile)
            }.value
            guard let result else { return .failure(.nothingToJudge) }
            return .success(result.scored(by: .quickMatch))
        } catch {
            NSLog("Quick match scoring failed for \(job.title): \(error)")
            return .failure(.failed(error.localizedDescription))
        }
    }

    /// "Refine top matches with the detailed model" (`AIConfig.triageRefine`, default off): after
    /// a scoring run, the NLI model re-scores the top `refineShare` of that run's Quick match
    /// scores, when it is switched on and installed. Returns the new results to persist.
    /// Preview-only jobs (no requirement lines) are skipped: the NLI model has nothing to judge there either.
    public static let refineShare = 0.15

    public static func refineTop(_ scored: [(job: Job, score: Double)], profile: Profile, config: AppConfig,
                                 nli: LocalNLI.Provider = LocalNLI.live) async -> [(job: Job, result: FitResult)] {
        let scored = scored.filter { !LocalNLI.requirementLines($0.job.description).isEmpty }
        guard config.ai.triageRefine, !scored.isEmpty, let scorer = await nli(config) else { return [] }
        let top = scored.enumerated().sorted { $0.element.score != $1.element.score ? $0.element.score > $1.element.score : $0.offset < $1.offset }
            .prefix(Int((refineShare * Double(scored.count)).rounded(.up))).map(\.element.job)
        var out: [(job: Job, result: FitResult)] = []
        for job in top {
            if Task.isCancelled { break }
            let start = ContinuousClock.now
            let result = try? await Task.detached(priority: .utility) {
                try LocalNLI.fitScore(job: job, profile: profile, nli: scorer)
            }.value
            guard let result = result ?? nil else { continue }
            let elapsed = ContinuousClock.now - start
            out.append((job, result.scored(by: .localModel)
                .timed(Double(elapsed.components.seconds) + Double(elapsed.components.attoseconds) / 1e18)))
        }
        return out
    }

    private static func localScore(job: Job, profile: Profile, config: AppConfig,
                                   nli: LocalNLI.Provider) async -> Result<FitResult, LocalMiss> {
        guard let scorer = await nli(config) else {
            if let why = await NLIRuntime.shared.lastLoadError { return .failure(.failed(why)) }
            return .failure(.notReady)
        }
        do {
            let result = try await Task.detached(priority: .utility) {
                try LocalNLI.fitScore(job: job, profile: profile, nli: scorer)
            }.value
            guard let result else { return .failure(.nothingToJudge) }
            return .success(result.scored(by: .localModel))
        } catch {
            NSLog("Local-model scoring failed for \(job.title): \(error)")
            return .failure(.failed(error.localizedDescription))
        }
    }

    static func scoreWithLLM(job: Job, profile: Profile, config: AppConfig,
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
                        .scored(by: .llm(config.ai, retry.tier))
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
                .scored(by: .llm(config.ai, request.tier))
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
