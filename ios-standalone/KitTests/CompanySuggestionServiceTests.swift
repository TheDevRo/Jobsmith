import XCTest
@testable import JobsmithKit

final class CompanySuggestionServiceTests: XCTestCase {

    func testParseDedupesCaseInsensitively() {
        let text = #"""
        {"companies": [
          {"name": "Stripe", "why": "Payments infra fits your backend work."},
          {"name": "stripe", "why": "duplicate"},
          {"name": "Notion", "why": "Docs + collaboration."}
        ]}
        """#
        let result = CompanySuggestionService.parse(text)
        XCTAssertEqual(result.map(\.name), ["Stripe", "Notion"])
        XCTAssertEqual(result.first?.why, "Payments infra fits your backend work.")
    }

    func testParseSalvagesEmbeddedJSON() {
        let text = "Sure! Here you go:\n{\"companies\":[{\"name\":\"Ramp\",\"why\":\"Fintech.\"}]}\nHope that helps."
        XCTAssertEqual(CompanySuggestionService.parse(text).map(\.name), ["Ramp"])
    }

    func testParseHonorsExcludeList() {
        let text = #"{"companies":[{"name":"Stripe","why":"x"},{"name":"Linear","why":"y"}]}"#
        let result = CompanySuggestionService.parse(text, excluding: ["stripe"])
        XCTAssertEqual(result.map(\.name), ["Linear"])
    }

    func testParseAcceptsBareStrings() {
        let text = #"{"companies":["Stripe","Notion"]}"#
        XCTAssertEqual(CompanySuggestionService.parse(text).map(\.name), ["Stripe", "Notion"])
    }

    func testParseReturnsEmptyOnJunk() {
        XCTAssertTrue(CompanySuggestionService.parse("no json here").isEmpty)
        XCTAssertTrue(CompanySuggestionService.parse(#"{"other":[]}"#).isEmpty)
    }
}
