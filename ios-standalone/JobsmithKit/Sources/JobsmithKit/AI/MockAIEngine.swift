import Foundation

/// Test double: canned responses keyed by a substring match on the prompt,
/// consumed in registration order. Records every request for assertions.
public final class MockAIEngine: AIEngine, @unchecked Sendable {
    public struct MockError: Error, Equatable, Sendable {
        public let message: String
        public init(_ message: String = "mock failure") { self.message = message }
    }

    public enum Canned: Sendable {
        case text(String)
        case failure(String)
    }

    private let lock = NSLock()
    private var canned: [(key: String, queue: [Canned])] = []
    private var recorded: [CompletionRequest] = []
    private var modelList: [String] = []
    private var modelError: String?

    public init() {}

    /// Register responses for prompts containing `substring`. Responses are
    /// consumed in order; the last one repeats for any further matches.
    public func register(_ substring: String, _ responses: Canned...) {
        register(substring, responses)
    }

    public func register(_ substring: String, _ responses: [Canned]) {
        lock.lock(); defer { lock.unlock() }
        canned.append((substring, responses))
    }

    public func setModels(_ models: [String], error: String? = nil) {
        lock.lock(); defer { lock.unlock() }
        modelList = models
        modelError = error
    }

    public var requests: [CompletionRequest] {
        lock.lock(); defer { lock.unlock() }
        return recorded
    }

    /// Per-call artificial latency, so a mock run can stand in for a real
    /// LLM's timing (e.g. testing whether a batch survives backgrounding).
    /// Set from the -MockAIDelay debug launch arg; 0 = instant. Guarded by the
    /// class's lock like every other mutable field — this type is `@unchecked
    /// Sendable`, so a bare `var` touched from `complete` would be a data race.
    private var _delaySeconds: Double = 0

    /// Set the per-call latency (see `_delaySeconds`).
    public func setDelay(_ seconds: Double) {
        lock.lock(); defer { lock.unlock() }
        _delaySeconds = seconds
    }

    public func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
        let outcome: Canned
        lock.lock()
        recorded.append(req)
        let haystack = (req.system.map { $0 + "\n" } ?? "") + req.user
        var matched: Canned?
        for i in canned.indices where haystack.contains(canned[i].key) {
            guard let first = canned[i].queue.first else { continue }
            matched = canned[i].queue.count > 1 ? canned[i].queue.removeFirst() : first
            break
        }
        outcome = matched ?? .failure("no canned response for prompt: \(String(req.user.prefix(80)))")
        let delaySeconds = _delaySeconds
        lock.unlock()

        // Sleep OUTSIDE the lock (and cancellably — a cancelled call must
        // surface like a real in-flight request being cut off).
        if delaySeconds > 0 {
            try await Task.sleep(for: .seconds(delaySeconds))
        }
        switch outcome {
        case .text(let text): return text
        case .failure(let message): throw MockError(message)
        }
    }

    public func listModels(config: AIConfig) async throws -> [String] {
        lock.lock(); defer { lock.unlock() }
        if let modelError { throw MockError(modelError) }
        return modelList
    }
}
