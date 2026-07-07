import XCTest

/// Smoke tests against a live Jobsmith backend on 127.0.0.1:8888.
/// Run scripts/run-ui-tests.sh (or start the backend first by hand):
/// the suite seeds the server URL via launch arguments, then drives the real
/// web frontend inside the app's WKWebView.
final class DashboardSmokeTests: XCTestCase {

    private func launchApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments += ["-jobsmith.serverURL", "http://127.0.0.1:8888"]
        app.launch()
        return app
    }

    func testDashboardLoads() {
        let app = launchApp()
        let dashboard = app.webViews.staticTexts["Dashboard"]
        XCTAssertTrue(dashboard.waitForExistence(timeout: 20),
                      "Dashboard topbar should render inside the webview")
        XCTAssertTrue(app.webViews.staticTexts["Fetch Jobs"].waitForExistence(timeout: 10),
                      "Fetch Jobs card should render")
    }

    func testFirstRunSetupFlow() {
        let app = XCUIApplication()
        app.launchArguments = ["--reset-server"]
        app.launch()

        let field = app.textFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 10), "Setup screen should show the address field")
        field.tap()
        // Trailing newline submits the field, which runs the connect flow.
        field.typeText("127.0.0.1:8888\n")

        XCTAssertTrue(app.webViews.staticTexts["Dashboard"].waitForExistence(timeout: 20),
                      "Connecting should land on the dashboard")
    }

    func testNavigateToJobFeed() {
        let app = launchApp()
        XCTAssertTrue(app.webViews.staticTexts["Dashboard"].waitForExistence(timeout: 20))

        // The hamburger toggle is the first button in the topbar.
        let webView = app.webViews.firstMatch
        let hamburger = webView.buttons.firstMatch
        XCTAssertTrue(hamburger.waitForExistence(timeout: 10))
        hamburger.tap()

        let jobFeedLink = webView.links["Job Feed"].firstMatch
        if jobFeedLink.waitForExistence(timeout: 5) {
            jobFeedLink.tap()
        } else {
            // Sidebar entries may expose as buttons or static texts depending
            // on the frontend's markup.
            let candidate = webView.staticTexts["Job Feed"].firstMatch
            XCTAssertTrue(candidate.waitForExistence(timeout: 5), "Job Feed nav entry should exist")
            candidate.tap()
        }

        XCTAssertTrue(webView.staticTexts["Job Feed"].firstMatch.waitForExistence(timeout: 10),
                      "Job Feed page should render after navigation")
    }
}
