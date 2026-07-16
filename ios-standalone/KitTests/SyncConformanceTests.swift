import XCTest
@testable import JobsmithKit

/// The iOS side of the cross-implementation agreement: the Swift merge must
/// reproduce the same merged state as the Python oracle for every vendored
/// vector, and satisfy read-order/idempotency invariants.
final class SyncConformanceTests: XCTestCase {

    /// Repo-root vendored vectors (shared with the Python conformance test).
    /// #filePath is ios-standalone/KitTests/<this file>, so three pops reach the
    /// repo root.
    private var vectorsDir: URL {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<3 { url.deleteLastPathComponent() }  // -> repo root
        return url.appendingPathComponent("backend/sync/test-vectors")
    }

    private func vectorFolders() -> [URL] {
        let fm = FileManager.default
        let entries = (try? fm.contentsOfDirectory(at: vectorsDir, includingPropertiesForKeys: nil)) ?? []
        return entries
            .filter { fm.fileExists(atPath: $0.appendingPathComponent("expected.json").path) }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    func testVectorsExist() {
        XCTAssertFalse(vectorFolders().isEmpty, "no merge vectors under \(vectorsDir.path)")
    }

    func testVectorsMatchExpected() throws {
        for folder in vectorFolders() {
            let records = try SyncMerge.loadLogs(folder)
            let actual = SyncMerge.merge(records).strippingAnnotations()

            let expectedData = try Data(contentsOf: folder.appendingPathComponent("expected.json"))
            let expected = try JSONDecoder().decode(JSONValue.self, from: expectedData).strippingAnnotations()

            XCTAssertEqual(actual, expected, "vector \(folder.lastPathComponent) diverged from oracle")
        }
    }

    func testInvariants() throws {
        for folder in vectorFolders() {
            let records = try SyncMerge.loadLogs(folder)
            let base = SyncMerge.merge(records)

            XCTAssertEqual(SyncMerge.merge(records.reversed()), base,
                           "\(folder.lastPathComponent): reversed order changed result")

            var rng = SeededRNG(seed: 20260708)
            XCTAssertEqual(SyncMerge.merge(records.shuffled(using: &rng)), base,
                           "\(folder.lastPathComponent): shuffled order changed result")

            XCTAssertEqual(SyncMerge.merge(records + records), base,
                           "\(folder.lastPathComponent): duplicate delivery changed result")
        }
    }
}

/// Deterministic RNG (SplitMix64) so the shuffle invariant is reproducible.
struct SeededRNG: RandomNumberGenerator {
    private var state: UInt64
    init(seed: UInt64) { state = seed }
    mutating func next() -> UInt64 {
        state &+= 0x9E3779B97F4A7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58476D1CE4E5B9
        z = (z ^ (z >> 27)) &* 0x94D049BB133111EB
        return z ^ (z >> 31)
    }
}
