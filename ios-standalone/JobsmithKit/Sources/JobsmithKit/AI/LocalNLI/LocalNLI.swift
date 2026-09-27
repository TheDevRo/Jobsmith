import Foundation

/// One (premise, hypothesis) pair for the NLI model.
public struct NLIPair: Hashable, Sendable {
    public let premise: String
    public let hypothesis: String
    public init(_ premise: String, _ hypothesis: String) {
        self.premise = premise; self.hypothesis = hypothesis
    }
}

/// An NLI model: [P(entailment), P(neutral), P(contradiction)] per pair.
public protocol NLIScorer: Sendable {
    func probs(_ pairs: [NLIPair]) throws -> [[Double]]
    /// Untruncated pair length in tokens, when the scorer can tell (long fit premises get split).
    func countTokens(_ pair: NLIPair) -> Int?
}

public extension NLIScorer {
    func entail(_ pairs: [NLIPair]) throws -> [Double] { try probs(pairs).map { $0[0] } }
    func countTokens(_ pair: NLIPair) -> Int? { nil }
}

/// "Local AI model (beta)": one on-device NLI model behind one Settings switch
/// (`AIConfig.nliBetaEnabled`, default off). On, it answers Apply Assist's
/// leftover fields extractively (`Extractive`) and scores jobs when the
/// scoring LLM is unavailable (`LocalNLI.fitScore`). Off, nothing here runs:
/// callers check `enabled` first, and the model is only ever loaded through
/// `live`, which checks it again.
public enum LocalNLI {
    public static func enabled(_ config: AppConfig) -> Bool { config.ai.nliBetaEnabled }

    /// How callers get the loaded model (nil = not enabled, not installed, or won't load).
    /// Tests pass fakes; the app uses `live`.
    public typealias Provider = @Sendable (AppConfig) async -> (any NLIScorer)?

    public static let live: Provider = { config in
        guard enabled(config) else { return nil }
        return await NLIRuntime.shared.scorer()
    }
}

/// The process-wide model instance: loading takes seconds, so it is cached,
/// and one inference runs at a time (the GPU is already saturated by one).
public actor NLIRuntime {
    public static let shared = NLIRuntime()
    private var cached: CoreMLNLI?
    /// Times the model was actually loaded (the off-mode test asserts 0).
    public private(set) var loads = 0

    public func scorer() -> CoreMLNLI? {
        if let cached { return cached }
        guard NLIModel.isInstalled else { return nil }
        do {
            loads += 1
            cached = try CoreMLNLI(directory: NLIModel.directory)
        } catch {
            // A broken model must never break the LLM path.
            NSLog("Local AI model failed to load; using the LLM instead: \(error)")
        }
        return cached
    }

    public func unload() { cached = nil }
}
