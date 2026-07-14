import Foundation

/// Tells a *transient* transport failure from a *fatal* one.
///
/// The distinction is what lets a long search or scoring run survive iOS
/// suspending the app: when the process is frozen mid-request the sockets die,
/// and the errors that surface are indistinguishable — at the URLSession level —
/// from a flaky network. Both are worth resuming. A refused connection or an
/// unresolvable host, by contrast, will fail identically on every retry, so a
/// run that hits one should stop and say so.
///
/// Cancellation counts as transient: the app cancels in-flight work from the
/// background-task expiration handler precisely so it can be resumed.
public enum TransientNetwork {
    public static func isTransient(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if error is SourceInterruptedError { return true }
        if let aiError = error as? AIEngineError, case .interrupted = aiError { return true }
        if let scoringError = error as? ScoringError, case .interrupted = scoringError { return true }
        guard let urlError = error as? URLError else { return false }
        switch urlError.code {
        case .cancelled,                 // task cancelled (suspension, Stop button)
             .networkConnectionLost,     // socket died under us — the classic suspend symptom
             .notConnectedToInternet,
             .timedOut,
             .dataNotAllowed,            // cellular disallowed for this app
             .internationalRoamingOff,
             .callIsActive,
             .requestBodyStreamExhausted:
            return true
        default:
            // .cannotConnectToHost (refused), .cannotFindHost (DNS), .badURL,
            // .userAuthenticationRequired… — retrying changes nothing.
            return false
        }
    }
}
