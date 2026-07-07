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
