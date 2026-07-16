import XCTest

/// App Store screenshot pass: drives the five listing screens (inbox, job
/// detail, pipeline, activity, settings) over seeded demo data with mock AI —
/// no network, so it runs deterministically on any simulator. Run it on the
/// current 6.9" device (iPhone Pro Max class) and export the attachments from
/// the xcresult bundle; names match beta-release/ios/screenshots/.
final class StoreScreenshotsTests: XCTestCase {
    func testCaptureListingScreenshots() {
        let app = XCUIApplication()
        app.launchArguments = ["-SkipOnboarding", "-SeedDemoData", "-UseMockAI"]
        app.launch()

        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))
        snap(app, "01-inbox")

        // Shortlist the top seeded card so the pipeline has a row to show.
        app.buttons["Shortlist"].tap()
        app.tabBars.buttons["Pipeline"].tap()
        let row = app.buttons.matching(
            NSPredicate(format: "label CONTAINS[c] %@", "not scored")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        snap(app, "03-pipeline")

        // Score + tailor with canned AI so the detail screen shows the full
        // scored state (heat chip, reasoning, document link).
        row.tap()
        XCTAssertTrue(app.buttons["Score"].waitForExistence(timeout: 5))
        app.buttons["Score"].tap()
        let reasoning = app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS %@", "Strong overlap on core backend skills"))
        XCTAssertTrue(reasoning.firstMatch.waitForExistence(timeout: 15))
        app.buttons["Tailor"].tap()
        XCTAssertTrue(app.buttons["Review documents"].waitForExistence(timeout: 30))
        snap(app, "02-job-detail")

        app.tabBars.buttons["Activity"].tap()
        XCTAssertTrue(app.tabBars.buttons["Activity"].isSelected)
        snap(app, "04-activity")

        app.tabBars.buttons["Settings"].tap()
        XCTAssertTrue(app.tabBars.buttons["Settings"].isSelected)
        snap(app, "05-settings")
    }

    private func snap(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
