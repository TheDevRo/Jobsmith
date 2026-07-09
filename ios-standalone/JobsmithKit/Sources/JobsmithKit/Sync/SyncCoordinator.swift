import Foundation

/// iOS transport wiring — resolves the shared sync folder (iCloud Drive by
/// default, a Files-app folder as the escape hatch) and runs one sync cycle
/// under NSFileCoordinator so reads/writes play nicely with the file provider.
///
/// The heavy lifting (merge, mapping, documents) is in SyncEngine/SyncFolder,
/// which are verified against the desktop; this type only chooses the folder and
/// coordinates access. Real iCloud behavior requires the app's iCloud entitlement
/// and a signed-in device (see backend/sync/SYNC.md).
public final class SyncCoordinator {
    public enum FolderSource {
        /// The app's iCloud ubiquity container (nil = default container).
        case iCloud(containerId: String?)
        /// A user-picked folder, persisted as a security-scoped bookmark.
        case bookmark(Data)
    }

    public struct Result: Sendable {
        public var imported: SyncEngine.ImportStats
        public var exported: SyncEngine.ExportStats
        public var compacted: Int
    }

    private let engine: SyncEngine
    private let deviceId: String
    private let deviceLabel: String?

    public init(engine: SyncEngine, deviceId: String, deviceLabel: String? = nil) {
        self.engine = engine
        self.deviceId = deviceId
        self.deviceLabel = deviceLabel
    }

    /// The iCloud Drive folder Jobsmith syncs through (creating it if possible).
    public static func iCloudFolder(containerId: String? = nil, subfolder: String = "JobsmithSync") -> URL? {
        guard let container = FileManager.default.url(forUbiquityContainerIdentifier: containerId) else {
            return nil  // iCloud unavailable / not signed in
        }
        let folder = container.appendingPathComponent("Documents").appendingPathComponent(subfolder)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder
    }

    /// Resolve a user-picked folder from its security-scoped bookmark.
    public static func resolveBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
        var stale = false
        let url = try URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale)
        return (url, stale)
    }

    public static func resolveFolder(_ source: FolderSource) throws -> URL {
        switch source {
        case .iCloud(let containerId):
            guard let url = iCloudFolder(containerId: containerId) else {
                throw NSError(domain: "JobsmithSync", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "iCloud Drive is not available"])
            }
            return url
        case .bookmark(let data):
            return try resolveBookmark(data).url
        }
    }

    /// Import remote changes, export local ones, register + compact — all inside
    /// one coordinated write so the file provider serializes access.
    @discardableResult
    public func syncOnce(folder: URL, securityScoped: Bool = false) throws -> Result {
        if securityScoped {
            guard folder.startAccessingSecurityScopedResource() else {
                throw NSError(domain: "JobsmithSync", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "cannot access the picked folder"])
            }
        }
        defer { if securityScoped { folder.stopAccessingSecurityScopedResource() } }

        var result: Result?
        var thrown: Error?
        var coordError: NSError?
        NSFileCoordinator(filePresenter: nil).coordinate(writingItemAt: folder, options: [], error: &coordError) { url in
            do {
                let sf = SyncFolder(url)
                try sf.ensureDirs()
                try sf.registerDevice(deviceId, label: deviceLabel, platform: "ios")
                // Export BEFORE import: a just-made local shortlist must reach
                // the folder (stamped now) before import evaluates any incoming
                // deletion tombstone, or the engagement-override can't see it and
                // the stale tombstone wipes the fresh shortlist. See engine.py.
                let exported = try engine.export(to: url)
                let imported = try engine.importChanges(from: url)
                let compacted = try sf.compactOwnLog(deviceId)
                result = Result(imported: imported, exported: exported, compacted: compacted)
            } catch { thrown = error }
        }
        if let coordError { throw coordError }
        if let thrown { throw thrown }
        return result!
    }
}
