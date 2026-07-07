import XCTest

/// Full-pipeline walkthrough: real network fetch → triage → mock-AI score +
/// tailor → document review. Captures a screenshot at each stage (exported
/// from the xcresult bundle). Network-dependent by design — this is the
/// end-to-end verification pass, not part of the fast smoke suite.
final class EndToEndWalkthroughTests: XCTestCase {
    func testFullPipelineWalkthrough() {
        let app = XCUIApplication()
        app.launchArguments = ["-SkipOnboarding", "-SeedDemoData", "-UseMockAI",
                               "-E2EKeywords", "engineer,developer,designer,manager,analyst"]
        app.launch()

        XCTAssertTrue(app.staticTexts["2 TO TRIAGE"].waitForExistence(timeout: 10))
        snap(app, "1-inbox-deck")

        // Real fetch from the default sources (RemoteOK, WWR, Arbeitnow).
        app.buttons["Fetch jobs"].tap()
        for _ in 0..<60 {
            dismissErrorAlert(app)
            if !app.staticTexts["2 TO TRIAGE"].exists { break }
            sleep(1)
        }
        dismissErrorAlert(app)
        snap(app, "2-inbox-after-fetch")

        // Shortlist the top card (whatever the fetch put there), then drive
        // the single shortlisted row in the pipeline.
        app.buttons["Shortlist"].tap()
        app.tabBars.buttons["Pipeline"].tap()
        // SwiftUI List rows surface as Buttons (not cells); the not-yet-scored
        // row is the one whose HeatChip reads "Not scored".
        let row = app.buttons.matching(
            NSPredicate(format: "label CONTAINS %@", "Not scored")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 5),
                      "shortlisted job should appear in the pipeline")
        snap(app, "3-pipeline")

        // Open detail, score and tailor with canned AI responses.
        row.tap()
        XCTAssertTrue(app.buttons["Score"].waitForExistence(timeout: 5))
        app.buttons["Score"].tap()
        let reasoning = app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS %@", "Strong overlap on core backend skills"))
        XCTAssertTrue(reasoning.firstMatch.waitForExistence(timeout: 15))

        app.buttons["Tailor"].tap()
        let review = app.buttons["Review documents"]
        XCTAssertTrue(review.waitForExistence(timeout: 30),
                      "tailoring should produce documents and a review link")
        snap(app, "4-job-detail-scored")

        // Review screen shows the tailored resume text; DOCX was generated.
        review.tap()
        let resumeLine = app.textViews.containing(
            NSPredicate(format: "value CONTAINS %@", "Backend engineer with 8 years"))
        XCTAssertTrue(resumeLine.firstMatch.waitForExistence(timeout: 10))
        snap(app, "5-document-review")
    }

    private func dismissErrorAlert(_ app: XCUIApplication) {
        let alert = app.alerts["Something went wrong"]
        if alert.exists { alert.buttons["OK"].tap() }
    }

    private func snap(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
