import XCTest
import GRDB
@testable import JobsmithKit

/// The `work_request` hand-off entity, mirroring tests/test_sync_workrequests.py:
/// a pending request exported here lands on the peer; the peer's `done` re-emit
/// (newer timestamp) wins back under LWW; pruned rows tombstone everywhere.
/// Plus the WorkRequestStore's own guards (duplicate suppression, pruning).
final class WorkRequestSyncTests: XCTestCase {

    final class Clock: @unchecked Sendable {
        var t = Date(timeIntervalSince1970: 1_775_000_000)
        func now() -> Date { t += 1; return t }
    }

    private func tempFolder() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("wrtest-\(UUID().uuidString)")
    }

    func testStoreCreateAndDuplicateGuard() throws {
        let db = try AppDatabase.inMemory()
        let store = WorkRequestStore(db)

        XCTAssertFalse(try store.hasPending(kind: WorkRequest.kindScoreAll, requestedBy: "PHONE01"))
        let req = try store.create(kind: WorkRequest.kindScoreAll,
                                   params: ["cap": .int(25), "pool": .string("inbox")],
                                   requestedBy: "PHONE01")
        XCTAssertTrue(req.isPending)
        XCTAssertTrue(try store.hasPending(kind: WorkRequest.kindScoreAll, requestedBy: "PHONE01"))

        let all = try store.all()
        XCTAssertEqual(all.count, 1)
        XCTAssertEqual(all[0].params["cap"], .int(25))
        XCTAssertEqual(all[0].requestedBy, "PHONE01")
    }

    /// A pending request imported from another device must not suppress this
    /// device from filing its own — the request path is scoped to `requestedBy`.
    func testHasPendingScopedToRequestingDevice() throws {
        let db = try AppDatabase.inMemory()
        let store = WorkRequestStore(db)

        // A foreign device's pending request lands here (as it would after import).
        try db.writer.write { dbc in
            try dbc.execute(sql: """
                INSERT INTO work_requests (id, kind, status, requestedBy, requestedAt)
                VALUES ('foreign', 'score_all', 'pending', 'PEER99', '2026-07-16T00:00:00.000Z')
                """)
        }
        // This device (PHONE01) has filed nothing, so its request path is clear
        // even though a pending row exists...
        XCTAssertFalse(try store.hasPending(kind: WorkRequest.kindScoreAll, requestedBy: "PHONE01"))
        // ...while the fulfiller/display path (all rows) still sees the peer's row.
        XCTAssertEqual(try store.all().count, 1)

        // Once this device files its own, its request path reports pending.
        _ = try store.create(kind: WorkRequest.kindScoreAll, params: [:], requestedBy: "PHONE01")
        XCTAssertTrue(try store.hasPending(kind: WorkRequest.kindScoreAll, requestedBy: "PHONE01"))
        // The peer's scope is unaffected by our row's existence.
        XCTAssertTrue(try store.hasPending(kind: WorkRequest.kindScoreAll, requestedBy: "PEER99"))
    }

    func testPruneDropsOldRetiredAndAbandonedRequests() throws {
        let db = try AppDatabase.inMemory()
        let now = Date()
        try db.writer.write { dbc in
            // Retired 8 days ago -> pruned. Retired yesterday -> kept.
            // Pending 31 days ago -> pruned (nothing ever answered).
            try dbc.execute(sql: """
                INSERT INTO work_requests (id, kind, status, requestedAt, completedAt) VALUES
                ('old-done', 'score_all', 'done', '2020-01-01T00:00:00.000Z', '2020-01-08T00:00:00.000Z'),
                ('new-done', 'score_all', 'done', '2020-01-01T00:00:00.000Z', '2999-01-01T00:00:00.000Z'),
                ('stale-pending', 'score_all', 'pending', '2020-01-01T00:00:00.000Z', NULL),
                ('live-pending', 'score_all', 'pending', '2999-01-01T00:00:00.000Z', NULL)
                """)
            try WorkRequestStore.prune(dbc, now: now)
        }
        let ids = Set(try WorkRequestStore(db).all().map(\.id))
        XCTAssertEqual(ids, ["new-done", "live-pending"])
    }

    func testRoundTripAndDoneWinsBack() throws {
        let clock = Clock()
        let folder = tempFolder()
        let dbA = try AppDatabase.inMemory()   // the requester
        let dbB = try AppDatabase.inMemory()   // the fulfiller

        let request = try WorkRequestStore(dbA).create(
            kind: WorkRequest.kindScoreAll,
            params: ["cap": .int(25), "pool": .string("inbox")],
            requestedBy: "A1B2")

        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        let exp = try a.export(to: folder)
        XCTAssertEqual(exp.live, 1)
        let imp = try b.importChanges(from: folder)
        XCTAssertEqual(imp.upserts, 1)

        var onB = try WorkRequestStore(dbB).all()
        XCTAssertEqual(onB.count, 1)
        XCTAssertEqual(onB[0].id, request.id)
        XCTAssertEqual(onB[0].status, "pending")
        XCTAssertEqual(onB[0].params["pool"], .string("inbox"))

        // The fulfiller retires it; its re-emit must win back on A.
        try dbB.writer.write { dbc in
            try dbc.execute(sql: """
                UPDATE work_requests SET status = 'done', completedBy = 'C3D4',
                    completedAt = '2026-07-15T09:00:00.000Z' WHERE id = ?
                """, arguments: [request.id])
        }
        let expB = try b.export(to: folder)
        XCTAssertEqual(expB.live, 1)
        _ = try a.importChanges(from: folder)

        let backOnA = try WorkRequestStore(dbA).all()
        XCTAssertEqual(backOnA[0].status, "done")
        XCTAssertEqual(backOnA[0].completedBy, "C3D4")
        XCTAssertFalse(try WorkRequestStore(dbA).hasPending(kind: WorkRequest.kindScoreAll, requestedBy: "A1B2"))

        // Steady state: no re-emits on either side.
        XCTAssertEqual(try a.export(to: tempFolder()).total, 0)
        XCTAssertEqual(try b.export(to: tempFolder()).total, 0)

        onB = try WorkRequestStore(dbB).all()
        XCTAssertEqual(onB[0].status, "done")
    }

    func testTombstonePropagates() throws {
        let clock = Clock()
        let folder = tempFolder()
        let dbA = try AppDatabase.inMemory()
        let dbB = try AppDatabase.inMemory()

        _ = try WorkRequestStore(dbA).create(kind: WorkRequest.kindScoreAll,
                                             params: [:], requestedBy: "A1B2")
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)
        _ = try a.export(to: folder)
        _ = try b.importChanges(from: folder)
        XCTAssertEqual(try WorkRequestStore(dbB).all().count, 1)

        // The requester prunes; the peer's row goes too.
        try dbA.writer.write { dbc in
            try dbc.execute(sql: "DELETE FROM work_requests")
        }
        let exp = try a.export(to: folder)
        XCTAssertEqual(exp.tombstones, 1)
        let imp = try b.importChanges(from: folder)
        XCTAssertEqual(imp.deletes, 1)
        XCTAssertEqual(try WorkRequestStore(dbB).all().count, 0)
    }
}
