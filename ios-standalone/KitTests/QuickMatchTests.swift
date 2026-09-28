import XCTest
@testable import JobsmithKit

/// Quick match (embedding triage): golden parity with the bench and the desktop (same fixture as
/// tests/test_triage.py), the WordPiece tokenizer, routing, the refine pass and the download.
/// No model needed: the fixture carries every text's embedding. Env-gated extra
/// (pass as TEST_RUNNER_<name> to xcodebuild):
///   TRIAGE_MODEL_DIR=<dir with Triage.mlmodelc, tokenizer.json, triage-data.json>
///       full-vocab tokenizer parity, Core ML vs golden scores, and simulator timing for 300 jobs.
enum TriageGolden {
    struct Case: Decodable {
        let profile: String, job: String, lines: [String]
        let p: [Double]?
        let score_raw: Double?
        let preview: Bool?
    }
    struct PostingJSON: Decodable { let title: String, description: String }
    struct File: Decodable {
        let today: String
        let cases: [Case]
        let jobs: [String: PostingJSON]
        let embeddings: [String: [Double]]
        let ids: [String: [Int32]]
        let vocab: [String: Int32]
        let data: QuickMatch.Weights
    }

    static let raw = try! Fixtures.data("triage_golden", "json")
    static let file = try! JSONDecoder().decode(File.self, from: raw)
    static let today = Extractive.Today(year: Int(file.today.prefix(4))!, month: Int(file.today.dropFirst(5).prefix(2))!)

    /// The fixture's profiles are desktop-shaped dicts (snake_case).
    static let profiles: [String: Profile] = {
        let obj = try! JSONSerialization.jsonObject(with: raw) as! [String: Any]
        return (obj["profiles"] as! [String: [String: Any]]).mapValues { d in
            var p = Profile()
            p.summary = d["summary"] as? String ?? ""
            p.skills = d["skills"] as? [String] ?? []
            p.certifications = d["certifications"] as? [String] ?? []
            p.experience = (d["experience"] as? [[String: Any]] ?? []).map {
                WorkExperience(title: $0["title"] as? String ?? "", company: $0["company"] as? String ?? "",
                               startDate: $0["start_date"] as? String ?? "", endDate: $0["end_date"] as? String ?? "",
                               bullets: $0["bullets"] as? [String] ?? [])
            }
            p.education = (d["education"] as? [[String: Any]] ?? []).map {
                Education(degree: $0["degree"] as? String ?? "", school: $0["school"] as? String ?? "", year: $0["year"] as? String ?? "")
            }
            return p
        }
    }()

    static func job(_ key: String) -> Job {
        let j = file.jobs[key]!
        return Job(from: NormalizedJob(source: "demo", externalId: "g-\(key)", title: j.title, company: "Fixture",
                                       location: "", description: j.description))
    }

    static func engine(counting calls: CallCounter? = nil) throws -> QuickMatch {
        let emb = file.embeddings
        return try QuickMatch(data: file.data) { texts in
            calls?.add(texts.count)
            return texts.map { emb[$0]! }
        }
    }
}

final class CallCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    func add(_ k: Int) { lock.lock(); n += k; lock.unlock() }
    var count: Int { lock.lock(); defer { lock.unlock() }; return n }
}

final class QuickMatchTests: XCTestCase {
    /// Same lines, per-line P(met) and raw job score as laya-bench triage_eval and backend/nli/triage.py.
    func testGoldenMatchesTheBench() throws {
        let q = try TriageGolden.engine()
        for c in TriageGolden.file.cases {
            let j = TriageGolden.file.jobs[c.job]!
            let ev = try q.evaluate(title: j.title, description: j.description,
                                    profile: TriageGolden.profiles[c.profile]!, today: TriageGolden.today)
            guard let want = c.score_raw else { XCTAssertNil(ev, "\(c.profile)-\(c.job)"); continue }
            let got = try XCTUnwrap(ev, "\(c.profile)-\(c.job)")
            XCTAssertEqual(got.lines, c.lines, "\(c.profile)-\(c.job)")
            XCTAssertEqual(got.preview, c.preview ?? false, "\(c.profile)-\(c.job)")
            for (a, b) in zip(got.p, c.p!) { XCTAssertEqual(a, b, accuracy: 1e-4, "\(c.profile)-\(c.job)") }
            XCTAssertEqual(got.raw, want, accuracy: 0.01, "\(c.profile)-\(c.job)")
        }
    }

    func testScoreBucketReportAndClamp() throws {
        let q = try TriageGolden.engine()
        let b = TriageGolden.file.data.buckets
        for c in TriageGolden.file.cases {
            let r = try q.fitScore(job: TriageGolden.job(c.job), profile: TriageGolden.profiles[c.profile]!, today: TriageGolden.today)
            guard let raw = c.score_raw else { XCTAssertNil(r); continue }
            let result = try XCTUnwrap(r)
            XCTAssertEqual(result.score, (min(100, max(0, raw)) * 10).rounded() / 10, accuracy: 0.051)
            let want = result.score >= b.great ? "Great" : result.score >= b.good ? "Good" : result.score >= b.possible ? "Possible" : "Poor"
            let prefix = c.preview == true ? "Quick match (preview only)" : "Quick match"
            XCTAssertTrue(result.reasoning.hasPrefix("\(prefix): \(want) fit"), result.reasoning)
            XCTAssertEqual(ScoreSource.previewOnly(matchReport: result.matchReportJSON), c.preview ?? false)
            let report = try JSONSerialization.jsonObject(with: Data(result.matchReportJSON!.utf8)) as! [String: Any]
            XCTAssertEqual(report["bucket"] as? String, want)
            let met = zip(c.lines, c.p!).filter { $0.1 > 0.5 }.map(\.0)
            XCTAssertEqual(Set(report["matched_skills"] as! [String]), Set(met.map { String($0.prefix(80)) }))
        }
        XCTAssertTrue(TriageGolden.file.cases.contains { ($0.score_raw ?? 0) > 100 })  // the clamp is exercised
        XCTAssertTrue(TriageGolden.file.cases.contains { $0.preview == true })  // preview-only scoring is exercised
    }

    /// Same as test_preview_lines_when_there_are_no_requirement_lines (triage.job_lines).
    func testPreviewLinesWhenThereAreNoRequirementLines() {
        let d = TriageGolden.file.jobs["preview"]!.description
        XCTAssertEqual(LocalNLI.requirementLines(d), [])
        let (lines, preview) = QuickMatch.jobLines(d)
        XCTAssertTrue(preview)
        XCTAssertEqual(lines.count, 4)
        XCTAssertTrue(lines.allSatisfy { (25...300).contains($0.unicodeScalars.count) })
        XCTAssertEqual(QuickMatch.jobLines("Make great coffee. Smile a lot.").lines, ["Make great coffee. Smile a lot."])
        XCTAssertEqual(QuickMatch.jobLines(String(repeating: "x", count: 400)).lines, [String(repeating: "x", count: 300)])
        XCTAssertEqual(QuickMatch.jobLines(String(repeating: "• One two three four five six seven\n", count: 30)).lines,
                       Array(repeating: "One two three four five six seven", count: 20))
        let bi = TriageGolden.file.jobs["bi"]!.description
        XCTAssertEqual(QuickMatch.jobLines(bi).lines, LocalNLI.requirementLines(bi))
        XCTAssertFalse(QuickMatch.jobLines(bi).preview)
        for empty in ["", "   \n ", "Now hiring!"] {
            XCTAssertEqual(QuickMatch.jobLines(empty).lines, [])
            XCTAssertFalse(QuickMatch.jobLines(empty).preview)
        }
    }

    func testEmbeddingsAreCachedPerText() throws {
        let calls = CallCounter()
        let q = try TriageGolden.engine(counting: calls)
        let prof = TriageGolden.profiles["analyst"]!
        _ = try q.fitScore(job: TriageGolden.job("bi"), profile: prof, today: TriageGolden.today)
        let first = calls.count
        _ = try q.fitScore(job: TriageGolden.job("bi"), profile: prof, today: TriageGolden.today)
        XCTAssertEqual(calls.count, first)
        _ = try q.fitScore(job: TriageGolden.job("sre"), profile: prof, today: TriageGolden.today)
        XCTAssertEqual(calls.count - first, TriageGolden.file.cases.first { $0.profile == "analyst" && $0.job == "sre" }!.lines.count)
    }

    /// Token ids identical to HF tokenizers on every fixture text (+ accents, CJK, emoji, >100-char word, truncation).
    func testTokenizerMatchesPython() throws {
        try checkTokenizer(BertTokenizer(vocab: TriageGolden.file.vocab))
    }

    func checkTokenizer(_ tok: BertTokenizer) throws {
        let bad = TriageGolden.file.ids.filter { tok.encode($0.key, maxLength: 64) != $0.value }
            .map { "\($0.key.prefix(50)): swift \(tok.encode($0.key, maxLength: 64).prefix(20)) python \($0.value.prefix(20))" }
        XCTAssertEqual(bad, [])
        XCTAssertGreaterThan(TriageGolden.file.ids.count, 60)
    }

    // MARK: - Routing

    private var picked: AppConfig {
        var c = AppConfig()
        c.ai.nliBetaEnabled = true
        c.ai.fastModel = AIConfig.localMatchModelID
        return c
    }

    private func llm() -> MockAIEngine {
        let e = MockAIEngine()
        e.register("career advisor", .text(#"{"score": 42, "reasoning": "LLM"}"#))
        return e
    }

    private func source(_ r: FitResult) -> ScoreSource? { ScoreSource.of(matchReport: r.matchReportJSON, reasoning: r.reasoning) }

    func testLocalMatchModelScoresWithQuickMatchAndNoLLM() async throws {
        let engine = llm(), q = try TriageGolden.engine()
        let nliCalls = CallCounter()
        let r = try await ScoringService.score(job: TriageGolden.job("sre"), profile: TriageGolden.profiles["devops"]!,
                                               config: picked, engine: engine,
                                               nli: { _ in nliCalls.add(1); return FakeNLI(fixed: 0.9) }, quick: { _ in q })
        XCTAssertEqual(source(r), .quickMatch)
        XCTAssertEqual(source(r)?.label, "Quick match")
        XCTAssertEqual(engine.requests.count, 0)
        XCTAssertEqual(nliCalls.count, 0)
        XCTAssertNotNil(ScoreSource.seconds(matchReport: r.matchReportJSON))
    }

    func testWithoutQuickMatchTheDetailedModelThenTheLLMScore() async throws {
        let viaNLI = try await ScoringService.score(job: TriageGolden.job("sre"), profile: TriageGolden.profiles["devops"]!,
                                                    config: picked, engine: llm(), nli: { _ in FakeNLI(fixed: 0.9) }, quick: { _ in nil })
        XCTAssertEqual(source(viaNLI), .localModel)
        let engine = llm()
        let viaLLM = try await ScoringService.score(job: TriageGolden.job("blank"), profile: TriageGolden.profiles["devops"]!,
                                                    config: picked, engine: engine, nli: { _ in nil },
                                                    quick: { _ in try? TriageGolden.engine() })
        XCTAssertEqual(viaLLM.score, 42)  // nothing to judge -> the LLM
        XCTAssertEqual(engine.requests.count, 1)
    }

    func testAPreviewIsScoredByQuickMatchAndMarked() async throws {
        let engine = llm(), q = try TriageGolden.engine()
        let r = try await ScoringService.score(job: TriageGolden.job("preview"), profile: TriageGolden.profiles["devops"]!,
                                               config: picked, engine: engine, nli: { _ in nil }, quick: { _ in q })
        XCTAssertEqual(source(r), .quickMatch)
        XCTAssertTrue(r.reasoning.hasPrefix("Quick match (preview only): "), r.reasoning)
        XCTAssertTrue(ScoreSource.previewOnly(matchReport: r.matchReportJSON))
        XCTAssertEqual(engine.requests.count, 0)
        let full = try await ScoringService.score(job: TriageGolden.job("sre"), profile: TriageGolden.profiles["devops"]!,
                                                  config: picked, engine: engine, nli: { _ in nil }, quick: { _ in q })
        XCTAssertFalse(ScoreSource.previewOnly(matchReport: full.matchReportJSON))
    }

    func testQuickMatchOnlyWhenTheLocalMatchModelIsPicked() async throws {
        var other = AppConfig(); other.ai.nliBetaEnabled = true; other.ai.fastModel = "qwen3"
        let none = await QuickMatchRuntime.live(other)
        XCTAssertNil(none)
        let r = try await ScoringService.score(job: TriageGolden.job("sre"), profile: TriageGolden.profiles["devops"]!,
                                               config: other, engine: llm(), nli: { _ in nil },
                                               quick: { _ in XCTFail("Quick match consulted"); return nil })
        XCTAssertEqual(r.score, 42)
    }

    func testPlannedSource() {
        XCTAssertEqual(ScoreSource.planned(config: picked, localReady: true, quickReady: true), .quickMatch)
        XCTAssertEqual(ScoreSource.planned(config: picked, localReady: true, quickReady: false), .localModel)
        var off = picked; off.ai.nliBetaEnabled = false
        XCTAssertNotEqual(ScoreSource.planned(config: off, localReady: true, quickReady: true), .quickMatch)
        XCTAssertEqual(ScoreSource.of(matchReport: #"{"scored_by":"triage"}"#, reasoning: nil), .quickMatch)
    }

    func testRefineRescoresTheTopShareWithTheDetailedModel() async {
        let jobs = (0..<20).map { i in (job: TriageGolden.job("sre").renamed("job\(i)"), score: Double(i)) }
        var on = picked; on.ai.triageRefine = true
        let out = await ScoringService.refineTop(jobs, profile: TriageGolden.profiles["devops"]!, config: on,
                                                 nli: { _ in FakeNLI(fixed: 0.9) })
        XCTAssertEqual(out.map(\.job.title), ["job19", "job18", "job17"])  // ceil(15% of 20)
        XCTAssertTrue(out.allSatisfy { source($0.result) == .localModel && abs($0.result.score - 90) < 0.1 })
        let off = await ScoringService.refineTop(jobs, profile: Profile(), config: picked, nli: { _ in FakeNLI(fixed: 0.9) })
        XCTAssertTrue(off.isEmpty, "off by default")
        let noModel = await ScoringService.refineTop(jobs, profile: Profile(), config: on, nli: { _ in nil })
        XCTAssertTrue(noModel.isEmpty, "needs the detailed model")
    }

    func testRefineSkipsPreviewOnlyJobs() async {
        let previews = (0..<10).map { i in (job: TriageGolden.job("preview").renamed("p\(i)"), score: 99.0) }
        let jobs = (0..<20).map { i in (job: TriageGolden.job("sre").renamed("job\(i)"), score: Double(i)) }
        var on = picked; on.ai.triageRefine = true
        let out = await ScoringService.refineTop(previews + jobs, profile: TriageGolden.profiles["devops"]!, config: on,
                                                 nli: { _ in FakeNLI(fixed: 0.9) })
        XCTAssertEqual(out.map(\.job.title), ["job19", "job18", "job17"])
    }

    func testConfigRoundTripsTheNewSettings() throws {
        var c = AppConfig(); c.ai.triageRefine = true; c.ai.triageUseNeuralEngine = true
        let back = try JSONDecoder().decode(AppConfig.self, from: JSONEncoder().encode(c))
        XCTAssertTrue(back.ai.triageRefine && back.ai.triageUseNeuralEngine)
        XCTAssertFalse(AIConfig().triageRefine || AIConfig().triageUseNeuralEngine)  // off by default, CPU by default
    }

    // MARK: - Real model (env-gated)

    private func env(_ k: String) -> String? { ProcessInfo.processInfo.environment[k].flatMap { $0.isEmpty ? nil : $0 } }

    /// The release files in the simulator: tokenizer parity (full vocab), Core ML scores vs the
    /// golden, and the time to score 300 jobs (every line new; profile embedded once). CPU only.
    func testRealModelParityAndTiming() throws {
        guard let path = env("TRIAGE_MODEL_DIR") else { throw XCTSkip("set TEST_RUNNER_TRIAGE_MODEL_DIR") }
        let dir = URL(fileURLWithPath: path)
        try checkTokenizer(BertTokenizer(contentsOf: dir.appendingPathComponent(QuickMatchModel.tokenizerFile)))
        let t0 = Date()
        let q = try QuickMatchModel.load(directory: dir, neuralEngine: false)
        let load = Date().timeIntervalSince(t0)
        var maxDiff = 0.0
        for c in TriageGolden.file.cases {
            guard let want = c.score_raw else { continue }
            let j = TriageGolden.file.jobs[c.job]!
            let got = try XCTUnwrap(q.evaluate(title: j.title, description: j.description,
                                               profile: TriageGolden.profiles[c.profile]!, today: TriageGolden.today))
            maxDiff = max(maxDiff, abs(got.raw - want))
        }
        XCTAssertLessThan(maxDiff, 1.0, "Core ML fp16 vs golden job score")
        let prof = TriageGolden.profiles["analyst"]!
        _ = try q.fitScore(job: TriageGolden.job("bi"), profile: prof, today: TriageGolden.today)  // profile chunks once
        let keys = ["bi", "sre", "nurse", "fe"]
        let jobs = (0..<300).map { i -> Job in
            let d = TriageGolden.file.jobs[keys[i % 4]]!.description.replacingOccurrences(of: ". ", with: ".\n")
                .components(separatedBy: "\n").map { $0.count > 20 ? "\($0) (team \(i))" : $0 }.joined(separator: "\n")
            return TriageGolden.job(keys[i % 4]).redescribed(d)
        }
        let t1 = Date()
        var lines = 0
        for j in jobs {
            lines += LocalNLI.requirementLines(j.description).count
            _ = try q.fitScore(job: j, profile: prof, today: TriageGolden.today)
        }
        let el = Date().timeIntervalSince(t1)
        print(String(format: "Quick match simulator (CPU): load %.2fs; golden max |raw diff| %.3f; 300 jobs %.2fs = %.1f ms/job (%d lines)",
                     load, maxDiff, el, 1000 * el / 300, lines))
        XCTAssertLessThan(el, 60, "300 jobs under a minute")
    }
}

// MARK: - Download (stubbed network)

@MainActor
final class QuickMatchStoreTests: XCTestCase {
    private var savedRoot: URL!

    override func setUp() async throws {
        savedRoot = QuickMatchModel.root
        QuickMatchModel.root = FileManager.default.temporaryDirectory.appendingPathComponent("qm-\(UUID().uuidString)")
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: QuickMatchModel.root)
        QuickMatchModel.root = savedRoot
    }

    func testShippedPinsAndLocation() {
        XCTAssertEqual(QuickMatchModel.baseURL.absoluteString, "https://github.com/TheDevRo/Jobsmith/releases/download/triage-model-v1")
        XCTAssertEqual(QuickMatchModel.files.map(\.name),
                       ["triage-bge-small-en-v1.5-fp16-64.mlmodelc.zip", "tokenizer.json", "triage-data.json"])
        XCTAssertTrue(QuickMatchModel.files.allSatisfy { $0.sha256.count == 64 && $0.size > 0 })
    }

    func testInstallUnzipsAndDeletes() async throws {
        let src = FileManager.default.temporaryDirectory.appendingPathComponent("src-\(UUID().uuidString)")
        let pkg = src.appendingPathComponent("Triage.mlmodelc")
        try FileManager.default.createDirectory(at: pkg, withIntermediateDirectories: true)
        try Data(repeating: 3, count: 50_000).write(to: pkg.appendingPathComponent("weights.bin"))
        let zip = src.appendingPathComponent("m.zip")
        try FileManager.default.zipItem(at: pkg, to: zip)
        let bodies = ["triage-bge-small-en-v1.5-fp16-64.mlmodelc.zip": try Data(contentsOf: zip),
                      "tokenizer.json": Data(#"{"tok": 1}"#.utf8), "triage-data.json": Data("{}".utf8)]
        StubServer.files = bodies
        StubServer.failAfter = nil
        func sha(_ d: Data) throws -> String {
            let u = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try d.write(to: u)
            return try NLIModelStore.sha256(u)
        }
        let files = try QuickMatchModel.files.map { NLIModel.File(name: $0.name, size: Int64(bodies[$0.name]!.count), sha256: try sha(bodies[$0.name]!)) }
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [StubServer.self]
        let s = NLIModelStore(session: URLSession(configuration: cfg), baseURL: URL(string: "https://models.invalid/q")!,
                              files: files, model: QuickMatchModel.spec)
        XCTAssertEqual(s.state, .notInstalled)
        s.install()
        let t = Date()
        while s.isDownloading && Date().timeIntervalSince(t) < 20 { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertEqual(s.state, .ready)
        let names = try FileManager.default.contentsOfDirectory(atPath: QuickMatchModel.directory.path).sorted()
        XCTAssertEqual(names, ["Triage.mlmodelc", "tokenizer.json", "triage-data.json"])
        XCTAssertFalse(NLIModel.isInstalled && NLIModel.root == QuickMatchModel.root, "separate from the NLI model")
        await s.delete()
        XCTAssertEqual(s.state, .notInstalled)
        XCTAssertFalse(FileManager.default.fileExists(atPath: QuickMatchModel.root.path))
    }
}

private extension Job {
    func renamed(_ t: String) -> Job { var j = self; j.title = t; return j }
    func redescribed(_ d: String) -> Job { var j = self; j.description = d; return j }
}
