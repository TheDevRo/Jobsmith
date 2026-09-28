import Foundation

/// One chat-completion call: optional system message plus the user prompt.
public struct CompletionRequest: Equatable, Sendable {
    public var system: String?
    public var user: String
    public var tier: ModelTier
    public var temperature: Double
    public var maxTokens: Int

    public init(system: String? = nil, user: String, tier: ModelTier,
                temperature: Double, maxTokens: Int) {
        self.system = system; self.user = user; self.tier = tier
        self.temperature = temperature; self.maxTokens = maxTokens
    }
}

/// Abstraction over the chat backend so the AI services can run against a
/// real OpenAI-compatible endpoint or a mock in tests.
public protocol AIEngine: Sendable {
    func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String
    func listModels(config: AIConfig) async throws -> [String]
    /// The Setup Assistant's real test: a 1-token chat completion to `model`.
    /// Throws the engine's typed error for `AIErrorMapper`.
    func pingChat(model: String, config: AIConfig) async throws
}

/// Result of the Settings connection probe (desktop `test_connection`).
public struct ConnectionStatus: Equatable, Sendable {
    public let connected: Bool
    public let models: [String]
    public let error: String?

    public init(connected: Bool, models: [String], error: String?) {
        self.connected = connected; self.models = models; self.error = error
    }
}

public extension AIEngine {
    /// Default ping: one tiny completion on the strong tier, pinned to `model`.
    func pingChat(model: String, config: AIConfig) async throws {
        var probe = config
        probe.strongModel = model
        _ = try await complete(CompletionRequest(user: "ping", tier: .strong, temperature: 0, maxTokens: 1),
                               config: probe)
    }

    /// Probe the models endpoint; never throws.
    func testConnection(config: AIConfig) async -> ConnectionStatus {
        do {
            let models = try await listModels(config: config)
            return ConnectionStatus(connected: true, models: models, error: nil)
        } catch {
            return ConnectionStatus(connected: false, models: [],
                                    error: error.localizedDescription)
        }
    }
}

/// Plain-English AI failures for the setup wizard and profile import. Twin of
/// desktop `ai_engine.describe_ai_error`: same codes, same wording.
public enum AIErrorMapper {
    public static func describe(_ error: Error, baseURL: String, onDevice: Bool = false) -> (code: String, message: String) {
        if onDevice {
            return ("unavailable", error.localizedDescription)
        }
        guard let e = error as? AIEngineError else {
            return ("error", error.localizedDescription)
        }
        switch e {
        case .invalidBaseURL(let url) where url.trimmingCharacters(in: .whitespaces).isEmpty:
            return ("no_url", "Enter the server address first")
        case .invalidBaseURL, .unreachable, .interrupted:
            let host = URL(string: baseURL)?.host ?? (baseURL.isEmpty ? "that address" : baseURL)
            return ("unreachable", "Could not reach the server at \(host)")
        case .httpStatus(let code, let body):
            let text = body.lowercased()
            if code == 401 || code == 403 { return ("auth", "That API key was rejected") }
            if code == 402 || text.contains("insufficient_quota") || text.contains("insufficient quota") {
                return ("credit", "Your provider account has no credit")
            }
            if code == 404 || text.contains("model_not_found")
                || (text.contains("model") && (text.contains("not found") || text.contains("does not exist"))) {
                return ("model", "That model is not available on this account")
            }
            if code == 429 { return ("rate_limit", "The provider is rate-limiting; try again in a minute") }
            return ("error", e.localizedDescription)
        case .emptyResponse, .refused:
            return ("error", e.localizedDescription)
        }
    }
}
