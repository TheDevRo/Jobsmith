import XCTest
import GRDB
@testable import JobsmithKit

/// Real GRDB round-trip for the iOS SyncEngine, mirroring
/// tests/test_sync_engine.py: export -> folder -> import across two in-memory
/// databases ("device A" and "device B"), plus LWW, tombstone, and profile.
///
/// The user's lifecycle decision syncs as the separate `triage` entity, so every
/// job emits BOTH a `job` (facts) and a `triage` record. A delete is just
/// triage='deleted' — an ordinary last-writer-wins value, symmetric with a
/// shortlist — so there is no tombstone table and no engaged-status override.
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

    /// Append a peer's `triage` decision record to the folder (a delete is just
    /// status='deleted').
    private func writePeerTriage(_ folder: URL, syncId: String, status: String,
                                 ts: String, device: String = "PEER") throws {
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        let rec = "{\"v\":1,\"entity\":\"triage\",\"id\":\"\(syncId)\","
            + "\"updated_at\":\"\(ts)\",\"device\":\"\(device)\",\"deleted\":false,"
            + "\"data\":{\"status\":\"\(status)\"}}\n"
        let url = changes.appendingPathComponent("\(device).jsonl")
        var text = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        text += rec
        try text.write(to: url, atomically: true, encoding: .utf8)
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
        XCTAssertEqual(exp.live, 3)   // job facts + triage + application
        XCTAssertEqual(exp.tombstones, 0)

        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)
        let imp = try b.importChanges(from: folder)
        XCTAssertEqual(imp.upserts, 3)
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

    /// A shortlist crosses devices via the `triage` entity: iOS models it on
    /// `triage`, the wire carries the desktop's single `status`. Device A
    /// shortlists; device B must end up with triage='shortlisted' (in its
    /// Pipeline) — proving foldStatus/unfoldStatus round-trips.
    func testShortlistLifecycleCrossesDevices() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "222", fitScore: 80)
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET triage = 'shortlisted' WHERE id = ?", arguments: [jobId])
        }
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        XCTAssertEqual(try a.export(to: folder).live, 2)  // job facts + triage

        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)
        XCTAssertEqual(try b.importChanges(from: folder).upserts, 2)

        try dbB.writer.read { dbc in
            let job = try Row.fetchOne(dbc, sql: "SELECT * FROM jobs WHERE externalId = '222'")!
            XCTAssertEqual(job["triage"] as String, "shortlisted")  // reaches B's Pipeline
            XCTAssertEqual(job["status"] as String, "discovered")   // sub-stage unchanged
        }
        // B re-exports nothing: the fold is idempotent through the snapshot.
        XCTAssertEqual(try b.export(to: folder.appendingPathComponent("empty2")).total, 0)
    }

    /// Migration: bumping the format version force-re-emits every current row
    /// once, so a device stuck on an older snapshot re-broadcasts correctly, then
    /// settles back to a no-op.
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

        // First export (fresh device): job facts + triage.
        XCTAssertEqual(try a.export(to: folder).live, 2)
        // Simulate a device stuck on an old format with a matching snapshot:
        // roll the stored version back so the next export must force again.
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "UPDATE sync_meta SET value = '1' WHERE key = 'sync_format'")
        }
        let folder2 = folder.appendingPathComponent("mig")
        // Even though the snapshot already matches, force re-emits both records.
        XCTAssertEqual(try a.export(to: folder2).live, 2)

        // The forced records fold correctly: B lands it in the Pipeline.
        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)
        XCTAssertEqual(try b.importChanges(from: folder2).upserts, 2)
        try dbB.writer.read { dbc in
            let job = try Row.fetchOne(dbc, sql: "SELECT * FROM jobs WHERE externalId = '333'")!
            XCTAssertEqual(job["triage"] as String, "shortlisted")
        }
        // Migration is one-shot: a subsequent export is a no-op.
        XCTAssertEqual(try a.export(to: folder2.appendingPathComponent("after")).total, 0)
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

        // B edits the job later than A's create -> B wins. A facts change
        // (fitScore) and a decision change (triage/status) — two entities.
        try dbB.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET fitScore = 99, triage = 'shortlisted', status = 'applied' WHERE externalId = '111'")
        }
        let bexp = try b.export(to: folder)
        XCTAssertEqual(bexp.live, 2)  // job facts + triage

        try a.importChanges(from: folder)
        try dbA.writer.read { dbc in
            let job = try Row.fetchOne(dbc, sql: "SELECT fitScore, status, triage FROM jobs WHERE externalId = '111'")!
            XCTAssertEqual(job["fitScore"] as Double, 99)
            XCTAssertEqual(job["status"] as String, "applied")     // decision status crosses
            XCTAssertEqual(job["triage"] as String, "shortlisted") // lifecycle axis crosses
        }
    }

    /// The generic tombstone path (real application deletes, or a safety net if a
    /// job row is physically removed). User-facing job deletes are soft — see
    /// testDeleteViaTriagePropagates.
    func testGenericTombstonePropagates() throws {
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

        // A physically removes the job row (cascade removes its application).
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "DELETE FROM applications")
            try dbc.execute(sql: "DELETE FROM jobs")
        }
        let aexp = try a.export(to: folder)
        XCTAssertEqual(aexp.tombstones, 3)  // job facts + triage + application

        // Only two of those three are *applied* as deletes: `triage` never
        // tombstones on import (a delete is a live 'deleted' status), so the
        // deletes are the application row and the job row.
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

    /// The real delete path is soft: triage='deleted', synced as a `triage`
    /// record. It reaches the other device and hides the job there — no tombstone
    /// and no side table.
    func testDeleteViaTriagePropagates() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "111", fitScore: 42)
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let dbB = try AppDatabase.inMemory()
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        try a.export(to: folder)
        try b.importChanges(from: folder)
        try dbB.writer.read { XCTAssertEqual(try Int.fetchOne($0, sql: "SELECT COUNT(*) FROM jobs WHERE triage != 'deleted'"), 1) }

        // A deletes through the real (soft) delete path.
        try JobStore(dbA).delete(jobId: jobId)
        try dbA.writer.read {
            XCTAssertEqual(try String.fetchOne($0, sql: "SELECT triage FROM jobs WHERE externalId='111'"), "deleted")
        }

        // One cycle converges B to 'deleted' (hidden from its lists).
        try a.export(to: folder)
        try b.importChanges(from: folder)
        try dbB.writer.read { dbc in
            XCTAssertEqual(try String.fetchOne(dbc, sql: "SELECT triage FROM jobs WHERE externalId='111'"), "deleted")
        }
        // Outside the read block: JobStore.jobs() opens its own read, and GRDB
        // database methods are not reentrant.
        XCTAssertEqual(try JobStore(dbB).jobs().filter { $0.externalId == "111" }.count, 0)
    }

    /// Permanent delete: once triage='deleted', a later fetch of the same posting
    /// matches it as a duplicate and does NOT resurrect it.
    func testRefetchStaysDeleted() throws {
        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "777", fitScore: 5)
        try JobStore(dbA).delete(jobId: jobId)

        let summary = try JobStore(dbA).upsert(
            [NormalizedJob(source: "greenhouse", externalId: "777", title: "Engineer", company: "Acme")])
        XCTAssertEqual(summary.inserted, 0)
        try dbA.writer.read {
            XCTAssertEqual(try String.fetchOne($0, sql: "SELECT triage FROM jobs WHERE externalId='777'"), "deleted")
        }
    }

    /// Symmetric LWW: a shortlist stamped after a peer's delete wins — you can't
    /// lose a job by shortlisting it. Export-before-import (as the coordinator
    /// runs it) stamps the local shortlist so it out-ranks the older delete.
    func testNewerShortlistBeatsOlderDelete() throws {
        let clock = Clock()  // ~2026, strictly after the peer delete below
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        _ = try seedJob(dbA, externalId: "555", fitScore: 5)
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET triage = 'shortlisted' WHERE externalId = '555'")
        }
        try writePeerTriage(folder, syncId: "greenhouse:555", status: "deleted",
                            ts: "2026-01-01T00:00:00.000Z")

        let engine = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        try engine.export(to: folder)         // local shortlist stamped ~2026-...
        try engine.importChanges(from: folder)

        try dbA.writer.read { dbc in
            XCTAssertEqual(try String.fetchOne(dbc, sql: "SELECT triage FROM jobs WHERE externalId='555'"),
                           "shortlisted")
        }
    }

    /// The mirror case: a delete stamped after our shortlist wins and hides the
    /// job locally — deletes and shortlists are fully symmetric.
    func testNewerDeleteBeatsOlderShortlist() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        _ = try seedJob(dbA, externalId: "556", fitScore: 5)
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET triage = 'shortlisted' WHERE externalId = '556'")
        }

        let engine = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        try engine.export(to: folder)  // shortlist stamped ~2026-...
        try writePeerTriage(folder, syncId: "greenhouse:556", status: "deleted",
                            ts: "2099-01-01T00:00:00.000Z")

        try engine.importChanges(from: folder)
        try dbA.writer.read { dbc in
            XCTAssertEqual(try String.fetchOne(dbc, sql: "SELECT triage FROM jobs WHERE externalId='556'"),
                           "deleted")
        }
    }
}

// MARK: - outcome history (the `application_event` entity)

extension SyncEngineTests {

    private func seedApplication(_ db: AppDatabase, jobId: String) throws -> Application {
        let store = ApplicationStore(db)
        let app = try store.createOrReplace(jobId: jobId, resume: "RESUME", coverLetter: "COVER",
                                            honestyLevel: "honest", stylePreset: "standard")
        try store.updateStatus(id: app.id, status: "applied")
        return app
    }

    /// The hazard the `application_event` entity exists to prevent: merging is
    /// last-writer-wins over the WHOLE record, so an outcome carried as a field
    /// on `application` was silently overwritten whenever the other device
    /// touched any other field of that application.
    func testOutcomeSurvivesConcurrentEditOnOtherDevice() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "111", fitScore: 80)
        let app = try seedApplication(dbA, jobId: jobId)

        let dbB = try AppDatabase.inMemory()
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        try a.export(to: folder)
        try b.importChanges(from: folder)

        // A (the phone) records the interview...
        try ApplicationStore(dbA).recordOutcome(id: app.id, outcome: .interview)

        // ...while B (the desktop), not yet having seen it, edits the resume.
        try ApplicationStore(dbB).updateContent(id: app.id, resume: "REVISED", coverLetter: nil)

        try a.export(to: folder)
        try b.export(to: folder)
        try b.importChanges(from: folder)
        try a.importChanges(from: folder)

        for (name, db) in [("A", dbA), ("B", dbB)] {
            let merged = try XCTUnwrap(ApplicationStore(db).application(id: app.id))
            XCTAssertEqual(merged.outcome, "interview", "outcome lost on device \(name)")
            XCTAssertEqual(merged.resumeContent, "REVISED", "resume edit lost on device \(name)")
        }
    }

    /// Two devices each record outcomes offline; the histories merge as a union
    /// and the derived column converges on the latest event.
    func testOfflineOutcomeHistoriesMergeAsUnion() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "111", fitScore: 80)
        let app = try seedApplication(dbA, jobId: jobId)

        let dbB = try AppDatabase.inMemory()
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        try a.export(to: folder)
        try b.importChanges(from: folder)

        // Explicit stamps: both devices use the real wall clock, so without these
        // the two events can land in the same millisecond and the assertion below
        // would be testing the clock rather than the merge.
        try ApplicationStore(dbA).recordOutcome(id: app.id, outcome: .screening,
                                                occurredAt: "2026-07-13T10:00:00.000Z")
        try ApplicationStore(dbB).recordOutcome(id: app.id, outcome: .interview,
                                                occurredAt: "2026-07-13T11:00:00.000Z")

        try a.export(to: folder)
        try b.export(to: folder)
        try a.importChanges(from: folder)
        try b.importChanges(from: folder)

        for (name, db) in [("A", dbA), ("B", dbB)] {
            let events = try ApplicationStore(db).events(id: app.id)
            XCTAssertEqual(events.map(\.toOutcome), ["screening", "interview"],
                           "history diverged on device \(name)")
            let merged = try XCTUnwrap(ApplicationStore(db).application(id: app.id))
            XCTAssertEqual(merged.outcome, "interview", "derived outcome wrong on device \(name)")
        }
    }

    /// Two events stamped at the SAME instant on different devices must still
    /// derive the same outcome everywhere.
    ///
    /// This is why the tiebreak can't be the rowid: each device inserts its own
    /// event first, so rowid order is opposite on the two devices and they would
    /// read different "latest" outcomes off an identical history. The tiebreak is
    /// the event's sync identity instead, which every device agrees on.
    func testIdenticallyStampedEventsConverge() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "111", fitScore: 80)
        let app = try seedApplication(dbA, jobId: jobId)

        let dbB = try AppDatabase.inMemory()
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        try a.export(to: folder)
        try b.importChanges(from: folder)

        let sameInstant = "2026-07-13T10:00:00.000Z"
        try ApplicationStore(dbA).recordOutcome(id: app.id, outcome: .screening,
                                                occurredAt: sameInstant)
        try ApplicationStore(dbB).recordOutcome(id: app.id, outcome: .interview,
                                                occurredAt: sameInstant)

        try a.export(to: folder)
        try b.export(to: folder)
        try a.importChanges(from: folder)
        try b.importChanges(from: folder)

        let outcomeA = try XCTUnwrap(ApplicationStore(dbA).application(id: app.id)).outcome
        let outcomeB = try XCTUnwrap(ApplicationStore(dbB).application(id: app.id)).outcome
        XCTAssertEqual(outcomeA, outcomeB, "devices disagree on the outcome of an identical history")
        XCTAssertEqual(try ApplicationStore(dbA).events(id: app.id).map(\.toOutcome),
                       try ApplicationStore(dbB).events(id: app.id).map(\.toOutcome),
                       "devices present the same history in a different order")
    }

    /// Re-importing the same log must not duplicate immutable events.
    func testEventImportIsIdempotent() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "111", fitScore: 80)
        let app = try seedApplication(dbA, jobId: jobId)
        try ApplicationStore(dbA).recordOutcome(id: app.id, outcome: .screening)

        let dbB = try AppDatabase.inMemory()
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        try a.export(to: folder)
        try b.importChanges(from: folder)
        try b.importChanges(from: folder)
        try b.importChanges(from: folder)

        XCTAssertEqual(try ApplicationStore(dbB).events(id: app.id).count, 1)
        XCTAssertEqual(try XCTUnwrap(ApplicationStore(dbB).application(id: app.id)).outcome, "screening")
    }
}

// MARK: - outcome store semantics

final class ApplicationOutcomeTests: XCTestCase {

    private func makeApplied() throws -> (AppDatabase, ApplicationStore, Application) {
        let db = try AppDatabase.inMemory()
        let jobId = try db.writer.write { dbc -> String in
            var job = Job(from: NormalizedJob(source: "greenhouse", externalId: "1",
                                              title: "Engineer", company: "Acme"))
            job.status = "applied"
            try job.insert(dbc)
            return job.id
        }
        let store = ApplicationStore(db)
        let app = try store.createOrReplace(jobId: jobId, resume: "r", coverLetter: "c",
                                            honestyLevel: "honest", stylePreset: "standard")
        try store.updateStatus(id: app.id, status: "applied")
        return (db, store, app)
    }

    func testTransitionsRecordedAsEventsAndNoOpOnRepeat() throws {
        let (_, store, app) = try makeApplied()
        try store.recordOutcome(id: app.id, outcome: .screening)
        try store.recordOutcome(id: app.id, outcome: .interview)
        try store.recordOutcome(id: app.id, outcome: .interview)  // repeat: no-op

        let events = try store.events(id: app.id)
        XCTAssertEqual(events.map(\.fromOutcome), ["awaiting", "screening"])
        XCTAssertEqual(events.map(\.toOutcome), ["screening", "interview"])
        XCTAssertEqual(try XCTUnwrap(store.application(id: app.id)).outcome, "interview")
    }

    /// The bug the event log fixes: a rejection used to erase the stages the
    /// application had actually reached.
    func testFunnelKeepsStagesARejectedApplicationReached() throws {
        let (_, store, app) = try makeApplied()
        try store.recordOutcome(id: app.id, outcome: .screening)
        try store.recordOutcome(id: app.id, outcome: .interview)
        try store.recordOutcome(id: app.id, outcome: .rejected)

        let funnel = try store.funnel()
        XCTAssertEqual(funnel.applied, 1)
        XCTAssertEqual(funnel.stages.map(\.1), [1, 1, 0])  // screening, interview, offer
        XCTAssertEqual(try XCTUnwrap(store.application(id: app.id)).outcome, "rejected")
    }

    /// Skipping ahead in the menu still implies the earlier stages.
    func testFunnelImpliesEarlierStages() throws {
        let (_, store, app) = try makeApplied()
        try store.recordOutcome(id: app.id, outcome: .offer)

        XCTAssertEqual(try store.funnel().stages.map(\.1), [1, 1, 1])
    }

    /// Tapping through the funnel faster than the clock ticks must still record a
    /// causally-ordered history.
    ///
    /// Stamps are millisecond-precision, so screening → interview → offer in quick
    /// succession all land in the same millisecond. Order then falls to the
    /// alphabetical tiebreak and the history reads "interview, offer, screening" —
    /// inverting what happened, and leaving `outcome` on the wrong stage.
    func testRapidTransitionsStayCausallyOrdered() throws {
        let (_, store, app) = try makeApplied()
        try store.recordOutcome(id: app.id, outcome: .screening)
        try store.recordOutcome(id: app.id, outcome: .interview)
        try store.recordOutcome(id: app.id, outcome: .offer)

        let events = try store.events(id: app.id)
        XCTAssertEqual(events.map(\.toOutcome), ["screening", "interview", "offer"])
        XCTAssertEqual(Set(events.map(\.occurredAt)).count, 3, "stamps collided")
        XCTAssertEqual(try XCTUnwrap(store.application(id: app.id)).outcome, "offer")
    }

    /// Recording an outcome must not bump `updatedAt` — that column is the LWW
    /// clock for the `application` entity, and winning it would overwrite an
    /// unrelated edit made on the other device.
    func testRecordingAnOutcomeDoesNotTouchTheLWWClock() throws {
        let (_, store, app) = try makeApplied()
        let before = try XCTUnwrap(store.application(id: app.id)).updatedAt
        try store.recordOutcome(id: app.id, outcome: .screening)
        XCTAssertEqual(try XCTUnwrap(store.application(id: app.id)).updatedAt, before)
    }
}

// MARK: - wire-format interop with the desktop

extension SyncEngineTests {

    /// The cross-language conformance harness (tests/test_sync_crosslang.py) only
    /// compiles the pure mappers, so the `application_event` wire format — which
    /// lives in the GRDB-dependent engine — is not covered there. This pins it:
    /// the record below is verbatim what backend/sync/entities.py emits, including
    /// the desktop's timestamp shape (microseconds + "+00:00", where Swift writes
    /// milliseconds + "Z"). Both must import.
    func testImportsDesktopEmittedEventRecord() throws {
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let db = try AppDatabase.inMemory()
        let jobId = try seedJob(db, externalId: "111", fitScore: 80)
        let app = try seedApplication(db, jobId: jobId)

        let occurredAt = "2026-07-13T03:52:57.060761+00:00"
        let syncId = "\(app.id):\(occurredAt):screening"
        let record = """
        {"v":1,"entity":"application_event","id":"\(syncId)",\
        "updated_at":"2026-07-13T03:52:57.062Z","device":"DESK","deleted":false,\
        "data":{"application_ref":"\(app.id)","from_outcome":"awaiting",\
        "to_outcome":"screening","occurred_at":"\(occurredAt)","note":null,"source":"user"}}
        """
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        try (record + "\n").write(to: changes.appendingPathComponent("DESK.jsonl"),
                                  atomically: true, encoding: .utf8)

        let engine = SyncEngine(db: db, deviceId: "A1B2", now: Clock().now)
        try engine.importChanges(from: folder)

        let events = try ApplicationStore(db).events(id: app.id)
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events.first?.toOutcome, "screening")
        XCTAssertEqual(events.first?.occurredAt, occurredAt)
        // ...and the derived column is rebuilt from the imported history.
        XCTAssertEqual(try XCTUnwrap(ApplicationStore(db).application(id: app.id)).outcome, "screening")
    }
}

// MARK: - reminder dates (the `application_schedule` entity)

extension SyncEngineTests {

    /// Dates get their own entity for the same reason the outcome does: on
    /// `application` they'd be wiped by any unrelated edit from the other device.
    func testScheduleSurvivesConcurrentEditAndClears() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("synctest-\(UUID().uuidString)")

        let dbA = try AppDatabase.inMemory()
        let jobId = try seedJob(dbA, externalId: "111", fitScore: 80)
        let app = try seedApplication(dbA, jobId: jobId)

        let dbB = try AppDatabase.inMemory()
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        try a.export(to: folder)
        try b.importChanges(from: folder)

        // A schedules an interview; B, not yet having seen it, edits the resume.
        try ApplicationStore(dbA).setSchedule(id: app.id, interviewAt: "2026-07-20T15:00:00.000Z")
        try ApplicationStore(dbB).updateContent(id: app.id, resume: "REVISED", coverLetter: nil)

        try a.export(to: folder)
        try b.export(to: folder)
        try b.importChanges(from: folder)
        try a.importChanges(from: folder)

        for (name, db) in [("A", dbA), ("B", dbB)] {
            let merged = try XCTUnwrap(ApplicationStore(db).application(id: app.id))
            XCTAssertEqual(merged.interviewAt, "2026-07-20T15:00:00.000Z", "interview lost on \(name)")
            XCTAssertEqual(merged.resumeContent, "REVISED", "resume edit lost on \(name)")
        }

        // Clearing the dates propagates as a tombstone — which must mean "no
        // dates", not "no application".
        try ApplicationStore(dbA).setSchedule(id: app.id, clearFollowUp: true, clearInterview: true)
        try a.export(to: folder)
        try b.importChanges(from: folder)

        let cleared = try XCTUnwrap(ApplicationStore(dbB).application(id: app.id))
        XCTAssertNil(cleared.interviewAt)
        XCTAssertEqual(cleared.resumeContent, "REVISED", "the application itself was deleted")
    }

    /// Only open applications want a nudge — an employer that already said no
    /// doesn't need chasing.
    func testScheduledExcludesClosedApplications() throws {
        let db = try AppDatabase.inMemory()
        let store = ApplicationStore(db)

        let openJob = try seedJob(db, externalId: "111", fitScore: 80)
        let closedJob = try seedJob(db, externalId: "222", fitScore: 80)
        let open = try seedApplication(db, jobId: openJob)
        let closed = try seedApplication(db, jobId: closedJob)

        try store.setSchedule(id: open.id, followUpAt: "2026-07-20T15:00:00.000Z")
        try store.setSchedule(id: closed.id, followUpAt: "2026-07-20T15:00:00.000Z")
        try store.recordOutcome(id: closed.id, outcome: .rejected)

        XCTAssertEqual(try store.scheduled().map(\.id), [open.id])
    }
}

// MARK: - duplicate-application guard

final class DupeGuardTests: XCTestCase {

    /// Must match the desktop's database.normalize_identity, or the two platforms
    /// would badge different jobs.
    func testIdentityKeyNormalizesAndRequiresBothHalves() {
        XCTAssertEqual(JobStore.identityKey(title: "Senior  Engineer", company: "Acme, Inc."),
                       JobStore.identityKey(title: "senior engineer", company: "acme inc"))
        XCTAssertNil(JobStore.identityKey(title: "Engineer", company: ""))
        XCTAssertNil(JobStore.identityKey(title: "", company: "Acme"))
    }

    /// A repost from another board carries a new externalId and URL, so the
    /// fetch-time deduplicator can't see it — only the applied-history key can.
    func testRepostFromAnotherBoardMatchesAppliedHistory() throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        let apps = ApplicationStore(db)

        let appliedId = try db.writer.write { dbc -> String in
            var job = Job(from: NormalizedJob(source: "greenhouse", externalId: "1",
                                              title: "Senior Backend Engineer", company: "Acme Corp"))
            job.status = "applied"
            try job.insert(dbc)
            return job.id
        }
        let application = try apps.createOrReplace(jobId: appliedId, resume: "r", coverLetter: "c",
                                                   honestyLevel: "honest", stylePreset: "standard")
        try apps.updateStatus(id: application.id, status: "applied")

        let identities = try store.appliedIdentities()
        // Same role, different board, punctuation and spacing differ.
        XCTAssertTrue(identities.contains(
            JobStore.identityKey(title: "Senior  Backend Engineer", company: "Acme Corp.")!))
        XCTAssertFalse(identities.contains(
            JobStore.identityKey(title: "Platform Engineer", company: "Globex")!))
    }
}

// MARK: - "Best bets" ranking

final class DigestRankerTests: XCTestCase {

    private func job(_ db: AppDatabase, _ externalId: String, source: String,
                     score: Double?, easy: Bool = false, salary: Int? = nil) throws -> Job {
        try db.writer.write { dbc in
            var job = Job(from: NormalizedJob(source: source, externalId: externalId,
                                              title: "Engineer \(externalId)",
                                              company: "Co\(externalId)",
                                              salaryMax: salary, isEasyApply: easy))
            job.fitScore = score
            try job.insert(dbc)
            return job
        }
    }

    /// The whole point of the ranking: a board that has never once replied to you
    /// stops outranking one that converts, even at identical fit.
    func testASourceThatNeverRepliesGetsBuried() throws {
        let db = try AppDatabase.inMemory()
        let dead = try job(db, "1", source: "linkedin", score: 80)
        let live = try job(db, "2", source: "greenhouse", score: 80)

        let ranked = DigestRanker.rank([dead, live],
                                       conversion: ["linkedin": 0.0, "greenhouse": 1.0])
        XCTAssertEqual(ranked.map(\.id), [live.id, dead.id])
    }

    /// One silent application is not evidence a board is bad. With no measured
    /// history a source scores neutral, not zero.
    func testUnprovenSourceIsNeutralNotPenalized() throws {
        let db = try AppDatabase.inMemory()
        let unproven = try job(db, "1", source: "arbeitnow", score: 80)
        let silent = try job(db, "2", source: "linkedin", score: 80)

        let ranked = DigestRanker.rank([silent, unproven], conversion: ["linkedin": 0.0])
        XCTAssertEqual(ranked.map(\.id), [unproven.id, silent.id],
                       "an unproven board should not be buried like a proven-silent one")
    }

    /// Unscored jobs have no fit signal, so they sink rather than scoring as zero.
    func testUnscoredJobsSinkBelowScoredOnes() throws {
        let db = try AppDatabase.inMemory()
        let unscored = try job(db, "1", source: "greenhouse", score: nil)
        let weak = try job(db, "2", source: "greenhouse", score: 10)

        XCTAssertEqual(DigestRanker.rank([unscored, weak], conversion: [:]).map(\.id),
                       [weak.id, unscored.id])
    }

    func testEasyApplyAndSalaryLiftTheScore() throws {
        let db = try AppDatabase.inMemory()
        let plain = try job(db, "1", source: "greenhouse", score: 80)
        let easyWellPaid = try job(db, "2", source: "greenhouse", score: 80,
                                   easy: true, salary: 200_000)

        XCTAssertEqual(DigestRanker.rank([plain, easyWellPaid], conversion: [:]).map(\.id),
                       [easyWellPaid.id, plain.id])
    }

    /// The iOS response-rate query must agree with the desktop's: below the
    /// sample threshold a source is omitted (and so scores neutral), and a
    /// rejection still counts as a reply.
    func testResponseRateBySourceMatchesTheDesktopRules() throws {
        let db = try AppDatabase.inMemory()
        let apps = ApplicationStore(db)

        func applied(_ source: String, _ id: String, _ outcome: ApplicationOutcome?) throws {
            let j = try job(db, id, source: source, score: 50)
            let a = try apps.createOrReplace(jobId: j.id, resume: "r", coverLetter: "c",
                                             honestyLevel: "honest", stylePreset: "standard")
            try apps.updateStatus(id: a.id, status: "applied")
            if let outcome { try apps.recordOutcome(id: a.id, outcome: outcome) }
        }

        // greenhouse: 3 applications — one rejection (a reply!), two silent.
        try applied("greenhouse", "g1", .rejected)
        try applied("greenhouse", "g2", nil)
        try applied("greenhouse", "g3", .noResponse)
        // linkedin: only 2 — too thin to judge, so it must be omitted entirely.
        try applied("linkedin", "l1", .interview)
        try applied("linkedin", "l2", nil)

        let rates = try apps.responseRateBySource()
        XCTAssertEqual(rates["greenhouse"] ?? -1, 1.0 / 3.0, accuracy: 0.001)
        XCTAssertNil(rates["linkedin"], "a source below the sample threshold must not be judged")
    }
}
