import XCTest
import JobsmithKit

final class JobStoreTests: XCTestCase {
    private func makeStore() throws -> JobStore {
        JobStore(try AppDatabase.inMemory())
    }

    private func sample(externalId: String = "gh-1", description: String = "desc",
                        salaryMin: Int? = nil, tags: [String] = [],
                        applyType: String = "unknown") -> NormalizedJob {
        NormalizedJob(source: "greenhouse", externalId: externalId,
                      title: "Software Engineer", company: "Acme",
                      location: "Remote", url: "https://example.com/j/\(externalId)",
                      description: description, salaryMin: salaryMin,
                      tags: tags, applyType: applyType)
    }

    func testInsertNewJob() throws {
        let store = try makeStore()
        let summary = try store.upsert([sample()])
        XCTAssertEqual(summary.inserted, 1)
        let jobs = try store.jobs()
        XCTAssertEqual(jobs.count, 1)
        XCTAssertEqual(jobs[0].triage, "new")
        XCTAssertEqual(jobs[0].status, "discovered")
    }

    func testDuplicateBackfillsEmptyFieldsOnly() throws {
        let store = try makeStore()
        try store.upsert([sample(description: "")])
        // Re-fetch with description + salary: both backfill.
        try store.upsert([sample(description: "full description", salaryMin: 90000, tags: ["swift"])])
        var job = try store.jobs()[0]
        XCTAssertEqual(job.description, "full description")
        XCTAssertEqual(job.salaryMin, 90000)
        XCTAssertEqual(job.tagList, ["swift"])
        XCTAssertEqual(job.timesSeen, 2)

        // Existing non-empty description must NOT be overwritten.
        try store.upsert([sample(description: "different text")])
        job = try store.jobs()[0]
        XCTAssertEqual(job.description, "full description")
        XCTAssertEqual(job.timesSeen, 3)
    }

    func testDuplicateNeverRegressesApplyType() throws {
        let store = try makeStore()
        try store.upsert([sample(applyType: "external")])
        try store.upsert([sample(applyType: "unknown")])
        XCTAssertEqual(try store.jobs()[0].applyType, "external")
    }

    func testTriageAndKnownIDs() throws {
        let store = try makeStore()
        try store.upsert([sample(externalId: "a"), sample(externalId: "b")])
        let inbox = try store.inbox()
        XCTAssertEqual(inbox.count, 2)
        try store.setTriage("dismissed", jobId: inbox[0].id)
        XCTAssertEqual(try store.inbox().count, 1)
        XCTAssertEqual(try store.knownExternalIDs(source: "greenhouse"), ["a", "b"])
    }
}

final class ApplicationStoreTests: XCTestCase {
    func testCreateReviewApproveFlow() throws {
        let db = try AppDatabase.inMemory()
        let jobs = JobStore(db)
        let apps = ApplicationStore(db)
        try jobs.upsert([NormalizedJob(source: "manual", externalId: "m1", title: "Engineer")])
        let job = try jobs.jobs()[0]

        let app = try apps.createOrReplace(jobId: job.id, resume: "SUMMARY\ntext",
                                           coverLetter: "Dear Hiring Team,",
                                           honestyLevel: "honest", stylePreset: "standard")
        XCTAssertEqual(app.status, "pending_review")

        try apps.updateContent(id: app.id, resume: "SUMMARY\nedited", coverLetter: nil)
        try apps.updateStatus(id: app.id, status: "applied")
        let reloaded = try apps.application(id: app.id)
        XCTAssertEqual(reloaded?.resumeContent, "SUMMARY\nedited")
        XCTAssertEqual(reloaded?.status, "applied")
        XCTAssertNotNil(reloaded?.appliedAt)

        // Re-tailoring replaces the existing application.
        _ = try apps.createOrReplace(jobId: job.id, resume: "v2", coverLetter: "v2",
                                     honestyLevel: "tailored", stylePreset: "modern")
        XCTAssertEqual(try apps.applications().count, 1)
        XCTAssertEqual(try apps.application(jobId: job.id)?.resumeContent, "v2")
    }
}

final class ConfigStoreTests: XCTestCase {
    func testRoundTrip() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("config-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let store = ConfigStore(fileURL: dir.appendingPathComponent("config.json"))

        var config = await store.load()
        XCTAssertTrue(config.profile.isEmpty)

        config.profile.fullName = "Jane Doe"
        config.profile.skills = ["Swift", "Python"]
        config.ai.baseURL = "http://192.168.1.7:1234/v1"
        config.honesty.level = .tailored
        try await store.save(config)

        let fresh = ConfigStore(fileURL: dir.appendingPathComponent("config.json"))
        let loaded = await fresh.load()
        XCTAssertEqual(loaded.profile.fullName, "Jane Doe")
        XCTAssertEqual(loaded.ai.baseURL, "http://192.168.1.7:1234/v1")
        XCTAssertEqual(loaded.honesty.level, .tailored)
    }

    func testModelTierFallbackChain() {
        var ai = AIConfig()
        XCTAssertEqual(ai.model(for: .utility), "local-model")
        ai.strongModel = "mistral-7b"
        XCTAssertEqual(ai.model(for: .utility), "mistral-7b")
        ai.fastModel = "qwen-9b"
        XCTAssertEqual(ai.model(for: .utility), "qwen-9b")
        ai.utilityModel = "tiny"
        XCTAssertEqual(ai.model(for: .utility), "tiny")
        XCTAssertEqual(ai.model(for: .strong), "mistral-7b")
    }
}

final class AnswerBankStoreTests: XCTestCase {
    func testUpsertAndDelete() throws {
        let store = AnswerBankStore(try AppDatabase.inMemory())
        try store.upsert(AnswerBankEntry(key: "work_auth", label: "Work authorization",
                                         keywords: ["authorized", "work authorization"],
                                         value: "Yes"))
        XCTAssertEqual(try store.all().count, 1)
        XCTAssertEqual(try store.all()[0].keywordList, ["authorized", "work authorization"])
        try store.delete(key: "work_auth")
        XCTAssertTrue(try store.all().isEmpty)
    }
}
