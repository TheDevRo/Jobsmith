import XCTest

/// The scenario the resume work exists for: a LinkedIn search is still running
/// when the app goes away, and neither the jobs it collected nor the run itself
/// may be lost.
///
/// The app is *terminated* mid-search rather than merely backgrounded. The
/// simulator doesn't enforce background suspension the way a device does — a
/// backgrounded app there just keeps running and the search finishes, which
/// proves nothing. Killing the process is both a faithful stand-in for iOS
/// suspending an app whose background window has expired, and the harsher case:
/// the process dies without even running its expiration handler, so the run is
/// left mid-flight with no chance to tidy up.
///
/// Live network, like `LinkedInLiveTests`, and gated the same way.
final class BackgroundResumeTests: XCTestCase {
    private static let seededInboxCount = "2 TO SCOUT"

    func testKilledMidSearchKeepsItsJobsAndResumesOnRelaunch() throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["LINKEDIN_LIVE"] == "1",
                          "live LinkedIn scrape — set TEST_RUNNER_LINKEDIN_LIVE=1 to run")

        // --- Collect for a few seconds, then kill the process mid-search ---
        let app = makeApp(seed: true)
        app.launch()
        XCTAssertTrue(app.staticTexts[Self.seededInboxCount].waitForExistence(timeout: 10),
                      "starts from the 2 seeded demo jobs")
        app.buttons["Fetch jobs"].tap()

        // Long enough for the search phase to hand over a page or two of cards,
        // nowhere near long enough to finish the whole multi-minute scrape.
        Thread.sleep(forTimeInterval: 10)
        attach(app.screenshot(), name: "1-mid-search")
        app.terminate()

        // --- Relaunch onto the database the killed run left behind ---
        let relaunched = makeApp(seed: false)   // no reseed: keep what phase one wrote
        relaunched.launch()

        // The jobs collected before the kill are already here. The old pipeline
        // pooled everything and upserted once at the very end, so this is
        // precisely what used to be thrown away.
        XCTAssertTrue(relaunched.staticTexts["Inbox"].waitForExistence(timeout: 10))
        XCTAssertFalse(relaunched.staticTexts[Self.seededInboxCount].exists,
                       "jobs collected before the kill survived it")
        attach(relaunched.screenshot(), name: "2-relaunched-jobs-survived")

        // And being cut off is not reported as a failure.
        XCTAssertFalse(relaunched.alerts["Something went wrong"].exists,
                       "an interrupted search is resumable, not an error")

        // The unfinished run is picked up on foreground and carried to
        // completion; give the resumed detail phase room to finish.
        Thread.sleep(forTimeInterval: 90)
        attach(relaunched.screenshot(), name: "3-after-resume")
        XCTAssertFalse(relaunched.alerts["Something went wrong"].exists,
                       "the resumed run finishes cleanly")
    }

    private func makeApp(seed: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        var args = ["-SkipOnboarding", "-UseMockAI",
                    "-E2EKeywords", "security engineer",
                    "-E2ESources", "linkedin"]
        if seed { args.insert("-SeedDemoData", at: 1) }
        app.launchArguments = args
        return app
    }

    private func attach(_ screenshot: XCUIScreenshot, name: String) {
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
