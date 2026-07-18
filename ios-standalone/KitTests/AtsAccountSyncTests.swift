import XCTest
import GRDB
@testable import JobsmithKit

/// The `ats_account` per-tenant registry entity, mirroring
/// tests/test_sync_ats_accounts.py: a pending create exported here lands on the
/// peer; the peer's `active` re-emit (newer timestamp) wins back under LWW; a
/// removed account tombstones everywhere. Plus the AtsAccountStore's own guards.
final class AtsAccountSyncTests: XCTestCase {

    final class Clock: @unchecked Sendable {
        var t = Date(timeIntervalSince1970: 1_775_000_000)
        func now() -> Date { t += 1; return t }
    }

    private func tempFolder() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("atstest-\(UUID().uuidString)")
    }

    func testStoreUpsertGetAndPromote() throws {
        let db = try AppDatabase.inMemory()
        let store = AtsAccountStore(db)

        let created = try store.upsert(tenantHost: "ACME.wd5.myworkdayjobs.com",
                                       email: "me@example.com",
                                       status: AtsAccount.statusPending)
        // Host normalized to lowercase.
        XCTAssertEqual(created.tenantHost, "acme.wd5.myworkdayjobs.com")
        XCTAssertEqual(created.status, AtsAccount.statusPending)
        XCTAssertNotNil(created.createdAt)

        let promoted = try store.markSignedIn("acme.wd5.myworkdayjobs.com")
        XCTAssertEqual(promoted?.status, AtsAccount.statusActive)
        XCTAssertNotNil(promoted?.lastSignInAt)
        XCTAssertEqual(promoted?.createdAt, created.createdAt)  // preserved

        XCTAssertNil(try store.get("unknown.wd1.myworkdayjobs.com"))
        XCTAssertEqual(try store.all().count, 1)
    }

    func testRoundTripAndSignInWinsBack() throws {
        let clock = Clock()
        let folder = tempFolder()
        let dbA = try AppDatabase.inMemory()   // the creator (iOS)
        let dbB = try AppDatabase.inMemory()   // the peer (desktop-like)

        _ = try AtsAccountStore(dbA).upsert(tenantHost: "acme.wd5.myworkdayjobs.com",
                                            email: "me@example.com",
                                            status: AtsAccount.statusPending)

        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)

        let exp = try a.export(to: folder)
        XCTAssertEqual(exp.live, 1)
        let imp = try b.importChanges(from: folder)
        XCTAssertEqual(imp.upserts, 1)

        let onB = try AtsAccountStore(dbB).all()
        XCTAssertEqual(onB.count, 1)
        XCTAssertEqual(onB[0].tenantHost, "acme.wd5.myworkdayjobs.com")
        XCTAssertEqual(onB[0].status, AtsAccount.statusPending)

        // The peer signs in → promotes to active with a newer timestamp.
        _ = try AtsAccountStore(dbB).markSignedIn("acme.wd5.myworkdayjobs.com")
        let expB = try b.export(to: folder)
        XCTAssertEqual(expB.live, 1)
        _ = try a.importChanges(from: folder)

        let backOnA = try AtsAccountStore(dbA).all()
        XCTAssertEqual(backOnA[0].status, AtsAccount.statusActive)
        XCTAssertNotNil(backOnA[0].lastSignInAt)

        // Steady state: no re-emits on either side.
        XCTAssertEqual(try a.export(to: tempFolder()).total, 0)
        XCTAssertEqual(try b.export(to: tempFolder()).total, 0)
    }

    func testTombstonePropagates() throws {
        let clock = Clock()
        let folder = tempFolder()
        let dbA = try AppDatabase.inMemory()
        let dbB = try AppDatabase.inMemory()

        _ = try AtsAccountStore(dbA).upsert(tenantHost: "globex.wd1.myworkdayjobs.com",
                                            email: "me@example.com")
        let a = SyncEngine(db: dbA, deviceId: "A1B2", now: clock.now)
        let b = SyncEngine(db: dbB, deviceId: "C3D4", now: clock.now)
        _ = try a.export(to: folder)
        _ = try b.importChanges(from: folder)
        XCTAssertEqual(try AtsAccountStore(dbB).all().count, 1)

        // The user forgets the account; the delete propagates.
        try AtsAccountStore(dbA).delete("globex.wd1.myworkdayjobs.com")
        let exp = try a.export(to: folder)
        XCTAssertEqual(exp.tombstones, 1)
        _ = try b.importChanges(from: folder)
        XCTAssertTrue(try AtsAccountStore(dbB).all().isEmpty)
    }
}
