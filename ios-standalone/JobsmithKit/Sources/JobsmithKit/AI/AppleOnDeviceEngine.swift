import Foundation
#if canImport(FoundationModels)
import FoundationModels
#endif

/// Apple's on-device foundation model (iOS 26+, Apple Intelligence devices).
/// Free, private, and offline — but a ~3B model with a small context window,
/// so it's only offered for short-context tasks (scoring, classification,
/// field mapping); document generation stays on the configured endpoint.
public struct AppleOnDeviceEngine: AIEngine {
    /// Inputs beyond this are trimmed — the on-device context window is far
    /// smaller than server models'.
    static let maxUserChars = 8000

    public init() {}

    public static var isAvailable: Bool {
        #if canImport(FoundationModels)
        if #available(iOS 26.0, *) {
            return SystemLanguageModel.default.availability == .available
        }
        #endif
        return false
    }

    public func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
        #if canImport(FoundationModels)
        if #available(iOS 26.0, *) {
            guard SystemLanguageModel.default.availability == .available else {
                throw AIEngineError.unreachable("Apple Intelligence model is not available on this device")
            }
            let session = LanguageModelSession(instructions: req.system ?? "")
            let user = String(req.user.prefix(Self.maxUserChars))
            var options = GenerationOptions()
            options.temperature = req.temperature
            let response = try await session.respond(to: user, options: options)
            let text = response.content.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { throw AIEngineError.emptyResponse }
            return text
        }
        #endif
        throw AIEngineError.unreachable("On-device model requires iOS 26")
    }

    public func listModels(config: AIConfig) async throws -> [String] {
        Self.isAvailable ? ["apple-on-device"] : []
    }
}

/// Routes each request to the right engine: a tier whose assigned model is
/// the on-device sentinel runs on-device when the model is available;
/// everything else — and every fallback — uses the OpenAI-compatible
/// endpoint.
public struct EngineRouter: AIEngine {
    let endpoint: any AIEngine
    let onDevice: any AIEngine

    public init(endpoint: any AIEngine = OpenAICompatibleEngine(),
                onDevice: any AIEngine = AppleOnDeviceEngine()) {
        self.endpoint = endpoint
        self.onDevice = onDevice
    }

    public func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
        if config.usesOnDevice(for: req.tier) && AppleOnDeviceEngine.isAvailable {
            do {
                return try await onDevice.complete(req, config: config)
            } catch {
                // Fall through to the endpoint rather than failing the task.
            }
        }
        return try await endpoint.complete(req, config: config)
    }

    public func listModels(config: AIConfig) async throws -> [String] {
        try await endpoint.listModels(config: config)
    }
}
