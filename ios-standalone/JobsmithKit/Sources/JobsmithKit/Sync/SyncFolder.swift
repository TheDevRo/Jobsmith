import Foundation

/// Sync folder transport — the Swift twin of backend/sync/transport.py.
/// Manages the manifest (advisory device list), derives the authoritative
/// device list from log filenames, and compacts this device's own log.
///
/// Reads/writes here should be wrapped in NSFileCoordinator by the caller when
/// the folder is an iCloud/Files provider (see SyncCoordinator, Step 7 wiring);
/// the pure folder logic lives here so it is testable without a provider.
public struct SyncFolder {
    public static let formatVersion = "0.1.0-draft"
    public let root: URL

    public init(_ root: URL) { self.root = root }

    public var changesDir: URL { root.appendingPathComponent("changes") }
    public var documentsDir: URL { root.appendingPathComponent("documents") }
    public var manifestURL: URL { root.appendingPathComponent("manifest.json") }

    public func ensureDirs() throws {
        try FileManager.default.createDirectory(at: changesDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: documentsDir, withIntermediateDirectories: true)
    }

    public func logURL(for deviceId: String) -> URL {
        changesDir.appendingPathComponent("\(deviceId).jsonl")
    }

    /// Authoritative device list: one changes/{id}.jsonl per device.
    public func logDeviceIds() -> Set<String> {
        let entries = (try? FileManager.default.contentsOfDirectory(
            at: changesDir, includingPropertiesForKeys: nil)) ?? []
        return Set(entries.filter { $0.pathExtension == "jsonl" }.map { $0.deletingPathExtension().lastPathComponent })
    }

    // MARK: manifest

    public func readManifest() -> [String: Any] {
        if let data = try? Data(contentsOf: manifestURL),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            return obj
        }
        return ["format_version": Self.formatVersion, "devices": []]
    }

    /// Add this device to the manifest if absent; rewrite only on change.
    public func registerDevice(_ deviceId: String, label: String? = nil, platform: String = "ios") throws {
        var manifest = readManifest()
        var devices = (manifest["devices"] as? [[String: Any]]) ?? []
        var changed = false

        if let idx = devices.firstIndex(where: { $0["id"] as? String == deviceId }) {
            if let label, devices[idx]["label"] as? String != label { devices[idx]["label"] = label; changed = true }
            if devices[idx]["platform"] as? String != platform { devices[idx]["platform"] = platform; changed = true }
        } else {
            var entry: [String: Any] = ["id": deviceId, "platform": platform]
            if let label { entry["label"] = label }
            devices.append(entry)
            changed = true
        }
        if manifest["format_version"] as? String != Self.formatVersion {
            manifest["format_version"] = Self.formatVersion
            changed = true
        }
        manifest["devices"] = devices
        if changed {
            let data = try JSONSerialization.data(withJSONObject: manifest, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: manifestURL, options: .atomic)
        }
    }

    // MARK: compaction

    /// Rewrite this device's log to keep only the records it still wins, one
    /// line per key. Returns the number of lines dropped. Mirrors
    /// transport.py::compact_own_log.
    @discardableResult
    public func compactOwnLog(_ deviceId: String) throws -> Int {
        let ownLog = logURL(for: deviceId)
        // Pull the log back if iCloud evicted it, so compaction sees real
        // history instead of silently skipping (a nil read here is safe — we
        // just return 0 without rewriting — but skipping compaction forever is
        // not what we want).
        SyncFile.materialize(ownLog)
        guard let text = try? String(contentsOf: ownLog, encoding: .utf8) else { return 0 }

        let winners = SyncMerge.winners(try SyncMerge.loadLogs(root))
        var kept: [String] = []
        var seen = Set<SyncMerge.Key>()
        var dropped = 0
        for rawLine in text.split(separator: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty, let data = line.data(using: .utf8),
                  let rec = try? JSONDecoder().decode(ChangeRecord.self, from: data) else { continue }
            let key = SyncMerge.Key(entity: rec.entity, id: rec.id)
            let w = winners[key]
            let isWinner = w != nil && w!.device == deviceId
                && w!.updatedAt == rec.updatedAt && !seen.contains(key)
            if isWinner { kept.append(line); seen.insert(key) } else { dropped += 1 }
        }
        if dropped > 0 {
            let out = kept.isEmpty ? "" : kept.joined(separator: "\n") + "\n"
            try out.write(to: ownLog, atomically: true, encoding: .utf8)
        }
        return dropped
    }
}
