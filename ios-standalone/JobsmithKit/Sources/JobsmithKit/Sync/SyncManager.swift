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
        let profileSnapshot = SyncManager.profileToDict(await configStore.load().profile)
        var mergedProfile: [String: JSONValue]?

        let engine = SyncEngine(
            db: db, deviceId: device,
            loadProfile: { profileSnapshot },
            saveProfile: { mergedProfile = $0 },
            docsLocalDir: docsLocalDir
        )
        let coordinator = SyncCoordinator(engine: engine, deviceId: device, deviceLabel: deviceLabel)
        let result = try coordinator.syncOnce(folder: folder, securityScoped: securityScoped)

        if let mergedProfile {
            let profile = SyncManager.dictToProfile(mergedProfile)
            _ = try await configStore.update { $0.profile = profile }
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
