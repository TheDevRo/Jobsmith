import XCTest

/// Live LinkedIn guest-scrape verification — hits linkedin.com for real, so
/// it's skipped unless LINKEDIN_LIVE=1 is in the runner environment:
///   xcodebuild ... test TEST_RUNNER_LINKEDIN_LIVE=1 \
///     -only-testing:JobsmithStandaloneUITests/LinkedInLiveTests
/// Throttled scraping makes this take a few minutes; don't run it in a loop
/// or LinkedIn will 429 the IP.
final class LinkedInLiveTests: XCTestCase {
    func testLinkedInFetchLandsJobs() throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["LINKEDIN_LIVE"] == "1",
                          "live LinkedIn scrape — set TEST_RUNNER_LINKEDIN_LIVE=1 to run")

        let app = XCUIApplication()
        app.launchArguments = ["-SkipOnboarding", "-SeedDemoData", "-UseMockAI",
                               "-E2EKeywords", "security engineer",
                               "-E2ESources", "linkedin"]
        app.launch()

        XCTAssertTrue(app.staticTexts["2 TO TRIAGE"].waitForExistence(timeout: 10))
        app.buttons["Fetch jobs"].tap()

        // LinkedIn is deliberately slow (1.5s search spacing, throttled
        // detail fetches) — poll until the inbox count moves.
        var landed = false
        for _ in 0..<300 {
            if app.alerts["Something went wrong"].exists {
                app.alerts["Something went wrong"].buttons["OK"].tap()
            }
            if !app.staticTexts["2 TO TRIAGE"].exists {
                landed = true
                break
            }
            sleep(1)
        }
        XCTAssertTrue(landed, "LinkedIn fetch should add jobs to the inbox")

        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = "linkedin-fetch-result"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
