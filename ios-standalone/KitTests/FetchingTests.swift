import XCTest
import CryptoKit
@testable import JobsmithKit

// MARK: - Fixture loading

private final class FixtureToken {}

enum Fixtures {
    struct MissingFixture: Error { let name: String }

    /// xcodegen may flatten resources to the bundle root or keep the
    /// Fixtures/ subdirectory — try both.
    static func url(_ name: String, _ ext: String) throws -> URL {
        let bundle = Bundle(for: FixtureToken.self)
        if let url = bundle.url(forResource: name, withExtension: ext, subdirectory: "Fixtures") {
            return url
        }
        if let url = bundle.url(forResource: name, withExtension: ext) {
            return url
        }
        throw MissingFixture(name: "\(name).\(ext)")
    }

    static func data(_ name: String, _ ext: String) throws -> Data {
        try Data(contentsOf: url(name, ext))
    }

    static func string(_ name: String, _ ext: String) throws -> String {
        try String(contentsOf: url(name, ext), encoding: .utf8)
    }
}

// MARK: - Source parsing

final class SourceParsingTests: XCTestCase {
    private let noExcludes: [NSRegularExpression] = []

    func testGreenhouseBoardParsing() throws {
        let jobs = try GreenhouseSource.parseBoard(
            data: try Fixtures.data("greenhouse", "json"),
            slug: "acme",
            keywords: ["security"],
            excludePatterns: JobFilters.compileExcludes(["TS/SCI"]),
            knownIDs: ["gh-acme-4012348"])
        // Office Manager fails keywords, TS/SCI is excluded, 4012348 is known.
        XCTAssertEqual(jobs.count, 1)
        let job = jobs[0]
        XCTAssertEqual(job.source, "greenhouse")
        XCTAssertEqual(job.externalId, "gh-acme-4012345")
        XCTAssertEqual(job.title, "Senior Security Engineer")
        XCTAssertEqual(job.company, "Acme Corp")
        XCTAssertEqual(job.location, "Remote - US")
        XCTAssertEqual(job.url, "https://boards.greenhouse.io/acme/jobs/4012345")
        XCTAssertEqual(job.description,
                       "We are looking for a security engineer to harden our platform.")
        XCTAssertEqual(job.tags, ["Security"])
        XCTAssertEqual(job.datePosted, "2026-07-01T12:30:45-04:00")
        XCTAssertTrue(job.isRemote)
    }

    func testGreenhouseCompanyFallsBackToSlug() throws {
        let jobs = try GreenhouseSource.parseBoard(
            data: try Fixtures.data("greenhouse", "json"),
            slug: "acme-corp", keywords: ["office"], excludePatterns: noExcludes, knownIDs: [])
        XCTAssertEqual(jobs.count, 1)
        // Office Manager has no company_name — slug is title-cased.
        XCTAssertEqual(jobs[0].company, "Acme Corp")
    }

    func testLeverBoardParsing() throws {
        let jobs = try GreenhouseSource.parseLeverBoard(
            data: try Fixtures.data("lever", "json"),
            slug: "acme", keywords: ["engineer"], excludePatterns: noExcludes, knownIDs: [])
        XCTAssertEqual(jobs.count, 1)
        let job = jobs[0]
        XCTAssertEqual(job.source, "lever")
        XCTAssertEqual(job.externalId, "lv-acme-a1b2c3d4-0000-4000-8000-1234567890ab")
        XCTAssertEqual(job.title, "Backend Engineer")
        XCTAssertEqual(job.company, "Acme")
        XCTAssertEqual(job.location, "Remote - North America")
        XCTAssertEqual(job.tags, ["Platform", "Engineering"])
        XCTAssertTrue(job.isRemote)
        XCTAssertTrue(job.description.contains("backend services in Go"))
    }

    func testLeverEmptyKeywordsMatchesNothing() throws {
        // Python parity: Lever gates on matches_keywords without an
        // empty-list bypass, so no keywords means no jobs.
        let jobs = try GreenhouseSource.parseLeverBoard(
            data: try Fixtures.data("lever", "json"),
            slug: "acme", keywords: [], excludePatterns: noExcludes, knownIDs: [])
        XCTAssertTrue(jobs.isEmpty)
    }

    func testAshbyParsing() throws {
        let jobs = try AshbySource.parse(
            data: try Fixtures.data("ashby", "json"),
            board: "acme", keywords: ["engineer"], excludePatterns: noExcludes)
        // The unlisted posting is skipped.
        XCTAssertEqual(jobs.count, 2)

        let ios = jobs[0]
        XCTAssertEqual(ios.externalId, "ashby-acme-f47ac10b-58cc-4372-a567-0e02b2c3d479")
        XCTAssertEqual(ios.company, "Acme")
        XCTAssertEqual(ios.location, "San Francisco")
        XCTAssertTrue(ios.isRemote)
        XCTAssertEqual(ios.salaryMin, 140_000)
        XCTAssertEqual(ios.salaryMax, 180_000)
        XCTAssertEqual(ios.salaryPeriod, "annual")
        XCTAssertEqual(ios.tags, ["Engineering", "Mobile"])
        XCTAssertEqual(ios.applyType, "external")
        XCTAssertEqual(ios.description, "Ship the app used by millions.")

        let support = jobs[1]
        XCTAssertEqual(support.salaryMin, 30)
        XCTAssertEqual(support.salaryMax, 40)
        XCTAssertEqual(support.salaryPeriod, "hourly")
        XCTAssertEqual(support.location, "")
        XCTAssertEqual(support.tags, ["Support"])
    }

    func testRecruiteeParsing() throws {
        let jobs = try RecruiteeSource.parse(
            data: try Fixtures.data("recruitee", "json"),
            company: "acme", keywords: ["engineer", "payroll"], excludePatterns: noExcludes)
        // The draft offer is skipped.
        XCTAssertEqual(jobs.count, 2)

        let ios = jobs[0]
        XCTAssertEqual(ios.externalId, "recruitee-acme-501")
        XCTAssertEqual(ios.company, "Acme BV")
        XCTAssertEqual(ios.location, "Amsterdam, Netherlands")
        XCTAssertEqual(ios.datePosted, "2026-07-01T10:00:00+00:00")
        XCTAssertEqual(ios.salaryMin, 60_000)
        XCTAssertEqual(ios.salaryMax, 80_000)
        XCTAssertEqual(ios.salaryPeriod, "annual")
        XCTAssertEqual(ios.tags, ["Mobile", "swift", "ios"])
        XCTAssertTrue(ios.isRemote)

        // Monthly salary is dropped rather than mislabeled; empty location
        // falls back to city + country.
        let payroll = jobs[1]
        XCTAssertNil(payroll.salaryMin)
        XCTAssertEqual(payroll.salaryPeriod, "unknown")
        XCTAssertEqual(payroll.location, "Rotterdam, Netherlands")
    }

    func testRemoteOKParsing() throws {
        let jobs = try RemoteOKSource.parse(
            body: try Fixtures.data("remoteok", "json"),
            keywords: ["ios", "marketing"], excludePatterns: noExcludes)
        // Metadata element [0] is skipped.
        XCTAssertEqual(jobs.count, 2)

        let ios = jobs[0]
        XCTAssertEqual(ios.externalId, "1093221")
        XCTAssertEqual(ios.title, "iOS Engineer")
        // Direct apply link preferred over the RemoteOK page.
        XCTAssertEqual(ios.url, "https://acme.com/careers/ios")
        XCTAssertEqual(ios.location, "Worldwide")
        XCTAssertEqual(ios.salaryMin, 90_000)
        XCTAssertEqual(ios.salaryPeriod, "annual")
        XCTAssertTrue(ios.isRemote)
        XCTAssertEqual(ios.tags, ["swift", "ios", "mobile"])

        // Empty apply_url falls back to the listing URL.
        XCTAssertEqual(jobs[1].url, "https://remoteok.com/remote-jobs/1093222")
    }

    func testRemoteOKHTMLBodyThrowsBlocked() {
        let html = Data("<!doctype html>\n<HTML><body>Access denied</body></HTML>".utf8)
        XCTAssertThrowsError(try RemoteOKSource.parse(body: html, keywords: ["ios"],
                                                      excludePatterns: [])) {
            XCTAssertTrue($0 is SourceBlockedError)
        }
    }

    func testArbeitnowParsing() throws {
        var seen = Set<String>()
        let page = try ArbeitnowSource.parsePage(
            data: try Fixtures.data("arbeitnow", "json"),
            keywords: ["ios"], excludePatterns: noExcludes, seenSlugs: &seen)
        XCTAssertEqual(page.rawCount, 2)
        XCTAssertFalse(page.hasNext)
        XCTAssertEqual(page.jobs.count, 1)
        let job = page.jobs[0]
        XCTAssertEqual(job.externalId, "ios-developer-acme-gmbh-berlin-321")
        XCTAssertEqual(job.company, "Acme GmbH")
        XCTAssertEqual(job.location, "Berlin")
        XCTAssertEqual(job.tags, ["ios", "swift"])
        // Unix epoch is carried as a string and parses downstream.
        XCTAssertEqual(job.datePosted, "1751500800")
        XCTAssertNotNil(JobFilters.parsePostedDate(job.datePosted))
        XCTAssertTrue(job.isRemote)
    }

    func testAdzunaParsing() throws {
        var seen = Set<String>()
        let page = try AdzunaSource.parsePage(
            data: try Fixtures.data("adzuna", "json"),
            excludePatterns: JobFilters.compileExcludes(["TS/SCI"]), seenIDs: &seen)
        XCTAssertEqual(page.rawCount, 2)
        XCTAssertEqual(page.jobs.count, 1)
        let job = page.jobs[0]
        XCTAssertEqual(job.externalId, "4987654321")
        XCTAssertEqual(job.company, "Acme Defense")
        XCTAssertEqual(job.location, "Denver, CO")
        XCTAssertEqual(job.salaryMin, 85_000)
        XCTAssertEqual(job.salaryMax, 105_000)
        XCTAssertEqual(job.salaryPeriod, "annual")
        XCTAssertEqual(job.tags, ["it-jobs"])
        XCTAssertEqual(job.datePosted, "2026-07-01T14:22:00Z")
        XCTAssertFalse(job.isRemote)
    }

    func testUSAJobsParsing() throws {
        var seen = Set<String>()
        let jobs = try USAJobsSource.parse(
            data: try Fixtures.data("usajobs", "json"),
            excludePatterns: noExcludes, seenIDs: &seen)
        XCTAssertEqual(jobs.count, 2)

        let infosec = jobs[0]
        XCTAssertEqual(infosec.externalId, "AF-25-12345678")
        XCTAssertEqual(infosec.company, "Department of the Air Force (Department of Defense)")
        XCTAssertEqual(infosec.location, "Colorado Springs, Colorado, Peterson AFB, Colorado")
        XCTAssertEqual(infosec.salaryMin, 88_000)
        XCTAssertEqual(infosec.salaryMax, 115_000)
        XCTAssertEqual(infosec.description,
                       "Secure and harden networks. Monitor systems for intrusions.")
        XCTAssertEqual(infosec.tags, ["government", "federal"])
        XCTAssertFalse(infosec.isRemote)

        // org == dept collapses; zero salary is dropped; empty MajorDuties
        // falls back to QualificationSummary; "remote" location detected.
        let cisa = jobs[1]
        XCTAssertEqual(cisa.company, "Cybersecurity and Infrastructure Security Agency")
        XCTAssertNil(cisa.salaryMin)
        XCTAssertNil(cisa.salaryMax)
        XCTAssertEqual(cisa.description, "Defend federal networks.")
        XCTAssertTrue(cisa.isRemote)
    }

    func testWeWorkRemotelyFeedParsing() throws {
        let items = WeWorkRemotelySource.parseFeed(try Fixtures.data("wwr.rss", "xml"))
        XCTAssertEqual(items.count, 3)
        XCTAssertEqual(items[0].title, "Acme Corp: Senior Rails Developer")
        XCTAssertEqual(items[0].link,
                       "https://weworkremotely.com/remote-jobs/acme-corp-senior-rails-developer")
        XCTAssertEqual(items[0].pubDate, "Wed, 01 Jul 2026 10:00:00 +0000")
        XCTAssertTrue(items[0].description.contains("Rails monolith"))

        var seenLinks = Set<String>()
        let jobs = WeWorkRemotelySource.buildJobs(items: items, keywords: ["rails"],
                                                  excludePatterns: [], seenLinks: &seenLinks)
        XCTAssertEqual(jobs.count, 2)
        XCTAssertEqual(jobs[0].title, "Senior Rails Developer")
        XCTAssertEqual(jobs[0].company, "Acme Corp")
        XCTAssertEqual(jobs[0].location, "Remote")
        XCTAssertTrue(jobs[0].isRemote)
        XCTAssertNotNil(JobFilters.parsePostedDate(jobs[0].datePosted))
        // No-colon titles keep the full title and an empty company.
        XCTAssertEqual(jobs[1].title, "Untitled Posting Without Company")
        XCTAssertEqual(jobs[1].company, "")

        // The superset feed re-serves the same links — dedup swallows them.
        let again = WeWorkRemotelySource.buildJobs(items: items, keywords: ["rails"],
                                                   excludePatterns: [], seenLinks: &seenLinks)
        XCTAssertTrue(again.isEmpty)
    }
}

// MARK: - Filters

final class JobFiltersTests: XCTestCase {
    private func job(title: String = "Engineer", company: String = "Acme",
                     location: String = "", source: String = "test",
                     salaryMin: Int? = nil, salaryMax: Int? = nil,
                     salaryPeriod: String? = nil, datePosted: String = "",
                     isRemote: Bool = false) -> NormalizedJob {
        NormalizedJob(source: source, externalId: "x", title: title, company: company,
                      location: location, salaryMin: salaryMin, salaryMax: salaryMax,
                      salaryPeriod: salaryPeriod, datePosted: datePosted, isRemote: isRemote)
    }

    func testExcludeWordBoundary() {
        let patterns = JobFilters.compileExcludes(["SC", "TS/SCI"])
        // "SC" must not match inside words like Cisco or Scientist.
        XCTAssertFalse(JobFilters.matchesExclude("Cisco Network Engineer", patterns))
        XCTAssertFalse(JobFilters.matchesExclude("Data Scientist", patterns))
        XCTAssertTrue(JobFilters.matchesExclude("SC cleared analyst", patterns))
        XCTAssertTrue(JobFilters.matchesExclude("Analyst (SC)", patterns))
        // Keywords with non-word chars still anchor correctly.
        XCTAssertTrue(JobFilters.matchesExclude("Requires TS/SCI clearance", patterns))
        XCTAssertFalse(JobFilters.matchesExclude("BATS/SCIENCE department", patterns))
    }

    func testExcludeAppliesToTitleAndCompany() {
        var search = SearchConfig()
        search.locations = []
        search.excludeKeywords = ["staffing"]
        XCTAssertFalse(JobFilters.passesGlobalFilters(
            job(title: "Engineer", company: "ABC Staffing"), search: search))
        XCTAssertFalse(JobFilters.passesGlobalFilters(
            job(title: "Staffing Coordinator", company: "Acme"), search: search))
        XCTAssertTrue(JobFilters.passesGlobalFilters(
            job(title: "Engineer", company: "Acme"), search: search))
    }

    func testMaxAgeWithGraceDay() {
        var search = SearchConfig()
        search.locations = []
        search.maxAgeDays = 7
        let now = Date()
        let iso = ISO8601DateFormatter()

        let recent = job(datePosted: iso.string(from: now.addingTimeInterval(-7.5 * 86400)))
        XCTAssertTrue(JobFilters.passesGlobalFilters(recent, search: search, now: now),
                      "7.5 days is inside the +1 day grace window")

        let stale = job(datePosted: iso.string(from: now.addingTimeInterval(-9 * 86400)))
        XCTAssertFalse(JobFilters.passesGlobalFilters(stale, search: search, now: now))

        let relative = job(datePosted: "Posted 3 days ago")
        XCTAssertTrue(JobFilters.passesGlobalFilters(relative, search: search, now: now),
                      "unparseable dates pass through unchecked")
    }

    func testMinSalaryHourlyConversion() {
        var search = SearchConfig()
        search.locations = []
        search.minSalary = 80_000

        // Bare 45 infers hourly: 45 * 2080 = 93,600 — passes.
        XCTAssertTrue(JobFilters.passesGlobalFilters(job(salaryMin: 45), search: search))
        // 30/hr = 62,400 — fails.
        XCTAssertFalse(JobFilters.passesGlobalFilters(job(salaryMax: 30), search: search))
        // Explicit hourly period.
        XCTAssertTrue(JobFilters.passesGlobalFilters(
            job(salaryMax: 50, salaryPeriod: "hourly"), search: search))
        // Stated annual below the floor fails.
        XCTAssertFalse(JobFilters.passesGlobalFilters(job(salaryMax: 70_000), search: search))
        // Upper bound is what counts.
        XCTAssertTrue(JobFilters.passesGlobalFilters(
            job(salaryMin: 70_000, salaryMax: 90_000), search: search))
    }

    func testNoSalaryPasses() {
        var search = SearchConfig()
        search.locations = []
        search.minSalary = 200_000
        XCTAssertTrue(JobFilters.passesGlobalFilters(job(), search: search))
        XCTAssertTrue(JobFilters.passesGlobalFilters(job(salaryMin: 0, salaryMax: 0), search: search))
    }

    func testLocationFilter() {
        var search = SearchConfig()
        search.locations = ["Remote", "Denver"]

        XCTAssertTrue(JobFilters.passesGlobalFilters(job(location: "Denver, CO"), search: search))
        XCTAssertFalse(JobFilters.passesGlobalFilters(job(location: "New York, NY"), search: search))
        XCTAssertTrue(JobFilters.passesGlobalFilters(job(isRemote: true), search: search))
        XCTAssertTrue(JobFilters.passesGlobalFilters(
            job(location: "Remote - US"), search: search))
        // RemoteOK/WWR jobs always pass the location filter.
        XCTAssertTrue(JobFilters.passesGlobalFilters(
            job(location: "Worldwide", source: "remoteok"), search: search))

        // No locations configured: everything passes.
        search.locations = []
        XCTAssertTrue(JobFilters.passesGlobalFilters(job(location: "New York, NY"), search: search))
    }

    func testParsePostedDateVariants() {
        let epoch = JobFilters.parsePostedDate("1751500800")
        XCTAssertEqual(epoch?.timeIntervalSince1970, 1_751_500_800)
        let epochMs = JobFilters.parsePostedDate("1751500800000")
        XCTAssertEqual(epochMs?.timeIntervalSince1970, 1_751_500_800)

        XCTAssertNotNil(JobFilters.parsePostedDate("2026-07-01T10:00:00Z"))
        XCTAssertNotNil(JobFilters.parsePostedDate("2026-07-01T10:00:00+02:00"))
        XCTAssertNotNil(JobFilters.parsePostedDate("2026-07-01T10:00:00.123Z"))
        // Naive ISO and date-only are treated as UTC.
        XCTAssertNotNil(JobFilters.parsePostedDate("2026-07-01T10:00:00"))
        XCTAssertNotNil(JobFilters.parsePostedDate("2026-07-01"))

        let rfc822 = JobFilters.parsePostedDate("Wed, 01 Jul 2026 10:00:00 +0000")
        XCTAssertNotNil(rfc822)
        if let rfc822 {
            var cal = Calendar(identifier: .gregorian)
            cal.timeZone = TimeZone(identifier: "UTC")!
            let parts = cal.dateComponents([.year, .month, .day, .hour], from: rfc822)
            XCTAssertEqual([parts.year, parts.month, parts.day, parts.hour], [2026, 7, 1, 10])
        }

        XCTAssertNil(JobFilters.parsePostedDate("Posted 3 days ago"))
        XCTAssertNil(JobFilters.parsePostedDate(""))
    }

    func testCleanDescription() {
        XCTAssertEqual(JobFilters.cleanDescription("Line1<br/>Line2 &amp;amp; more"),
                       "Line1\nLine2 & more")
        XCTAssertEqual(JobFilters.cleanDescription("&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;"),
                       "Hello world")
        XCTAssertEqual(JobFilters.cleanDescription(nil), "")
    }

    /// Block tags become line breaks and <li> items become bullets, so a
    /// paragraph + list no longer collapses into one run-on line.
    func testCleanDescriptionPreservesStructure() {
        let html = "<p>About the role</p><ul><li>Ship features</li><li>Mentor peers</li></ul>"
        XCTAssertEqual(JobFilters.cleanDescription(html),
                       "About the role\n• Ship features\n• Mentor peers")
    }

    func testCleanDescriptionInlineTagsDoNotBreakLines() {
        XCTAssertEqual(
            JobFilters.cleanDescription("We use <strong>Swift</strong> and <em>GRDB</em> daily."),
            "We use Swift and GRDB daily.")
    }

    func testCleanDescriptionDropsEmptyBulletsAndBlankRuns() {
        XCTAssertEqual(
            JobFilters.cleanDescription("<p>One</p><ul><li></li></ul><br><br><br><p>Two</p>"),
            "One\n\nTwo")
    }
}

// MARK: - Dedup

final class DeduplicatorTests: XCTestCase {
    private func job(source: String = "a", id: String = "1", title: String,
                     company: String, location: String = "", url: String) -> NormalizedJob {
        NormalizedJob(source: source, externalId: id, title: title, company: company,
                      location: location, url: url)
    }

    func testURLDedupFirstWins() {
        let first = job(title: "Engineer", company: "Acme", url: "https://x.com/1")
        let second = job(source: "b", title: "Totally Different", company: "Other",
                         url: "https://x.com/1")
        let result = Deduplicator.dedupe([first, second])
        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result[0].title, "Engineer")
    }

    func testEmptyURLsNeverDedupedByURL() {
        // Python parity: `if url and url in seen` — jobs without a URL skip
        // the URL pass entirely; distinct jobs both survive.
        let first = job(title: "Engineer", company: "Acme", url: "")
        let second = job(title: "Designer", company: "Other", url: "")
        let result = Deduplicator.dedupe([first, second])
        XCTAssertEqual(result.count, 2)

        // Identical identity still merges via the key pass.
        let dupA = job(title: "Engineer", company: "Acme", location: "Remote", url: "")
        let dupB = job(title: "Engineer", company: "Acme", location: "Remote", url: "")
        XCTAssertEqual(Deduplicator.dedupe([dupA, dupB]).count, 1)
    }

    func testIdentityDedupAcrossSources() {
        let adzuna = job(source: "adzuna", title: "Senior Engineer", company: "Acme, Inc.",
                         location: "Denver CO", url: "https://adzuna.com/1")
        let linkedin = job(source: "linkedin", title: "senior engineer", company: "Acme Inc",
                           location: "denver-co", url: "https://linkedin.com/2")
        let result = Deduplicator.dedupe([adzuna, linkedin])
        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result[0].source, "adzuna")
    }

    func testMissingCompanyNeverMerges() {
        let first = job(title: "Engineer", company: "", url: "https://x.com/1")
        let second = job(title: "Engineer", company: "", url: "https://y.com/2")
        XCTAssertEqual(Deduplicator.dedupe([first, second]).count, 2)
    }

    func testDistinctLocationsSurvive() {
        let denver = job(title: "Engineer", company: "Acme", location: "Denver",
                         url: "https://x.com/1")
        let nyc = job(title: "Engineer", company: "Acme", location: "New York",
                      url: "https://x.com/2")
        XCTAssertEqual(Deduplicator.dedupe([denver, nyc]).count, 2)
    }
}

// MARK: - Generic parser

final class GenericJobParserTests: XCTestCase {
    func testJSONLDSimple() throws {
        let url = URL(string: "https://acme.com/careers/staff-swe-123")!
        let job = GenericJobParser.parse(html: try Fixtures.string("jsonld_simple", "html"), url: url)
        let parsed = try XCTUnwrap(job)
        XCTAssertEqual(parsed.source, "manual")
        // JSON-LD beats the og:title fallback.
        XCTAssertEqual(parsed.title, "Staff Software Engineer")
        XCTAssertEqual(parsed.company, "Acme Corp")
        XCTAssertEqual(parsed.location, "Denver, CO")
        XCTAssertEqual(parsed.salaryMin, 170_000)
        XCTAssertEqual(parsed.salaryMax, 210_000)
        XCTAssertEqual(parsed.salaryPeriod, "annual")
        XCTAssertEqual(parsed.tags, ["FULL_TIME"])
        XCTAssertEqual(parsed.datePosted, "2026-06-28")
        XCTAssertTrue(parsed.description.contains("distributed systems"))
        XCTAssertFalse(parsed.isRemote)

        // Deterministic manual id: "manual:" + first 16 hex chars of SHA1(url).
        let digest = Insecure.SHA1.hash(data: Data(url.absoluteString.utf8))
        let hex = digest.map { String(format: "%02x", $0) }.joined().prefix(16)
        XCTAssertEqual(parsed.externalId, "manual:" + hex)
    }

    func testJSONLDGraphNesting() throws {
        let url = URL(string: "https://beta.example.com/jobs/platform")!
        let job = GenericJobParser.parse(html: try Fixtures.string("jsonld_graph", "html"), url: url)
        let parsed = try XCTUnwrap(job)
        XCTAssertEqual(parsed.title, "Platform Engineer")
        // hiringOrganization as a bare string.
        XCTAssertEqual(parsed.company, "Beta Inc")
        XCTAssertTrue(parsed.isRemote, "jobLocationType TELECOMMUTE sets remote")
        // minValue 60 with no maxValue; < 1000 infers hourly.
        XCTAssertEqual(parsed.salaryMin, 60)
        XCTAssertNil(parsed.salaryMax)
        XCTAssertEqual(parsed.salaryPeriod, "hourly")
        XCTAssertEqual(parsed.datePosted, "2026-07-03T09:00:00Z")
    }

    func testOGFallback() throws {
        let url = URL(string: "https://gamma.example.com/jobs/data-eng")!
        let job = GenericJobParser.parse(html: try Fixtures.string("og_fallback", "html"), url: url)
        let parsed = try XCTUnwrap(job)
        XCTAssertEqual(parsed.title, "Data Engineer - Gamma Analytics")
        XCTAssertEqual(parsed.company, "Gamma Analytics")
        XCTAssertEqual(parsed.description, "Own our data pipelines end to end.")
    }

    func testNoTitleReturnsNil() {
        let job = GenericJobParser.parse(html: "<div><p>nothing here</p></div>",
                                         url: URL(string: "https://x.com/y")!)
        XCTAssertNil(job)
    }
}

// MARK: - Apply-type detection

final class ApplyTypeDetectorTests: XCTestCase {
    func testGreenhouseDetector() {
        XCTAssertEqual(ApplyTypeDetector.detect(
            source: "greenhouse", url: "https://boards.greenhouse.io/acme/jobs/1"), "easy_apply")
        XCTAssertEqual(ApplyTypeDetector.detect(
            source: "greenhouse", url: "https://job-boards.greenhouse.io/acme/jobs/1"), "easy_apply")
        XCTAssertEqual(ApplyTypeDetector.detect(
            source: "greenhouse", url: "https://careers.acme.com/jobs/1"), "external")
        XCTAssertEqual(ApplyTypeDetector.detect(source: "greenhouse", url: ""), "unknown")
        XCTAssertEqual(ApplyTypeDetector.detect(source: "greenhouse", url: nil), "unknown")
    }

    func testLeverDetector() {
        XCTAssertEqual(ApplyTypeDetector.detect(
            source: "lever", url: "https://jobs.lever.co/acme/abc"), "easy_apply")
        // Custom-domain Lever boards can't be identified from the URL alone.
        XCTAssertEqual(ApplyTypeDetector.detect(
            source: "lever", url: "https://jobs.acme.com/abc"), "external")
    }

    func testUSAJobsDetector() {
        XCTAssertEqual(ApplyTypeDetector.detect(
            source: "usajobs", url: "https://www.usajobs.gov/job/812345600"), "easy_apply")
        XCTAssertEqual(ApplyTypeDetector.detect(
            source: "usajobs", url: "https://apply.af.mil/job/1"), "external")
    }

    func testUnknownSourceStaysUnknown() {
        XCTAssertEqual(ApplyTypeDetector.detect(
            source: "remoteok", url: "https://remoteok.com/remote-jobs/1"), "unknown")
    }
}

// MARK: - Manual URL routing

final class ManualURLFetcherTests: XCTestCase {
    func testGreenhouseSlugAndIDExtraction() {
        var pair = ManualURLFetcher.greenhouseSlugAndID(
            from: "https://boards.greenhouse.io/acme/jobs/4012345?gh_src=abc")
        XCTAssertEqual(pair?.slug, "acme")
        XCTAssertEqual(pair?.id, "4012345")

        pair = ManualURLFetcher.greenhouseSlugAndID(
            from: "https://job-boards.greenhouse.io/other-co/jobs/99")
        XCTAssertEqual(pair?.slug, "other-co")
        XCTAssertEqual(pair?.id, "99")

        XCTAssertNil(ManualURLFetcher.greenhouseSlugAndID(
            from: "https://boards.greenhouse.io/acme"))
    }

    func testRejectsNonHTTPURLs() async {
        do {
            _ = try await ManualURLFetcher().fetchJob(from: "ftp://example.com/job")
            XCTFail("expected invalidURL")
        } catch let error as ManualURLFetcher.FetchError {
            XCTAssertEqual(error, .invalidURL)
        } catch {
            XCTFail("unexpected error \(error)")
        }
    }
}

// MARK: - Pipeline

final class FetchPipelineTests: XCTestCase {
    /// Greenhouse with no boards configured returns [] without touching the
    /// network, which makes it a convenient zero-job source for stats tests.
    func testSuspectAfterZeroStreak() async throws {
        let db = try AppDatabase.inMemory()
        try await db.writer.write {
            try $0.execute(sql: """
                INSERT INTO source_stats (source, lastCount, consecutiveZero, everReturned, lastRun)
                VALUES ('greenhouse', 4, 2, 1, '2026-07-01T00:00:00Z')
                """)
        }
        let summary = await FetchPipeline().run(config: AppConfig(),
                                                sources: ["greenhouse"],
                                                jobStore: JobStore(db))
        XCTAssertEqual(summary.perSource["greenhouse"], 0)
        XCTAssertEqual(summary.suspect, ["greenhouse"])
        XCTAssertTrue(summary.failed.isEmpty)
        XCTAssertTrue(summary.timedOut.isEmpty)
        XCTAssertEqual(summary.inserted, 0)
    }

    func testNeverReturnedSourceIsNotSuspect() async throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        let pipeline = FetchPipeline()
        // An unconfigured source is not a broken one, however long it zeroes.
        for _ in 0..<4 {
            let summary = await pipeline.run(config: AppConfig(),
                                             sources: ["greenhouse"], jobStore: store)
            XCTAssertTrue(summary.suspect.isEmpty)
        }
    }

    func testProgressStreamEmitsAndFinishes() async throws {
        let db = try AppDatabase.inMemory()
        let pipeline = FetchPipeline()
        let stream = await pipeline.progressUpdates()
        let collector = Task { () -> [FetchProgress] in
            var events: [FetchProgress] = []
            for await event in stream { events.append(event) }
            return events
        }
        _ = await pipeline.run(config: AppConfig(), sources: ["greenhouse"],
                               jobStore: JobStore(db))
        let events = await collector.value
        XCTAssertGreaterThanOrEqual(events.count, 2)
        XCTAssertEqual(events.first?.sourcesTotal, 1)
        XCTAssertEqual(events.last?.sourcesDone, 1)
    }

    func testUnknownSourceIDsAreIgnored() async throws {
        let db = try AppDatabase.inMemory()
        let summary = await FetchPipeline().run(config: AppConfig(),
                                                sources: ["indeed"],
                                                jobStore: JobStore(db))
        XCTAssertTrue(summary.perSource.isEmpty)
    }

    func testRegistryCoversPortedSources() {
        XCTAssertEqual(SourceRegistry.allIDs,
                       ["remoteok", "weworkremotely", "adzuna", "greenhouse",
                        "linkedin", "arbeitnow", "usajobs", "ashby", "workable",
                        "recruitee"])
        for id in SourceRegistry.allIDs {
            XCTAssertNotNil(SourceRegistry.source(for: id))
        }
        XCTAssertNil(SourceRegistry.source(for: "indeed"))
    }
}
