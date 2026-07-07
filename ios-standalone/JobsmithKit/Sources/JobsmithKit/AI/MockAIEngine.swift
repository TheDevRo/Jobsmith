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

    public func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
        lock.lock(); defer { lock.unlock() }
        recorded.append(req)
        let haystack = (req.system.map { $0 + "\n" } ?? "") + req.user
        for i in canned.indices where haystack.contains(canned[i].key) {
            guard let first = canned[i].queue.first else { continue }
            let response = canned[i].queue.count > 1 ? canned[i].queue.removeFirst() : first
            switch response {
            case .text(let text): return text
            case .failure(let message): throw MockError(message)
            }
        }
        throw MockError("no canned response for prompt: \(String(req.user.prefix(80)))")
    }

    public func listModels(config: AIConfig) async throws -> [String] {
        lock.lock(); defer { lock.unlock() }
        if let modelError { throw MockError(modelError) }
        return modelList
    }
}
