import XCTest
@testable import JobsmithKit

final class BoardDetectorTests: XCTestCase {

    func testSlugCandidatesDropsLegalSuffix() {
        XCTAssertEqual(BoardDetector.slugCandidates("Notion Labs"),
                       ["notionlabs", "notion-labs", "notion"])
    }

    func testSlugCandidatesSingleWord() {
        // Joined and hyphen-joined collapse to one candidate for a single word.
        XCTAssertEqual(BoardDetector.slugCandidates("Stripe"), ["stripe"])
    }

    func testSlugCandidatesStripsPunctuation() {
        XCTAssertEqual(BoardDetector.slugCandidates("Acme, Inc."),
                       ["acmeinc", "acme-inc", "acme"])
    }

    func testSlugCandidatesEmptyForNoLetters() {
        XCTAssertEqual(BoardDetector.slugCandidates("!!!"), [])
        XCTAssertEqual(BoardDetector.slugCandidates(""), [])
    }

    func testATSMappingAndKeyPaths() {
        // Lever is fetched by GreenhouseSource, so it shares the enabled id.
        XCTAssertEqual(BoardDetector.ATS.lever.enabledSourceID, "greenhouse")
        XCTAssertEqual(BoardDetector.ATS.greenhouse.enabledSourceID, "greenhouse")
        XCTAssertEqual(BoardDetector.ATS.ashby.enabledSourceID, "ashby")
        XCTAssertEqual(BoardDetector.ATS.workable.enabledSourceID, "workable")
        XCTAssertEqual(BoardDetector.ATS.recruitee.enabledSourceID, "recruitee")

        var config = SearchConfig()
        config[keyPath: BoardDetector.ATS.lever.keyPath].append("openai")
        config[keyPath: BoardDetector.ATS.ashby.keyPath].append("ramp")
        XCTAssertEqual(config.leverCompanies, ["openai"])
        XCTAssertEqual(config.ashbyBoards, ["ramp"])
    }

    func testParseProbeGreenhouseAndAshby() {
        let gh = Data(#"{"jobs":[{"id":1},{"id":2}]}"#.utf8)
        let match = BoardDetector.parseProbe(ats: .greenhouse, slug: "stripe", status: 200, data: gh)
        XCTAssertEqual(match?.jobs, 2)
        XCTAssertEqual(match?.slug, "stripe")
        XCTAssertNil(match?.companyName)

        let ashby = Data(#"{"jobs":[{"id":"a"}]}"#.utf8)
        XCTAssertEqual(BoardDetector.parseProbe(ats: .ashby, slug: "ramp", status: 200, data: ashby)?.jobs, 1)
    }

    func testParseProbeLeverIsArray() {
        let lever = Data(#"[{"id":"a"},{"id":"b"},{"id":"c"}]"#.utf8)
        XCTAssertEqual(BoardDetector.parseProbe(ats: .lever, slug: "openai", status: 200, data: lever)?.jobs, 3)
    }

    func testParseProbeExtractsCompanyName() {
        let workable = Data(#"{"name":"Acme Co","jobs":[{"id":1}]}"#.utf8)
        let wm = BoardDetector.parseProbe(ats: .workable, slug: "acme", status: 200, data: workable)
        XCTAssertEqual(wm?.jobs, 1)
        XCTAssertEqual(wm?.companyName, "Acme Co")

        let recruitee = Data(#"{"offers":[{"company_name":"Acme Co"}]}"#.utf8)
        let rm = BoardDetector.parseProbe(ats: .recruitee, slug: "acme", status: 200, data: recruitee)
        XCTAssertEqual(rm?.jobs, 1)
        XCTAssertEqual(rm?.companyName, "Acme Co")
    }

    func testParseProbeRejectsNon200AndGarbage() {
        let ok = Data(#"{"jobs":[]}"#.utf8)
        XCTAssertNil(BoardDetector.parseProbe(ats: .greenhouse, slug: "x", status: 404, data: ok))
        XCTAssertNil(BoardDetector.parseProbe(ats: .greenhouse, slug: "x", status: 200, data: Data("not json".utf8)))
        // Wrong shape (array where an object is expected) → no count → nil.
        XCTAssertNil(BoardDetector.parseProbe(ats: .greenhouse, slug: "x", status: 200, data: Data("[]".utf8)))
    }
}
