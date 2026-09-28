import CoreML
import Foundation

/// The Core ML NLI scorer (DeBERTa-v3-large, int8 weights, fp32 compute). Input `input_ids`
/// has one fixed length, [PAD]-filled (longer pairs are truncated premise-first); the
/// attention mask is derived inside the model (ids != 0). Output `logits`: entailment,
/// neutral, contradiction.
public final class CoreMLNLI: NLIScorer, @unchecked Sendable {
    /// Fit premises are built to fit it (`LocalNLI.maxPairTokens`); Apply Assist pairs are shorter.
    static let length = LocalNLI.maxPairTokens
    private let model: MLModel
    private let tokenizer: DebertaTokenizer
    private let lock = NSLock()  // one prediction at a time

    public init(directory: URL, neuralEngine: Bool = false) throws {
        let t0 = Date()
        tokenizer = try DebertaTokenizer(contentsOf: directory.appendingPathComponent(NLIModel.tokenizerFile))
        let config = MLModelConfiguration()
        // CPU only. On an iPhone 15 Pro (A17) the GPU path aborts inside Metal's graph compiler
        // on the first prediction (TestFlight build 44) — an abort the app can't catch — though it
        // runs on an M-series Mac GPU. fp16 on the CPU: 174 ms/pair on an M3 Pro, loads in 0.1 s;
        // the Neural Engine was slower (509 ms) and can't run fp32 at all.
        // The Neural Engine is opt-in (Settings) while its speed on iPhone is measured.
        config.computeUnits = neuralEngine ? .cpuAndNeuralEngine : .cpuOnly
        model = try MLModel(contentsOf: directory.appendingPathComponent(NLIModel.modelDirName), configuration: config)
        NSLog("Local AI model loaded in %.1fs (%@)", Date().timeIntervalSince(t0), neuralEngine ? "Neural Engine" : "CPU")
    }

    public func probs(_ pairs: [NLIPair]) throws -> [[Double]] {
        lock.lock()
        defer { lock.unlock() }
        guard !pairs.isEmpty else { return [] }
        let length = Self.length
        let inputs: [MLFeatureProvider] = try pairs.map { pair in
            let ids = tokenizer.encodePair(pair.premise, pair.hypothesis, maxLength: length)
            let input = try MLMultiArray(shape: [1, NSNumber(value: length)], dataType: .int32)
            let ptr = input.dataPointer.bindMemory(to: Int32.self, capacity: length)
            for i in 0..<length { ptr[i] = i < ids.count ? ids[i] : 0 }
            return try MLDictionaryFeatureProvider(dictionary: ["input_ids": input])
        }
        // One batch call instead of one prediction per pair: on the CPU Core ML spreads a
        // batch across cores — 81 vs 184 ms/pair for 12 pairs on an M3 Pro, identical output.
        let outs = try model.predictions(fromBatch: MLArrayBatchProvider(array: inputs))
        return try (0..<outs.count).map { k in
            guard let logits = outs.features(at: k).featureValue(for: "logits")?.multiArrayValue, logits.count == 3 else {
                throw CocoaError(.coderValueNotFound)
            }
            return Self.softmax((0..<3).map { logits[$0].doubleValue })
        }
    }

    public func countTokens(_ pair: NLIPair) -> Int? { tokenizer.countTokens(pair.premise, pair.hypothesis) }

    /// Rounded to 5 places, like the desktop runtime, so near-ties break the same way.
    static func softmax(_ logits: [Double]) -> [Double] {
        let top = logits.max() ?? 0
        let e = logits.map { exp($0 - top) }
        let sum = e.reduce(0, +)
        return e.map { ($0 / sum * 100_000).rounded() / 100_000 }
    }
}
