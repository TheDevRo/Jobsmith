import XCTest
@testable import JobsmithKit

final class SourceCatalogTests: XCTestCase {

    func testKnownSlugsGetPrettyNames() {
        XCTAssertEqual(SourceCatalog.displayName(for: "linkedin"), "LinkedIn")
        XCTAssertEqual(SourceCatalog.displayName(for: "remoteok"), "RemoteOK")
        XCTAssertEqual(SourceCatalog.displayName(for: "usajobs"), "USAJobs")
    }

    func testSlugIsCaseAndWhitespaceInsensitive() {
        XCTAssertEqual(SourceCatalog.displayName(for: "  LinkedIn "), "LinkedIn")
        XCTAssertEqual(SourceCatalog.displayName(for: "GREENHOUSE"), "Greenhouse")
    }

    func testUnknownSlugFallsBackToCapitalized() {
        XCTAssertEqual(SourceCatalog.displayName(for: "someboard"), "Someboard")
    }

    func testEmptyRendersUnknown() {
        XCTAssertEqual(SourceCatalog.displayName(for: ""), "Unknown")
        XCTAssertEqual(SourceCatalog.displayName(for: "   "), "Unknown")
    }
}
