import XCTest
import JobsmithKit

final class ScaffoldTests: XCTestCase {
    func testAppGroupIdentifier() {
        XCTAssertEqual(AppGroup.identifier, "group.com.thedevro.jobsmith.standalone")
    }
}
