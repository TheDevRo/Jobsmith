import Foundation

/// One line of a `changes/{deviceId}.jsonl` log — a single version of one
/// record. Mirrors backend/sync/schema/change-record.schema.json.
public struct ChangeRecord: Codable, Equatable, Sendable {
    public var v: Int
    public var entity: String
    public var id: String
    public var updatedAt: String
    public var device: String
    public var deleted: Bool
    public var data: [String: JSONValue]?

    enum CodingKeys: String, CodingKey {
        case v, entity, id
        case updatedAt = "updated_at"
        case device, deleted, data
    }

    public init(v: Int = 1, entity: String, id: String, updatedAt: String,
                device: String, deleted: Bool, data: [String: JSONValue]? = nil) {
        self.v = v; self.entity = entity; self.id = id
        self.updatedAt = updatedAt; self.device = device
        self.deleted = deleted; self.data = data
    }
}

/// The merge oracle in Swift. Rules (spec/FORMAT.md): fold records by
/// (entity, id); winner = max by updated_at, ties broken by higher device id;
/// a winning tombstone deletes. Must reproduce backend/sync/merge.py exactly.
public enum SyncMerge {
    struct Key: Hashable { let entity: String; let id: String }

    private static let withFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let noFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    /// Parse an RFC3339 UTC timestamp (with or without fractional seconds).
    static func parseTS(_ s: String) -> Date {
        withFraction.date(from: s) ?? noFraction.date(from: s) ?? .distantPast
    }

    /// True if `candidate` should replace `current` as the winner for a key.
    static func wins(_ candidate: ChangeRecord, over current: ChangeRecord) -> Bool {
        let ct = parseTS(candidate.updatedAt), pt = parseTS(current.updatedAt)
        if ct != pt { return ct > pt }
        return candidate.device > current.device  // deterministic tiebreak
    }

    /// The winning version per (entity, id).
    static func winners(_ records: [ChangeRecord]) -> [Key: ChangeRecord] {
        var winners: [Key: ChangeRecord] = [:]
        for rec in records {
            let key = Key(entity: rec.entity, id: rec.id)
            if let cur = winners[key] {
                if wins(rec, over: cur) { winners[key] = rec }
            } else {
                winners[key] = rec
            }
        }
        return winners
    }

    /// Fold into `{"live": ..., "tombstones": ...}` (matches merge.py output).
    public static func merge(_ records: [ChangeRecord]) -> JSONValue {
        var live: [String: [String: JSONValue]] = [:]
        var tombstones: [String: [String: JSONValue]] = [:]
        for (key, rec) in winners(records) {
            if rec.deleted {
                tombstones[key.entity, default: [:]][key.id] = .object([:])
            } else {
                live[key.entity, default: [:]][key.id] = .object(rec.data ?? [:])
            }
        }
        func wrap(_ m: [String: [String: JSONValue]]) -> JSONValue {
            .object(m.mapValues { JSONValue.object($0) })
        }
        return .object(["live": wrap(live), "tombstones": wrap(tombstones)])
    }

    /// Parse a `changes/*.jsonl` string into records (blank lines skipped).
    public static func parseLog(_ text: String) -> [ChangeRecord] {
        let decoder = JSONDecoder()
        return text.split(separator: "\n").compactMap { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8) else { return nil }
            return try? decoder.decode(ChangeRecord.self, from: data)
        }
    }

    /// Read and concatenate every `changes/*.jsonl` under a folder.
    public static func loadLogs(_ folder: URL) -> [ChangeRecord] {
        let changes = folder.appendingPathComponent("changes")
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: changes, includingPropertiesForKeys: nil) else { return [] }
        var records: [ChangeRecord] = []
        for url in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent })
        where url.pathExtension == "jsonl" {
            if let text = try? String(contentsOf: url, encoding: .utf8) {
                records.append(contentsOf: parseLog(text))
            }
        }
        return records
    }
}
