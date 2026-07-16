import XCTest
import GRDB
@testable import JobsmithKit

/// Data-loss safety for the change-log transport. The device's own
/// `changes/{id}.jsonl` is written only by that device, so if an export ever
/// treats an evicted/unreadable log as "empty" and rewrites it, the history is
/// gone for good. These tests pin the append-not-truncate contract and the
/// iCloud `.icloud` placeholder detection used on both the export and merge side.
final class SyncLogSafetyTests: XCTestCase {

    final class Clock: @unchecked Sendable {
        var t = Date(timeIntervalSince1970: 1_775_000_000)
        func now() -> Date { t += 1; return t }
    }

    private func tempFolder() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("logsafe-\(UUID().uuidString)")
    }

    @discardableResult
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

    private func logURL(_ folder: URL, _ device: String) -> URL {
        folder.appendingPathComponent("changes").appendingPathComponent("\(device).jsonl")
    }

    // MARK: (a) append preserves prior lines

    func testExportAppendsWithoutLosingPriorLines() throws {
        let clock = Clock()
        let folder = tempFolder()
        let db = try AppDatabase.inMemory()
        try seedJob(db, externalId: "111", fitScore: 50)

        let engine = SyncEngine(db: db, deviceId: "A1B2", now: clock.now)
        _ = try engine.export(to: folder)

        let log = logURL(folder, "A1B2")
        let first = try String(contentsOf: log, encoding: .utf8)
        let firstLineCount = first.split(separator: "\n").count
        XCTAssertGreaterThan(firstLineCount, 0)

        // A genuine local change makes the next export emit new records.
        try db.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET fitScore = 99 WHERE externalId = '111'")
        }
        _ = try engine.export(to: folder)

        let second = try String(contentsOf: log, encoding: .utf8)
        // Every original line must still be present verbatim (true append), and
        // there must be strictly more lines than before.
        XCTAssertTrue(second.hasPrefix(first), "prior log content was not preserved by the append")
        XCTAssertGreaterThan(second.split(separator: "\n").count, firstLineCount)
    }

    // MARK: (b) unreadable-but-exists aborts instead of truncating

    func testExportThrowsInsteadOfTruncatingUnreadableLog() throws {
        let clock = Clock()
        let folder = tempFolder()
        let db = try AppDatabase.inMemory()
        try seedJob(db, externalId: "222", fitScore: 50)

        let engine = SyncEngine(db: db, deviceId: "A1B2", now: clock.now)
        _ = try engine.export(to: folder)

        let log = logURL(folder, "A1B2")
        let original = try String(contentsOf: log, encoding: .utf8)
        XCTAssertFalse(original.isEmpty)

        // Make the existing log unreadable/unwritable — the same observable
        // symptom as an evicted-mid-download file: it exists, but we can't open
        // it for writing.
        let fm = FileManager.default
        try fm.setAttributes([.posixPermissions: 0], ofItemAtPath: log.path)
        defer { try? fm.setAttributes([.posixPermissions: 0o644], ofItemAtPath: log.path) }

        // A local change so the export has records and reaches appendLog.
        try db.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET fitScore = 77 WHERE externalId = '222'")
        }

        XCTAssertThrowsError(try engine.export(to: folder)) { error in
            guard case SyncError.logUnavailable = error else {
                return XCTFail("expected SyncError.logUnavailable, got \(error)")
            }
        }

        // The history must be byte-for-byte intact — nothing truncated.
        try fm.setAttributes([.posixPermissions: 0o644], ofItemAtPath: log.path)
        XCTAssertEqual(try String(contentsOf: log, encoding: .utf8), original,
                       "log was mutated despite the abort")

        // And because the write transaction rolled back, a retry once the file
        // is readable again appends cleanly on top of the preserved history.
        _ = try engine.export(to: folder)
        let retried = try String(contentsOf: log, encoding: .utf8)
        XCTAssertTrue(retried.hasPrefix(original))
        XCTAssertGreaterThan(retried.count, original.count)
    }

    // MARK: (c) placeholder detection helpers

    func testPlaceholderNameDetection() {
        XCTAssertTrue(SyncFile.isPlaceholderName(".A1B2.jsonl.icloud"))
        XCTAssertTrue(SyncFile.isPlaceholderName(".manifest.json.icloud"))
        XCTAssertFalse(SyncFile.isPlaceholderName("A1B2.jsonl"))       // a real log
        XCTAssertFalse(SyncFile.isPlaceholderName(".icloud"))          // degenerate, no inner name
        XCTAssertFalse(SyncFile.isPlaceholderName("A1B2.jsonl.icloud"))// missing leading dot
    }

    func testPlaceholderAndRealURLAreInverses() {
        let real = URL(fileURLWithPath: "/sync/changes/A1B2.jsonl")
        let placeholder = SyncFile.placeholderURL(for: real)
        XCTAssertEqual(placeholder.lastPathComponent, ".A1B2.jsonl.icloud")
        XCTAssertEqual(SyncFile.realURL(forPlaceholder: placeholder).path, real.path)
        // A non-placeholder passes through unchanged.
        XCTAssertEqual(SyncFile.realURL(forPlaceholder: real).path, real.path)
    }

    func testIsEvictedWhenOnlyPlaceholderPresent() throws {
        let folder = tempFolder()
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        let real = changes.appendingPathComponent("PEER.jsonl")
        // Missing real file + present placeholder => evicted.
        try Data("x".utf8).write(to: SyncFile.placeholderURL(for: real))
        XCTAssertTrue(SyncFile.isEvicted(real, fileManager: .default))
        // Once the real file lands, it is no longer considered evicted.
        try Data("{}\n".utf8).write(to: real)
        XCTAssertFalse(SyncFile.isEvicted(real, fileManager: .default))
    }

    // MARK: (c) loader surfaces an evicted peer log rather than skipping it

    func testLoadLogsThrowsOnEvictedPeerPlaceholder() throws {
        let folder = tempFolder()
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)

        // A readable local log plus a peer log that exists only as an iCloud
        // placeholder. The old glob would silently drop the peer; the loader
        // must instead surface "still syncing" (materialize can't complete in a
        // test environment, so the bounded wait expires and it throws).
        try Data("{\"v\":1,\"entity\":\"job\",\"id\":\"greenhouse:1\",\"updated_at\":\"2026-01-01T00:00:00.000Z\",\"device\":\"A1B2\",\"deleted\":false,\"data\":{}}\n".utf8)
            .write(to: changes.appendingPathComponent("A1B2.jsonl"))
        let peerReal = changes.appendingPathComponent("PEER.jsonl")
        try Data("x".utf8).write(to: SyncFile.placeholderURL(for: peerReal))

        XCTAssertThrowsError(try SyncMerge.loadLogs(folder, materializeTimeout: 0.2)) { error in
            guard case SyncError.logUnavailable(let url) = error else {
                return XCTFail("expected SyncError.logUnavailable, got \(error)")
            }
            XCTAssertEqual(url.lastPathComponent, "PEER.jsonl")
        }
    }

    func testLoadLogsReadsNormallyWithoutPlaceholders() throws {
        let folder = tempFolder()
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        try Data("{\"v\":1,\"entity\":\"job\",\"id\":\"greenhouse:1\",\"updated_at\":\"2026-01-01T00:00:00.000Z\",\"device\":\"A1B2\",\"deleted\":false,\"data\":{}}\n".utf8)
            .write(to: changes.appendingPathComponent("A1B2.jsonl"))

        let records = try SyncMerge.loadLogs(folder, materializeTimeout: 0.2)
        XCTAssertEqual(records.count, 1)
        XCTAssertEqual(records.first?.entity, "job")
    }
}
