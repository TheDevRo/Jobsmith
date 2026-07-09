import XCTest
import GRDB
@testable import JobsmithKit

/// Real GRDB round-trip for the iOS SyncEngine, mirroring
/// tests/test_sync_engine.py: export -> folder -> import across two in-memory
/// databases ("device A" and "device B"), plus LWW, tombstone, and profile.
final class SyncEngineTests: XCTestCase {

    /// Monotonic fake clock so last-writer-wins is deterministic.
    final class Clock: @unchecked Sendable {
        var t = Date(timeIntervalSince1970: 1_775_000_000)
        func now() -> Date { t += 1; return t }
    }

    private func seedJob(_ db: AppDatabase, externalId: String, fitScore: Double) throws -> String {
        try db.writer.write { dbc in
            var job = Job(from: NormalizedJob(source: "greenhouse", externalId: externalId,
                                              title: "Engineer", company: "Acme",
                                              tags: ["swift"], isRemote: true))
            job.fitScore = fitScore
            job.status = "discovered"
            try job.insert(dbc)
            return job.id
        }
    }

    func testExportImportRoundTrip() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "111", fitScore: 87.5)
        _ = try dbA.writer.write { dbc -> Void in
            let app = Application(jobId: jobId, resumeContent: "R", coverLetterContent: "C",
                                  honestyLevel: "honest", stylePreset: "modern")
            try app.insert(dbc)
        }

        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let exp = try a.export(to: folder)
        XCTAssertEqual(exp.live, 2)
        XCTAssertEqual(exp.tombstones, 0)

        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)
        let imp = try b.importChanges(from: folder)
        XCTAssertEqual(imp.upserts, 2)
        XCTAssertEqual(imp.deferred, 0)

        try dbB.writer.read { dbc in
            let job = try Row.fetchOne(dbc, sql: "SELECT * FROM jobs WHERE externalId = '111'")!
            XCTAssertEqual(job["title"] as String, "Engineer")
            XCTAssertEqual(job["fitScore"] as Double, 87.5)
            XCTAssertEqual(job["isRemote"] as Int, 1)
            XCTAssertEqual(job["triage"] as String, "new")  // status 'discovered' unfolds to triage 'new'

            let app = try Row.fetchOne(dbc, sql: "SELECT * FROM applications")!
            XCTAssertEqual(app["status"] as String, "pending_review")
            XCTAssertEqual(app["stylePreset"] as String, "modern")
            XCTAssertEqual(app["resumeContent"] as String, "R")
        }

        // Re-export from B emits nothing (snapshot is in sync).
        let reexp = try b.export(to: folder.appendingPathComponent("empty"))
        XCTAssertEqual(reexp.total, 0)
    }

    /// A shortlist (or dismiss) crosses devices: iOS models it on `triage`, but
    /// the wire carries the desktop's single `status`. Device A shortlists;
    /// device B must end up with triage='shortlisted' (in its Pipeline), not
    /// stuck in the inbox — proving foldStatus/unfoldStatus round-trips.
    func testShortlistLifecycleCrossesDevices() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "222", fitScore: 80)
        // Shortlist on A: triage moves, status stays 'discovered' (the "just
        // shortlisted" pipeline stage).
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET triage = 'shortlisted' WHERE id = ?", arguments: [jobId])
        }
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        XCTAssertEqual(try a.export(to: folder).live, 1)

        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)
        XCTAssertEqual(try b.importChanges(from: folder).upserts, 1)

        try dbB.writer.read { dbc in
            let job = try Row.fetchOne(dbc, sql: "SELECT * FROM jobs WHERE externalId = '222'")!
            XCTAssertEqual(job["triage"] as String, "shortlisted")  // reaches B's Pipeline
            XCTAssertEqual(job["status"] as String, "discovered")   // sub-stage unchanged
        }
        // B re-exports nothing: the fold is idempotent through the snapshot.
        XCTAssertEqual(try b.export(to: folder.appendingPathComponent("empty2")).total, 0)
    }

    /// Migration: a device that already exported a shortlisted job under the old
    /// format (status='discovered') has a snapshot that would suppress the
    /// corrective export. Bumping the format version force-re-emits every row
    /// once, so a fresh status='shortlisted' record supersedes the stale one —
    /// then settles back to a no-op.
    func testFormatMigrationForcesReexport() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "333", fitScore: 70)
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET triage = 'shortlisted' WHERE id = ?", arguments: [jobId])
        }
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)

        // First export migrates (format absent -> re-emits) and sets format=2.
        XCTAssertEqual(try a.export(to: folder).live, 1)
        // Simulate a device stuck on the old format with a matching snapshot:
        // roll the stored version back so the next export must force again.
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "UPDATE sync_meta SET value = '1' WHERE key = 'sync_format'")
        }
        let folder2 = folder.appendingPathComponent("mig")
        // Even though the snapshot already matches, force re-emits the job.
        XCTAssertEqual(try a.export(to: folder2).live, 1)

        // The forced record folds correctly: B lands it in the Pipeline.
        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)
        XCTAssertEqual(try b.importChanges(from: folder2).upserts, 1)
        try dbB.writer.read { dbc in
            let job = try Row.fetchOne(dbc, sql: "SELECT * FROM jobs WHERE externalId = '333'")!
            XCTAssertEqual(job["triage"] as String, "shortlisted")
        }
        // Migration is one-shot: a subsequent export is a no-op.
        XCTAssertEqual(try a.export(to: folder2.appendingPathComponent("after")).total, 0)
    }

    /// Back-compat: importing an OLD-format record (status='discovered' plus a
    /// raw triage='shortlisted') must honor the explicit triage, not reset the
    /// job to the inbox by deriving triage from status.
    func testImportHonorsRawTriageFromOldFormat() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        // Hand-write an old-format job record (pre-fold wire shape).
        let oldRecord = """
        {"v":1,"entity":"job","id":"greenhouse:444","updated_at":"2026-07-08T10:00:00.000Z",\
        "device":"OLD1","deleted":false,"data":{"source":"greenhouse","external_id":"444",\
        "title":"Legacy Job","status":"discovered","triage":"shortlisted"}}
        """
        try (oldRecord + "\n").write(to: changes.appendingPathComponent("OLD1.jsonl"),
                                     atomically: true, encoding: .utf8)

        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)
        XCTAssertEqual(try b.importChanges(from: folder).upserts, 1)
        try dbB.writer.read { dbc in
            let job = try Row.fetchOne(dbc, sql: "SELECT * FROM jobs WHERE externalId = '444'")!
            XCTAssertEqual(job["triage"] as String, "shortlisted")  // NOT reset to 'new'
        }
    }

    func testLastWriterWins() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        _ = try seedJob(dbA, externalId: "111", fitScore: 10)
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        try a.export(to: folder)
        try b.importChanges(from: folder)

        // B edits the job later than A's create -> B wins. Use a realistic
        // state: applying implies the job was shortlisted first (triage drives
        // the lifecycle; an untriaged inbox job is 'discovered' on the wire).
        try dbB.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET fitScore = 99, triage = 'shortlisted', status = 'applied' WHERE externalId = '111'")
        }
        let bexp = try b.export(to: folder)
        XCTAssertEqual(bexp.live, 1)

        try a.importChanges(from: folder)
        try dbA.writer.read { dbc in
            let job = try Row.fetchOne(dbc, sql: "SELECT fitScore, status, triage FROM jobs WHERE externalId = '111'")!
            XCTAssertEqual(job["fitScore"] as Double, 99)
            XCTAssertEqual(job["status"] as String, "applied")   // status folds/unfolds intact
            XCTAssertEqual(job["triage"] as String, "shortlisted")  // and the lifecycle axis crosses
        }
    }

    func testTombstoneDeletesAcrossDevices() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "111", fitScore: 10)
        _ = try dbA.writer.write { dbc -> Void in
            try Application(jobId: jobId, resumeContent: "R", coverLetterContent: "C",
                            honestyLevel: "honest", stylePreset: "standard").insert(dbc)
        }
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        try a.export(to: folder)
        try b.importChanges(from: folder)
        try dbB.writer.read { XCTAssertEqual(try Int.fetchOne($0, sql: "SELECT COUNT(*) FROM jobs"), 1) }

        // A deletes the job (cascade removes its application).
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "DELETE FROM applications")
            try dbc.execute(sql: "DELETE FROM jobs")
        }
        let aexp = try a.export(to: folder)
        XCTAssertEqual(aexp.tombstones, 2)

        let imp = try b.importChanges(from: folder)
        XCTAssertEqual(imp.deletes, 2)
        try dbB.writer.read { dbc in
            XCTAssertEqual(try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM jobs"), 0)
            XCTAssertEqual(try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM applications"), 0)
        }
    }

    func testProfileSyncsWithBaseOverlay() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let profileA: [String: JSONValue] = [
            "fullName": .string("Deven"), "email": .string("d@example.com"),
            "summary": .string("iOS dev"), "skills": .array([.string("Swift")]),
        ]
        let a = SyncEngine(db: dbA, deviceId: "A1B2", loadProfile: { profileA }, now: clock.now)
        let exp = try a.export(to: folder)
        XCTAssertEqual(exp.live, 1)

        let dbB = try AppDatabase.inMemory()
        var savedB: [String: JSONValue] = ["noticePeriod": .string("2 weeks")]  // B-local field
        let b = SyncEngine(db: dbB, deviceId: "C3D4",
                           loadProfile: { savedB }, saveProfile: { savedB = $0 }, now: clock.now)
        let imp = try b.importChanges(from: folder)
        XCTAssertTrue(imp.profileUpdated)
        XCTAssertEqual(savedB["fullName"], .string("Deven"))
        XCTAssertEqual(savedB["summary"], .string("iOS dev"))
        XCTAssertEqual(savedB["noticePeriod"], .string("2 weeks"))  // preserved
    }

    func testDeletePropagatesThroughImportFirstCycle() throws {
        // A hard delete must survive a real cycle (import BEFORE export) and
        // reach the other device. Without a durable tombstone the import re-adds
        // the job from the folder's still-live record — the resurrection bug.
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "111", fitScore: 42)
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        // Both devices hold the job.
        try a.export(to: folder)
        try b.importChanges(from: folder)
        try dbB.writer.read { XCTAssertEqual(try Int.fetchOne($0, sql: "SELECT COUNT(*) FROM jobs"), 1) }

        // A deletes through the real delete path (records a durable tombstone).
        try JobStore(dbA).delete(jobId: jobId)
        try dbA.writer.read {
            XCTAssertEqual(try String.fetchOne($0, sql: "SELECT sync_id FROM deleted_jobs"), "greenhouse:111")
        }

        // Full cycle, import first: the old live record must NOT resurrect it.
        try a.importChanges(from: folder)
        try dbA.writer.read { XCTAssertEqual(try Int.fetchOne($0, sql: "SELECT COUNT(*) FROM jobs"), 0) }
        try a.export(to: folder)

        // B converges: job gone, and the tombstone is recorded durably on B too.
        let imp = try b.importChanges(from: folder)
        XCTAssertGreaterThanOrEqual(imp.deletes, 1)
        try dbB.writer.read { dbc in
            XCTAssertEqual(try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM jobs"), 0)
            XCTAssertEqual(try String.fetchOne(dbc, sql: "SELECT sync_id FROM deleted_jobs"), "greenhouse:111")
        }

        // B, now holding the marker, also refuses to re-discover the posting.
        let summary = try JobStore(dbB).upsert(
            [NormalizedJob(source: "greenhouse", externalId: "111", title: "Engineer", company: "Acme")])
        XCTAssertEqual(summary.inserted, 0)
        try dbB.writer.read { XCTAssertEqual(try Int.fetchOne($0, sql: "SELECT COUNT(*) FROM jobs"), 0) }

        // Stays gone across another A cycle — no flip-flop.
        try a.importChanges(from: folder)
        try a.export(to: folder)
        try dbA.writer.read { XCTAssertEqual(try Int.fetchOne($0, sql: "SELECT COUNT(*) FROM jobs"), 0) }
    }

    /// Delete greenhouse:777 on A, then import a peer live record (newer than the
    /// deletion) with `status`. Returns the DB for assertions.
    private func deleteThenImportPeerLive(status: String) throws -> AppDatabase {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "777", fitScore: 5)
        try JobStore(dbA).delete(jobId: jobId)   // tombstone at real-now

        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        let rec = "{\"v\":1,\"entity\":\"job\",\"id\":\"greenhouse:777\","
            + "\"updated_at\":\"2099-01-01T00:00:00.000Z\",\"device\":\"PEER\",\"deleted\":false,"
            + "\"data\":{\"source\":\"greenhouse\",\"external_id\":\"777\",\"title\":\"Dev\","
            + "\"status\":\"\(status)\",\"date_discovered\":\"2026-07-08T09:00:00Z\"}}\n"
        try rec.write(to: changes.appendingPathComponent("PEER.jsonl"), atomically: true, encoding: .utf8)

        try SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now).importChanges(from: folder)
        return dbA
    }

    func testPlainRefetchStaysDeleted() throws {
        // A re-fetch ('discovered') after the delete does NOT resurrect it.
        let dbA = try deleteThenImportPeerLive(status: "discovered")
        try dbA.writer.read { dbc in
            XCTAssertEqual(try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM jobs WHERE externalId='777'"), 0)
            XCTAssertEqual(try String.fetchOne(dbc, sql: "SELECT sync_id FROM deleted_jobs"), "greenhouse:777")
        }
    }

    func testShortlistOverridesDeletion() throws {
        // Engagement wins: a peer that shortlisted the job after the delete
        // brings it back and clears the tombstone — the reported bug.
        let dbA = try deleteThenImportPeerLive(status: "shortlisted")
        try dbA.writer.read { dbc in
            // Canonical 'shortlisted' unfolds to triage=shortlisted on iOS.
            XCTAssertEqual(try String.fetchOne(dbc, sql: "SELECT triage FROM jobs WHERE externalId='777'"),
                           "shortlisted")
            XCTAssertEqual(try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM deleted_jobs"), 0)
        }
    }

    func testFreshShortlistSurvivesStaleTombstoneInCycle() throws {
        // The reported bug: shortlist a job on THIS device while the folder
        // already holds a peer's older delete for it. A full cycle must not wipe
        // the fresh shortlist. Only holds if the cycle EXPORTS BEFORE IMPORT —
        // the shortlist must reach the folder (stamped now) before import
        // evaluates the incoming tombstone. Mirrors SyncCoordinator.syncOnce.
        let clock = Clock()  // ~2026, strictly after the tombstone below
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        _ = try seedJob(dbA, externalId: "555", fitScore: 5)

        // A peer deleted the same posting earlier — its tombstone is already in
        // the folder, at a time BEFORE this device's clock.
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        let tomb = "{\"v\":1,\"entity\":\"job\",\"id\":\"greenhouse:555\","
            + "\"updated_at\":\"2026-01-01T00:00:00.000Z\",\"device\":\"PEER\",\"deleted\":true}\n"
        try tomb.write(to: changes.appendingPathComponent("PEER.jsonl"), atomically: true, encoding: .utf8)

        // The user swipes to shortlist.
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET triage = 'shortlisted' WHERE externalId = '555'")
        }

        // One cycle in the real coordinator order: EXPORT then IMPORT.
        let engine = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        try engine.export(to: folder)
        try engine.importChanges(from: folder)

        // The shortlist survived and the stale delete did not take.
        try dbA.writer.read { dbc in
            XCTAssertEqual(try String.fetchOne(dbc, sql: "SELECT triage FROM jobs WHERE externalId='555'"),
                           "shortlisted")
            XCTAssertEqual(try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM deleted_jobs"), 0)
        }
    }
}
