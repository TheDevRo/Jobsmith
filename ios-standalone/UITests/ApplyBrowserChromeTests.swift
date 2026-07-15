import XCTest

/// Opens the in-app Apply browser from a seeded job and checks the slim
/// toolbar's controls, then confirms Close returns to the job detail.
final class ApplyBrowserChromeTests: XCTestCase {
    func testApplyBrowserToolbarControls() {
        let app = XCUIApplication()
        app.launchArguments = ["-SkipOnboarding", "-SeedDemoData", "-UseMockAI"]
        app.launch()

        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))
        app.buttons["Shortlist"].tap()
        app.tabBars.buttons["Pipeline"].tap()

        let first = app.staticTexts["Senior Backend Engineer"]
        let second = app.staticTexts["Platform Engineer"]
        let row = first.waitForExistence(timeout: 5) ? first : second
        XCTAssertTrue(row.exists)
        row.tap()

        let apply = app.buttons["Apply"]
        XCTAssertTrue(apply.waitForExistence(timeout: 5))
        apply.tap()

        // The new single-toolbar chrome.
        XCTAssertTrue(app.buttons["Autofill"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Close"].exists)
        XCTAssertTrue(app.buttons["Back"].exists)
        XCTAssertTrue(app.buttons["Forward"].exists)
        XCTAssertTrue(app.buttons["Reload"].exists)
        XCTAssertTrue(app.buttons["Open in Safari"].exists)

        sleep(3) // let the page settle for the screenshot
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.lifetime = .keepAlways
        shot.name = "apply-browser-chrome"
        add(shot)

        // Close returns to the job detail.
        app.buttons["Close"].tap()
        XCTAssertTrue(apply.waitForExistence(timeout: 5))
    }
}
