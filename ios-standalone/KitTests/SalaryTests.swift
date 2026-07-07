import XCTest
import GRDB
@testable import JobsmithKit

// MARK: - Histogram percentile math

final class HistogramPercentileTests: XCTestCase {
    func testPercentilesFromFixtureHistogram() throws {
        let data = try Fixtures.data("adzuna_histogram", "json")
        let root = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: data) as? [String: Any])
        let histogram = try XCTUnwrap(root["histogram"] as? [String: Any])

        let (p25, p50, p75) = SalaryEstimator.percentilesFromHistogram(histogram)
        // Buckets (30k:10, 40k:20, 50k:40, 60k:20, 70k:10), total 100.
        // p25: target 25 lands in the 40k bucket, cum 10 before it:
        //      40000 + ((25-10)/20) * (50000-40000) = 47500.
        XCTAssertEqual(try XCTUnwrap(p25), 47500, accuracy: 0.001)
        // p50: target 50 in the 50k bucket, cum 30: 50000 + (20/40)*10000 = 55000.
        XCTAssertEqual(try XCTUnwrap(p50), 55000, accuracy: 0.001)
        // p75: target 75 in the 60k bucket, cum 70: 60000 + (5/20)*10000 = 62500.
        XCTAssertEqual(try XCTUnwrap(p75), 62500, accuracy: 0.001)
    }

    func testLastBucketInterpolatesAgainstLowerBoundTimes1Point1() throws {
        // Single bucket: next_lo = lo * 1.1, frac = target/count.
        let (p25, p50, p75) = SalaryEstimator.percentilesFromHistogram(["100000": 4])
        XCTAssertEqual(try XCTUnwrap(p25), 102500, accuracy: 0.001)
        XCTAssertEqual(try XCTUnwrap(p50), 105000, accuracy: 0.001)
        XCTAssertEqual(try XCTUnwrap(p75), 107500, accuracy: 0.001)
    }

    func testZeroCountBucketsAreDropped() throws {
        let (p25, _, _) = SalaryEstimator.percentilesFromHistogram(
            ["10000": 0, "50000": 10])
        XCTAssertEqual(try XCTUnwrap(p25), 51250, accuracy: 0.001,
                       "10k bucket ignored; 50000 + 0.25*(55000-50000)")
    }

    func testMalformedHistogramReturnsNils() {
        let (p25, p50, p75) = SalaryEstimator.percentilesFromHistogram(
            ["not-a-number": 5])
        XCTAssertNil(p25)
        XCTAssertNil(p50)
        XCTAssertNil(p75)

        let empty = SalaryEstimator.percentilesFromHistogram([:])
        XCTAssertNil(empty.0)
    }
}

// MARK: - Seniority multipliers

final class SeniorityMultiplierTests: XCTestCase {
    func testMultiplierTable() {
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("intern"), 0.55)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("entry"), 0.80)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("junior"), 0.85)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("mid"), 1.00)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("senior"), 1.20)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("staff"), 1.45)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("principal"), 1.70)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("manager"), 1.25)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("director"), 1.55)
    }

    func testUnknownAndNilSeniorityAreNeutral() {
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier(nil), 1.0)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier(""), 1.0)
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("wizard"), 1.0)
    }

    func testMultiplierIsCaseInsensitive() {
        XCTAssertEqual(SalaryEstimator.seniorityMultiplier("Senior"), 1.20)
    }
}

// MARK: - Classification fallbacks (regex path, no engine)

final class ClassificationFallbackTests: XCTestCase {
    func testFallbackCanonicalTitleStripsSeniority() {
        XCTAssertEqual(SalaryEstimator.fallbackCanonicalTitle("Senior Software Engineer"),
                       "Software Engineer")
        XCTAssertEqual(SalaryEstimator.fallbackCanonicalTitle("Lead Platform Engineer (Remote)"),
                       "Platform Engineer")
        XCTAssertEqual(SalaryEstimator.fallbackCanonicalTitle("Entry-Level Help Desk Technician"),
                       "Help Desk Technician")
        XCTAssertEqual(SalaryEstimator.fallbackCanonicalTitle("Staff Security Engineer [Hybrid]"),
                       "Security Engineer")
    }

    func testFallbackCanonicalTitleKeepsOriginalWhenEverythingStripped() {
        XCTAssertEqual(SalaryEstimator.fallbackCanonicalTitle("Manager"), "Manager")
    }

    func testFallbackSeniorityKeywordOrder() {
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Software Engineering Intern"), "intern")
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Entry Level Analyst"), "entry")
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Junior Developer"), "junior")
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Staff Engineer"), "staff")
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Principal Architect"), "principal")
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Senior Security Engineer"), "senior")
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Director of Engineering"), "director")
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Engineering Manager"), "manager")
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Tech Lead"), "senior")
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Software Engineer"), "mid")
        // "senior" is checked before "manager" — Python tuple order.
        XCTAssertEqual(SalaryEstimator.fallbackSeniority("Senior Engineering Manager"), "senior")
    }

    func testClassifyWithoutEngineUsesFallbacksAndCaches() async throws {
        let db = try AppDatabase.inMemory()
        let config = AppConfig()

        let result = await SalaryEstimator.classifyJobRole(
            title: "Senior Security Engineer", description: "Defend the platform.",
            config: config, engine: nil, database: db)
        XCTAssertEqual(result.canonicalTitle, "Security Engineer")
        XCTAssertEqual(result.seniority, "senior")
        XCTAssertNil(result.socCode)

        // Result cached in ai_cache under a sha-derived "soc:" key.
        let row = try await db.writer.read { dbc in
            try Row.fetchOne(dbc, sql: "SELECT key, value FROM ai_cache")
        }
        let key: String = try XCTUnwrap(row)["key"]
        XCTAssertTrue(key.hasPrefix("soc:"))
        XCTAssertEqual(key.count, "soc:".count + 24)

        // Second call is a cache hit and returns the same classification.
        let again = await SalaryEstimator.classifyJobRole(
            title: "Senior Security Engineer", description: "Defend the platform.",
            config: config, engine: nil, database: db)
        XCTAssertEqual(again, result)
    }

    func testClassifyWithEngineParsesJSONAndNormalizesSOC() async throws {
        let db = try AppDatabase.inMemory()
        let config = AppConfig()
        let engine = MockAIEngine()
        engine.register("Classify the job posting", .text("""
        {"canonical_title": "software engineer", "seniority": "senior",
         "soc_code": "SOC 15-1252 (developers)", "soc_title": "Software Developers"}
        """))

        let result = await SalaryEstimator.classifyJobRole(
            title: "Sr. Software Engineer", description: "",
            config: config, engine: engine, database: db)
        XCTAssertEqual(result.canonicalTitle, "software engineer")
        XCTAssertEqual(result.seniority, "senior")
        XCTAssertEqual(result.socCode, "15-1252", "SOC normalized to NN-NNNN")
        XCTAssertEqual(result.socTitle, "Software Developers")
    }

    func testClassifyEngineFailureFallsBackToRegex() async throws {
        let config = AppConfig()
        let engine = MockAIEngine()
        engine.register("Classify the job posting", .failure("model offline"))

        let result = await SalaryEstimator.classifyJobRole(
            title: "Junior Data Analyst", description: "",
            config: config, engine: engine, database: nil)
        XCTAssertEqual(result.canonicalTitle, "Data Analyst")
        XCTAssertEqual(result.seniority, "junior")
    }
}

// MARK: - MSA mapping

final class MSAMappingTests: XCTestCase {
    func testLocationToMSA() {
        XCTAssertEqual(SalaryEstimator.locationToMSA("Denver, CO"), "1974000")
        XCTAssertEqual(SalaryEstimator.locationToMSA("Greater Boston Area"), "1471650")
        XCTAssertNil(SalaryEstimator.locationToMSA("Boise, ID"), "unknown metro falls to national")
        XCTAssertNil(SalaryEstimator.locationToMSA(""))
    }

    func testMSATableSize() {
        XCTAssertEqual(SalaryEstimator.msaCodes.count, 21)
    }
}

// MARK: - Estimate shape (fallback path, no external APIs)

final class SalaryEstimateShapeTests: XCTestCase {
    func testEstimateReturnsNilWithoutKeysOrSOC() async throws {
        // No Adzuna keys and the regex fallback yields no SOC code, so the
        // estimator has no data source at all — must return nil, not invent.
        let db = try AppDatabase.inMemory()
        let job = Job(from: NormalizedJob(source: "linkedin", externalId: "li-1",
                                          title: "Senior Security Engineer",
                                          location: "Denver, CO"))
        let estimate = try await SalaryEstimator().estimate(
            job: job, config: AppConfig(), engine: nil, database: db)
        XCTAssertNil(estimate)
    }

    func testEstimateReturnsNilForEmptyTitle() async throws {
        let job = Job(from: NormalizedJob(source: "linkedin", externalId: "li-2",
                                          title: "   "))
        let estimate = try await SalaryEstimator().estimate(
            job: job, config: AppConfig(), engine: nil, database: nil)
        XCTAssertNil(estimate)
    }

    func testSalaryEstimateEncodesForJobsTableColumn() throws {
        let estimate = SalaryEstimate(p25: 90000, p50: 105000, p75: 126000,
                                      currency: "USD", confidence: "high",
                                      source: "adzuna",
                                      canonicalTitle: "security engineer",
                                      seniority: "senior")
        let json = try XCTUnwrap(
            String(data: JSONEncoder().encode(estimate), encoding: .utf8))
        let decoded = try JSONDecoder().decode(SalaryEstimate.self, from: Data(json.utf8))
        XCTAssertEqual(decoded, estimate)
    }
}

// MARK: - AICache TTL

final class AICacheTests: XCTestCase {
    func testRoundTripAndExpiry() async throws {
        let db = try AppDatabase.inMemory()
        await AICache.set(db, key: "k1", value: "v1")
        let hit = await AICache.get(db, key: "k1", maxAgeDays: 30)
        XCTAssertEqual(hit, "v1")

        // Backdate the row past the TTL — must read as a miss.
        let old = ISO8601DateFormatter().string(
            from: Date(timeIntervalSinceNow: -31 * 86400))
        try await db.writer.write { dbc in
            try dbc.execute(sql: "UPDATE ai_cache SET createdAt = ? WHERE key = ?",
                            arguments: [old, "k1"])
        }
        let expired = await AICache.get(db, key: "k1", maxAgeDays: 30)
        XCTAssertNil(expired)

        let missing = await AICache.get(db, key: "nope", maxAgeDays: 30)
        XCTAssertNil(missing)
    }
}
