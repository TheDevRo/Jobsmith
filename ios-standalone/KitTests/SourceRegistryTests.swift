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

    // MARK: enabledIDs — LinkedIn is governed by the flag, not set membership

    func testEnabledIDsRequireMembershipForOrdinarySources() {
        var config = AppConfig()
        config.search.enabledSources = ["remoteok"]
        config.search.linkedInEnabled = false
        XCTAssertEqual(SourceRegistry.enabledIDs(for: config), ["remoteok"])
    }

    func testLinkedInEnabledByFlagAloneAfterSyncStripsMembership() {
        // Settings sync's unfold removes "linkedin" from enabledSources and
        // keeps it on the linkedInEnabled flag (SettingsSync rule #3). The
        // registry must still fetch it, or every sync import silently turns
        // LinkedIn off while the Settings toggle shows it on.
        var config = AppConfig()
        config.search.enabledSources = []          // post-unfold: no "linkedin" member
        config.search.linkedInEnabled = true
        XCTAssertEqual(SourceRegistry.enabledIDs(for: config), ["linkedin"])
    }

    func testLinkedInOffByFlagEvenWhenSetMembershipLingers() {
        // The master switch wins over a stale set entry.
        var config = AppConfig()
        config.search.enabledSources = ["linkedin"]
        config.search.linkedInEnabled = false
        XCTAssertEqual(SourceRegistry.enabledIDs(for: config), [])
    }
}
