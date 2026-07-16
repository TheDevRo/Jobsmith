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
/// Entities: job, triage, application, application_event, application_schedule,
/// work_request, profile, and setting (per-key config LWW). Documents travel as
/// content-addressed references (resume_doc / cover_doc) via DocumentStore.
/// The `answer` entity is intentionally desktop-only: iOS's answer_bank has a
/// different model, so this engine neither emits `answer` records nor imports
/// them — a client with no handler for an entity skips it (see
/// SyncConformanceTests), and iOS skips `answer` records by design.
public final class SyncEngine {
    public struct ExportStats: Equatable { public var live = 0; public var tombstones = 0
        public var total: Int { live + tombstones } }
    public struct ImportStats: Equatable { public var upserts = 0; public var deletes = 0
        public var deferred = 0; public var profileUpdated = false; public var settingsUpdated = 0 }

    enum Kind { case text, int, double, bool, json }

    /// Bumped when the canonical wire format changes in a way that makes this
    /// device's previously-emitted records wrong (v2: fold triage into status;
    /// v3: split job facts from the `triage` decision entity; v4: add the config-
    /// backed `setting` entity). A device whose stored `sync_format` is older
    /// force-re-exports once. A client predating an entity MUST skip records it
    /// has no handler for on import (it does — see SyncConformanceTests).
    /// Keep in lockstep with backend/sync/engine.py:SYNC_FORMAT_VERSION.
    static let syncFormatVersion = 4

    let db: AppDatabase
    let deviceId: String
    let loadProfile: () -> [String: JSONValue]?
    let saveProfile: ([String: JSONValue]) -> Void
    /// Config-backed `setting` bridge (per-key LWW). loadSettings hands back the
    /// iOS-native config dict (camelCase, nested honesty/search/ai/promptOverrides)
    /// snapshotted before the run; saveSettings writes the merged result after.
    /// `settingsEnabled` is the set of category keys this device syncs (device-
    /// local UserDefaults flags), and also gates the profile bridge via
    /// `.contains("profile")`.
    let loadSettings: () -> [String: JSONValue]?
    let saveSettings: ([String: JSONValue]) -> Void
    let settingsEnabled: Set<String>
    let docsLocalDir: URL?
    let now: () -> Date

    private var profileEnabled: Bool { settingsEnabled.contains("profile") }

    public init(db: AppDatabase, deviceId: String,
                loadProfile: @escaping () -> [String: JSONValue]? = { nil },
                saveProfile: @escaping ([String: JSONValue]) -> Void = { _ in },
                loadSettings: @escaping () -> [String: JSONValue]? = { nil },
                saveSettings: @escaping ([String: JSONValue]) -> Void = { _ in },
                settingsEnabled: Set<String> = ["profile"],
                docsLocalDir: URL? = nil,
                now: @escaping () -> Date = { Date() }) {
        self.db = db
        self.deviceId = deviceId
        self.loadProfile = loadProfile
        self.saveProfile = saveProfile
        self.loadSettings = loadSettings
        self.saveSettings = saveSettings
        self.settingsEnabled = settingsEnabled
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

    /// The `application_event` entity — one immutable outcome transition each.
    ///
    /// The outcome is NOT a field on `application` on purpose. Merging is
    /// last-writer-wins over the whole record, so an outcome recorded here would
    /// be silently dropped the moment the desktop touched any other field of the
    /// same application. Events never change once written, so two devices that
    /// each record outcomes offline merge as a plain union — nothing to lose.
    /// `applications.outcome` is recomputed from the merged history on import.
    ///
    /// Identity is content-derived — "{applicationId}:{occurredAt}:{toOutcome}" —
    /// so the same transition seen twice is the same record. Must match the
    /// desktop's ApplicationEventAdapter.sync_id.
    private func appEventSnapshot(_ dbc: Database) throws -> [String: [String: JSONValue]] {
        var out: [String: [String: JSONValue]] = [:]
        for row in try Row.fetchAll(dbc, sql: """
            SELECT e.applicationId, e.fromOutcome, e.toOutcome, e.occurredAt, e.note, e.source
            FROM application_events e
            JOIN applications a ON a.id = e.applicationId
            JOIN jobs j ON j.id = a.jobId
            WHERE j.externalId IS NOT NULL AND j.externalId != ''
            """) {
            let appId = (row["applicationId"] as String?) ?? ""
            let occurredAt = (row["occurredAt"] as String?) ?? ""
            let toOutcome = (row["toOutcome"] as String?) ?? ""
            var data: [String: JSONValue] = [
                "application_ref": .string(appId),
                "to_outcome": .string(toOutcome),
                "occurred_at": .string(occurredAt),
                "source": .string((row["source"] as String?) ?? "user"),
            ]
            if let from = row["fromOutcome"] as String? { data["from_outcome"] = .string(from) }
            if let note = row["note"] as String? { data["note"] = .string(note) }
            out["\(appId):\(occurredAt):\(toOutcome)"] = data
        }
        return out
    }

    /// Insert a synced outcome event, then refresh the derived column. Defers if
    /// the application hasn't been imported yet. Re-importing the same event is a
    /// no-op — events are immutable and identified by their content.
    private func applyAppEvent(_ dbc: Database, _ syncId: String, _ canonData: [String: JSONValue]) throws {
        let appId = canonData["application_ref"]?.stringValue
            ?? String(syncId.prefix(while: { $0 != ":" }))
        guard try Row.fetchOne(dbc, sql: "SELECT id FROM applications WHERE id = ?",
                               arguments: [appId]) != nil else { throw DeferError() }

        let occurredAt = canonData["occurred_at"]?.stringValue ?? ""
        let toOutcome = canonData["to_outcome"]?.stringValue ?? ""
        let existing = try Row.fetchOne(dbc, sql: """
            SELECT id FROM application_events
            WHERE applicationId = ? AND occurredAt = ? AND toOutcome = ?
            """, arguments: [appId, occurredAt, toOutcome])
        if existing == nil {
            try dbc.execute(sql: """
                INSERT INTO application_events
                    (applicationId, fromOutcome, toOutcome, occurredAt, note, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """, arguments: [appId, canonData["from_outcome"]?.stringValue, toOutcome,
                                 occurredAt, canonData["note"]?.stringValue,
                                 canonData["source"]?.stringValue ?? "user"])
        }
        try ApplicationStore.recomputeOutcome(dbc, applicationId: appId)
    }

    /// The `application_schedule` entity — reminder dates, keyed by application id.
    ///
    /// Its own entity for the same reason the outcome is: LWW resolves whole
    /// records, so dates carried on `application` would be wiped by any unrelated
    /// edit from the other device. Unlike events these are mutable (you reschedule
    /// an interview), so they stay last-writer-wins — just on their own stream.
    /// An application with no dates is absent from the snapshot, which the engine
    /// emits as a tombstone: "no dates", not "no application".
    private func scheduleSnapshot(_ dbc: Database) throws -> [String: [String: JSONValue]] {
        var out: [String: [String: JSONValue]] = [:]
        for row in try Row.fetchAll(dbc, sql: """
            SELECT a.id, a.followUpAt, a.interviewAt
            FROM applications a JOIN jobs j ON j.id = a.jobId
            WHERE j.externalId IS NOT NULL AND j.externalId != ''
              AND (a.followUpAt IS NOT NULL OR a.interviewAt IS NOT NULL)
            """) {
            var data: [String: JSONValue] = [:]
            data["follow_up_at"] = (row["followUpAt"] as String?).map { .string($0) } ?? .null
            data["interview_at"] = (row["interviewAt"] as String?).map { .string($0) } ?? .null
            out[row["id"]] = data
        }
        return out
    }

    private func applySchedule(_ dbc: Database, _ id: String, _ canonData: [String: JSONValue]) throws {
        guard try Row.fetchOne(dbc, sql: "SELECT id FROM applications WHERE id = ?",
                               arguments: [id]) != nil else { throw DeferError() }
        try dbc.execute(sql: "UPDATE applications SET followUpAt = ?, interviewAt = ? WHERE id = ?",
                        arguments: [canonData["follow_up_at"]?.stringValue,
                                    canonData["interview_at"]?.stringValue, id])
    }

    /// The `work_request` entity — a hand-off command ("score everything
    /// unscored"), keyed by its own UUID. One flat LWW record: the requester
    /// writes it `pending`, the fulfiller re-emits it `done` with a newer
    /// timestamp, and whichever devices see both keep the later one. It
    /// carries no job references, so there is nothing to defer on.
    /// Must match the desktop's WorkRequestAdapter (backend/sync/entities.py).
    private func workRequestSnapshot(_ dbc: Database) throws -> [String: [String: JSONValue]] {
        var out: [String: [String: JSONValue]] = [:]
        for row in try Row.fetchAll(dbc, sql: "SELECT * FROM work_requests") {
            var data: [String: JSONValue] = [
                "kind": .string((row["kind"] as String?) ?? ""),
                "status": .string((row["status"] as String?) ?? "pending"),
                "params": .object(WorkRequestStore.decode(row["paramsJSON"])),
            ]
            if let v = row["requestedBy"] as String? { data["requested_by"] = .string(v) }
            if let v = row["requestedAt"] as String? { data["requested_at"] = .string(v) }
            if let v = row["completedBy"] as String? { data["completed_by"] = .string(v) }
            if let v = row["completedAt"] as String? { data["completed_at"] = .string(v) }
            out[row["id"]] = data
        }
        return out
    }

    private func applyWorkRequest(_ dbc: Database, _ id: String, _ canonData: [String: JSONValue]) throws {
        let params: [String: JSONValue]
        if case .object(let obj)? = canonData["params"] { params = obj } else { params = [:] }
        try dbc.execute(sql: """
            INSERT INTO work_requests
                (id, kind, status, requestedBy, requestedAt, completedBy, completedAt, paramsJSON)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind = excluded.kind, status = excluded.status,
                requestedBy = excluded.requestedBy, requestedAt = excluded.requestedAt,
                completedBy = excluded.completedBy, completedAt = excluded.completedAt,
                paramsJSON = excluded.paramsJSON
            """, arguments: [id,
                             canonData["kind"]?.stringValue ?? "",
                             canonData["status"]?.stringValue ?? "pending",
                             canonData["requested_by"]?.stringValue,
                             canonData["requested_at"]?.stringValue,
                             canonData["completed_by"]?.stringValue,
                             canonData["completed_at"]?.stringValue,
                             WorkRequestStore.encode(params)])
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
            try diff(entity: "application_event", current: try appEventSnapshot(dbc))
            try diff(entity: "application_schedule", current: try scheduleSnapshot(dbc))
            try diff(entity: "work_request", current: try workRequestSnapshot(dbc))

            // Profile bridge — GATED by the `profile` category. Off ⇒ skip export
            // entirely (and never tombstone profile/me), keeping a device-specific
            // profile.
            if profileEnabled, let iosProfile = loadProfile() {
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

            // Settings bridge — one record per enabled canonical path. Only
            // modeled paths ever enter the snapshot, so an unmodeled path a peer
            // sent (pipeline.*) is never tombstoned from here.
            if let cfg = loadSettings() {
                let current = SettingsSync.export(cfg, enabled: settingsEnabled)
                let snap = try loadSnapshot(dbc, "setting")
                for (path, data) in current {
                    let cj = canon(data)
                    let prev = snap[path]
                    if force || prev == nil || prev!.deleted || prev!.dataJSON != cj {
                        records.append(ChangeRecord(entity: "setting", id: path, updatedAt: ts,
                                                    device: deviceId, deleted: false, data: data))
                        try putSnapshot(dbc, "setting", path, ts, false, cj)
                        stats.live += 1
                    }
                }
                for (path, prev) in snap where !prev.deleted && current[path] == nil {
                    guard SettingsSync.isModeled(path),
                          let cat = SettingsSync.category(for: path), settingsEnabled.contains(cat)
                    else { continue }
                    // A model tier newly routed on-device drops out of the
                    // export set — but that is "this device opted out", not
                    // "delete this setting": a tombstone here would erase the
                    // desktop's own model choice.
                    if SettingsSync.isDeviceLocal(path, config: cfg) { continue }
                    records.append(ChangeRecord(entity: "setting", id: path, updatedAt: ts,
                                                device: deviceId, deleted: true, data: nil))
                    try putSnapshot(dbc, "setting", path, ts, true, nil)
                    stats.tombstones += 1
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
            // Outcome history last — an event needs its application to exist.
            for (key, rec) in winners where key.entity == "application_event" && !rec.deleted {
                do { try applyAppEvent(dbc, key.id, rec.data ?? [:]); stats.upserts += 1 }
                catch is DeferError { deferred.insert("application_event:\(key.id)"); stats.deferred += 1 }
            }
            for (key, rec) in winners where key.entity == "application_schedule" && !rec.deleted {
                do { try applySchedule(dbc, key.id, rec.data ?? [:]); stats.upserts += 1 }
                catch is DeferError { deferred.insert("application_schedule:\(key.id)"); stats.deferred += 1 }
            }
            // Work requests have no dependencies — the fulfilling side derives
            // the actual work from its own database.
            for (key, rec) in winners where key.entity == "work_request" && !rec.deleted {
                try applyWorkRequest(dbc, key.id, rec.data ?? [:]); stats.upserts += 1
            }
            // Tombstones, children before parents. `triage` never tombstones
            // (a delete is a live 'deleted' status). Job tombstones are the
            // generic safety net for a physically-removed row.
            //
            // A schedule tombstone means "the dates were cleared", NOT "the
            // application was deleted" — so it nulls the columns and leaves the
            // row alone.
            for (key, rec) in winners where key.entity == "application_schedule" && rec.deleted {
                try dbc.execute(
                    sql: "UPDATE applications SET followUpAt = NULL, interviewAt = NULL WHERE id = ?",
                    arguments: [key.id])
                stats.deletes += 1
            }
            // A work-request tombstone is the requester pruning old news.
            for (key, rec) in winners where key.entity == "work_request" && rec.deleted {
                try dbc.execute(sql: "DELETE FROM work_requests WHERE id = ?", arguments: [key.id])
                stats.deletes += 1
            }
            for (key, rec) in winners where key.entity == "application_event" && rec.deleted {
                try deleteAppEvent(dbc, key.id); stats.deletes += 1
            }
            for (key, rec) in winners where key.entity == "application" && rec.deleted {
                try dbc.execute(sql: "DELETE FROM applications WHERE id = ?", arguments: [key.id]); stats.deletes += 1
            }
            for (key, rec) in winners where key.entity == "job" && rec.deleted {
                try deleteJob(dbc, key.id); stats.deletes += 1
            }
            // Profile — GATED by the `profile` category (symmetric with export).
            if profileEnabled,
               let rec = winners[SyncMerge.Key(entity: "profile", id: "me")], !rec.deleted {
                let base = loadProfile() ?? [:]
                let merged = base.merging(SyncEntities.profileCanonicalToIOS(rec.data ?? [:])) { _, new in new }
                saveProfile(merged)
                stats.profileUpdated = true
            }

            // Settings — apply each winning `setting/<path>` whose category is
            // enabled here. loadSettings hands back a pre-run snapshot, so we
            // thread the merged dict straight into rebuildSnapshot rather than
            // re-loading (the closure wouldn't yet see the write-back).
            var settingsCfg = loadSettings()
            if settingsCfg != nil {
                var changed = 0
                for (key, rec) in winners where key.entity == "setting" {
                    guard let cat = SettingsSync.category(for: key.id),
                          settingsEnabled.contains(cat) else { continue }
                    if rec.deleted {
                        SettingsSync.removePrompt(&settingsCfg!, key.id)  // unmodeled: no-op
                    } else {
                        SettingsSync.apply(&settingsCfg!, path: key.id,
                                           value: rec.data?["value"] ?? .null)
                    }
                    changed += 1
                }
                if changed > 0 { saveSettings(settingsCfg!); stats.settingsUpdated = changed }
            }

            try rebuildSnapshot(dbc, winners, deferred, store, settingsCfg)
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

    /// An event is only tombstoned when its application went away; the id is
    /// content-derived, so split it back into its parts. The toOutcome has no
    /// colons, so the last one separates it from the timestamp.
    private func deleteAppEvent(_ dbc: Database, _ syncId: String) throws {
        guard let firstColon = syncId.firstIndex(of: ":"),
              let lastColon = syncId.lastIndex(of: ":"), firstColon < lastColon else { return }
        let appId = String(syncId[syncId.startIndex..<firstColon])
        let occurredAt = String(syncId[syncId.index(after: firstColon)..<lastColon])
        let toOutcome = String(syncId[syncId.index(after: lastColon)...])
        try dbc.execute(sql: """
            DELETE FROM application_events
            WHERE applicationId = ? AND occurredAt = ? AND toOutcome = ?
            """, arguments: [appId, occurredAt, toOutcome])
        try ApplicationStore.recomputeOutcome(dbc, applicationId: appId)
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
                                 _ deferred: Set<String>, _ store: DocumentStore?,
                                 _ settingsCfg: [String: JSONValue]?) throws {
        let jobs = try jobSnapshot(dbc)
        let triage = try triageSnapshot(dbc)
        let apps = try appSnapshot(dbc, store)
        let appEvents = try appEventSnapshot(dbc)
        let schedules = try scheduleSnapshot(dbc)
        let workRequests = try workRequestSnapshot(dbc)
        // Re-read what we'd emit next for settings so a following export is a
        // no-op; only modeled paths in an enabled category were applied.
        let settingsExport = settingsCfg.map { SettingsSync.export($0, enabled: settingsEnabled) } ?? [:]
        for (key, rec) in winners {
            if rec.deleted {
                if key.entity == "setting",
                   !(SettingsSync.isModeled(key.id)
                     && (SettingsSync.category(for: key.id).map(settingsEnabled.contains) ?? false)) {
                    continue  // unmodeled / disabled: we never touched it
                }
                try putSnapshot(dbc, key.entity, key.id, rec.updatedAt, true, nil); continue
            }
            if deferred.contains("\(key.entity):\(key.id)") { continue }
            let dataJSON: String
            switch key.entity {
            case "setting":
                // Skip unmodeled / disabled paths so re-enabling later still
                // exports our local value (diff vs an untouched snapshot).
                guard let cat = SettingsSync.category(for: key.id), settingsEnabled.contains(cat),
                      SettingsSync.isModeled(key.id), let data = settingsExport[key.id] else { continue }
                dataJSON = canon(data)
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
            case "application_event":
                guard let known = appEvents[key.id] else { continue }
                dataJSON = canon((rec.data ?? [:]).merging(known) { _, new in new })
            case "application_schedule":
                guard let known = schedules[key.id] else { continue }
                dataJSON = canon((rec.data ?? [:]).merging(known) { _, new in new })
            case "work_request":
                guard let known = workRequests[key.id] else { continue }
                dataJSON = canon((rec.data ?? [:]).merging(known) { _, new in new })
            default: continue
            }
            try putSnapshot(dbc, key.entity, key.id, rec.updatedAt, false, dataJSON)
        }
    }
}
