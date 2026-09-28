import Accelerate
import CoreML
import Foundation

/// Quick match: job-fit triage from a small sentence-embedding model (bge-small), no LLM and no
/// NLI, fast enough to score every job. Swift twin of `backend/nli/triage.py` (the laya-bench
/// line model), pinned by the shared golden fixture (`triage_golden.json`). Each requirement
/// line gets P(met) from a logistic on its best / top-3 cosine to the profile's chunks, how much
/// better that is than reference people (synthetic), its cosine to soft-skill prototypes, a
/// named profile skill, and "N+ years" vs the profile's years; the job score is a weighted sum of
/// mean P(met) and skill overlap, clamped to 0-100, plus a bucket. Weights, thresholds, aliases
/// and prototype/reference embeddings come from the downloaded data file, so another embedding
/// model is a data swap, not a code change.
public final class QuickMatch: @unchecked Sendable {
    public struct Weights: Decodable, Sendable {
        public struct Linear: Decodable, Sendable {
            let cols: [String]
            let coef: [Double]?
            let weights: [Double]?
            let intercept: Double
            var w: [Double] { coef ?? weights ?? [] }
        }
        public struct Buckets: Decodable, Sendable { let possible, good, great: Double }
        let seq: Int
        let line: Linear
        let job: Linear
        let buckets: Buckets
        let aliases: [String: [String]]
        let soft: [[Double]]
        let ref: [[Double]]
    }

    /// Text -> unit vector (the model has pooling + L2 norm inside).
    public typealias Embed = @Sendable ([String]) throws -> [[Double]]

    static let lineCols = ["max", "top3", "rel", "soft", "skill", "has_years", "years_ratio"]
    static let jobCols: Set<String> = ["cov_line", "skill_lines", "skill_count", "title"]
    public static let buckets = ["Poor", "Possible", "Good", "Great"]
    public static let reasoningPrefix = "Quick match"
    static let yearsRe = Extractive.rx(#"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*years?"#)

    let data: Weights
    private let embed: Embed
    private let lineIx: [Int]
    private var cache: [String: [Double]] = [:]
    private var termRe: [String: NSRegularExpression] = [:]
    private let lock = NSLock()

    public init(data: Weights, embed: @escaping Embed) throws {
        for c in data.job.cols where !Self.jobCols.contains(c) {
            throw CocoaError(.coderInvalidValue, userInfo: [NSDebugDescriptionErrorKey: "unknown job feature \(c)"])
        }
        let ix = data.line.cols.compactMap { Self.lineCols.firstIndex(of: $0) }
        guard ix.count == data.line.cols.count, data.line.w.count == ix.count, data.job.w.count == data.job.cols.count else {
            throw CocoaError(.coderInvalidValue, userInfo: [NSDebugDescriptionErrorKey: "malformed triage data"])
        }
        self.data = data; self.embed = embed; lineIx = ix
    }

    /// Embeddings, cached per text: a profile's chunks once, each job's lines and title once.
    func vectors(_ texts: [String]) throws -> [[Double]] {
        lock.lock(); defer { lock.unlock() }
        var seen = Set<String>()
        let todo = texts.filter { cache[$0] == nil && seen.insert($0).inserted }
        if !todo.isEmpty {
            if cache.count > 50_000 { cache.removeAll() }  // ponytail: wholesale reset, an LRU if long sessions need one
            let vs = try embed(todo)
            guard vs.count == todo.count else { throw CocoaError(.coderValueNotFound) }
            for (t, v) in zip(todo, vs) { cache[t] = v }
        }
        return texts.map { cache[$0]! }
    }

    // MARK: - Features (1:1 with triage.py)

    static func chunks(_ p: Profile) -> [String] {
        var out = p.skills
        for r in p.experience {
            if !r.title.isEmpty { out.append("\(r.title) at \(r.company)") }
            out += r.bullets.filter { !$0.isEmpty }
        }
        out += Extractive.split(p.summary, Extractive.rx(#"(?<=[.!?])\s+"#))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { $0.unicodeScalars.count > 2 }
        out += p.education.filter { !$0.degree.isEmpty }
            .map { "\($0.degree) \($0.school)".trimmingCharacters(in: .whitespacesAndNewlines) }
        return out + p.certifications
    }

    func skillTerms(_ p: Profile) -> [(skill: String, terms: [String])] {
        p.skills.compactMap { raw in
            let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            let terms = ([s.lowercased()] + (data.aliases[s.lowercased()] ?? [])).filter { $0.unicodeScalars.count >= 2 }
            return terms.isEmpty ? nil : (s, terms)
        }
    }

    /// `term` in lowercased `text` as a whole word (a trailing + or # makes a different word: C vs C++).
    func has(_ text: String, _ term: String) -> Bool {
        lock.lock()
        let re = termRe[term] ?? Extractive.rx("(?<![a-z0-9])" + NSRegularExpression.escapedPattern(for: term)
                                               + "(?![a-z0-9+#])", caseInsensitive: false)
        termRe[term] = re
        lock.unlock()
        return Extractive.search(re, text)
    }

    static func dot(_ a: [Double], _ b: [Double]) -> Double { vDSP.dot(a, b) }

    /// [lines x lineCols] (bench `_lf`).
    func lineFeatures(_ p: Profile, lines: [String], today: Extractive.Today) throws -> [[Double]] {
        let C = try vectors(Self.chunks(p)), Lv = try vectors(lines)
        let sk = skillTerms(p)
        return lines.enumerated().map { i, line in
            let sims = C.map { Self.dot(Lv[i], $0) }.sorted(by: >)
            let low = line.lowercased()
            let hit = sk.filter { s in s.terms.contains { has(low, $0) } }.map(\.skill)
            var hy = 0.0, yr = 0.0
            if let m = Extractive.match(Self.yearsRe, line), let n = Int(m[1] ?? ""), (1...20).contains(n) {
                hy = 1
                yr = min(1, (Extractive.yearsWith(p, hit.isEmpty ? nil : hit, today) ?? 0) / Double(n))
            }
            let refMax = data.ref.map { Self.dot(Lv[i], $0) }.max() ?? 0
            let softMax = data.soft.map { Self.dot(Lv[i], $0) }.max() ?? 0
            return [sims[0], sims.prefix(3).reduce(0, +) / Double(min(3, sims.count)), sims[0] - refMax, softMax,
                    hit.isEmpty ? 0 : 1, hy, yr]
        }
    }

    /// (lines to judge, preview only). The requirement lines; when there are none (a short feed
    /// preview, e.g. Adzuna's 500 chars), every 25-300 char line, else the whole text (first 300
    /// chars) as one line when it is at least 25 chars. ([], false) when there is no usable text.
    static func jobLines(_ description: String) -> (lines: [String], preview: Bool) {
        let req = LocalNLI.requirementLines(description)
        if !req.isEmpty { return (req, false) }
        var lines = Array(LocalNLI.candidateLines(description).prefix(LocalNLI.maxLines))
        let text = description.trimmingCharacters(in: .whitespacesAndNewlines)
        if lines.isEmpty, text.unicodeScalars.count >= 25 {
            lines = [String(String.UnicodeScalarView(text.unicodeScalars.prefix(300)))]
        }
        return (lines, !lines.isEmpty)
    }

    /// (lines, P(met) per line, raw job score, preview only), or nil when there is nothing to judge.
    func evaluate(title: String, description: String, profile p: Profile,
                  today: Extractive.Today) throws -> (lines: [String], p: [Double], raw: Double, preview: Bool)? {
        let (lines, preview) = Self.jobLines(description)
        guard !lines.isEmpty, !Self.chunks(p).isEmpty else { return nil }
        let lf = try lineFeatures(p, lines: lines, today: today)
        let pm = lf.map { row in
            1 / (1 + exp(-(zip(lineIx, data.line.w).reduce(data.line.intercept) { $0 + row[$1.0] * $1.1 })))
        }
        let sk = skillTerms(p)
        let desc = description.lowercased()
        var feats: [String: Double] = [
            "cov_line": pm.reduce(0, +) / Double(pm.count),
            "skill_lines": lf.map { $0[4] }.reduce(0, +) / Double(lf.count),
            "skill_count": min(1, Double(sk.filter { s in s.terms.contains { has(desc, $0) } }.count)
                                   / Double(min(8, max(1, sk.count)))),
        ]
        if data.job.cols.contains("title") {
            let titles = p.experience.map(\.title).filter { !$0.isEmpty }
            let tv = try vectors([title])[0]
            feats["title"] = try titles.isEmpty ? 0 : vectors(titles).map { Self.dot(tv, $0) }.max() ?? 0
        }
        let raw = zip(data.job.cols, data.job.w).reduce(data.job.intercept) { $0 + feats[$1.0]! * $1.1 }
        return (lines, pm, raw, preview)
    }

    /// The fit result, or nil when the posting has no usable text (or the profile is empty).
    public func fitScore(job: Job, profile: Profile) throws -> FitResult? {
        try fitScore(job: job, profile: profile, today: Extractive.Today(Date()))
    }

    func fitScore(job: Job, profile: Profile, today: Extractive.Today) throws -> FitResult? {
        guard let ev = try evaluate(title: job.title, description: job.description,
                                    profile: profile, today: today) else { return nil }
        let (lines, pm, raw, preview) = ev
        let score = (min(100, max(0, raw)) * 10).rounded(.toNearestOrEven) / 10
        let b = data.buckets
        let bucket = Self.buckets[[b.possible, b.good, b.great].filter { score >= $0 }.count]
        let order = pm.indices.sorted { pm[$0] != pm[$1] ? pm[$0] > pm[$1] : $0 < $1 }
        let met = order.filter { pm[$0] > 0.5 }.map { lines[$0] }
        let missing = order.reversed().filter { pm[$0] <= 0.5 }.map { lines[$0] }
        let report: [String: Any] = ["matched_skills": met, "missing_skills": missing, "keywords": [String]()]
        let reasoning = preview
            ? "\(Self.reasoningPrefix) (preview only): \(bucket) fit, meets about \(met.count) of \(lines.count) preview lines."
            : "\(Self.reasoningPrefix): \(bucket) fit, meets about \(met.count) of \(lines.count) requirement lines."
        return FitResult(score: score, reasoning: reasoning,
                         matchReportJSON: ScoreResponseParser.sanitizedMatchReportJSON(report))
            .adding(preview ? ["bucket": bucket, "preview": true] : ["bucket": bucket])
    }
}

/// The Quick match model files: a model-only GitHub pre-release, pinned by size + SHA-256, in
/// Application Support next to the NLI model (never the App Group).
public enum QuickMatchModel {
    /// bge-small-en-v1.5 (BAAI, MIT), Core ML fp16, CLS pooling + L2 norm inside, one fixed input
    /// length (64 tokens), compiled and zipped; its tokenizer; the triage weights + embeddings.
    static let revision = "bge-small-en-v1.5-triage-v1"
    public static let files = [
        NLIModel.File(name: "triage-bge-small-en-v1.5-fp16-64.mlmodelc.zip", size: 60_671_981,
                      sha256: "729556bdbbc86ef84763de71987bb240fab4fede261cafee2053e2cde1c17656"),
        NLIModel.File(name: "tokenizer.json", size: 711_396,
                      sha256: "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"),
        NLIModel.File(name: "triage-data.json", size: 274_937,
                      sha256: "f757b61f69af1c178a712bfcdf4ae2a44c50cc238f0cc87f132176b9ce1c7468"),
    ]
    public static let sizeBytes = files.reduce(0) { $0 + $1.size }
    public static let baseURL = URL(string: "https://github.com/TheDevRo/Jobsmith/releases/download/triage-model-v1")!
    static let modelDirName = "Triage.mlmodelc", tokenizerFile = "tokenizer.json", dataFile = "triage-data.json"

    /// Parent of every revision. Tests point it at a temp directory.
    nonisolated(unsafe) static var root: URL = FileManager.default
        .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("models/triage", isDirectory: true)

    static var spec: LocalModelSpec {
        LocalModelSpec(root: root, revision: revision, files: files, baseURL: baseURL, modelDirName: modelDirName,
                       unload: { await QuickMatchRuntime.shared.unload() })
    }
    static var directory: URL { spec.directory }
    public static var isInstalled: Bool { spec.isInstalled }

    /// Loads the engine from an installed directory (tests: any directory holding the three files).
    static func load(directory: URL, neuralEngine: Bool) throws -> QuickMatch {
        let data = try JSONDecoder().decode(QuickMatch.Weights.self,
                                            from: Data(contentsOf: directory.appendingPathComponent(dataFile)))
        let embedder = try CoreMLEmbedder(directory: directory, seq: data.seq, neuralEngine: neuralEngine)
        return try QuickMatch(data: data) { try embedder.embed($0) }
    }
}

/// The Core ML embedding model: `ids` [1, seq] int32 ([PAD] = 0, mask derived inside) -> `emb`.
final class CoreMLEmbedder: @unchecked Sendable {
    private let model: MLModel
    private let tokenizer: BertTokenizer
    private let seq: Int
    private let lock = NSLock()

    init(directory: URL, seq: Int, neuralEngine: Bool) throws {
        tokenizer = try BertTokenizer(contentsOf: directory.appendingPathComponent(QuickMatchModel.tokenizerFile))
        let config = MLModelConfiguration()
        // CPU by default (as for the NLI model: GPU aborted on an A17); the Neural Engine is an
        // advanced opt-in until it has run on a real iPhone.
        config.computeUnits = neuralEngine ? .cpuAndNeuralEngine : .cpuOnly
        model = try MLModel(contentsOf: directory.appendingPathComponent(QuickMatchModel.modelDirName), configuration: config)
        self.seq = seq
    }

    func embed(_ texts: [String]) throws -> [[Double]] {
        lock.lock(); defer { lock.unlock() }
        guard !texts.isEmpty else { return [] }
        let inputs: [MLFeatureProvider] = try texts.map { text in
            let ids = tokenizer.encode(text, maxLength: seq)
            let input = try MLMultiArray(shape: [1, NSNumber(value: seq)], dataType: .int32)
            let ptr = input.dataPointer.bindMemory(to: Int32.self, capacity: seq)
            for i in 0..<seq { ptr[i] = i < ids.count ? ids[i] : 0 }
            return try MLDictionaryFeatureProvider(dictionary: ["ids": input])
        }
        // One batch call: Core ML spreads it across CPU cores (as CoreMLNLI does).
        let outs = try model.predictions(fromBatch: MLArrayBatchProvider(array: inputs))
        return try (0..<outs.count).map { k in
            guard let v = outs.features(at: k).featureValue(for: "emb")?.multiArrayValue else {
                throw CocoaError(.coderValueNotFound)
            }
            if v.dataType == .float32 {
                let p = v.dataPointer.bindMemory(to: Float.self, capacity: v.count)
                return (0..<v.count).map { Double(p[$0]) }
            }
            return (0..<v.count).map { v[$0].doubleValue }
        }
    }
}

/// The process-wide Quick match engine (loading takes a moment; the embedding cache lives on it).
public actor QuickMatchRuntime {
    public static let shared = QuickMatchRuntime()
    private var cached: QuickMatch?
    private var cachedOnNeuralEngine = false
    public private(set) var lastLoadError: String?

    public func engine(neuralEngine: Bool = false) -> QuickMatch? {
        if let cached, cachedOnNeuralEngine == neuralEngine { return cached }
        cached = nil
        guard QuickMatchModel.isInstalled else { return nil }
        do {
            cached = try QuickMatchModel.load(directory: QuickMatchModel.directory, neuralEngine: neuralEngine)
            cachedOnNeuralEngine = neuralEngine
            lastLoadError = nil
        } catch {
            lastLoadError = error.localizedDescription
            NSLog("Quick match model failed to load; using the next scorer instead: \(error)")
        }
        return cached
    }

    public func unload() { cached = nil; lastLoadError = nil }

    /// How scoring gets the engine: nil unless the local match model is picked (and switched on)
    /// and Quick match is installed and loads. Tests pass fakes.
    public static let live: QuickMatchProvider = { config in
        guard ScoreSource.prefersLocal(config) else { return nil }
        return await shared.engine(neuralEngine: config.ai.triageUseNeuralEngine)
    }
}

public typealias QuickMatchProvider = @Sendable (AppConfig) async -> QuickMatch?
