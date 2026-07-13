import XCTest

/// Drives the post-apply outcome UI in the real app.
///
/// Worth an actual UI test rather than trusting the unit coverage: the outcome
/// chip is a `Menu` living inside a `List` row that already carries a tap gesture
/// (open detail) and a long-press gesture (multi-select). Those can swallow the
/// menu's taps, and nothing below the view layer would catch it.
///
/// -SeedApplied puts demo-1 in the submitted state with an application, so the
/// outcome UI is reachable without driving a real apply through a live posting.
final class OutcomeTests: XCTestCase {

    private func launch() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-SkipOnboarding", "-SeedDemoData", "-SeedApplied", "-UseMockAI"]
        app.launch()
        app.tabBars.buttons["Pipeline"].tap()
        return app
    }

    func testRecordingAnOutcomeFromThePipelineRow() {
        let app = launch()

        // A submitted job starts out awaiting a response.
        let chip = app.buttons["Outcome: Awaiting response"]
        XCTAssertTrue(chip.waitForExistence(timeout: 10),
                      "an applied job should show its outcome in the pipeline")

        // The menu opens despite the row's tap/long-press gestures...
        chip.tap()
        let screening = app.buttons["Screening"]
        XCTAssertTrue(screening.waitForExistence(timeout: 5),
                      "the outcome menu should open from inside the list row")
        screening.tap()

        // ...and the choice sticks.
        XCTAssertTrue(app.buttons["Outcome: Screening"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Outcome: Awaiting response"].exists)
    }

    /// Tapping the row itself must still open the job, not the menu.
    func testRowStillOpensDetailAndShowsOutcome() {
        let app = launch()
        XCTAssertTrue(app.buttons["Outcome: Awaiting response"].waitForExistence(timeout: 10))

        app.buttons.matching(
            NSPredicate(format: "label CONTAINS %@", "Senior Backend Engineer")
        ).firstMatch.tap()

        // Eyebrow renders its label uppercased.
        XCTAssertTrue(app.staticTexts["OUTCOME"].waitForExistence(timeout: 5),
                      "the detail screen should show the outcome section for an applied job")
    }

    /// The funnel counts an application that was rejected after interviewing
    /// toward the stages it actually reached — the bug the event log fixes.
    func testFunnelKeepsStagesARejectedApplicationReached() {
        let app = launch()
        let chip = app.buttons["Outcome: Awaiting response"]
        XCTAssertTrue(chip.waitForExistence(timeout: 10))

        chip.tap()
        app.buttons["Screening"].tap()
        app.buttons["Outcome: Screening"].tap()
        app.buttons["Interview"].tap()
        app.buttons["Outcome: Interview"].tap()
        app.buttons["Rejected"].tap()
        XCTAssertTrue(app.buttons["Outcome: Rejected"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Activity"].tap()
        let funnel = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@", "Applied: 1")).firstMatch
        XCTAssertTrue(funnel.waitForExistence(timeout: 5), "the funnel should appear once you've applied")
        // Rejected, but it did reach screening and interview — both still count.
        XCTAssertTrue(funnel.label.contains("Screening: 1"), "funnel lost the screening stage: \(funnel.label)")
        XCTAssertTrue(funnel.label.contains("Interview: 1"), "funnel lost the interview stage: \(funnel.label)")
        XCTAssertTrue(funnel.label.contains("Offer: 0"), "funnel invented an offer: \(funnel.label)")
    }
}
