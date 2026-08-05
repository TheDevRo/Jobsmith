import Foundation
#if canImport(FoundationModels)
import FoundationModels
#endif

/// Everything that touches Apple's on-device foundation model. Mirrors
/// JobsmithKit's `AppleOnDeviceEngine` (ios-standalone/JobsmithKit/Sources/
/// JobsmithKit/AI/AppleOnDeviceEngine.swift) so the desktop and iOS apps
/// describe the same model in the same words — same trim, same availability
/// reasons, same sorting of failure modes.
enum OnDeviceModel {
    /// The sentinel model id the app routes with; the same string on iOS.
    static let modelID = "apple-on-device"

    /// Inputs beyond this are trimmed — the on-device context window is far
    /// smaller than server models'.
    static let maxUserChars = 8000

    /// Availability as the HTTP layer needs it: a flag plus a sentence we can
    /// hand a user verbatim.
    struct Availability {
        let available: Bool
        /// nil when available; otherwise something the user can act on.
        let reason: String?
    }

    static func availability() -> Availability {
        #if canImport(FoundationModels)
        if #available(macOS 26.0, *) {
            switch SystemLanguageModel.default.availability {
            case .available:
                return Availability(available: true, reason: nil)
            case .unavailable(let reason):
                // Say WHY — "not available" reads as a bug; "turned off in
                // Settings" is something the user can act on.
                return Availability(available: false, reason: describe(reason))
            @unknown default:
                return Availability(available: false,
                                    reason: "The on-device model is unavailable right now")
            }
        }
        #endif
        return Availability(available: false, reason: unsupportedOSReason)
    }

    /// Used both when the OS is too old at runtime and when the bridge was
    /// compiled against an SDK without FoundationModels at all.
    static let unsupportedOSReason =
        "The on-device model requires macOS 26 on an Apple Intelligence Mac"

    #if canImport(FoundationModels)
    @available(macOS 26.0, *)
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
    #endif

    /// What went wrong, in the shape the HTTP layer turns into an OpenAI error
    /// body. Sorted the way JobsmithKit sorts them: per-input declines are the
    /// caller's problem with *this* request (400, skip the job and keep the
    /// batch going), rate limiting is retryable (429), everything else means
    /// the model isn't usable right now (503, stop and tell the user).
    struct Failure: Error {
        let status: Int
        let message: String
        let type: String
        let code: String
    }

    private static func refused(_ message: String, code: String) -> Failure {
        Failure(status: 400, message: message, type: "invalid_request_error", code: code)
    }

    static func unreachable(_ message: String) -> Failure {
        Failure(status: 503, message: message, type: "server_error", code: "model_unavailable")
    }

    #if canImport(FoundationModels)
    @available(macOS 26.0, *)
    static func map(_ error: LanguageModelSession.GenerationError) -> Failure {
        switch error {
        case .guardrailViolation:
            return refused("Apple's on-device model declined this content (safety guardrails)",
                           code: "content_filter")
        case .exceededContextWindowSize:
            return refused("This job posting is too long for the on-device model",
                           code: "context_length_exceeded")
        case .unsupportedLanguageOrLocale:
            return refused("The on-device model does not support this content's language",
                           code: "unsupported_language")
        case .rateLimited:
            return Failure(status: 429,
                           message: "The on-device model is rate-limited right now",
                           type: "rate_limit_error",
                           code: "rate_limit_exceeded")
        default:
            return unreachable(error.localizedDescription)
        }
    }
    #endif

    /// One request at a time. The on-device model rejects overlapping work and
    /// a second concurrent request would surface as a baffling generation
    /// error; an actor turns that into a queue instead. The backend never
    /// streams and rarely fans out, so the wait is cheap.
    actor Generator {
        static let shared = Generator()

        func respond(system: String?, user: String, temperature: Double?, maxTokens: Int?) async throws -> String {
            #if canImport(FoundationModels)
            if #available(macOS 26.0, *) {
                let state = OnDeviceModel.availability()
                guard state.available else {
                    throw OnDeviceModel.unreachable(state.reason ?? OnDeviceModel.unsupportedOSReason)
                }
                let session = LanguageModelSession(instructions: system ?? "")
                let prompt = String(user.prefix(OnDeviceModel.maxUserChars))
                var options = GenerationOptions()
                if let temperature { options.temperature = temperature }
                if let maxTokens { options.maximumResponseTokens = maxTokens }
                let response: LanguageModelSession.Response<String>
                do {
                    response = try await session.respond(to: prompt, options: options)
                } catch let error as LanguageModelSession.GenerationError {
                    throw OnDeviceModel.map(error)
                } catch {
                    throw OnDeviceModel.unreachable(error.localizedDescription)
                }
                let text = response.content.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else {
                    throw OnDeviceModel.unreachable("The on-device model returned an empty response")
                }
                return text
            }
            #endif
            throw OnDeviceModel.unreachable(OnDeviceModel.unsupportedOSReason)
        }
    }
}
