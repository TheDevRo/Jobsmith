import XCTest
@testable import JobsmithKit

final class JobListFilterTests: XCTestCase {

    private func job(source: String, title: String = "Engineer", company: String = "Acme",
                     location: String = "Remote", tags: [String] = []) -> Job {
        Job(from: NormalizedJob(source: source, externalId: "\(source)-\(title)",
                                title: title, company: company, location: location, tags: tags))
    }

    private lazy var jobs: [Job] = [
        job(source: "linkedin", title: "Security Engineer", company: "Cloudflare"),
        job(source: "greenhouse", title: "Backend Engineer", company: "Stripe", tags: ["python"]),
        job(source: "adzuna", title: "Barista", company: "Cafe", location: "Denver"),
    ]

    // --- Board filter ---

    func testEmptyBoardSetReturnsAll() {
        XCTAssertEqual(JobListFilter.apply(jobs, query: "", boards: []).count, 3)
    }

    func testBoardFilterRestrictsToSelectedSources() {
        let result = JobListFilter.apply(jobs, query: "", boards: ["linkedin", "adzuna"])
        XCTAssertEqual(Set(result.map(\.source)), ["linkedin", "adzuna"])
    }

    func testAvailableBoardsAreDistinctAndSortedByDisplayName() {
        // Adzuna, Greenhouse, LinkedIn — alphabetical by pretty name.
        XCTAssertEqual(JobListFilter.availableBoards(in: jobs), ["adzuna", "greenhouse", "linkedin"])
    }

    // --- Text search ---

    func testSearchMatchesTitleCompanyBoardAndTags() {
        XCTAssertEqual(JobListFilter.apply(jobs, query: "security", boards: []).count, 1)   // title
        XCTAssertEqual(JobListFilter.apply(jobs, query: "stripe", boards: []).count, 1)     // company
        XCTAssertEqual(JobListFilter.apply(jobs, query: "python", boards: []).count, 1)     // tag
        XCTAssertEqual(JobListFilter.apply(jobs, query: "LinkedIn", boards: []).count, 1)   // board display name
        XCTAssertEqual(JobListFilter.apply(jobs, query: "denver", boards: []).count, 1)     // location
    }

    func testSearchIsCaseInsensitiveAndTrimmed() {
        XCTAssertEqual(JobListFilter.apply(jobs, query: "  ENGINEER ", boards: []).count, 2)
    }

    func testSearchAndBoardCompose() {
        // "engineer" matches 2, but only the greenhouse one survives the board filter.
        let result = JobListFilter.apply(jobs, query: "engineer", boards: ["greenhouse"])
        XCTAssertEqual(result.map(\.company), ["Stripe"])
    }

    func testNoMatchReturnsEmpty() {
        XCTAssertTrue(JobListFilter.apply(jobs, query: "nonexistent", boards: []).isEmpty)
    }
}
