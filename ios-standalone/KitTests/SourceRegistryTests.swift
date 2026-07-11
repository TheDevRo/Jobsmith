import XCTest
@testable import JobsmithKit

final class SourceRegistryTests: XCTestCase {

    func testTimeoutForKnownAndUnknownSource() {
        XCTAssertNotNil(SourceRegistry.timeout(for: "greenhouse"))
        XCTAssertNil(SourceRegistry.timeout(for: "not-a-source"))
    }

    func testEstimatedDurationIsTheSlowestEnabledSource() {
        // Sources run in parallel, so the estimate is the single slowest budget.
        // remoteok = 60s, greenhouse = 300s → 300s.
        let estimate = SourceRegistry.estimatedDuration(for: ["remoteok", "greenhouse"])
        XCTAssertEqual(estimate, .seconds(300))
    }

    func testEstimatedDurationIgnoresUnknownSources() {
        let estimate = SourceRegistry.estimatedDuration(for: ["remoteok", "bogus"])
        XCTAssertEqual(estimate, .seconds(60))
    }

    func testEstimatedDurationFallsBackWhenEmpty() {
        // Empty request estimates over all registered sources (non-zero).
        XCTAssertGreaterThan(SourceRegistry.estimatedDuration(for: []), .seconds(0))
    }
}
