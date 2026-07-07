import Foundation

/// Owns AppConfig persistence: atomic JSON in the App Group container, with
/// an AsyncStream of change notifications for UI observation.
public actor ConfigStore {
    public static let shared = ConfigStore()

    private let fileURL: URL
    private var cached: AppConfig?
    private var continuations: [UUID: AsyncStream<AppConfig>.Continuation] = [:]

    public init(fileURL: URL = AppGroup.configURL) {
        self.fileURL = fileURL
    }

    public func load() -> AppConfig {
        if let cached { return cached }
        guard let data = try? Data(contentsOf: fileURL),
              let config = try? JSONDecoder().decode(AppConfig.self, from: data) else {
            let fresh = AppConfig()
            cached = fresh
            return fresh
        }
        cached = config
        return config
    }

    public func save(_ config: AppConfig) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(config)
        try data.write(to: fileURL, options: .atomic)
        cached = config
        for continuation in continuations.values {
            continuation.yield(config)
        }
    }

    public func update(_ mutate: (inout AppConfig) -> Void) throws -> AppConfig {
        var config = load()
        mutate(&config)
        try save(config)
        return config
    }

    /// Reload from disk, picking up writes from other processes
    /// (share extension, Safari extension).
    public func reload() -> AppConfig {
        cached = nil
        return load()
    }

    public func changes() -> AsyncStream<AppConfig> {
        let id = UUID()
        return AsyncStream { continuation in
            continuations[id] = continuation
            continuation.onTermination = { _ in
                Task { await self.removeContinuation(id) }
            }
        }
    }

    private func removeContinuation(_ id: UUID) {
        continuations[id] = nil
    }
}
