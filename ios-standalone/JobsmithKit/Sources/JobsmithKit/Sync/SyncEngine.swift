import Foundation
import GRDB

/// iOS sync engine — the Swift twin of backend/sync/engine.py, over GRDB.
///
/// export: diff live rows (mapped to canonical via SyncEntities) against a
///   sync_snapshot table with base-overlay; append genuine local changes to
///   changes/{deviceId}.jsonl. import: fold every device's log (SyncMerge) and
///   upsert/delete rows + write the profile; rebuild the snapshot so a
///   subsequent export re-emits nothing.
///
/// Entities: job, application, profile. Answers are desktop-only (iOS
/// answer_bank has a different model). Documents travel as content-addressed
/// references (resume_doc / cover_doc) via DocumentStore.
public final class SyncEngine {
    public struct ExportStats: Equatable { public var live = 0; public var tombstones = 0
        public var total: Int { live + tombstones } }
    public struct ImportStats: Equatable { public var upserts = 0; public var deletes = 0
        public var deferred = 0; public var profileUpdated = false }

    enum Kind { case text, int, double, bool, json }

    /// Bumped when the canonical wire format changes in a way that makes this
    /// device's previously-emitted records wrong (v2: fold triage into status;
    /// v3: split job facts from the `triage` decision entity). A device whose
    /// stored `sync_format` is older force-re-exports once.
    static let syncFormatVersion = 3

    let db: AppDatabase
    let deviceId: String
    let loadProfile: () -> [String: JSONValue]?
    let saveProfile: ([String: JSONValue]) -> Void
    let docsLocalDir: URL?
    let now: () -> Date

    public init(db: AppDatabase, deviceId: String,
                loadProfile: @escaping () -> [String: JSONValue]? = { nil },
                saveProfile: @escaping ([String: JSONValue]) -> Void = { _ in },
                docsLocalDir: URL? = nil,
                now: @escaping () -> Date = { Date() }) {
        self.db = db
        self.deviceId = deviceId
        self.loadProfile = loadProfile
        self.saveProfile = saveProfile
        self.docsLocalDir = docsLocalDir
        self.now = now
    }

    // MARK: column metadata (iOS camelCase)

    // Job FACTS columns only. `status` and `triage` are deliberately excluded —
    // the user's lifecycle decision syncs as the separate `triage` entity.
    static let jobKinds: [(String, Kind)] = [
        ("source", .text), ("externalId", .text), ("title", .text), ("company", .text),
        ("location", .text), ("url", .text), ("description", .text), ("salaryMin", .int),
        ("salaryMax", .int), ("salaryPeriod", .text), ("datePosted", .text),
        ("dateDiscovered", .text), ("fitScore", .double),
        ("fitReasoning", .text), ("applyType", .text), ("isRemote", .bool),
        ("isEasyApply", .bool), ("tags", .json), ("matchReport", .json),
        ("embellishmentLog", .json), ("salaryEstimate", .json),
    ]
    static let appKinds: [(String, Kind)] = [
        ("resumeContent", .text), ("coverLetterContent", .text), ("customAnswers", .json),
        ("status", .text), ("honestyLevel", .text), ("stylePreset", .text),
        ("appliedAt", .text), ("createdAt", .text), ("updatedAt", .text),
    ]
    static let appDocs: [(String, String)] = [  // canonical ref key -> path column
        ("resume_doc", "resumeDocxPath"), ("cover_doc", "coverDocxPath"),
    ]

    // MARK: value <-> JSONValue

    private func read(_ row: Row, _ name: String, _ kind: Kind) -> JSONValue? {
        let dbv: DatabaseValue = row[name]
        if dbv.isNull { return nil }
        switch kind {
        case .text: return .string(String.fromDatabaseValue(dbv) ?? "")
        case .int: return .int(Int.fromDatabaseValue(dbv) ?? 0)
        case .double: return .double(Double.fromDatabaseValue(dbv) ?? 0)
        case .bool: return .bool((Int.fromDatabaseValue(dbv) ?? 0) != 0)
        case .json:
            let s = String.fromDatabaseValue(dbv) ?? "null"
            if let d = s.data(using: .utf8), let jv = try? JSONDecoder().decode(JSONValue.self, from: d) { return jv }
            return .null
        }
    }

    private func dbValue(_ jv: JSONValue, _ kind: Kind) -> (any DatabaseValueConvertible)? {
        switch kind {
        case .text: if case .string(let s) = jv { return s }; return nil
        case .int:
            if case .int(let i) = jv { return i }; if case .double(let d) = jv { return Int(d) }; return nil
        case .double:
            if case .double(let d) = jv { return d }; if case .int(let i) = jv { return Double(i) }; return nil
        case .bool:
            if case .bool(let b) = jv { return b ? 1 : 0 }; return 0
        case .json:
            let data = (try? JSONSerialization.data(withJSONObject: jv.toAny(), options: [.fragmentsAllowed])) ?? Data("null".utf8)
            return String(data: data, encoding: .utf8)
        }
    }

    // MARK: timestamps

    private static let isoFmt: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'"
        return f
    }()
    private func isoMs(_ date: Date) -> String { Self.isoFmt.string(from: date) }

    private func nextTS(_ dbc: Database) throws -> String {
        var candidate = now()
        if let last = try meta(dbc, "last_ts"), let lastDate = SyncEngine.isoFmt.date(from: last),
           candidate <= lastDate {
            candidate = lastDate.addingTimeInterval(0.001)
        }
        let stamp = isoMs(candidate)
        try setMeta(dbc, "last_ts", stamp)
        return stamp
    }

    // MARK: bookkeeping tables

    private func ensureTables(_ dbc: Database) throws {
        try dbc.execute(sql: """
            CREATE TABLE IF NOT EXISTS sync_snapshot (
                entity TEXT NOT NULL, id TEXT NOT NULL, updated_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0, data_json TEXT,
                PRIMARY KEY (entity, id));
            CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT);
            """)
    }

    private struct Snap { var updatedAt: String; var deleted: Bool; var dataJSON: String? }

    private func loadSnapshot(_ dbc: Database, _ entity: String) throws -> [String: Snap] {
        var out: [String: Snap] = [:]
        for row in try Row.fetchAll(dbc, sql:
            "SELECT id, updated_at, deleted, data_json FROM sync_snapshot WHERE entity = ?",
            arguments: [entity]) {
            out[row["id"]] = Snap(updatedAt: row["updated_at"],
                                  deleted: (row["deleted"] as Int) != 0,
                                  dataJSON: row["data_json"])
        }
        return out
    }

    private func putSnapshot(_ dbc: Database, _ entity: String, _ id: String,
                            _ ts: String, _ deleted: Bool, _ dataJSON: String?) throws {
        try dbc.execute(sql: """
            INSERT INTO sync_snapshot (entity, id, updated_at, deleted, data_json)
            VALUES (?,?,?,?,?)
            ON CONFLICT(entity, id) DO UPDATE SET
              updated_at = excluded.updated_at, deleted = excluded.deleted,
              data_json = excluded.data_json
            """, arguments: [entity, id, ts, deleted ? 1 : 0, dataJSON])
    }

    /// Flag a one-time re-export when the wire format version has advanced and
    /// this device already holds old-format snapshot rows (a fresh device has
    /// nothing to migrate, so it must not re-broadcast what it just imported).
    /// Runs at the head of both import and export so whichever fires first — and
    /// reads the pre-rebuild snapshot — decides; export consumes the flag.
    private func markMigrationIfNeeded(_ dbc: Database) throws {
        let stored = (try meta(dbc, "sync_format")).flatMap { Int($0) } ?? 0
        guard stored < Self.syncFormatVersion else { return }
        let count = try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM sync_snapshot") ?? 0
        if count > 0 { try setMeta(dbc, "pending_migration", "1") }
        try setMeta(dbc, "sync_format", String(Self.syncFormatVersion))
    }

    private func meta(_ dbc: Database, _ key: String) throws -> String? {
        try Row.fetchOne(dbc, sql: "SELECT value FROM sync_meta WHERE key = ?", arguments: [key])?["value"]
    }
    private func setMeta(_ dbc: Database, _ key: String, _ value: String) throws {
        try dbc.execute(sql:
            "INSERT INTO sync_meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            arguments: [key, value])
    }

    private func canon(_ data: [String: JSONValue]) -> String { data.canonicalString() }
    private func prevData(_ snap: Snap?) -> [String: JSONValue] {
        guard let snap, !snap.deleted, let json = snap.dataJSON, let d = json.data(using: .utf8),
              case .object(let o)? = try? JSONDecoder().decode(JSONValue.self, from: d) else { return [:] }
        return o
    }

    // MARK: current-state readers (DB -> canonical)

    private func store(_ folder: URL) -> DocumentStore? {
        guard let docsLocalDir else { return nil }
        return DocumentStore(storeDir: folder.appendingPathComponent("documents"), localDir: docsLocalDir)
    }

    private func jobSnapshot(_ dbc: Database) throws -> [String: [String: JSONValue]] {
        var out: [String: [String: JSONValue]] = [:]
        let cols = SyncEngine.jobKinds.map { $0.0 }.joined(separator: ", ")
        for row in try Row.fetchAll(dbc, sql:
            "SELECT \(cols) FROM jobs WHERE externalId IS NOT NULL AND externalId != ''") {
            var native: [String: JSONValue] = [:]
            for (name, kind) in SyncEngine.jobKinds { if let v = read(row, name, kind) { native[name] = v } }
            let source = native["source"]?.stringValue ?? ""
            let ext = native["externalId"]?.stringValue ?? ""
            out["\(source):\(ext)"] = SyncEntities.jobIOSToCanonical(native)
        }
        return out
    }

    /// The user's lifecycle decision per job (canonical `status`), keyed by sync
    /// id — the `triage` entity. Folds the iOS (triage, status) pair.
    private func triageSnapshot(_ dbc: Database) throws -> [String: [String: JSONValue]] {
        var out: [String: [String: JSONValue]] = [:]
        for row in try Row.fetchAll(dbc, sql:
            "SELECT source, externalId, status, triage FROM jobs WHERE externalId IS NOT NULL AND externalId != ''") {
            let source = (row["source"] as String?) ?? ""
            let ext = (row["externalId"] as String?) ?? ""
            let status = (row["status"] as String?) ?? "discovered"
            let triage = (row["triage"] as String?) ?? "new"
            out["\(source):\(ext)"] = SyncEntities.triageIOSToCanonical(triage: triage, status: status)
        }
        return out
    }

    private func appSnapshot(_ dbc: Database, _ store: DocumentStore?) throws -> [String: [String: JSONValue]] {
        var out: [String: [String: JSONValue]] = [:]
        let cols = (["id"] + SyncEngine.appKinds.map { "a.\($0.0)" } + SyncEngine.appDocs.map { "a.\($0.1)" }).joined(separator: ", ")
            .replacingOccurrences(of: "id,", with: "a.id,")
        for row in try Row.fetchAll(dbc, sql: """
            SELECT \(cols), j.source AS _src, j.externalId AS _ext
            FROM applications a JOIN jobs j ON j.id = a.jobId
            WHERE j.externalId IS NOT NULL AND j.externalId != ''
            """) {
            var native: [String: JSONValue] = [:]
            for (name, kind) in SyncEngine.appKinds { if let v = read(row, name, kind) { native[name] = v } }
            var data = SyncEntities.appIOSToCanonical(native)
            data["job_ref"] = .string("\(row["_src"] as String):\(row["_ext"] as String)")
            if let store {
                for (refKey, pathCol) in SyncEngine.appDocs {
                    let dbv: DatabaseValue = row[pathCol]
                    if !dbv.isNull, let path = String.fromDatabaseValue(dbv),
                       FileManager.default.fileExists(atPath: path),
                       let ref = try? store.put(URL(fileURLWithPath: path)) {
                        data[refKey] = .object(ref.mapValues { .string($0) })
                    }
                }
            }
            out[row["id"]] = data
        }
        return out
    }

    // MARK: export

    @discardableResult
    public func export(to folder: URL) throws -> ExportStats {
        var stats = ExportStats()
        var records: [ChangeRecord] = []
        let store = store(folder)

        try db.writer.write { dbc in
            try ensureTables(dbc)
            try markMigrationIfNeeded(dbc)
            let ts = try nextTS(dbc)

            // One-time format migration: records this device emitted under the
            // old format carry the wrong canonical `status` (a shortlisted job
            // as 'discovered', see SyncEntities). When flagged, re-emit every
            // current row once, ignoring the snapshot diff, so fresh correctly-
            // folded records supersede the stale ones for every other device.
            // This also defeats the case where a prior import rebuilt the
            // snapshot to match the DB and would otherwise suppress the fix.
            let force = (try meta(dbc, "pending_migration")) == "1"
            if force { try setMeta(dbc, "pending_migration", "0") }

            func diff(entity: String, current: [String: [String: JSONValue]]) throws {
                let snap = try loadSnapshot(dbc, entity)
                for (id, data) in current {
                    let merged = prevData(snap[id]).merging(data) { _, new in new }
                    let cj = canon(merged)
                    let prev = snap[id]
                    if force || prev == nil || prev!.deleted || prev!.dataJSON != cj {
                        records.append(ChangeRecord(entity: entity, id: id, updatedAt: ts,
                                                    device: deviceId, deleted: false, data: merged))
                        try putSnapshot(dbc, entity, id, ts, false, cj)
                        stats.live += 1
                    }
                }
                for (id, prev) in snap where !prev.deleted && current[id] == nil {
                    records.append(ChangeRecord(entity: entity, id: id, updatedAt: ts,
                                                device: deviceId, deleted: true, data: nil))
                    try putSnapshot(dbc, entity, id, ts, true, nil)
                    stats.tombstones += 1
                }
            }

            try diff(entity: "job", current: try jobSnapshot(dbc))
            try diff(entity: "triage", current: try triageSnapshot(dbc))
            try diff(entity: "application", current: try appSnapshot(dbc, store))

            if let iosProfile = loadProfile() {
                let canonProfile = SyncEntities.profileIOSToCanonical(iosProfile)
                let cj = canon(canonProfile)
                let snap = try loadSnapshot(dbc, "profile")["me"]
                if force || snap == nil || snap!.deleted || snap!.dataJSON != cj {
                    records.append(ChangeRecord(entity: "profile", id: "me", updatedAt: ts,
                                                device: deviceId, deleted: false, data: canonProfile))
                    try putSnapshot(dbc, "profile", "me", ts, false, cj)
                    stats.live += 1
                }
            }

            if !records.isEmpty { try appendLog(folder, records) }
        }
        return stats
    }

    private func appendLog(_ folder: URL, _ records: [ChangeRecord]) throws {
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        let logURL = changes.appendingPathComponent("\(deviceId).jsonl")
        let encoder = JSONEncoder()
        var text = (try? String(contentsOf: logURL, encoding: .utf8)) ?? ""
        for rec in records {
            text += String(data: try encoder.encode(rec), encoding: .utf8)! + "\n"
        }
        try text.write(to: logURL, atomically: true, encoding: .utf8)
    }

    // MARK: import

    @discardableResult
    public func importChanges(from folder: URL) throws -> ImportStats {
        let logs = SyncMerge.loadLogs(folder)
        let winners = SyncMerge.winners(logs)
        var stats = ImportStats()
        let store = store(folder)

        try db.writer.write { dbc in
            try ensureTables(dbc)
            try markMigrationIfNeeded(dbc)
            var deferred = Set<String>()

            // Live upserts in dependency order. A delete is NOT special here — it
            // is a `triage` record whose canonical status is 'deleted', applied
            // like any other decision. Facts first, then the decision, then apps.
            for (key, rec) in winners where key.entity == "job" && !rec.deleted {
                try applyJob(dbc, rec.data ?? [:]); stats.upserts += 1
            }
            for (key, rec) in winners where key.entity == "triage" && !rec.deleted {
                do { try applyTriage(dbc, key.id, rec.data ?? [:]); stats.upserts += 1 }
                catch is DeferError { deferred.insert("triage:\(key.id)"); stats.deferred += 1 }
            }
            for (key, rec) in winners where key.entity == "application" && !rec.deleted {
                do { try applyApplication(dbc, key.id, rec.data ?? [:], store); stats.upserts += 1 }
                catch is DeferError { deferred.insert("application:\(key.id)"); stats.deferred += 1 }
            }
            // Tombstones, children before parents. `triage` never tombstones
            // (a delete is a live 'deleted' status). Job tombstones are the
            // generic safety net for a physically-removed row.
            for (key, rec) in winners where key.entity == "application" && rec.deleted {
                try dbc.execute(sql: "DELETE FROM applications WHERE id = ?", arguments: [key.id]); stats.deletes += 1
            }
            for (key, rec) in winners where key.entity == "job" && rec.deleted {
                try deleteJob(dbc, key.id); stats.deletes += 1
            }
            // Profile.
            if let rec = winners[SyncMerge.Key(entity: "profile", id: "me")], !rec.deleted {
                let base = loadProfile() ?? [:]
                let merged = base.merging(SyncEntities.profileCanonicalToIOS(rec.data ?? [:])) { _, new in new }
                saveProfile(merged)
                stats.profileUpdated = true
            }

            try rebuildSnapshot(dbc, winners, deferred, store)
            if let maxTS = winners.values.map(\.updatedAt).max() {
                if let last = try meta(dbc, "last_ts"), last >= maxTS {} else { try setMeta(dbc, "last_ts", maxTS) }
            }
        }
        return stats
    }

    struct DeferError: Error {}

    private func resolveJobId(_ dbc: Database, _ jobRef: String) throws -> String? {
        let parts = jobRef.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2 else { return nil }
        return try Row.fetchOne(dbc, sql: "SELECT id FROM jobs WHERE source = ? AND externalId = ?",
                                arguments: [String(parts[0]), String(parts[1])])?["id"]
    }

    private func applyJob(_ dbc: Database, _ canonData: [String: JSONValue]) throws {
        var native = SyncEntities.jobCanonicalToIOS(canonData)
        if native["dateDiscovered"] == nil { native["dateDiscovered"] = .string(isoMs(now())) }
        let source = native["source"]?.stringValue ?? ""
        let ext = native["externalId"]?.stringValue ?? ""
        let kinds = Dictionary(uniqueKeysWithValues: SyncEngine.jobKinds)

        let existing = try Row.fetchOne(dbc, sql: "SELECT id FROM jobs WHERE source = ? AND externalId = ?",
                                        arguments: [source, ext])
        var cols: [String] = [], args: [(any DatabaseValueConvertible)?] = []
        for (name, jv) in native { if let kind = kinds[name] { cols.append(name); args.append(dbValue(jv, kind)) } }

        if let existing {
            let setClause = cols.map { "\($0) = ?" }.joined(separator: ", ")
            try dbc.execute(sql: "UPDATE jobs SET \(setClause) WHERE id = ?",
                            arguments: StatementArguments(args + [existing["id"] as String]))
        } else {
            let allCols = ["id"] + cols
            let placeholders = allCols.map { _ in "?" }.joined(separator: ", ")
            try dbc.execute(sql: "INSERT INTO jobs (\(allCols.joined(separator: ", "))) VALUES (\(placeholders))",
                            arguments: StatementArguments([UUID().uuidString] + args))
        }
    }

    /// Apply a `triage` decision: unfold the canonical status into the iOS
    /// (triage, status) pair and write it onto the job row. Defers if the job's
    /// facts haven't been imported yet.
    private func applyTriage(_ dbc: Database, _ syncId: String, _ canonData: [String: JSONValue]) throws {
        let parts = syncId.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2,
              let localId = try Row.fetchOne(dbc, sql: "SELECT id FROM jobs WHERE source = ? AND externalId = ?",
                                             arguments: [String(parts[0]), String(parts[1])])?["id"] as String?
        else { throw DeferError() }
        let (triage, status) = SyncEntities.triageCanonicalToIOS(canonData)
        try dbc.execute(sql: "UPDATE jobs SET status = ?, triage = ? WHERE id = ?",
                        arguments: [status, triage, localId])
    }

    private func applyApplication(_ dbc: Database, _ id: String, _ canonData: [String: JSONValue],
                                  _ store: DocumentStore?) throws {
        guard let jobRef = canonData["job_ref"]?.stringValue, let jobId = try resolveJobId(dbc, jobRef) else {
            throw DeferError()
        }
        var native = SyncEntities.appCanonicalToIOS(canonData)
        if native["createdAt"] == nil { native["createdAt"] = .string(isoMs(now())) }
        if native["updatedAt"] == nil { native["updatedAt"] = native["createdAt"] }
        let kinds = Dictionary(uniqueKeysWithValues: SyncEngine.appKinds)

        var cols: [String] = [], args: [(any DatabaseValueConvertible)?] = []
        for (name, jv) in native { if let kind = kinds[name] { cols.append(name); args.append(dbValue(jv, kind)) } }

        let existing = try Row.fetchOne(dbc, sql: "SELECT id FROM applications WHERE id = ?", arguments: [id])
        if existing != nil {
            let setClause = (["jobId"] + cols).map { "\($0) = ?" }.joined(separator: ", ")
            try dbc.execute(sql: "UPDATE applications SET \(setClause) WHERE id = ?",
                            arguments: StatementArguments([jobId] + args + [id]))
        } else {
            let allCols = ["id", "jobId"] + cols
            let placeholders = allCols.map { _ in "?" }.joined(separator: ", ")
            try dbc.execute(sql: "INSERT INTO applications (\(allCols.joined(separator: ", "))) VALUES (\(placeholders))",
                            arguments: StatementArguments([id, jobId] + args))
        }

        if let store {
            for (refKey, pathCol) in SyncEngine.appDocs {
                guard case .object(let refObj)? = canonData[refKey] else { continue }
                var ref: [String: String] = [:]
                for (k, v) in refObj { if case .string(let s) = v { ref[k] = s } }
                let base = "\(id)_" + refKey.replacingOccurrences(of: "_doc", with: "")
                if let local = try store.materialize(ref, basename: base) {
                    try dbc.execute(sql: "UPDATE applications SET \(pathCol) = ? WHERE id = ?",
                                    arguments: [local.path, id])
                }
            }
        }
    }

    private func deleteJob(_ dbc: Database, _ syncId: String) throws {
        let parts = syncId.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2,
              let localId = try Row.fetchOne(dbc, sql: "SELECT id FROM jobs WHERE source = ? AND externalId = ?",
                                             arguments: [String(parts[0]), String(parts[1])])?["id"] as String?
        else { return }
        try dbc.execute(sql: "DELETE FROM applications WHERE jobId = ?", arguments: [localId])
        try dbc.execute(sql: "DELETE FROM jobs WHERE id = ?", arguments: [localId])
    }

    private func rebuildSnapshot(_ dbc: Database, _ winners: [SyncMerge.Key: ChangeRecord],
                                 _ deferred: Set<String>, _ store: DocumentStore?) throws {
        let jobs = try jobSnapshot(dbc)
        let triage = try triageSnapshot(dbc)
        let apps = try appSnapshot(dbc, store)
        for (key, rec) in winners {
            if rec.deleted {
                try putSnapshot(dbc, key.entity, key.id, rec.updatedAt, true, nil); continue
            }
            if deferred.contains("\(key.entity):\(key.id)") { continue }
            let dataJSON: String
            switch key.entity {
            case "profile":
                dataJSON = canon(SyncEntities.profileIOSToCanonical(loadProfile() ?? [:]))
            case "job":
                guard let known = jobs[key.id] else { continue }
                dataJSON = canon((rec.data ?? [:]).merging(known) { _, new in new })
            case "triage":
                guard let known = triage[key.id] else { continue }
                dataJSON = canon((rec.data ?? [:]).merging(known) { _, new in new })
            case "application":
                guard let known = apps[key.id] else { continue }
                dataJSON = canon((rec.data ?? [:]).merging(known) { _, new in new })
            default: continue
            }
            try putSnapshot(dbc, key.entity, key.id, rec.updatedAt, false, dataJSON)
        }
    }
}
