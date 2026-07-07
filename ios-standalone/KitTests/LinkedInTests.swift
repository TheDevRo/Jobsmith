import XCTest
@testable import JobsmithKit

// MARK: - Search-card parsing

final class LinkedInSearchParsingTests: XCTestCase {
    func testParseSearchPage() throws {
        let html = try Fixtures.string("linkedin_search", "html")
        let cards = try XCTUnwrap(LinkedInGuestAPI.parseSearchPage(html: html))

        // 4 <li> elements, but the spacer has no title/link and is dropped.
        XCTAssertEqual(cards.count, 3)

        let first = cards[0]
        XCTAssertEqual(first.title, "Senior Security Engineer")
        XCTAssertEqual(first.company, "Acme Corp")
        XCTAssertEqual(first.location, "Denver, CO")
        // Query string stripped, id pulled from /view/...-(\d+).
        XCTAssertEqual(first.url,
                       "https://www.linkedin.com/jobs/view/senior-security-engineer-at-acme-corp-3901234567")
        XCTAssertEqual(first.externalID, "3901234567")
        XCTAssertEqual(first.datePosted, "2026-07-01")
        XCTAssertFalse(first.isEasyApply)

        let second = cards[1]
        XCTAssertEqual(second.externalID, "3901234568")
        XCTAssertEqual(second.location, "United States (Remote)")
        XCTAssertTrue(second.isEasyApply, "benefits footer says Easy Apply")

        let third = cards[2]
        XCTAssertEqual(third.externalID, "3901234569")
        XCTAssertEqual(third.datePosted, "", "card without <time> has empty datePosted")
    }

    func testParseSearchPageWithNoListItemsReturnsNil() {
        XCTAssertNil(LinkedInGuestAPI.parseSearchPage(html: "<div>auth wall</div>"))
    }
}

// MARK: - Query building

final class LinkedInQueryTests: XCTestCase {
    func testKeywordBatchingQuotesPhrasesAndBatchesOfFour() {
        let batches = LinkedInGuestAPI.batchKeywords([
            "security engineer", "soc analyst", "devops", "sre", "platform engineer",
        ])
        XCTAssertEqual(batches, [
            "\"security engineer\" OR \"soc analyst\" OR devops OR sre",
            "platform engineer",
        ])
    }

    func testKeywordBatchingSingleBareKeywordStaysUnquoted() {
        // A batch of one keeps the bare keyword to preserve fuzzy matching.
        XCTAssertEqual(LinkedInGuestAPI.batchKeywords(["security engineer"]),
                       ["security engineer"])
    }

    func testKeywordBatchingSkipsBlanks() {
        XCTAssertEqual(LinkedInGuestAPI.batchKeywords(["  ", "a", "", "b"]),
                       ["a OR b"])
    }

    func testTimeFilterMapping() {
        XCTAssertEqual(LinkedInGuestAPI.timeFilter(maxAgeDays: 1), "r86400")
        XCTAssertEqual(LinkedInGuestAPI.timeFilter(maxAgeDays: 3), "r604800")
        XCTAssertEqual(LinkedInGuestAPI.timeFilter(maxAgeDays: 7), "r604800")
        XCTAssertEqual(LinkedInGuestAPI.timeFilter(maxAgeDays: 30), "r2592000")
        XCTAssertEqual(LinkedInGuestAPI.timeFilter(maxAgeDays: 31), "")
    }

    func testSearchURLPrefersGeoIDAndAddsDistanceForCities() throws {
        let url = try XCTUnwrap(LinkedInGuestAPI.searchURL(
            query: "devops", geoID: "103736294", location: "Denver, CO",
            isRemoteLocation: false, timeFilter: "r604800", start: 25))
        let q = url.query ?? ""
        XCTAssertTrue(q.contains("geoId=103736294"))
        XCTAssertFalse(q.contains("location="), "geoId-only when we have one")
        XCTAssertTrue(q.contains("f_TPR=r604800"))
        XCTAssertTrue(q.contains("distance=25"))
        XCTAssertTrue(q.contains("start=25"))
        XCTAssertFalse(q.contains("f_WT="))
    }

    func testSearchURLRemoteAddsWorkTypeAndSkipsDistance() throws {
        let url = try XCTUnwrap(LinkedInGuestAPI.searchURL(
            query: "devops", geoID: "92000001", location: "Remote",
            isRemoteLocation: true, timeFilter: "", start: 0))
        let q = url.query ?? ""
        XCTAssertTrue(q.contains("f_WT=2"))
        XCTAssertFalse(q.contains("distance="))
        XCTAssertFalse(q.contains("f_TPR="))
    }

    func testSearchURLStateGeoIDSkipsDistance() throws {
        let url = try XCTUnwrap(LinkedInGuestAPI.searchURL(
            query: "devops", geoID: "105763813", location: "Colorado",
            isRemoteLocation: false, timeFilter: "", start: 0))
        XCTAssertFalse(url.query?.contains("distance=") ?? true)
    }

    func testSearchURLFallsBackToLocationText() throws {
        let url = try XCTUnwrap(LinkedInGuestAPI.searchURL(
            query: "devops", geoID: "", location: "Springfield, IL",
            isRemoteLocation: false, timeFilter: "", start: 0))
        let q = url.query ?? ""
        XCTAssertTrue(q.contains("location=Springfield"))
        XCTAssertFalse(q.contains("geoId="))
        XCTAssertFalse(q.contains("distance="), "no distance hint without a geoId")
    }
}

// MARK: - Detail page parsing

final class LinkedInDetailParserTests: XCTestCase {
    func testJSONLDPrimaryExtraction() throws {
        let html = try Fixtures.string("linkedin_detail", "html")
        let detail = LinkedInDetailParser.parse(html: html)

        let desc = try XCTUnwrap(detail.description)
        XCTAssertTrue(desc.contains("Defend the Acme platform against threats."))
        XCTAssertTrue(desc.contains("detection engineering"))
        XCTAssertFalse(desc.contains("<p>"), "HTML stripped")

        XCTAssertEqual(detail.salaryMin, 120000)
        XCTAssertEqual(detail.salaryMax, 150000)
        XCTAssertEqual(detail.salaryPeriod, "annual")
        XCTAssertEqual(detail.location, "Denver, CO")
        XCTAssertEqual(detail.tags, ["FULL_TIME"])
        XCTAssertTrue(detail.isEasyApply, "JSON-LD directApply=true")
    }

    func testHTMLFallbackExtraction() throws {
        let html = try Fixtures.string("linkedin_detail_fallback", "html")
        let detail = LinkedInDetailParser.parse(html: html)

        XCTAssertEqual(detail.description,
                       "Provide tier-one support for internal users and triage tickets.")
        // Hourly values preserved raw.
        XCTAssertEqual(detail.salaryMin, 25)
        XCTAssertEqual(detail.salaryMax, 30)
        XCTAssertEqual(detail.salaryPeriod, "hourly")
        XCTAssertNil(detail.location, "no JSON-LD address to backfill from")
        XCTAssertEqual(detail.tags, ["Entry level", "Full-time"], "criteria tags, 'Other' dropped")
        XCTAssertTrue(detail.isEasyApply, "short Easy Apply badge element")
    }

    func testEasyApplyNotDetectedOnPlainExternalPosting() {
        let html = """
        <html><body>
          <div class="show-more-less-html__markup"><p>Apply on our careers site.</p></div>
          <button class="sign-up-modal__outlet">Apply</button>
        </body></html>
        """
        let detail = LinkedInDetailParser.parse(html: html)
        XCTAssertFalse(detail.isEasyApply)
    }

    func testSalaryFromLDHourlyKeepsLowValues() {
        let ld: [String: Any] = ["baseSalary": ["value": [
            "minValue": 22, "maxValue": 28, "unitText": "HOUR",
        ]]]
        let (lo, hi, period) = LinkedInDetailParser.extractSalaryFromLD(ld)
        XCTAssertEqual(lo, 22)
        XCTAssertEqual(hi, 28)
        XCTAssertEqual(period, "hourly")
    }

    func testSalaryFromLDAnnualDropsHourlyShapedLeaks() {
        let ld: [String: Any] = ["baseSalary": ["value": [
            "minValue": 40, "maxValue": 90000, "unitText": "YEAR",
        ]]]
        let (lo, hi, period) = LinkedInDetailParser.extractSalaryFromLD(ld)
        XCTAssertNil(lo, "annual value below 15000 dropped")
        XCTAssertEqual(hi, 90000)
        XCTAssertEqual(period, "annual")
    }

    func testSalaryFromLDUnknownUnitDropsHourlyShapedValues() {
        // Python parity: without unitText the <15000 filter runs first, so
        // hourly-shaped values are dropped and no period is inferred.
        let ld: [String: Any] = ["baseSalary": ["value": [
            "minValue": 25, "maxValue": 30,
        ]]]
        let (lo, hi, period) = LinkedInDetailParser.extractSalaryFromLD(ld)
        XCTAssertNil(lo)
        XCTAssertNil(hi)
        XCTAssertEqual(period, "unknown")
    }

    func testParseSalaryTextKSuffixPerAmount() {
        // "401k" elsewhere must not turn amounts into thousands.
        let (lo, hi, period) = LinkedInDetailParser.parseSalaryText(
            "$80k - $120k per year plus 401k match")
        XCTAssertEqual(lo, 80000)
        XCTAssertEqual(hi, 120000)
        XCTAssertEqual(period, "annual")
    }

    func testParseSalaryTextSingleAnnualValue() {
        let (lo, hi, period) = LinkedInDetailParser.parseSalaryText("$95,000/yr")
        XCTAssertEqual(lo, 95000)
        XCTAssertNil(hi)
        XCTAssertEqual(period, "annual")
    }
}

// MARK: - Profile-link import

final class LinkedInProfileFetcherTests: XCTestCase {
    func testNormalizeProfileURLVariants() {
        let expected = URL(string: "https://www.linkedin.com/in/janedoe")
        XCTAssertEqual(LinkedInProfileFetcher.normalizeProfileURL(
            "https://www.linkedin.com/in/janedoe"), expected)
        XCTAssertEqual(LinkedInProfileFetcher.normalizeProfileURL(
            "linkedin.com/in/janedoe/"), expected)
        XCTAssertEqual(LinkedInProfileFetcher.normalizeProfileURL(
            "https://linkedin.com/in/janedoe?utm_source=share#section"), expected)
        XCTAssertEqual(LinkedInProfileFetcher.normalizeProfileURL(
            "in/janedoe"), expected)
        XCTAssertEqual(LinkedInProfileFetcher.normalizeProfileURL(
            "janedoe"), expected)
        XCTAssertEqual(LinkedInProfileFetcher.normalizeProfileURL(
            "  linkedin.com/in/janedoe  "), expected)
    }

    func testNormalizeProfileURLRejectsNonProfiles() {
        XCTAssertNil(LinkedInProfileFetcher.normalizeProfileURL(""))
        XCTAssertNil(LinkedInProfileFetcher.normalizeProfileURL("https://example.com/in/x"))
        XCTAssertNil(LinkedInProfileFetcher.normalizeProfileURL("linkedin.com/jobs/view/123"))
        XCTAssertNil(LinkedInProfileFetcher.normalizeProfileURL("Jane Doe"))
        XCTAssertNil(LinkedInProfileFetcher.normalizeProfileURL("linkedin.com/in/"))
    }

    func testExtractProfileTextIncludesJSONLDAndMain() throws {
        let html = try Fixtures.string("linkedin_profile", "html")
        let text = LinkedInProfileFetcher.extractProfileText(html: html)

        // JSON-LD Person comes through raw for the LLM.
        XCTAssertTrue(text.contains("\"@type\":\"Person\""))
        XCTAssertTrue(text.contains("Jane Doe"))
        // Visible sections included, chrome excluded.
        XCTAssertTrue(text.contains("Built detection pipelines"))
        XCTAssertTrue(text.contains("State University"))
        XCTAssertFalse(text.contains("User Agreement"), "footer chrome skipped via <main>")
    }

    func testExtractProfileTextCaps() throws {
        let html = "<main>" + String(repeating: "word ", count: 10_000) + "</main>"
        let text = LinkedInProfileFetcher.extractProfileText(html: html, cap: 500)
        XCTAssertEqual(text.count, 500)
    }
}

// MARK: - Location matching

final class LinkedInLocationMatchTests: XCTestCase {
    func testEmptyJobLocationPasses() {
        XCTAssertTrue(LinkedInGuestAPI.isLocationMatch("", configLocations: ["Denver"]))
    }

    func testRemoteConfigMatchesRemoteJob() {
        XCTAssertTrue(LinkedInGuestAPI.isLocationMatch(
            "United States (Remote)", configLocations: ["Remote"]))
    }

    func testSubstringMatch() {
        XCTAssertTrue(LinkedInGuestAPI.isLocationMatch(
            "Denver, CO", configLocations: ["denver"]))
        XCTAssertFalse(LinkedInGuestAPI.isLocationMatch(
            "Warsaw, Poland", configLocations: ["Denver", "Remote"]))
    }
}

// MARK: - GeoID resolution (cache + seed; no network)

final class GeoIDResolverTests: XCTestCase {
    func testNormalize() {
        XCTAssertEqual(GeoIDResolver.normalize("  Denver, CO "), "denver co")
        XCTAssertEqual(GeoIDResolver.normalize("Remote"), "remote")
    }

    func testSeedLookup() async throws {
        let db = try AppDatabase.inMemory()
        let resolver = GeoIDResolver(database: db)
        let geo = await resolver.resolve("Denver, CO")
        XCTAssertEqual(geo, "103736294")

        let remote = await resolver.resolve("Remote")
        XCTAssertEqual(remote, "92000001")

        let us = await resolver.resolve("United States")
        XCTAssertEqual(us, "103644278")
    }

    func testGeoCacheTableHitWinsOverSeed() async throws {
        let db = try AppDatabase.inMemory()
        try await db.writer.write { dbc in
            try dbc.execute(
                sql: "INSERT INTO geo_cache (location, geoId) VALUES (?, ?)",
                arguments: ["denver co", "999999999"])
            try dbc.execute(
                sql: "INSERT INTO geo_cache (location, geoId) VALUES (?, ?)",
                arguments: ["boulder colorado", "106360494"])
        }
        let resolver = GeoIDResolver(database: db)
        // Cache is checked before the seed dict, mirroring Python.
        let cached = await resolver.resolve("Denver, CO")
        XCTAssertEqual(cached, "999999999")
        let boulder = await resolver.resolve("Boulder, Colorado")
        XCTAssertEqual(boulder, "106360494")
    }

    func testStateOrCountryGeoIDs() {
        XCTAssertTrue(GeoIDResolver.stateOrCountryGeoIDs.contains("103644278"))
        XCTAssertTrue(GeoIDResolver.stateOrCountryGeoIDs.contains("105763813"))
        XCTAssertFalse(GeoIDResolver.stateOrCountryGeoIDs.contains("103736294"))
    }
}

// MARK: - Detail merge into search-phase jobs

final class LinkedInDetailMergeTests: XCTestCase {
    func testApplyBackfillsOnlyMissingFields() {
        var job = NormalizedJob(source: "linkedin", externalId: "li-1",
                                title: "SOC Analyst", location: "Denver, CO",
                                isEasyApply: true)
        let detail = LinkedInDetailParser.Detail(
            description: "Full description", salaryMin: 90000, salaryMax: 110000,
            salaryPeriod: "annual", location: "Austin, TX", tags: ["Full-time"],
            isEasyApply: false)
        LinkedInSource.apply(detail, to: &job)

        XCTAssertEqual(job.description, "Full description")
        XCTAssertEqual(job.salaryMin, 90000)
        XCTAssertEqual(job.salaryMax, 110000)
        XCTAssertEqual(job.salaryPeriod, "annual")
        XCTAssertEqual(job.location, "Denver, CO", "existing card location kept")
        XCTAssertEqual(job.tags, ["Full-time"])
        XCTAssertTrue(job.isEasyApply, "card-level Easy Apply survives a false detail signal")
    }

    func testApplyBackfillsEmptyLocation() {
        var job = NormalizedJob(source: "linkedin", externalId: "li-2", title: "SRE")
        var detail = LinkedInDetailParser.Detail()
        detail.location = "Denver, CO"
        LinkedInSource.apply(detail, to: &job)
        XCTAssertEqual(job.location, "Denver, CO")
    }
}
