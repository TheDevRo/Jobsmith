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
        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))

        let topTitle = topCardTitle(in: app).label
        app.buttons["Shortlist"].tap()
        XCTAssertTrue(app.staticTexts["1 TO SCOUT"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Pipeline"].tap()
        XCTAssertTrue(app.staticTexts[topTitle].waitForExistence(timeout: 5))
    }

    /// Actually drag the top card to the right (not the button) and confirm it
    /// triages as a shortlist — covers the finger-swipe gesture path itself.
    func testSwipeRightShortlistsTopCard() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))

        let topTitle = topCardTitle(in: app)
        let name = topTitle.label
        let start = topTitle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        let end = start.withOffset(CGVector(dx: 340, dy: 0))
        start.press(forDuration: 0.05, thenDragTo: end)

        XCTAssertTrue(app.staticTexts["1 TO SCOUT"].waitForExistence(timeout: 5),
                      "a right drag should remove the top card from the deck")
        app.tabBars.buttons["Pipeline"].tap()
        XCTAssertTrue(app.staticTexts[name].waitForExistence(timeout: 5),
                      "the swiped card should land in the pipeline as shortlisted")
    }

    func testDismissRemovesJobFromDeck() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))
        app.buttons["Pass"].tap()
        XCTAssertTrue(app.staticTexts["1 TO SCOUT"].waitForExistence(timeout: 5))
        app.buttons["Pass"].tap()
        // Deck empty → inbox-clear state.
        XCTAssertTrue(app.staticTexts["Inbox clear"].waitForExistence(timeout: 5))
    }

    /// Hold a pipeline row to enter selection mode, Select all, then Delete —
    /// the pipeline should empty out.
    func testPipelineHoldToSelectAndDelete() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))
        app.buttons["Shortlist"].tap()
        XCTAssertTrue(app.staticTexts["1 TO SCOUT"].waitForExistence(timeout: 5))
        app.buttons["Shortlist"].tap()
        XCTAssertTrue(app.staticTexts["Inbox clear"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Pipeline"].tap()
        let firstRow = app.staticTexts.matching(
            NSPredicate(format: "label == %@ OR label == %@",
                        "Senior Backend Engineer", "Platform Engineer")).firstMatch
        XCTAssertTrue(firstRow.waitForExistence(timeout: 5))
        firstRow.press(forDuration: 0.7)

        let selectAll = app.buttons["Select all"]
        XCTAssertTrue(selectAll.waitForExistence(timeout: 3), "selection mode toolbar")
        selectAll.tap()
        app.buttons["Delete"].tap()
        app.buttons["Delete 2"].tap()

        XCTAssertTrue(app.staticTexts["Nothing in flight"].waitForExistence(timeout: 5),
                      "deleting all selected postings empties the pipeline")
    }

    /// The buried Settings action wipes tracked postings without touching the
    /// rest of the app.
    func testSettingsDeleteAllPostings() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))

        app.tabBars.buttons["Settings"].tap()
        // The Danger-zone button sits at the bottom of the (lazily-rendered)
        // Settings list, so scroll it into view before tapping.
        let delete = app.buttons["Delete all tracked postings"]
        var scrolls = 0
        while !delete.exists && scrolls < 6 {
            app.swipeUp()
            scrolls += 1
        }
        XCTAssertTrue(delete.waitForExistence(timeout: 5))
        delete.tap()
        app.buttons["Delete all postings"].tap()

        app.tabBars.buttons["Inbox"].tap()
        XCTAssertTrue(app.staticTexts["Inbox clear"].waitForExistence(timeout: 5),
                      "deleting all postings empties the inbox")
    }

    /// "Sort by job board" must be offered in both the Inbox and the Pipeline
    /// sort menus (the shared JobSort options drive both).
    func testSortByJobBoardInInboxAndPipeline() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))

        // Inbox: the sort/score overflow menu offers Job board; pick it.
        app.buttons["Sort and score"].tap()
        let inboxOption = app.buttons["Job board"]
        XCTAssertTrue(inboxOption.waitForExistence(timeout: 5),
                      "Inbox sort menu should offer Job board")
        inboxOption.tap()

        // Pipeline: put a job in flight, then confirm its sort menu offers it too.
        app.buttons["Shortlist"].tap()
        app.tabBars.buttons["Pipeline"].tap()
        app.buttons["Sort"].tap()
        XCTAssertTrue(app.buttons["Job board"].waitForExistence(timeout: 5),
                      "Pipeline sort menu should offer Job board")
    }

    /// Background search settings: the Settings row opens the schedule screen,
    /// enabling it flips the cadence picker on, and the choice is reflected back
    /// on the Settings row (On/Off) after navigating back.
    func testBackgroundSearchScheduleToggle() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))

        // The row's accessibility label carries its On/Off detail, so match by prefix.
        func scheduleRow() -> XCUIElement {
            app.buttons.matching(
                NSPredicate(format: "label BEGINSWITH %@", "Background search")).firstMatch
        }

        app.tabBars.buttons["Settings"].tap()
        XCTAssertTrue(scheduleRow().waitForExistence(timeout: 5))
        scheduleRow().tap()

        let toggle = app.switches["Automatic background search"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 5))
        XCTAssertEqual(toggle.value as? String, "0", "starts off")

        // Cadence picker is disabled until the feature is turned on.
        let cadence = app.buttons.matching(
            NSPredicate(format: "label BEGINSWITH %@", "Search about every")).firstMatch
        XCTAssertTrue(cadence.waitForExistence(timeout: 5))
        XCTAssertFalse(cadence.isEnabled, "cadence should be disabled while off")

        // Tap the switch control on the trailing edge — a plain .tap() on a
        // SwiftUI Form toggle lands on the label and doesn't flip it.
        toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertEqual(toggle.value as? String, "1", "tapping turns background search on")
        XCTAssertTrue(cadence.isEnabled, "enabling background search enables the cadence picker")

        // Persistence: leave the screen and come back — the choice must stick
        // (prefs are UserDefaults-backed).
        app.navigationBars.buttons.firstMatch.tap()   // back to Settings
        XCTAssertTrue(scheduleRow().waitForExistence(timeout: 5))
        scheduleRow().tap()
        let toggleAgain = app.switches["Automatic background search"]
        XCTAssertTrue(toggleAgain.waitForExistence(timeout: 5))
        XCTAssertEqual(toggleAgain.value as? String, "1",
                       "background search stays on after leaving and reopening the screen")
    }

    func testScoreJobWithMockAI() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))

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

    /// Tailoring must generate the resume + cover letter (PDF by default) and
    /// reveal the review link — if DocxPDFRenderer threw, tailor() would revert
    /// status and the link would never appear. Network-free (mock AI).
    func testTailorGeneratesReviewableDocuments() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["2 TO SCOUT"].waitForExistence(timeout: 10))

        topCardTitle(in: app).tap()
        let tailor = app.buttons["Tailor"]
        XCTAssertTrue(tailor.waitForExistence(timeout: 5))
        tailor.tap()

        let review = app.buttons["Review documents"]
        XCTAssertTrue(review.waitForExistence(timeout: 30),
                      "tailoring should generate documents and reveal the review link")
        review.tap()

        // The tailored resume text loads — documents generated without error.
        let resumeLine = app.textViews.containing(
            NSPredicate(format: "value CONTAINS %@", "Backend engineer with 8 years"))
        XCTAssertTrue(resumeLine.firstMatch.waitForExistence(timeout: 10))

        // Build the PDF preview artifact — exercises DocxPDFRenderer in-app.
        app.buttons["Preview"].tap()
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

    /// The "Help me pick" AI title suggester opens from Search settings.
    func testTitleSuggestSheetOpens() {
        let app = launch()
        app.tabBars.buttons["Settings"].tap()
        app.staticTexts["Search & sources"].tap()
        let help = app.buttons["Help me pick"]
        XCTAssertTrue(help.waitForExistence(timeout: 5))
        help.tap()
        XCTAssertTrue(app.navigationBars["Suggest titles"].waitForExistence(timeout: 5),
                      "the title-suggestion sheet should appear")
    }

    /// The work-history cap is off by default; turning it on reveals the
    /// "most relevant roles" stepper (mirrors desktop max_resume_experience_entries).
    func testWorkHistoryLimitControl() {
        let app = launch()
        app.tabBars.buttons["Settings"].tap()

        let toggle = app.switches["Limit work history"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 5))
        XCTAssertFalse(app.steppers.firstMatch.exists,
                       "the count stepper is hidden until the limit is enabled")
        // Tap the switch itself (trailing edge) — tapping the row label wouldn't
        // flip a Form toggle.
        toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertTrue(app.steppers.firstMatch.waitForExistence(timeout: 5),
                      "enabling the limit reveals the role-count stepper")
    }

    /// The board finder opens from Search settings — type a company name instead
    /// of hunting for a board slug.
    func testBoardFinderSheetOpens() {
        let app = launch()
        app.tabBars.buttons["Settings"].tap()
        app.staticTexts["Search & sources"].tap()
        let find = app.buttons["Find a company's board"]
        scrollTo(find, in: app)
        XCTAssertTrue(find.waitForExistence(timeout: 5))
        find.tap()
        XCTAssertTrue(app.navigationBars["Find a company"].waitForExistence(timeout: 5),
                      "the board finder sheet should appear")
    }

    /// The AI company suggester ("who do you want to work for") opens from Search
    /// settings.
    func testCompanySuggestSheetOpens() {
        let app = launch()
        app.tabBars.buttons["Settings"].tap()
        app.staticTexts["Search & sources"].tap()
        let suggest = app.buttons["Suggest companies to follow"]
        scrollTo(suggest, in: app)
        XCTAssertTrue(suggest.waitForExistence(timeout: 5))
        suggest.tap()
        XCTAssertTrue(app.navigationBars["Suggest companies"].waitForExistence(timeout: 5),
                      "the company suggestion sheet should appear")
    }

    /// Swipe the Form up until `element` is on screen (it lives below the fold).
    private func scrollTo(_ element: XCUIElement, in app: XCUIApplication) {
        var tries = 0
        while !element.isHittable && tries < 8 {
            app.swipeUp()
            tries += 1
        }
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
