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

    // --- Pay filter ---

    private func paidJob(_ id: String, min: Int? = nil, max: Int? = nil,
                         period: String? = nil) -> Job {
        Job(from: NormalizedJob(source: "greenhouse", externalId: id, title: "Engineer",
                                salaryMin: min, salaryMax: max, salaryPeriod: period))
    }

    func testNoFloorPassesEverythingEvenWhenStrict() {
        let all = [paidJob("a"), paidJob("b", min: 50_000, period: "annual")]
        let off = JobListFilter.applyPayFilter(all, minSalary: nil, requireStatedPay: true)
        XCTAssertEqual(off.jobs.count, 2)
        XCTAssertEqual(off.hiddenNoPay, 0)
        let zero = JobListFilter.applyPayFilter(all, minSalary: 0, requireStatedPay: true)
        XCTAssertEqual(zero.jobs.count, 2)
    }

    func testFloorHidesStatedPayBelowItUsingUpperBound() {
        let all = [paidJob("low", min: 40_000, max: 60_000, period: "annual"),
                   paidJob("straddle", min: 90_000, max: 110_000, period: "annual"),
                   paidJob("high", min: 120_000, period: "annual")]
        let result = JobListFilter.applyPayFilter(all, minSalary: 100_000, requireStatedPay: false)
        // "straddle" survives on its upper bound — same leniency as fetch time.
        XCTAssertEqual(result.jobs.map(\.externalId), ["straddle", "high"])
        // Below-floor is a floor hide, not a no-pay hide.
        XCTAssertEqual(result.hiddenNoPay, 0)
        XCTAssertEqual(result.hiddenBelowFloor, 1)
    }

    func testHourlyPayIsAnnualizedAgainstTheFloor() {
        let all = [paidJob("low", max: 30, period: "hourly"),    // $62,400/yr
                   paidJob("high", max: 60, period: "hourly")]   // $124,800/yr
        let result = JobListFilter.applyPayFilter(all, minSalary: 100_000, requireStatedPay: false)
        XCTAssertEqual(result.jobs.map(\.externalId), ["high"])
    }

    func testLenientModeKeepsUnstatedAndUnknownPeriodPay() {
        let all = [paidJob("none"), paidJob("vague", min: 990, period: "unknown")]
        let result = JobListFilter.applyPayFilter(all, minSalary: 100_000, requireStatedPay: false)
        XCTAssertEqual(result.jobs.count, 2)
        XCTAssertEqual(result.hiddenNoPay, 0)
    }

    func testStrictModeHidesAndCountsUnstatedAndUnknownPeriodPay() {
        let all = [paidJob("none"),
                   paidJob("vague", min: 990, period: "unknown"),
                   paidJob("stated", min: 120_000, period: "annual")]
        let result = JobListFilter.applyPayFilter(all, minSalary: 100_000, requireStatedPay: true)
        XCTAssertEqual(result.jobs.map(\.externalId), ["stated"])
        XCTAssertEqual(result.hiddenNoPay, 2)
        XCTAssertEqual(result.hiddenBelowFloor, 0)
    }

    func testBothHideReasonsAreCountedSeparatelyAndSumToAllHidden() {
        let all = [paidJob("none"),
                   paidJob("low", max: 80_000, period: "annual"),
                   paidJob("lower", max: 50_000, period: "annual"),
                   paidJob("kept", min: 120_000, period: "annual")]
        let result = JobListFilter.applyPayFilter(all, minSalary: 100_000, requireStatedPay: true)
        XCTAssertEqual(result.jobs.map(\.externalId), ["kept"])
        XCTAssertEqual(result.hiddenNoPay, 1)
        XCTAssertEqual(result.hiddenBelowFloor, 2)
        // The invariant the Inbox badge depends on: kept + hidden == input.
        XCTAssertEqual(result.jobs.count + result.hiddenNoPay + result.hiddenBelowFloor, all.count)
    }
}
