import Foundation

/// Owns AppConfig persistence: atomic JSON in the App Group container, with
/// an AsyncStream of change notifications for UI observation.
///
/// Two secrecy rules live here rather than in `AppConfig`:
///  - the file is written with `completeUntilFirstUserAuthentication` protection,
///    so it is encrypted at rest but still readable by background fetches;
///  - live credentials — the LinkedIn `li_at`/`JSESSIONID` cookies and the AI
///    endpoint bearer keys (`ai.apiKey` and each `savedEndpoints[].apiKey`) —
///    are round-tripped through the Keychain (`KeychainStore`) instead of the
///    JSON, falling back to plaintext only when the Keychain is unavailable.
///    Callers keep using the plain in-memory properties; the redirect is
///    at-rest only, so `SyncManager.configToDict` still sees the live values.
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

        var rewriteForLegacyPlaintext = false
        // Rehydrate a secret from the Keychain when the JSON slot is empty;
        // otherwise the JSON still carries a legacy plaintext value — push it
        // into the Keychain and flag the file for rewrite-without-it.
        func rehydrate(_ value: inout String, _ key: SecretKey) {
            if value.isEmpty {
                value = secrets.get(key) ?? ""
            } else if secrets.set(value, for: key) {
                rewriteForLegacyPlaintext = true
            }
        }
        rehydrate(&config.apiKeys.linkedInCookie, .linkedInCookie)
        rehydrate(&config.apiKeys.linkedInJSessionId, .linkedInJSessionId)
        rehydrate(&config.apiKeys.workdayPassword, .workdayPassword)
        rehydrate(&config.ai.apiKey, .aiAPIKey)
        for i in config.ai.savedEndpoints.indices {
            rehydrate(&config.ai.savedEndpoints[i].apiKey,
                      .savedEndpointAPIKey(config.ai.savedEndpoints[i].id))
        }
        if rewriteForLegacyPlaintext {
            // Legacy plaintext credential(s) on disk — now in the Keychain;
            // rewrite the file without them. (`persist` strips whatever the
            // Keychain accepted.)
            try? persist(config)
        }
        cached = config
        return config
    }

    public func save(_ config: AppConfig) throws {
        let previous = cached
        try persist(config)
        cached = config
        // A saved endpoint that was deleted leaves an orphaned Keychain entry —
        // clear the key for every endpoint id present before but gone now.
        if let previous {
            let liveIDs = Set(config.ai.savedEndpoints.map(\.id))
            for ep in previous.ai.savedEndpoints where !liveIDs.contains(ep.id) {
                secrets.set("", for: .savedEndpointAPIKey(ep.id))
            }
        }
        for continuation in continuations.values {
            continuation.yield(config)
        }
    }

    /// Encode and write, keeping live credentials (LinkedIn cookies and AI
    /// bearer keys) out of the JSON whenever the Keychain accepted them.
    private func persist(_ config: AppConfig) throws {
        var onDisk = config
        // Divert a secret to the Keychain; on success blank its JSON slot so the
        // credential never lands in the plaintext file.
        func divert(_ value: String, _ key: SecretKey, _ slot: inout String) {
            if secrets.set(value, for: key) { slot = "" }
        }
        divert(config.apiKeys.linkedInCookie, .linkedInCookie, &onDisk.apiKeys.linkedInCookie)
        divert(config.apiKeys.linkedInJSessionId, .linkedInJSessionId, &onDisk.apiKeys.linkedInJSessionId)
        divert(config.apiKeys.workdayPassword, .workdayPassword, &onDisk.apiKeys.workdayPassword)
        divert(config.ai.apiKey, .aiAPIKey, &onDisk.ai.apiKey)
        for i in onDisk.ai.savedEndpoints.indices {
            divert(config.ai.savedEndpoints[i].apiKey,
                   .savedEndpointAPIKey(config.ai.savedEndpoints[i].id),
                   &onDisk.ai.savedEndpoints[i].apiKey)
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
