import Foundation

/// Owns AppConfig persistence: atomic JSON in the App Group container, with
/// an AsyncStream of change notifications for UI observation.
///
/// Two secrecy rules live here rather than in `AppConfig`:
///  - the file is written with `completeUntilFirstUserAuthentication` protection,
///    so it is encrypted at rest but still readable by background fetches;
///  - the LinkedIn `li_at` cookie is round-tripped through the Keychain
///    (`KeychainStore`) instead of the JSON, falling back to plaintext only when
///    the Keychain is unavailable. Callers keep using `config.apiKeys.linkedInCookie`.
public actor ConfigStore {
    public static let shared = ConfigStore()

    private let fileURL: URL
    private let secrets: any SecretStore
    private var cached: AppConfig?
    private var continuations: [UUID: AsyncStream<AppConfig>.Continuation] = [:]

    /// Set when `load()` found a non-empty config file it could not decode. The
    /// bad file is preserved at `config.corrupt.json` and the in-memory config
    /// falls back to defaults — but nothing overwrites the original until the
    /// user saves, so a botched upgrade is recoverable.
    public private(set) var loadWarning: String?

    public init(fileURL: URL = AppGroup.configURL,
                secrets: any SecretStore = KeychainStore.shared) {
        self.fileURL = fileURL
        self.secrets = secrets
    }

    /// Where a config file that failed to decode is copied for post-mortem.
    var corruptFileURL: URL {
        fileURL.deletingLastPathComponent()
            .appendingPathComponent("config.corrupt.json")
    }

    public func load() -> AppConfig {
        if let cached { return cached }

        guard let data = try? Data(contentsOf: fileURL), !data.isEmpty else {
            let fresh = AppConfig()
            cached = fresh
            return fresh
        }
        guard var config = try? JSONDecoder().decode(AppConfig.self, from: data) else {
            // Non-empty but undecodable. Never silently reset the user's
            // profile/keys: keep the file, stash a copy, and surface a warning.
            try? data.write(to: corruptFileURL, options: .atomic)
            loadWarning = "Your settings file could not be read and was left untouched; "
                + "a copy is at config.corrupt.json. Saving from Settings will replace it."
            let fresh = AppConfig()
            cached = fresh
            return fresh
        }

        if config.apiKeys.linkedInCookie.isEmpty {
            config.apiKeys.linkedInCookie = secrets.get(.linkedInCookie) ?? ""
        } else if secrets.set(config.apiKeys.linkedInCookie, for: .linkedInCookie) {
            // Legacy plaintext cookie on disk — now in the Keychain; rewrite the
            // file without it. (`save` strips whatever the Keychain accepted.)
            try? persist(config)
        }
        cached = config
        return config
    }

    public func save(_ config: AppConfig) throws {
        try persist(config)
        cached = config
        for continuation in continuations.values {
            continuation.yield(config)
        }
    }

    /// Encode and write, keeping the LinkedIn cookie out of the JSON whenever
    /// the Keychain accepted it.
    private func persist(_ config: AppConfig) throws {
        var onDisk = config
        if secrets.set(config.apiKeys.linkedInCookie, for: .linkedInCookie) {
            onDisk.apiKeys.linkedInCookie = ""
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(onDisk)
        try data.write(to: fileURL,
                       options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    public func update(_ mutate: (inout AppConfig) -> Void) throws -> AppConfig {
        var config = load()
        mutate(&config)
        try save(config)
        return config
    }

    /// Reload from disk, picking up writes from the share extension.
    public func reload() -> AppConfig {
        cached = nil
        loadWarning = nil
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
