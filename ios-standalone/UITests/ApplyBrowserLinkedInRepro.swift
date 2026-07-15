import XCTest

/// Live check that the Apply browser can show a LinkedIn posting to a guest:
/// LinkedIn serves the page but its bot check normally hides it and bounces
/// to the authwall — the Google-referer load plus the authwall-cancel in
/// ApplyWebController keep it visible. Hits linkedin.com for real, so it's
/// skipped unless LINKEDIN_LIVE=1 is in the runner environment (same switch
/// as LinkedInLiveTests). The posting URL will eventually expire; when the
/// run shows a "job not found" page, swap in any current /jobs/view URL.
final class ApplyBrowserLinkedInRepro: XCTestCase {
    func testOpenLinkedInJobInApplyBrowser() throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["LINKEDIN_LIVE"] == "1",
                          "live LinkedIn load — set TEST_RUNNER_LINKEDIN_LIVE=1 to run")

        let app = XCUIApplication()
        app.launchArguments = ["-SkipOnboarding", "-SeedDemoData", "-UseMockAI",
                               "-E2EJobURL",
                               "https://www.linkedin.com/jobs/view/software-engineer-early-career-at-notion-4437461678"]
        app.launch()

        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))
        app.buttons["Shortlist"].tap()
        app.tabBars.buttons["Pipeline"].tap()

        let row = app.staticTexts["Senior Backend Engineer"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        row.tap()

        let apply = app.buttons["Apply"]
        XCTAssertTrue(apply.waitForExistence(timeout: 5))
        apply.tap()

        XCTAssertTrue(app.buttons["Autofill"].waitForExistence(timeout: 10))

        // Screenshot at intervals so we can see redirects/blanking as they occur.
        for (i, delay) in [3, 5, 7].enumerated() {
            sleep(UInt32(delay))
            let shot = XCTAttachment(screenshot: app.screenshot())
            shot.lifetime = .keepAlways
            shot.name = "linkedin-apply-\(i)-after-\([3, 8, 15][i])s"
            add(shot)
        }

        // Dismiss LinkedIn's contextual sign-in modal if present, then confirm
        // the posting itself is readable.
        let dismiss = app.webViews.buttons["Dismiss"]
        if dismiss.waitForExistence(timeout: 5) {
            dismiss.tap()
        }
        sleep(2)
        let after = XCTAttachment(screenshot: app.screenshot())
        after.lifetime = .keepAlways
        after.name = "linkedin-apply-4-modal-dismissed"
        add(after)
    }
}
