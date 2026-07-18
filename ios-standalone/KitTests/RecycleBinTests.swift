import XCTest
import GRDB
@testable import JobsmithKit

/// The Recently Deleted (recycle bin) query layer: soft-delete stamps a
/// deletedAt and hides the job; restore clears it and brings the job back;
/// purge hard-deletes the bin (and its applications) so the posting becomes
/// re-discoverable by a later fetch.
final class RecycleBinTests: XCTestCase {

    private func seed(_ store: JobStore, externalId: String, title: String = "Engineer",
                      company: String = "Acme") throws -> String {
        let summary = try store.upsert([NormalizedJob(source: "greenhouse", externalId: externalId,
                                                      title: title, company: company)])
        XCTAssertEqual(summary.inserted, 1)
        return try XCTUnwrap(store.jobs(triage: "new").first { $0.externalId == externalId }).id
    }

    func testDeleteStampsDeletedAtAndHidesJob() throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        let id = try seed(store, externalId: "1")

        try store.delete(jobId: id)

        let job = try XCTUnwrap(store.job(id: id))
        XCTAssertEqual(job.triage, "deleted")
        XCTAssertNotNil(job.deletedAt, "delete must stamp deletedAt")

        XCTAssertEqual(try store.recentlyDeleted().map(\.id), [id])
        XCTAssertFalse(try store.inbox().contains { $0.id == id }, "deleted job must leave the inbox")
    }

    func testRestoreClearsDeletedAtAndReturnsToInbox() throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        let id = try seed(store, externalId: "2")
        try store.delete(jobId: id)

        // Restore == setTriage to something other than 'deleted'.
        try store.setTriage("new", jobId: id)

        let job = try XCTUnwrap(store.job(id: id))
        XCTAssertEqual(job.triage, "new")
        XCTAssertNil(job.deletedAt, "restore must clear deletedAt")
        XCTAssertTrue(try store.inbox().contains { $0.id == id })
        XCTAssertTrue(try store.recentlyDeleted().isEmpty)
    }

    func testPurgeRemovesRowsAndApplicationsAndAllowsRediscovery() throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        let apps = ApplicationStore(db)
        let id = try seed(store, externalId: "3")
        // Give it an application (with an outcome event) so we can prove those go too.
        let app = try apps.createOrReplace(jobId: id, resume: "r", coverLetter: "c",
                                           honestyLevel: "honest", stylePreset: "standard")
        try apps.updateStatus(id: app.id, status: "applied")
        try apps.recordOutcome(id: app.id, outcome: .screening)

        try store.delete(jobId: id)
        XCTAssertEqual(try store.purgeDeleted(), 1)

        try db.writer.read { dbc in
            XCTAssertEqual(try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM jobs"), 0)
            XCTAssertEqual(try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM applications"), 0)
            // application_events cascade from applications.
            XCTAssertEqual(try Int.fetchOne(dbc, sql: "SELECT COUNT(*) FROM application_events"), 0)
        }

        // The whole point: the same (source, externalId) now inserts as brand new.
        let summary = try store.upsert([NormalizedJob(source: "greenhouse", externalId: "3",
                                                      title: "Engineer", company: "Acme")])
        XCTAssertEqual(summary.inserted, 1)
        XCTAssertTrue(try store.inbox().contains { $0.externalId == "3" })
    }

    // The Apply browser's "This job wasn't for me" prompt runs AppModel
    // .resolvePendingApplyNotForMe(), which soft-deletes the posting through
    // JobStore.delete. AppModel lives in the App target and isn't importable
    // here, so this exercises the store-level flow that path depends on:
    // a job carrying generated state (an application) lands in the bin and
    // drops out of the ordinary jobs() listings.
    func testDeleteMovesGeneratedJobToBinAndOutOfListings() throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        let apps = ApplicationStore(db)
        let id = try seed(store, externalId: "notforme")
        _ = try apps.createOrReplace(jobId: id, resume: "r", coverLetter: "c",
                                     honestyLevel: "honest", stylePreset: "standard")

        try store.delete(jobId: id)

        let job = try XCTUnwrap(store.job(id: id))
        XCTAssertEqual(job.triage, "deleted")
        XCTAssertNotNil(job.deletedAt)
        XCTAssertEqual(try store.recentlyDeleted().map(\.id), [id])
        XCTAssertFalse(try store.jobs().contains { $0.id == id }, "deleted job must leave jobs() listings")
    }

    func testPurgeLeavesNonDeletedJobsUntouched() throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        let apps = ApplicationStore(db)

        let keptId = try seed(store, externalId: "keep")
        try store.setTriage("shortlisted", jobId: keptId)
        let keptApp = try apps.createOrReplace(jobId: keptId, resume: "r", coverLetter: "c",
                                               honestyLevel: "honest", stylePreset: "standard")

        let goneId = try seed(store, externalId: "gone")
        try store.delete(jobId: goneId)

        XCTAssertEqual(try store.purgeDeleted(), 1)

        // The shortlisted job and its application survive.
        XCTAssertNotNil(try store.job(id: keptId))
        XCTAssertNotNil(try apps.application(id: keptApp.id))
        XCTAssertTrue(try store.jobs(triage: "shortlisted").contains { $0.id == keptId })
    }
}
