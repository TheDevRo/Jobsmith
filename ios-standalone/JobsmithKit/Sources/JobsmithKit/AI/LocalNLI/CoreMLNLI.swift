import CoreML
import Foundation

/// The Core ML NLI scorer (DeBERTa-v3-large, int8 weights). Input `input_ids`
/// is one of the enumerated lengths below, [PAD]-filled; the attention mask is
/// derived inside the model (ids != 0). Output `logits`: entailment, neutral,
/// contradiction.
public final class CoreMLNLI: NLIScorer, @unchecked Sendable {
    static let lengths = [64, 128, 256, 512]
    private let model: MLModel
    private let tokenizer: DebertaTokenizer
    private let lock = NSLock()  // one prediction at a time

    public init(directory: URL) throws {
        let t0 = Date()
        tokenizer = try DebertaTokenizer(contentsOf: directory.appendingPathComponent(NLIModel.tokenizerFile))
        let config = MLModelConfiguration()
        // The Neural Engine path measured ~10x slower for this large model.
        config.computeUnits = .cpuAndGPU
        model = try MLModel(contentsOf: directory.appendingPathComponent(NLIModel.modelDirName), configuration: config)
        NSLog("Local AI model loaded in %.1fs", Date().timeIntervalSince(t0))
    }

    public func probs(_ pairs: [NLIPair]) throws -> [[Double]] {
        lock.lock()
        defer { lock.unlock() }
        return try pairs.map { pair in
            let ids = tokenizer.encodePair(pair.premise, pair.hypothesis, maxLength: Self.lengths.last!)
            let length = Self.lengths.first { $0 >= ids.count }!
            let input = try MLMultiArray(shape: [1, NSNumber(value: length)], dataType: .int32)
            let ptr = input.dataPointer.bindMemory(to: Int32.self, capacity: length)
            for i in 0..<length { ptr[i] = i < ids.count ? ids[i] : 0 }
            let out = try model.prediction(from: MLDictionaryFeatureProvider(dictionary: ["input_ids": input]))
            guard let logits = out.featureValue(for: "logits")?.multiArrayValue, logits.count == 3 else {
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
