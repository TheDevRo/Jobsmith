import XCTest
import ZIPFoundation
@testable import JobsmithKit

/// Local AI model (beta): switch gating, Apply Assist pass-4 routing, scoring
/// fallback, the no-hallucination invariant and Swift-vs-Python parity over the
/// extractive gold set, the tokenizer, and the model download flow. The Swift
/// twin of tests/test_nli_beta.py. No model is needed: fakes stand in for Core ML.
/// Env-gated extras (pass as TEST_RUNNER_<name> to xcodebuild):
///   NLI_TOKENIZER=<path to tokenizer.json>  token ids vs Python on every gold pair
///   NLI_REAL_DOWNLOAD=1                     downloads the hosted model, then smoke + latency
///   NLI_BASE_URL=<url>                      (with the above) download from here instead of the release

// MARK: - Fakes and gold fixtures

/// Deterministic pseudo-random scores (or a fixed entailment): the invariant must hold whatever it says.
final class FakeNLI: NLIScorer, @unchecked Sendable {
    private var state: UInt64
    private let fixed: Double?
    private let lock = NSLock()
    private(set) var calls = 0

    init(seed: UInt64 = 0, fixed: Double? = nil) { state = seed &+ 0x9E3779B97F4A7C15; self.fixed = fixed }

    private func next() -> Double {  // SplitMix64
        state &+= 0x9E3779B97F4A7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58476D1CE4E5B9
        z = (z ^ (z >> 27)) &* 0x94D049BB133111EB
        return Double((z ^ (z >> 31)) >> 11) / Double(1 << 53)
    }

    func probs(_ pairs: [NLIPair]) throws -> [[Double]] {
        lock.lock(); defer { lock.unlock() }
        calls += pairs.count
        return pairs.map { _ in
            let e = fixed ?? next()
            let c = (1 - e) * next()
            return [e, 1 - e - c, c]
        }
    }
}

struct BoomNLI: NLIScorer {
    struct Boom: Error {}
    func probs(_ pairs: [NLIPair]) throws -> [[Double]] { throw Boom() }
}

/// Replays a recorded probability table; any pair Python never scored is recorded as missing.
final class ReplayNLI: NLIScorer, @unchecked Sendable {
    let table: [NLIPair: [Double]]
    private(set) var missing: [NLIPair] = []
    init(_ table: [NLIPair: [Double]]) { self.table = table }
    func probs(_ pairs: [NLIPair]) throws -> [[Double]] {
        pairs.map { p in table[p] ?? { missing.append(p); return [0, 1, 0] }() }
    }
}

final class CountingProvider: @unchecked Sendable {
    private(set) var calls = 0
    let scorer: (any NLIScorer)?
    init(_ scorer: (any NLIScorer)?) { self.scorer = scorer }
    var provider: LocalNLI.Provider { { _ in self.calls += 1; return self.scorer } }
}

enum Gold {
    struct Form: Decodable {
        let id: String
        let kinds: [String]
        let job: ApplyJobContext
        let fields: [FieldDescriptor]
    }

    struct File: Decodable {
        let profiles: [String: Profile]
        let forms: [Form]
        let pairs: [[JSONValue]]
        let tokenizer_extra: [[JSONValue]]
        let decisions: [String: [String]]
    }

    static let today = Extractive.Today(year: 2026, month: 9)  // the gold set's reference date
    static let file: File = try! JSONDecoder().decode(File.self, from: Fixtures.data("extractive_gold", "json"))
    static var profileKeys: [String] { file.profiles.keys.sorted() }

    static var table: [NLIPair: [Double]] {
        Dictionary(uniqueKeysWithValues: file.pairs.map { row in
            (NLIPair(row[0].string, row[1].string), (2...4).map { row[$0].double })
        })
    }

    /// (premise, hypothesis, Python token ids) for every gold pair plus the edge cases.
    static var tokenCases: [(String, String, [Int32])] {
        (file.pairs.map { ($0[0], $0[1], $0[5]) } + file.tokenizer_extra.map { ($0[0], $0[1], $0[2]) })
            .map { ($0.0.string, $0.1.string, $0.2.ints) }
    }
}

private extension JSONValue {
    var string: String { if case .string(let s) = self { return s }; return "" }
    var double: Double {
        switch self {
        case .int(let i): return Double(i)
        case .double(let d): return d
        default: return .nan
        }
    }
    var ints: [Int32] { if case .array(let a) = self { return a.map { Int32($0.double) } }; return [] }
}

// MARK: - Switch, routing, off mode

final class LocalNLIRoutingTests: XCTestCase {
    static let essay = #"["I am excited about this role."]"#
    private let job = ApplyJobContext(jobId: "j1", title: "Customer Success Manager", company: "Pixelworks")

    private func config(on: Bool) -> AppConfig {
        var c = AppConfig()
        c.ai.nliBetaEnabled = on
        return c
    }

    /// Answers every field-map chunk with "LLM", every essay prompt with `essay`.
    private func engine(essay: MockAIEngine.Canned = .text(essay), fields: [FieldDescriptor]) -> MockAIEngine {
        let e = MockAIEngine()
        let items = fields.map { #"{"field_id": "\#($0.fieldId)", "value": "LLM", "action": "fill", "confidence": 0.9, "source": "llm_generated"}"# }
        e.register("FORM FIELDS TO MAP", .text("[" + items.joined(separator: ",") + "]"))
        e.register("QUESTION:", essay)
        return e
    }

    private func mapper(_ engine: MockAIEngine, _ nli: @escaping LocalNLI.Provider) throws -> FieldMapper {
        FieldMapper(engine: engine, bank: AnswerBankMatcher(store: AnswerBankStore(try AppDatabase.inMemory())), nli: nli)
    }

    private var csm: (Profile, [FieldDescriptor], [String]) {
        let form = Gold.file.forms[0]
        return (Gold.file.profiles["csm"]!, form.fields, form.kinds)
    }

    func testSwitchDefaultsOffAndNeverSyncs() throws {
        XCTAssertFalse(AIConfig().nliBetaEnabled)
        let old = try JSONDecoder().decode(AIConfig.self, from: Data(#"{"baseURL": "http://x/v1"}"#.utf8))
        XCTAssertFalse(old.nliBetaEnabled)
        XCTAssertFalse(LocalNLI.enabled(AppConfig()))
        XCTAssertTrue(LocalNLI.enabled(config(on: true)))
        let raw: [String: JSONValue] = ["ai": .object(["nliBetaEnabled": .bool(true), "baseURL": .string("http://x/v1")])]
        let out = SettingsSync.export(raw, enabled: Set(SettingsSync.registry.map(\.category)))
        XCTAssertFalse(out.keys.contains { $0.lowercased().contains("nli") }, "device-local: must never sync")
        XCTAssertNotNil(out["ai.base_url"])
    }

    /// Off = today's LLM path: the loader is never asked, the model never loads, results unchanged.
    func testOffModeNeverCallsTheLoader() async throws {
        let (profile, fields, _) = csm
        let counting = CountingProvider(FakeNLI(fixed: 1))
        let a = engine(fields: fields), b = engine(fields: fields)
        let viaSwitch = try await mapper(a, counting.provider).map(fields: fields, profile: profile, job: job, config: config(on: false))
        let viaLive = try await mapper(b, LocalNLI.live).map(fields: fields, profile: profile, job: job, config: config(on: false))
        XCTAssertEqual(counting.calls, 0)
        XCTAssertEqual(viaSwitch, viaLive)
        XCTAssertTrue(viaSwitch.contains { $0.value == "LLM" })
        XCTAssertEqual(a.requests.filter { $0.user.contains("QUESTION:") }.count, 0)
        // Scoring with the LLM down stays unavailable, and nothing loaded a model.
        let failing = MockAIEngine()
        for provider in [counting.provider, LocalNLI.live] {
            do {
                _ = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                                   config: config(on: false), engine: failing, nli: provider)
                XCTFail("expected engineUnavailable")
            } catch ScoringError.engineUnavailable {}
        }
        XCTAssertEqual(counting.calls, 0)
        let loads = await NLIRuntime.shared.loads
        XCTAssertEqual(loads, 0, "switch off must never load the Core ML model")
    }

    func testOnAndReadySkipsTheFieldMapLLM() async throws {
        let (profile, fields, kinds) = csm
        let fake = FakeNLI(fixed: 1)
        let e = engine(fields: fields)
        let values = try await mapper(e, { _ in fake }).map(fields: fields, profile: profile, job: job, config: config(on: true))
        XCTAssertEqual(e.requests.filter { $0.user.contains("FORM FIELDS TO MAP") }.count, 0)
        XCTAssertGreaterThan(fake.calls, 0)
        XCTAssertEqual(values.map(\.fieldId), fields.map(\.fieldId))
        XCTAssertFalse(values.contains { $0.value == "LLM" })
        XCTAssertTrue(Set(values.map(\.source)).isSubset(of: ["profile", "answer_bank", "llm_generated", "skip"]))
        let essays = zip(values, kinds).filter { $0.1 == "essay" }.map(\.0)
        XCTAssertFalse(essays.isEmpty)
        XCTAssertTrue(essays.allSatisfy { $0.source == "llm_generated" && $0.confidence == 0.5 })
        XCTAssertTrue(essays.allSatisfy { $0.value == "I am excited about this role." })  // JSON-wrapped output unwrapped
        // EEO answers never reach an essay prompt.
        let prompts = e.requests.filter { $0.user.contains("QUESTION:") }.map(\.user)
        XCTAssertFalse(prompts.isEmpty)
        let eeo = Gold.file.profiles.values.flatMap { [$0.gender, $0.raceEthnicity, $0.veteranStatus, $0.disabilityStatus] }
            .filter { !$0.isEmpty }
        let allEssayPrompts = try await essayPromptsForEveryProfile()
        XCTAssertFalse(allEssayPrompts.contains { p in eeo.contains { p.contains($0) } })
    }

    private func essayPromptsForEveryProfile() async throws -> [String] {
        var prompts: [String] = []
        for key in Gold.profileKeys {
            let fields = Gold.file.forms[0].fields
            let e = engine(fields: fields)
            _ = try await mapper(e, { _ in FakeNLI(fixed: 1) })
                .map(fields: fields, profile: Gold.file.profiles[key]!, job: job, config: config(on: true))
            prompts += e.requests.filter { $0.user.contains("QUESTION:") }.map(\.user)
        }
        return prompts
    }

    func testEssayRefusalOrOutageIsLeftForTheUser() async throws {
        let (profile, fields, kinds) = csm
        for essay in [MockAIEngine.Canned.text("I cannot answer this based on the profile."), .failure("LM Studio down")] {
            let values = try await mapper(engine(essay: essay, fields: fields), { _ in FakeNLI() })
                .map(fields: fields, profile: profile, job: job, config: config(on: true))
            let essays = zip(values, kinds).filter { $0.1 == "essay" }.map(\.0)
            XCTAssertFalse(essays.isEmpty)
            XCTAssertTrue(essays.allSatisfy { $0.value.isEmpty && $0.action == "skip" })
        }
    }

    func testOnButModelMissingUsesTheLLM() async throws {
        let (profile, fields, _) = csm
        let e = engine(fields: fields)
        let values = try await mapper(e, { _ in nil }).map(fields: fields, profile: profile, job: job, config: config(on: true))
        XCTAssertEqual(e.requests.filter { $0.user.contains("FORM FIELDS TO MAP") }.count, 1)
        XCTAssertTrue(values.contains { $0.value == "LLM" })
    }

    func testNLIRuntimeErrorFallsBackToTheLLM() async throws {
        let (profile, fields, _) = csm
        let e = engine(fields: fields)
        let on = try await mapper(e, { _ in BoomNLI() }).map(fields: fields, profile: profile, job: job, config: config(on: true))
        let off = try await mapper(engine(fields: fields), { _ in nil }).map(fields: fields, profile: profile, job: job, config: config(on: false))
        XCTAssertEqual(on, off)
        XCTAssertEqual(e.requests.filter { $0.user.contains("FORM FIELDS TO MAP") }.count, 1)
    }
}

// MARK: - Gold set: invariant + Swift-vs-Python parity

final class ExtractiveGoldTests: XCTestCase {
    /// Every pass-4, non-essay fill is a form option, a verbatim profile fact, or a date computation.
    private func invariantViolations(_ profile: Profile, _ fields: [FieldDescriptor], _ values: [FieldValue]) -> [String] {
        let facts = Set(Extractive.buildFacts(profile, today: Gold.today).map(\.value))
        let det = ProfileFieldMatcher.matchProfileFields(profile: profile, fields: fields)
        let byId = Dictionary(uniqueKeysWithValues: fields.map { ($0.fieldId, $0) })
        var bad: [String] = []
        for v in values where !v.value.isEmpty && det[v.fieldId] == nil && v.source != "llm_generated" {
            let f = byId[v.fieldId]!
            if f.fieldType == "file" { continue }
            let ok: Bool
            if let options = f.options, !options.isEmpty {
                ok = options.contains(v.value)
            } else if facts.contains(v.value) {
                ok = true
            } else {
                ok = Extractive.yearsWith(profile, Extractive.skillTerms(f.label), Gold.today).map { v.value == String(Int($0)) } ?? false
            }
            if !ok { bad.append("\(f.label) -> \(v.value) (\(v.source))") }
        }
        return bad
    }

    private func fill(_ profile: Profile, _ fields: [FieldDescriptor], _ nli: any NLIScorer,
                      essay: String? = nil) async throws -> [FieldValue] {
        try await Extractive.fill(profile: profile, fields: fields, bank: [:], nli: nli, today: Gold.today) { _ in essay }
    }

    func testNoHallucinationInvariant() async throws {
        for (name, scorer) in [("seed0", FakeNLI(seed: 0)), ("seed1", FakeNLI(seed: 1)), ("seed2", FakeNLI(seed: 2)),
                               ("always-entailed", FakeNLI(fixed: 1))] {
            var filled = 0
            for key in Gold.profileKeys {
                let profile = Gold.file.profiles[key]!
                for form in Gold.file.forms {
                    let values = try await fill(profile, form.fields, scorer, essay: "Draft.")
                    XCTAssertEqual(invariantViolations(profile, form.fields, values), [], "\(name) \(key)/\(form.id)")
                    filled += values.filter { !$0.value.isEmpty && $0.source == "profile" }.count
                    // Only the sales profile declines EEO questions.
                    let det = ProfileFieldMatcher.matchProfileFields(profile: profile, fields: form.fields)
                    if key != "sales" {
                        for (v, kind) in zip(values, form.kinds) where kind == "eeo" && !v.value.isEmpty && det[v.fieldId] == nil {
                            XCTAssertFalse(["decline", "wish", "want to answer", "prefer not"].contains { v.value.lowercased().contains($0) },
                                           "\(name) \(key): \(v.value)")
                        }
                    }
                }
            }
            XCTAssertGreaterThan(filled, 50, name)  // the scorer does make pass 4 fill things
        }
    }

    func testInvariantCheckCatchesAnInventedValue() {
        let profile = Gold.file.profiles["csm"]!
        let fields = Gold.file.forms[0].fields
        let det = ProfileFieldMatcher.matchProfileFields(profile: profile, fields: fields)
        let f = fields.first { ($0.options ?? []).isEmpty && $0.fieldType == "text" && det[$0.fieldId] == nil }!
        XCTAssertFalse(invariantViolations(profile, fields, [FieldValue(fieldId: f.fieldId, value: "Invented Corp")]).isEmpty)
    }

    /// Replaying the PyTorch probabilities Python used, Swift makes the same decisions
    /// (and asks the model exactly the pairs Python asked).
    func testSwiftMatchesPythonOnTheGoldSet() async throws {
        let replay = ReplayNLI(Gold.table)
        var same = 0, total = 0, differing: [String] = []
        for key in Gold.profileKeys {
            for form in Gold.file.forms {
                let values = try await fill(Gold.file.profiles[key]!, form.fields, replay)
                for v in values {
                    let id = "\(key)/\(form.id)/\(v.fieldId)"
                    total += 1
                    if [v.action, v.value, v.source] == Gold.file.decisions[id] { same += 1 } else {
                        differing.append("\(id): swift \([v.action, v.value, v.source]) python \(Gold.file.decisions[id] ?? [])")
                    }
                }
            }
        }
        print("Swift-vs-Python gold parity: \(same)/\(total) identical; missing pairs \(replay.missing.count)")
        XCTAssertEqual(total, Gold.file.decisions.count)
        XCTAssertEqual(replay.missing.map(\.hypothesis), [])
        XCTAssertEqual(differing, [])
        XCTAssertGreaterThanOrEqual(Double(same) / Double(total), 0.99)
    }

    func testYearsAndDateMath() {
        let p = Gold.file.profiles["sysadmin"]!
        XCTAssertEqual(Extractive.parseMonth("April 2021", Gold.today), 2021 * 12 + 3)
        XCTAssertEqual(Extractive.parseMonth("04/2021", Gold.today), 2021 * 12 + 3)
        XCTAssertEqual(Extractive.parseMonth("Present", Gold.today), 2026 * 12 + 8)
        XCTAssertNil(Extractive.parseMonth("someday", Gold.today))
        XCTAssertEqual(Extractive.skillTerms("Years of experience with JavaScript/TypeScript?"), ["javascript", "typescript"])
        XCTAssertNil(Extractive.skillTerms("Years of professional experience"))
        let d = Extractive.number(FieldDescriptor(fieldId: "y", label: "How many years of experience do you have?",
                                                  options: ["0-2 years", "3-5 years", "6-10 years", "11+ years"]), p, Gold.today)
        XCTAssertEqual(d.value, "11+ years")  // 2015-08 .. 2026-09 = 11.1 years
    }
}

// MARK: - Scoring fallback

enum JobFixtures {
    static let dataEngineer = Job(from: NormalizedJob(
        source: "demo", externalId: "d-1", title: "Data Engineer", company: "Acme", location: "Remote",
        description: "About us: we ship data.\n- 5+ years of experience with Python and SQL\n"
            + "- Bachelor's degree in Computer Science or similar\n- Experience with Airflow is a plus\n"
            + "We offer great benefits and snacks."))
    static let profile = Profile(summary: "Data engineer.", skills: ["Python", "SQL"],
                                 experience: [WorkExperience(title: "Data Engineer", company: "Acme", startDate: "2018-01", endDate: "")],
                                 education: [Education(degree: "BS Computer Science", school: "State U")])
}

final class LocalNLIScoringTests: XCTestCase {
    private var on: AppConfig { var c = AppConfig(); c.ai.nliBetaEnabled = true; return c }

    private struct Half: NLIScorer {
        func probs(_ pairs: [NLIPair]) throws -> [[Double]] {
            pairs.map { $0.hypothesis.contains("Python") || $0.hypothesis.contains("Bachelor") ? [0.9, 0.1, 0] : [0.1, 0.9, 0] }
        }
    }

    func testScoringFallsBackToTheLocalModel() async throws {
        let result = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                                    config: on, engine: MockAIEngine(), nli: { _ in Half() })
        XCTAssertTrue(result.reasoning.hasPrefix("Scored by Local match"))
        XCTAssertEqual(result.score, 100 * (0.9 + 0.9 + 0.1) / 3, accuracy: 0.1)
        let report = try JSONSerialization.jsonObject(with: Data(result.matchReportJSON!.utf8)) as! [String: Any]
        XCTAssertEqual(report["matched_skills"] as? [String], ["5+ years of experience with Python and SQL",
                                                              "Bachelor's degree in Computer Science or similar"])
        XCTAssertEqual(report["missing_skills"] as? [String], ["Experience with Airflow is a plus"])
    }

    func testScoringStaysUnavailable() async {
        let noLines = Job(from: NormalizedJob(source: "demo", externalId: "d-2", title: "x", company: "y",
                                              location: "", description: "We are nice. Great snacks."))
        let cases: [(AppConfig, (any NLIScorer)?, Job)] = [
            (AppConfig(), FakeNLI(fixed: 1), JobFixtures.dataEngineer),  // switch off
            (on, nil, JobFixtures.dataEngineer),                         // model not installed
            (on, BoomNLI(), JobFixtures.dataEngineer),                   // runtime error
            (on, FakeNLI(fixed: 1), noLines),                            // nothing to judge
        ]
        for (i, (config, scorer, job)) in cases.enumerated() {
            do {
                _ = try await ScoringService.score(job: job, profile: JobFixtures.profile, config: config,
                                                   engine: MockAIEngine(), nli: { _ in scorer })
                XCTFail("case \(i) scored")
            } catch ScoringError.engineUnavailable {
            } catch {
                XCTFail("case \(i): \(error)")
            }
        }
    }

    private final class Failing: AIEngine, @unchecked Sendable {
        let error: Error
        var calls = 0
        init(_ error: Error) { self.error = error }
        func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String { calls += 1; throw error }
        func listModels(config: AIConfig) async throws -> [String] { [] }
    }

    /// A wrong endpoint, a usage limit, a bad key or a garbled reply all fall back.
    func testAnyLLMFailureFallsBackToTheLocalModel() async throws {
        let errors: [Error] = [
            AIEngineError.interrupted("host not reachable"),        // wrong/out-of-reach endpoint
            AIEngineError.unreachable("Connection refused"),
            AIEngineError.httpStatus(429, "usage limit reached"),
            AIEngineError.httpStatus(401, "bad key"),
            AIEngineError.invalidBaseURL("htp:/nope"),
            AIEngineError.refused("guardrail"),
        ]
        for error in errors {
            let result = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                                        config: on, engine: Failing(error), nli: { _ in Half() })
            XCTAssertTrue(result.reasoning.hasPrefix(LocalNLI.reasoningPrefix), "\(error)")
        }
    }

    /// The app being suspended (or Stop) cancels the task: that still pauses.
    func testCancelledTaskStillPausesInsteadOfFallingBack() async {
        let task = Task { () -> Result<FitResult, Error> in
            await Task.yield()
            do {
                return .success(try await ScoringService.score(
                    job: JobFixtures.dataEngineer, profile: JobFixtures.profile, config: self.on,
                    engine: Failing(AIEngineError.interrupted("suspended")), nli: { _ in Half() }))
            } catch { return .failure(error) }
        }
        task.cancel()
        guard case .failure(ScoringError.interrupted) = await task.value else {
            return XCTFail("a cancelled scoring task must pause, not fall back")
        }
    }

    private final class Answering: AIEngine, @unchecked Sendable {
        func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
            #"{"score": 70, "reasoning": "Solid fit."}"#
        }
        func listModels(config: AIConfig) async throws -> [String] { [] }
    }

    /// Every score records what produced it, so the job can say so.
    func testScoresRecordTheirSource() async throws {
        func source(_ r: FitResult) -> ScoreSource? { ScoreSource.of(matchReport: r.matchReportJSON, reasoning: r.reasoning) }
        var endpoint = AppConfig(); endpoint.ai.fastModel = "qwen3.5-9b"
        let viaEndpoint = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                                         config: endpoint, engine: Answering())
        XCTAssertEqual(source(viaEndpoint), .endpoint("qwen3.5-9b"))
        XCTAssertEqual(source(viaEndpoint)?.label, "qwen3.5-9b")

        var apple = AppConfig(); apple.ai.fastModel = AIConfig.onDeviceModelID
        let viaApple = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                                      config: apple, engine: Answering())
        XCTAssertEqual(source(viaApple), .appleIntelligence)

        var local = on; local.ai.fastModel = AIConfig.localMatchModelID
        let viaLocal = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                                      config: local, engine: Answering(), nli: { _ in Half() })
        XCTAssertEqual(source(viaLocal), .localModel)
        XCTAssertEqual(source(viaLocal)?.label, "Local match")
        // The match report's own fields survive the tag.
        let report = try JSONSerialization.jsonObject(with: Data(viaLocal.matchReportJSON!.utf8)) as! [String: Any]
        XCTAssertNotNil(report["matched_skills"])

        // Scores saved before sources were recorded: local ones still show, others show nothing.
        XCTAssertEqual(ScoreSource.of(matchReport: nil, reasoning: LocalNLI.reasoningPrefix + ": meets 1 of 2"), .localModel)
        XCTAssertNil(ScoreSource.of(matchReport: #"{"matched_skills":[]}"#, reasoning: "Great fit"))
    }

    /// With the match model chosen, a failure names the local model's problem instead of
    /// blaming the endpoint, and a posting it can't judge is skipped, not batch-stopping.
    func testChosenMatchModelFailuresSayWhy() async {
        var config = on; config.ai.fastModel = AIConfig.localMatchModelID
        let down = Failing(AIEngineError.httpStatus(404, ""))
        do {
            _ = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                               config: config, engine: down, nli: { _ in nil })
            XCTFail("scored")
        } catch ScoringError.localModelUnavailable(let detail) {
            XCTAssertTrue(detail.contains("isn't downloaded"), detail)
        } catch { XCTFail("\(error)") }

        let noLines = Job(from: NormalizedJob(source: "demo", externalId: "d-3", title: "x", company: "y",
                                              location: "", description: "We are nice. Great snacks."))
        do {
            _ = try await ScoringService.score(job: noLines, profile: JobFixtures.profile,
                                               config: config, engine: down, nli: { _ in Half() })
            XCTFail("scored")
        } catch ScoringError.refused(let detail) {
            XCTAssertTrue(detail.contains("no requirements"), detail)
        } catch { XCTFail("\(error)") }
    }

    func testPreferLocalScoresWithoutCallingTheLLM() async throws {
        var config = on
        config.ai.fastModel = AIConfig.localMatchModelID
        let engine = Failing(AIEngineError.unreachable("should not be called"))
        let result = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                                    config: config, engine: engine, nli: { _ in Half() })
        XCTAssertTrue(result.reasoning.hasPrefix(LocalNLI.reasoningPrefix))
        XCTAssertEqual(engine.calls, 0)
    }

    func testPreferLocalIsIgnoredWhileTheSwitchIsOff() async {
        var config = AppConfig()
        config.ai.fastModel = AIConfig.localMatchModelID
        let engine = Failing(AIEngineError.unreachable("down"))
        _ = try? await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                            config: config, engine: engine, nli: { _ in Half() })
        XCTAssertGreaterThan(engine.calls, 0)
    }

    // MARK: - "Local match model" as the fast-tier pick

    /// Records the model each request would carry to an endpoint, and whether it would go on-device.
    private final class Recording: AIEngine, @unchecked Sendable {
        var models: [String] = []
        var onDevice: [Bool] = []
        func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
            models.append(config.endpointModel(for: req.tier)); models.append(config.model(for: req.tier))
            onDevice.append(config.usesOnDevice(for: req.tier))
            return #"{"score": 70, "reasoning": "Solid fit."}"#
        }
        func listModels(config: AIConfig) async throws -> [String] { [] }
    }

    private var picked: AppConfig {
        var c = on; c.ai.fastModel = AIConfig.localMatchModelID; c.ai.strongModel = "big-writer"; return c
    }

    func testSentinelNeverResolvesAsAModel() {
        for strong in ["big-writer", "", AIConfig.onDeviceModelID] {
            for utility in ["", "tiny"] {
                var ai = AIConfig(utilityModel: utility, fastModel: AIConfig.localMatchModelID, strongModel: strong)
                for tier in ModelTier.allCases {
                    XCTAssertNotEqual(ai.model(for: tier), AIConfig.localMatchModelID)
                    XCTAssertNotEqual(ai.endpointModel(for: tier), AIConfig.localMatchModelID)
                }
                XCTAssertEqual(ai.model(for: .fast), strong.isEmpty ? "local-model" : strong, "fast falls through to strong")
                XCTAssertEqual(ai.usesOnDevice(for: .fast), strong == AIConfig.onDeviceModelID)
                ai.apply(.init(name: "x", baseURL: "http://y/v1", apiKey: "", fastModel: "other"))
                XCTAssertEqual(ai.fastModel, AIConfig.localMatchModelID, "an endpoint switch keeps the pick")
            }
        }
    }

    /// Scoring when the local model can't, essays, field mapping: all go to the strong model.
    func testEveryFastCallFallsThroughToTheStrongModel() async throws {
        let engine = Recording()
        let result = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                                    config: picked, engine: engine, nli: { _ in nil })
        XCTAssertEqual(ScoreSource.of(matchReport: result.matchReportJSON, reasoning: result.reasoning), .endpoint("big-writer"))

        let field = FieldDescriptor(fieldId: "q", label: "Why us?")
        _ = await FieldMapper.essayAnswer(field: field, profile: JobFixtures.profile,
                                          job: ApplyJobContext(jobId: "j", title: "t", company: "c"),
                                          config: picked, engine: engine)
        let mapper = FieldMapper(engine: engine, bank: AnswerBankMatcher(store: AnswerBankStore(try AppDatabase.inMemory())),
                                 nli: { _ in nil })
        _ = try? await mapper.completeJSON(system: "s", user: "u", config: picked, maxRetries: 1)
        XCTAssertGreaterThanOrEqual(engine.onDevice.count, 3)
        XCTAssertEqual(Set(engine.models), ["big-writer"])
        XCTAssertFalse(engine.onDevice.contains(true))
    }

    func testPickedModelScoresFirstAndNeverCallsTheLLM() async throws {
        let engine = Recording()
        let result = try await ScoringService.score(job: JobFixtures.dataEngineer, profile: JobFixtures.profile,
                                                    config: picked, engine: engine, nli: { _ in Half() })
        XCTAssertEqual(ScoreSource.of(matchReport: result.matchReportJSON, reasoning: result.reasoning), .localModel)
        XCTAssertTrue(engine.models.isEmpty)
    }

    func testPlannedSourceMatchesRouting() {
        XCTAssertEqual(ScoreSource.planned(config: picked, localReady: true), .localModel)
        XCTAssertEqual(ScoreSource.planned(config: picked, localReady: false), .endpoint("big-writer"))
        var off = picked; off.ai.nliBetaEnabled = false
        XCTAssertEqual(ScoreSource.planned(config: off, localReady: true), .endpoint("big-writer"))
        var fallbackOnly = on; fallbackOnly.ai.fastModel = "small"
        XCTAssertEqual(ScoreSource.planned(config: fallbackOnly, localReady: true), .endpoint("small"))
        var apple = picked; apple.ai.strongModel = AIConfig.onDeviceModelID
        XCTAssertEqual(ScoreSource.planned(config: apple, localReady: false), .appleIntelligence)
        XCTAssertEqual(ScoreSource.planned(config: AppConfig(), localReady: true).label, "AI endpoint")

        XCTAssertTrue(ScoreSource.plannedFill(config: picked, localReady: true) == (.localModel, .endpoint("big-writer")))
        XCTAssertTrue(ScoreSource.plannedFill(config: fallbackOnly, localReady: true) == (.localModel, .endpoint("small")))
        XCTAssertTrue(ScoreSource.plannedFill(config: picked, localReady: false) == (.endpoint("big-writer"), nil))
    }

    func testLegacyPreferLocalMigratesToThePick() throws {
        func decode(_ json: String) throws -> AIConfig { try JSONDecoder().decode(AIConfig.self, from: Data(json.utf8)) }
        let migrated = try decode(#"{"fastModel": "small", "nliBetaEnabled": true, "nliScoringPreferLocal": true}"#)
        XCTAssertEqual(migrated.fastModel, AIConfig.localMatchModelID)
        XCTAssertEqual(try decode(#"{"fastModel": "small", "nliScoringPreferLocal": true}"#).fastModel, "small",
                       "the old toggle did nothing with the model off")
        XCTAssertEqual(try decode(#"{"fastModel": "small", "nliBetaEnabled": true}"#).fastModel, "small")
        let written = String(data: try JSONEncoder().encode(migrated), encoding: .utf8)!
        XCTAssertFalse(written.contains("nliScoringPreferLocal"))
        XCTAssertEqual(try decode(written).fastModel, AIConfig.localMatchModelID)
    }

    func testPickNeverSyncsAndIsNotOverwrittenByImports() {
        var config: [String: JSONValue] = ["ai": .object(["fastModel": .string(AIConfig.localMatchModelID),
                                                          "strongModel": .string("big-writer")])]
        XCTAssertEqual(SettingsSync.deviceLocalModels, AIConfig.sentinelModelIDs)
        let out = SettingsSync.export(config, enabled: ["ai_connection"])
        XCTAssertNil(out["ai.models.fast"])
        XCTAssertNotNil(out["ai.models.strong"])
        XCTAssertTrue(SettingsSync.isDeviceLocal("ai.models.fast", config: config))
        SettingsSync.apply(&config, path: "ai.models.fast", value: .string("desktop-model"))
        guard case .object(let ai)? = config["ai"] else { return XCTFail("no ai section") }
        XCTAssertEqual(ai["fastModel"], .string(AIConfig.localMatchModelID))
    }

    /// One model run per requirement line; each premise keeps the roles and puts the skills and
    /// summary sentences that match its line first, within the fixed pair length. Mirrors the desktop test.
    func testLongProfilesGetAPremisePerLineThatFits() throws {
        final class Words: NLIScorer, @unchecked Sendable {
            var seen: [NLIPair] = []
            func countTokens(_ pair: NLIPair) -> Int? {
                (pair.premise + " " + pair.hypothesis).split(whereSeparator: \.isWhitespace).count
            }
            func probs(_ pairs: [NLIPair]) throws -> [[Double]] {
                seen += pairs
                return pairs.map { _ in [0.9, 0.1, 0] }
            }
        }
        var long = JobFixtures.profile
        long.summary = (0..<200).map { "Filler sentence \($0)." }.joined(separator: " ") + " I love Airflow pipelines."
        long.skills = (0..<300).map { "skill\($0)" } + ["Airflow"]
        let words = Words()
        _ = try XCTUnwrap(LocalNLI.fitScore(job: JobFixtures.dataEngineer, profile: long, nli: words))
        let lines = LocalNLI.requirementLines(JobFixtures.dataEngineer.description)
        XCTAssertEqual(words.seen.map(\.hypothesis), lines.map(LocalNLI.hypothesis))
        for pair in words.seen {
            XCTAssertLessThanOrEqual(try XCTUnwrap(words.countTokens(pair)), LocalNLI.maxPairTokens)
            XCTAssertTrue(pair.premise.contains("Data Engineer at Acme"))
        }
        let airflow = words.seen[try XCTUnwrap(lines.firstIndex(of: "Experience with Airflow is a plus"))].premise
        XCTAssertTrue(airflow.contains("skills: Airflow, skill0") && airflow.contains("I love Airflow pipelines."))
        XCTAssertFalse(words.seen[0].premise.contains("skills: Airflow"))  // other lines keep profile order
        let short = LocalNLI.linePremises(JobFixtures.profile, lines: lines) { words.countTokens($0) }[0]
        XCTAssertTrue(short.contains("Data engineer.") && short.contains("Python, SQL"))
    }

    func testRequirementLinesMatchTheBench() {
        XCTAssertEqual(LocalNLI.requirementLines(JobFixtures.dataEngineer.description), [
            "5+ years of experience with Python and SQL", "Bachelor's degree in Computer Science or similar",
            "Experience with Airflow is a plus"])
    }

    /// Same requirement lines and premises as the desktop over the gold profiles
    /// (Fixtures/nli_line_premises.json, written by tests/test_nli_beta.py).
    func testLinePremisesMatchPython() throws {
        let file = try PremiseFixture.load()
        XCTAssertEqual(file.max_pair_tokens, LocalNLI.maxPairTokens)
        XCTAssertEqual(file.cases.count, Gold.profileKeys.count * file.jobs.count)
        for c in file.cases {
            let lines = LocalNLI.requirementLines(file.jobs[c.job])
            XCTAssertEqual(lines, c.lines, "\(c.profile) job \(c.job)")
            let prems = LocalNLI.linePremises(try XCTUnwrap(Gold.file.profiles[c.profile]), lines: lines) { _ in nil }
            XCTAssertEqual(prems, c.premises, "\(c.profile) job \(c.job)")
        }
    }
}

enum PremiseFixture {
    struct Case: Decodable { let profile: String; let job: Int; let lines: [String]; let premises: [String] }
    struct File: Decodable { let jobs: [String]; let max_pair_tokens: Int; let cases: [Case] }
    static func load() throws -> File {
        try JSONDecoder().decode(File.self, from: Fixtures.data("nli_line_premises", "json"))
    }
}

// MARK: - Tokenizer + Core ML (env-gated: needs the real files)

final class LocalNLIModelFileTests: XCTestCase {
    private func env(_ name: String) -> String? {
        ProcessInfo.processInfo.environment[name].flatMap { $0.isEmpty ? nil : $0 }
    }

    private func checkTokenizer(_ url: URL) throws {
        let t0 = Date()
        let tok = try DebertaTokenizer(contentsOf: url)
        let load = Date().timeIntervalSince(t0)
        var mismatches: [String] = []
        let cases = Gold.tokenCases
        let t1 = Date()
        for (a, b, ids) in cases where tok.encodePair(a, b) != ids {
            mismatches.append("\(a.prefix(60)) | \(b.prefix(60))\n swift \(tok.encodePair(a, b).prefix(40))\n python \(ids.prefix(40))")
        }
        print(String(format: "Tokenizer golden: %d/%d identical; load %.2fs, encode %.1f ms/pair",
                     cases.count - mismatches.count, cases.count, load, 1000 * Date().timeIntervalSince(t1) / Double(cases.count)))
        XCTAssertEqual(mismatches, [])
    }

    func testTokenizerMatchesPython() throws {
        guard let path = env("NLI_TOKENIZER") else { throw XCTSkip("set TEST_RUNNER_NLI_TOKENIZER to a tokenizer.json") }
        try checkTokenizer(URL(fileURLWithPath: path))
    }

    /// The real thing, in the simulator: download from the GitHub release (SHA-checked),
    /// load, score the gold pairs, time a form and a job score. Simulator = CPU only.
    @MainActor
    func testRealDownloadLoadAndLatency() async throws {
        guard env("NLI_REAL_DOWNLOAD") != nil else { throw XCTSkip("set TEST_RUNNER_NLI_REAL_DOWNLOAD=1") }
        let store = env("NLI_BASE_URL").flatMap(URL.init(string:)).map { NLIModelStore(baseURL: $0) } ?? .shared
        let t0 = Date()
        store.install()
        while store.isDownloading { try await Task.sleep(for: .milliseconds(500)) }
        XCTAssertEqual(store.state, .ready, "\(store.state)")
        print(String(format: "Real download + verify: %.0fs, %@", Date().timeIntervalSince(t0), NLIModel.directory.path))
        try checkTokenizer(NLIModel.directory.appendingPathComponent(NLIModel.tokenizerFile))

        let t1 = Date()
        let loaded = await NLIRuntime.shared.scorer()
        let scorer = try XCTUnwrap(loaded)
        print(String(format: "Model load: %.1fs", Date().timeIntervalSince(t1)))
        let p = try scorer.probs([NLIPair("The candidate lives in Denver.", "The candidate lives in Colorado."),
                                  NLIPair("The candidate lives in Denver.", "The candidate lives in Paris.")])
        XCTAssertGreaterThan(p[0][0], 0.5)
        XCTAssertLessThan(p[1][0], 0.5)

        // Parity vs PyTorch on a slice of the gold pairs (the full 631 run is the macOS script).
        let table = Gold.table
        let sample = Array(table.keys.sorted { $0.premise + $0.hypothesis < $1.premise + $1.hypothesis }.prefix(40))
        let got = try scorer.entail(sample)
        let deltas = zip(sample, got).map { abs(table[$0.0]![0] - $0.1) }
        print(String(format: "Simulator Core ML vs PyTorch (%d pairs): mean |dP| %.4f, max %.4f",
                     deltas.count, deltas.reduce(0, +) / Double(deltas.count), deltas.max()!))

        // Latency: every gold form for one profile, and one job score.
        var formTimes: [Double] = []
        for form in Gold.file.forms {
            let t = Date()
            _ = try await Extractive.fill(profile: Gold.file.profiles["sysadmin"]!, fields: form.fields, bank: [:],
                                          nli: scorer, today: Gold.today) { _ in nil }
            formTimes.append(Date().timeIntervalSince(t))
        }
        let t2 = Date()
        _ = try LocalNLI.fitScore(job: JobFixtures.dataEngineer, profile: Gold.file.profiles["sysadmin"]!, nli: scorer)
        print(String(format: "Simulator latency (CPU): per form median %.2fs max %.2fs; job score %.2fs",
                     formTimes.sorted()[formTimes.count / 2], formTimes.max()!, Date().timeIntervalSince(t2)))

        // Per job and per pair on the premise-fixture jobs (full-length 256-token premises).
        let jobs = try PremiseFixture.load().jobs
        var pairs = 0
        let t3 = Date()
        for (i, d) in jobs.enumerated() {
            pairs += LocalNLI.requirementLines(d).count
            _ = try LocalNLI.fitScore(job: Job(from: NormalizedJob(source: "demo", externalId: "fx-\(i)", title: "Fixture",
                                                                   company: "Fixture", location: "Remote", description: d)),
                                      profile: Gold.file.profiles["sysadmin"]!, nli: scorer)
        }
        let el = Date().timeIntervalSince(t3)
        print(String(format: "Simulator fit latency: %.2fs/job, %.0f ms/pair (%d jobs, %d pairs)",
                     el / Double(jobs.count), 1000 * el / Double(pairs), jobs.count, pairs))
    }
}

// MARK: - Download flow (stubbed network)

final class StubServer: URLProtocol {
    nonisolated(unsafe) static var files: [String: Data] = [:]
    nonisolated(unsafe) static var failAfter: Int?  // drop the connection after this many bytes of the zip
    nonisolated(unsafe) static var requests: [String] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        let name = request.url!.lastPathComponent
        Self.requests.append(name)
        guard let body = Self.files[name] else {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!,
                                cacheStoragePolicy: .notAllowed)
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil,
                                                              headerFields: ["Content-Length": "\(body.count)"])!,
                            cacheStoragePolicy: .notAllowed)
        if let cut = Self.failAfter, name.hasSuffix(".zip") {
            client?.urlProtocol(self, didLoad: body.prefix(cut))
            client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
            return
        }
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

@MainActor
final class NLIModelStoreTests: XCTestCase {
    private var savedRoot: URL!
    private var files: [NLIModel.File] = []

    override func setUp() async throws {
        savedRoot = NLIModel.root
        NLIModel.root = FileManager.default.temporaryDirectory.appendingPathComponent("nli-\(UUID().uuidString)")
        // A fake compiled model, zipped the way the release ships it.
        let src = FileManager.default.temporaryDirectory.appendingPathComponent("src-\(UUID().uuidString)")
        let pkg = src.appendingPathComponent("NLI.mlmodelc")
        try FileManager.default.createDirectory(at: pkg, withIntermediateDirectories: true)
        try Data(repeating: 7, count: 300_000).write(to: pkg.appendingPathComponent("weights.bin"))
        let zip = src.appendingPathComponent("model.zip")
        try FileManager.default.zipItem(at: pkg, to: zip)
        let tok = Data(#"{"tok": 1}"#.utf8)
        let zipData = try Data(contentsOf: zip)
        StubServer.files = ["model.mlmodelc.zip": zipData, "tokenizer.json": tok]
        StubServer.failAfter = nil
        StubServer.requests = []
        files = [NLIModel.File(name: "model.mlmodelc.zip", size: Int64(zipData.count), sha256: sha(zipData)),
                 NLIModel.File(name: "tokenizer.json", size: Int64(tok.count), sha256: sha(tok))]
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: NLIModel.root)
        NLIModel.root = savedRoot
    }

    private func sha(_ d: Data) -> String {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try! d.write(to: url)
        return try! NLIModelStore.sha256(url)
    }

    private func store(_ files: [NLIModel.File]) -> NLIModelStore {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [StubServer.self]
        return NLIModelStore(configuration: cfg, baseURL: URL(string: "https://models.invalid/v1")!, files: files)
    }

    private func finish(_ s: NLIModelStore) async throws {
        let t = Date()
        while s.isDownloading && Date().timeIntervalSince(t) < 20 { try await Task.sleep(for: .milliseconds(20)) }
    }

    func testInstallDeleteReinstall() async throws {
        let s = store(files)
        XCTAssertEqual(s.state, .notInstalled)
        s.install()
        try await finish(s)
        XCTAssertEqual(s.state, .ready)
        XCTAssertTrue(NLIModel.isInstalled)
        let names = try FileManager.default.contentsOfDirectory(atPath: NLIModel.directory.path).sorted()
        XCTAssertEqual(names, ["NLI.mlmodelc", "tokenizer.json"])  // zip and staging cleaned up
        XCTAssertEqual(try NLIModel.root.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup, true)
        await s.delete()
        XCTAssertEqual(s.state, .notInstalled)
        XCTAssertFalse(FileManager.default.fileExists(atPath: NLIModel.root.path))
        s.install()
        try await finish(s)
        XCTAssertEqual(s.state, .ready)
    }

    func testChecksumMismatchIsRejected() async throws {
        let bad = [NLIModel.File(name: files[0].name, size: files[0].size, sha256: String(repeating: "0", count: 64)), files[1]]
        let s = store(bad)
        s.install()
        try await finish(s)
        guard case .failed(let msg) = s.state else { return XCTFail("\(s.state)") }
        XCTAssertTrue(msg.contains("checksum"), msg)
        XCTAssertFalse(NLIModel.isInstalled)
        let left = (try? FileManager.default.contentsOfDirectory(atPath: NLIModel.directory.path)) ?? []
        XCTAssertFalse(left.contains { $0.hasSuffix(".verified") || $0.hasSuffix(".mlmodelc") }, "\(left)")
    }

    func testNetworkFailureLeavesNoModelThenRetrySucceeds() async throws {
        StubServer.failAfter = 100_000
        let s = store(files)
        s.install()
        try await finish(s)
        guard case .failed = s.state else { return XCTFail("\(s.state)") }
        XCTAssertFalse(NLIModel.isInstalled)
        StubServer.failAfter = nil
        s.install()  // the Retry button
        try await finish(s)
        XCTAssertEqual(s.state, .ready)
    }
}
