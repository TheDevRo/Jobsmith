import Foundation

/// App-level glue that ties the GRDB database and ConfigStore profile to the
/// SyncEngine + SyncCoordinator. This is the single entry point the UI calls:
/// `SyncManager.shared.syncOnce(...)`.
///
/// The engine's profile hooks are synchronous closures, but ConfigStore is an
/// actor, so we snapshot the profile before the run and write back the merged
/// result after — the engine only mutates the profile once, during import.
public final class SyncManager {
    public static let shared = SyncManager()

    private let defaultsKey = "jobsmith.sync.deviceId"

    public init() {}

    /// Stable per-install device id (generated + persisted on first use).
    public func deviceId(_ defaults: UserDefaults = .standard) -> String {
        if let existing = defaults.string(forKey: defaultsKey) { return existing }
        let id = String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(8)).uppercased()
        defaults.set(id, forKey: defaultsKey)
        return id
    }

    // MARK: Profile <-> [String: JSONValue]

    /// iOS Profile -> a camelCase dict matching SyncEntities' iOS keys.
    public static func profileToDict(_ profile: Profile) -> [String: JSONValue] {
        guard let data = try? JSONEncoder().encode(profile),
              let obj = try? JSONSerialization.jsonObject(with: data),
              case .object(let dict) = JSONValue.from(obj) else { return [:] }
        return dict
    }

    /// Inverse of profileToDict; missing/extra keys fall back to a default Profile.
    public static func dictToProfile(_ dict: [String: JSONValue]) -> Profile {
        guard let data = try? JSONSerialization.data(withJSONObject: JSONValue.object(dict).toAny()),
              let profile = try? JSONDecoder().decode(Profile.self, from: data) else { return Profile() }
        return profile
    }

    // MARK: AppConfig <-> [String: JSONValue]  (the settings bridge source)

    /// Whole AppConfig -> its native JSON dict (camelCase, nested
    /// honesty/search/ai/promptOverrides/…), the shape SettingsSync reads.
    public static func configToDict(_ config: AppConfig) -> [String: JSONValue] {
        guard let data = try? JSONEncoder().encode(config),
              let obj = try? JSONSerialization.jsonObject(with: data),
              case .object(let dict) = JSONValue.from(obj) else { return [:] }
        return dict
    }

    /// Inverse of configToDict; malformed/missing keys fall back to defaults.
    public static func dictToConfig(_ dict: [String: JSONValue]) -> AppConfig {
        guard let data = try? JSONSerialization.data(withJSONObject: JSONValue.object(dict).toAny()),
              let config = try? JSONDecoder().decode(AppConfig.self, from: data) else { return AppConfig() }
        return config
    }

    // MARK: sync cycle

    /// Run one sync cycle against `folder`. Reads the profile from `configStore`
    /// up front and writes any merged change back afterwards.
    @discardableResult
    public func syncOnce(folder: URL,
                         securityScoped: Bool = false,
                         db: AppDatabase,
                         configStore: ConfigStore = .shared,
                         docsLocalDir: URL,
                         deviceLabel: String? = nil,
                         defaults: UserDefaults = .standard) async throws -> SyncCoordinator.Result {
        let device = deviceId(defaults)
        let config = await configStore.load()
        let enabled = enabledSettingsCategories(defaults)
        let profileOn = enabled.contains("profile")

        // Snapshot both the profile and the whole settings config up front; the
        // engine's closures are synchronous while ConfigStore is an actor.
        let profileSnapshot = SyncManager.profileToDict(config.profile)
        let settingsSnapshot = SyncManager.configToDict(config)
        var mergedProfile: [String: JSONValue]?
        var mergedSettings: [String: JSONValue]?

        let engine = SyncEngine(
            db: db, deviceId: device,
            loadProfile: { profileOn ? profileSnapshot : nil },
            saveProfile: { mergedProfile = $0 },
            loadSettings: { settingsSnapshot },
            saveSettings: { mergedSettings = $0 },
            settingsEnabled: enabled,
            docsLocalDir: docsLocalDir
        )
        let coordinator = SyncCoordinator(engine: engine, deviceId: device, deviceLabel: deviceLabel)
        let result = try coordinator.syncOnce(folder: folder, securityScoped: securityScoped)

        if mergedProfile != nil || mergedSettings != nil {
            _ = try await configStore.update { cfg in
                if let mergedProfile { cfg.profile = SyncManager.dictToProfile(mergedProfile) }
                if let mergedSettings {
                    // Only the sections settings can affect — profile rides its own
                    // bridge, apiKeys (secrets) never sync.
                    let updated = SyncManager.dictToConfig(mergedSettings)
                    cfg.search = updated.search
                    cfg.ai = updated.ai
                    cfg.honesty = updated.honesty
                    cfg.promptOverrides = updated.promptOverrides
                }
            }
        }
        return result
    }
}

// MARK: - Preferences + folder bookmark (UserDefaults-backed)

public extension SyncManager {
    private var enabledKey: String { "jobsmith.sync.enabled" }
    private var bookmarkKey: String { "jobsmith.sync.folderBookmark" }

    private var intervalKey: String { "jobsmith.sync.intervalSeconds" }

    func isEnabled(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: enabledKey)
    }
    func setEnabled(_ on: Bool, _ defaults: UserDefaults = .standard) {
        defaults.set(on, forKey: enabledKey)
    }

    // MARK: desktop hand-off

    private var handoffKey: String { "jobsmith.sync.handoff" }

    /// Whether a scoring run this device can't finish should be handed to the
    /// desktop as a `work_request` through the sync folder. Off by default —
    /// asking another machine to spend LLM tokens is an explicit opt-in (the
    /// desktop has its own serving toggle, also off by default).
    func handoffEnabled(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: handoffKey)
    }
    func setHandoffEnabled(_ on: Bool, _ defaults: UserDefaults = .standard) {
        defaults.set(on, forKey: handoffKey)
    }

    // MARK: per-category settings-sync toggles (jobsmith.sync.settings.<key>)

    private func settingsFlagKey(_ category: String) -> String { "jobsmith.sync.settings.\(category)" }

    /// Whether this device syncs `category`, seeded from SettingsSync.categories'
    /// default when the flag was never written (so `profile` reads ON and every
    /// new category OFF, rather than false).
    func settingsCategoryEnabled(_ category: String, _ defaults: UserDefaults = .standard) -> Bool {
        if let v = defaults.object(forKey: settingsFlagKey(category)) as? Bool { return v }
        return SettingsSync.categories.first { $0.key == category }?.defaultOn ?? false
    }

    func setSettingsCategoryEnabled(_ category: String, _ on: Bool, _ defaults: UserDefaults = .standard) {
        defaults.set(on, forKey: settingsFlagKey(category))
    }

    /// The set of category keys currently synced on this device (seeded defaults).
    func enabledSettingsCategories(_ defaults: UserDefaults = .standard) -> Set<String> {
        var out = Set<String>()
        for c in SettingsSync.categories where settingsCategoryEnabled(c.key, defaults) { out.insert(c.key) }
        return out
    }

    /// Foreground auto-sync cadence in seconds while the app is open; 0 means
    /// "manual only". Defaults to 60s to match the desktop poller.
    func syncIntervalSeconds(_ defaults: UserDefaults = .standard) -> Int {
        defaults.object(forKey: intervalKey) as? Int ?? 60
    }
    func setSyncIntervalSeconds(_ seconds: Int, _ defaults: UserDefaults = .standard) {
        defaults.set(seconds, forKey: intervalKey)
    }

    /// Persist a user-picked folder as a security-scoped bookmark.
    func storeFolder(_ url: URL, _ defaults: UserDefaults = .standard) throws {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let data = try url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
        defaults.set(data, forKey: bookmarkKey)
    }

    /// Resolve the stored folder bookmark, if any.
    func resolvedFolder(_ defaults: UserDefaults = .standard) -> URL? {
        guard let data = defaults.data(forKey: bookmarkKey) else { return nil }
        var stale = false
        return try? URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale)
    }

    func folderName(_ defaults: UserDefaults = .standard) -> String? {
        resolvedFolder(defaults)?.lastPathComponent
    }

    /// Default local directory for materialized synced documents.
    static var defaultDocsDir: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        let dir = base.appendingPathComponent("JobsmithSyncDocs", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// Resolve the configured folder and run one cycle. Throws if no folder is set.
    @discardableResult
    func syncNow(db: AppDatabase,
                 configStore: ConfigStore = .shared,
                 deviceLabel: String? = nil,
                 defaults: UserDefaults = .standard) async throws -> SyncCoordinator.Result {
        guard let folder = resolvedFolder(defaults) else {
            throw NSError(domain: "JobsmithSync", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "No sync folder chosen yet"])
        }
        return try await syncOnce(folder: folder, securityScoped: true, db: db,
                                  configStore: configStore, docsLocalDir: Self.defaultDocsDir,
                                  deviceLabel: deviceLabel, defaults: defaults)
    }
}
