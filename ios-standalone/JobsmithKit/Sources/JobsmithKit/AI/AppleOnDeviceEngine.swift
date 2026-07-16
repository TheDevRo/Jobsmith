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
            switch SystemLanguageModel.default.availability {
            case .available:
                break
            case .unavailable(let reason):
                // Say WHY — "not available" reads as a bug; "turned off in
                // Settings" is something the user can act on.
                throw AIEngineError.unreachable(Self.describe(reason))
            }
            let session = LanguageModelSession(instructions: req.system ?? "")
            let user = String(req.user.prefix(Self.maxUserChars))
            var options = GenerationOptions()
            options.temperature = req.temperature
            let response: LanguageModelSession.Response<String>
            do {
                response = try await session.respond(to: user, options: options)
            } catch let error as LanguageModelSession.GenerationError {
                throw Self.map(error)
            }
            let text = response.content.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { throw AIEngineError.emptyResponse }
            return text
        }
        #endif
        throw AIEngineError.unreachable("On-device model requires iOS 26")
    }

    #if canImport(FoundationModels)
    @available(iOS 26.0, *)
    static func describe(_ reason: SystemLanguageModel.Availability.UnavailableReason) -> String {
        switch reason {
        case .deviceNotEligible:
            return "This device does not support Apple Intelligence"
        case .appleIntelligenceNotEnabled:
            return "Apple Intelligence is turned off — enable it in Settings to use the on-device model"
        case .modelNotReady:
            return "The on-device model is still downloading — try again in a few minutes"
        @unknown default:
            return "The on-device model is unavailable right now"
        }
    }

    /// Sort the model's failure modes into the app's error semantics:
    /// per-input declines are `refused` (skip the job, keep the batch going),
    /// rate limiting is `interrupted` (pause and resume later), everything
    /// else is `unreachable` (stop and tell the user).
    @available(iOS 26.0, *)
    static func map(_ error: LanguageModelSession.GenerationError) -> AIEngineError {
        switch error {
        case .guardrailViolation:
            return .refused("Apple's on-device model declined this content (safety guardrails)")
        case .exceededContextWindowSize:
            return .refused("This job posting is too long for the on-device model")
        case .unsupportedLanguageOrLocale:
            return .refused("The on-device model does not support this content's language")
        case .rateLimited:
            return .interrupted("The on-device model is rate-limited right now")
        default:
            return .unreachable(error.localizedDescription)
        }
    }
    #endif

    public func listModels(config: AIConfig) async throws -> [String] {
        Self.isAvailable ? ["apple-on-device"] : []
    }
}

/// Routes each request to the engine its tier is assigned to: the on-device
/// sentinel runs on-device, everything else uses the OpenAI-compatible
/// endpoint. Strictly — a tier the user routed on-device NEVER falls back to
/// the endpoint. The old silent fallback turned "score on-device" into cloud
/// calls the user didn't ask for, and surfaced on-device problems as baffling
/// endpoint errors ("404 page not found" while everything was set on-device).
public struct EngineRouter: AIEngine {
    let endpoint: any AIEngine
    let onDevice: any AIEngine

    public init(endpoint: any AIEngine = OpenAICompatibleEngine(),
                onDevice: any AIEngine = AppleOnDeviceEngine()) {
        self.endpoint = endpoint
        self.onDevice = onDevice
    }

    public func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
        if config.usesOnDevice(for: req.tier) {
            return try await onDevice.complete(req, config: config)
        }
        return try await endpoint.complete(req, config: config)
    }

    public func listModels(config: AIConfig) async throws -> [String] {
        try await endpoint.listModels(config: config)
    }
}
