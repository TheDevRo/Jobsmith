import XCTest

/// End-to-end smoke tests against seeded demo data and canned AI responses.
/// -SeedDemoData wipes and re-inserts two demo jobs so every run starts with
/// a fresh untriaged inbox; -UseMockAI serves fixture completions.
final class SmokeTests: XCTestCase {
    private func launch() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-SkipOnboarding", "-SeedDemoData", "-UseMockAI"]
        app.launch()
        return app
    }

    /// The deck's top card is the only hittable one — pick whichever seeded
    /// title is on top rather than assuming inbox ordering.
    private func topCardTitle(in app: XCUIApplication) -> XCUIElement {
        let first = app.staticTexts["Senior Backend Engineer"]
        let second = app.staticTexts["Platform Engineer"]
        return first.exists && first.isHittable ? first : second
    }

    func testAppLaunchesToTabs() {
        let app = launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.tabBars.buttons["Pipeline"].exists)
        XCTAssertTrue(app.tabBars.buttons["Activity"].exists)
        XCTAssertTrue(app.tabBars.buttons["Settings"].exists)
    }

    func testShortlistMovesJobToPipeline() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO TRIAGE"].waitForExistence(timeout: 10))

        let topTitle = topCardTitle(in: app).label
        app.buttons["Shortlist"].tap()
        XCTAssertTrue(app.staticTexts["1 TO TRIAGE"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Pipeline"].tap()
        XCTAssertTrue(app.staticTexts[topTitle].waitForExistence(timeout: 5))
    }

    /// Actually drag the top card to the right (not the button) and confirm it
    /// triages as a shortlist — covers the finger-swipe gesture path itself.
    func testSwipeRightShortlistsTopCard() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO TRIAGE"].waitForExistence(timeout: 10))

        let topTitle = topCardTitle(in: app)
        let name = topTitle.label
        let start = topTitle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        let end = start.withOffset(CGVector(dx: 340, dy: 0))
        start.press(forDuration: 0.05, thenDragTo: end)

        XCTAssertTrue(app.staticTexts["1 TO TRIAGE"].waitForExistence(timeout: 5),
                      "a right drag should remove the top card from the deck")
        app.tabBars.buttons["Pipeline"].tap()
        XCTAssertTrue(app.staticTexts[name].waitForExistence(timeout: 5),
                      "the swiped card should land in the pipeline as shortlisted")
    }

    func testDismissRemovesJobFromDeck() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO TRIAGE"].waitForExistence(timeout: 10))
        app.buttons["Pass"].tap()
        XCTAssertTrue(app.staticTexts["1 TO TRIAGE"].waitForExistence(timeout: 5))
        app.buttons["Pass"].tap()
        // Deck empty → inbox-clear state.
        XCTAssertTrue(app.staticTexts["Inbox clear"].waitForExistence(timeout: 5))
    }

    func testScoreJobWithMockAI() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO TRIAGE"].waitForExistence(timeout: 10))

        topCardTitle(in: app).tap()
        let scoreButton = app.buttons["Score"]
        XCTAssertTrue(scoreButton.waitForExistence(timeout: 5))
        scoreButton.tap()

        // Mock fixture returns score 82 with this reasoning.
        let reasoning = app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS %@", "Strong overlap on core backend skills"))
        XCTAssertTrue(reasoning.firstMatch.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["WHY THIS SCORE"].exists)
    }

    func testOnboardingAIStepPrecedesImport() {
        // No -SkipOnboarding: with the seeded (empty) profile, the setup
        // sheet appears. AI comes first so the profile import can use it.
        let app = XCUIApplication()
        app.launchArguments = ["-SeedDemoData", "-UseMockAI"]
        app.launch()

        let setUp = app.buttons["Set up"]
        XCTAssertTrue(setUp.waitForExistence(timeout: 10))
        setUp.tap()

        XCTAssertTrue(app.staticTexts["Connect your AI"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Test connection"].exists)
        attach(app, "onboarding-ai-step")
        app.buttons["Continue"].tap()

        XCTAssertTrue(app.staticTexts["Import your profile"].waitForExistence(timeout: 5))
        // Back returns to the AI step, forward again to the import step.
        app.buttons["Back"].tap()
        XCTAssertTrue(app.staticTexts["Connect your AI"].waitForExistence(timeout: 5))
        app.buttons["Continue"].tap()
        XCTAssertTrue(app.staticTexts["Import your profile"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["LinkedIn profile"].exists, "import source picker")
        app.buttons["LinkedIn profile"].tap()
        XCTAssertTrue(app.buttons["Sign in with LinkedIn"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.textFields["linkedin.com/in/yourname"].exists,
                      "profile link field")
        attach(app, "onboarding-import-linkedin")

        // Import a pasted resume via the mock AI and confirm the extracted
        // profile actually reaches the review screen (regression: the config
        // write used to race the step change and review showed empty).
        app.buttons["Resume"].tap()
        let editor = app.textViews.firstMatch
        XCTAssertTrue(editor.waitForExistence(timeout: 3))
        editor.tap()
        editor.typeText("Test User, engineer at Acme.")
        app.buttons["Extract profile"].tap()

        let nameField = app.textFields.matching(
            NSPredicate(format: "value == %@", "Test User")).firstMatch
        XCTAssertTrue(nameField.waitForExistence(timeout: 10),
                      "imported name should appear in the profile review")
        attach(app, "onboarding-profile-imported")
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func testSettingsTabShowsSections() {
        let app = launch()
        XCTAssertTrue(app.tabBars.buttons["Settings"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Settings"].tap()
        XCTAssertTrue(app.staticTexts["Profile"].waitForExistence(timeout: 5)
                      || app.buttons["Profile"].waitForExistence(timeout: 2))

        // The setup assistant can be re-run from Settings.
        app.buttons["Run setup assistant"].tap()
        XCTAssertTrue(app.buttons["Set up"].waitForExistence(timeout: 5),
                      "welcome step of the setup flow")
        app.buttons["Set up"].tap()
        XCTAssertTrue(app.staticTexts["Connect your AI"].waitForExistence(timeout: 5))
    }
}
