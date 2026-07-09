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
            XCTAssertEqual(job["triage"] as String, "new")  // iOS-only field round-tripped

            let app = try Row.fetchOne(dbc, sql: "SELECT * FROM applications")!
            XCTAssertEqual(app["status"] as String, "pending_review")
            XCTAssertEqual(app["stylePreset"] as String, "modern")
            XCTAssertEqual(app["resumeContent"] as String, "R")
        }

        // Re-export from B emits nothing (snapshot is in sync).
        let reexp = try b.export(to: folder.appendingPathComponent("empty"))
        XCTAssertEqual(reexp.total, 0)
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

        // B edits the job later than A's create -> B wins.
        try dbB.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET fitScore = 99, status = 'applied' WHERE externalId = '111'")
        }
        let bexp = try b.export(to: folder)
        XCTAssertEqual(bexp.live, 1)

        try a.importChanges(from: folder)
        try dbA.writer.read { dbc in
            let job = try Row.fetchOne(dbc, sql: "SELECT fitScore, status FROM jobs WHERE externalId = '111'")!
            XCTAssertEqual(job["fitScore"] as Double, 99)
            XCTAssertEqual(job["status"] as String, "applied")
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
}
