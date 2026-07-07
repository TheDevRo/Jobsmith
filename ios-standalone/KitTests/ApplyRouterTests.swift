import XCTest
import JobsmithKit

final class NativeMessageRouterTests: XCTestCase {
    private var db: AppDatabase!
    private var engine: MockAIEngine!
    private var router: NativeMessageRouter!
    private var activeJobStore: ActiveJobStore!
    private var tempURL: URL!

    override func setUpWithError() throws {
        db = try AppDatabase.inMemory()
        engine = MockAIEngine()
        tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("active_job_\(UUID().uuidString).json")
        activeJobStore = ActiveJobStore(fileURL: tempURL)
        router = NativeMessageRouter(
            db: db,
            engine: engine,
            config: { ApplyFixtures.config() },
            activeJobStore: activeJobStore,
            readDocument: { jobId, kind in
                kind == .resume ? Data("docx-bytes-\(jobId)".utf8) : nil
            },
            version: "1.2.3")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempURL)
    }

    private func result(_ response: [String: Any]) -> [String: Any] {
        response["result"] as? [String: Any] ?? [:]
    }

    @discardableResult
    private func insertJob(externalId: String = "x1", salaryMax: Int? = nil) throws -> Job {
        try JobStore(db).upsert([NormalizedJob(
            source: "greenhouse", externalId: externalId, title: "iOS Engineer",
            company: "Acme", location: "Remote", url: "https://acme.dev/jobs/\(externalId)",
            description: "Build the app.", salaryMax: salaryMax)])
        return try JobStore(db).jobs()[0]
    }

    func testHealth() async {
        let r = result(await router.handle(name: "health", body: [:]))
        XCTAssertEqual(r["ok"] as? Bool, true)
        XCTAssertEqual(r["version"] as? String, "1.2.3")
    }

    func testNoop() async {
        let r = result(await router.handle(name: "noop", body: [:]))
        XCTAssertEqual(r["ok"] as? Bool, true)
    }

    func testUnknownMessageErrors() async {
        let resp = await router.handle(name: "bogus", body: [:])
        XCTAssertEqual(resp["status"] as? Int, 404)
        XCTAssertNotNil(resp["error"])
        XCTAssertNil(resp["result"])
    }

    func testGetProfileReturnsSixteenSnakeCaseKeys() async {
        let r = result(await router.handle(name: "getProfile", body: [:]))
        XCTAssertEqual(r.count, 16)
        XCTAssertEqual(r["full_name"] as? String, "Jane Q Doe")
        XCTAssertEqual(r["zip_code"] as? String, "78701")
        XCTAssertEqual(r["work_authorization"] as? String, "Yes")
        XCTAssertEqual(r["sponsorship_required"] as? String, "No")
        XCTAssertEqual(r["notice_period"] as? String, "2 weeks")
    }

    func testGetJobFoundAndMissing() async throws {
        let job = try insertJob()
        let r = result(await router.handle(name: "getJob", body: ["jobId": job.id]))
        XCTAssertEqual(r["id"] as? String, job.id)
        XCTAssertEqual(r["title"] as? String, "iOS Engineer")
        XCTAssertEqual(r["company"] as? String, "Acme")
        XCTAssertEqual(r["description"] as? String, "Build the app.")

        let missing = await router.handle(name: "getJob", body: ["jobId": "nope"])
        XCTAssertEqual(missing["status"] as? Int, 404)
    }

    func testGetResumeAndCoverFiles() async throws {
        let job = try insertJob()
        let r = result(await router.handle(name: "getResumeFile", body: ["jobId": job.id]))
        XCTAssertEqual(r["filename"] as? String, "\(job.id)_resume.docx")
        XCTAssertEqual(r["mime"] as? String,
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        let data = Data(base64Encoded: r["base64"] as? String ?? "")
        XCTAssertEqual(data, Data("docx-bytes-\(job.id)".utf8))

        // Cover letter is absent — 404 like the desktop route.
        let missing = await router.handle(name: "getCoverFile", body: ["jobId": job.id])
        XCTAssertEqual(missing["status"] as? Int, 404)
    }

    func testScanResolvesDeterministicFieldsWithoutLLM() async throws {
        let body: [String: Any] = [
            "url": "https://acme.dev/apply",
            "fields": [
                ["field_id": "e1", "label": "Email", "field_type": "email"],
                ["field_id": "n1", "label": "Full Name", "field_type": "text"],
            ],
        ]
        let r = result(await router.handle(name: "scan", body: body))
        XCTAssertEqual(r["count"] as? Int, 2)
        let fields = r["fields"] as? [[String: Any]] ?? []
        XCTAssertEqual(fields.map { $0["field_id"] as? String }, ["e1", "n1"])
        XCTAssertEqual(fields[0]["value"] as? String, "jane@example.com")
        XCTAssertEqual(fields[1]["value"] as? String, "Jane Q Doe")
        XCTAssertEqual(fields[0]["source"] as? String, "profile")
        XCTAssertTrue(engine.requests.isEmpty)
    }

    func testScanUsesJobSalaryForDesiredSalary() async throws {
        let job = try insertJob(salaryMax: 200_000)
        let body: [String: Any] = [
            "url": job.url,
            "job_id": job.id,
            "fields": [["field_id": "s1", "label": "Desired Salary", "field_type": "text"]],
        ]
        let r = result(await router.handle(name: "scan", body: body))
        let fields = r["fields"] as? [[String: Any]] ?? []
        XCTAssertEqual(fields.first?["value"] as? String, "$200,000")
    }

    func testGetActiveJobRoundTrip() async throws {
        // Nothing written yet → {job_id: null}.
        var r = result(await router.handle(name: "getActiveJob", body: [:]))
        XCTAssertTrue(r["job_id"] is NSNull)

        try activeJobStore.write(ActiveJob(jobId: "j42", title: "iOS Engineer",
                                           company: "Acme", url: "https://acme.dev/jobs/42",
                                           savedAt: "2026-07-07T00:00:00Z"))
        r = result(await router.handle(name: "getActiveJob", body: [:]))
        XCTAssertEqual(r["job_id"] as? String, "j42")
        XCTAssertEqual(r["title"] as? String, "iOS Engineer")
        XCTAssertEqual(r["company"] as? String, "Acme")
        XCTAssertEqual(r["saved_at"] as? String, "2026-07-07T00:00:00Z")

        activeJobStore.clear()
        r = result(await router.handle(name: "getActiveJob", body: [:]))
        XCTAssertTrue(r["job_id"] is NSNull)
    }

    func testMarkAppliedUpdatesJobAndApplication() async throws {
        let job = try insertJob()
        let app = try ApplicationStore(db).createOrReplace(
            jobId: job.id, resume: "r", coverLetter: "c",
            honestyLevel: "honest", stylePreset: "standard")

        let r = result(await router.handle(name: "markApplied", body: ["jobId": job.id]))
        XCTAssertEqual(r["ok"] as? Bool, true)
        XCTAssertEqual(try JobStore(db).job(id: job.id)?.status, "applied")
        let updated = try ApplicationStore(db).application(id: app.id)
        XCTAssertEqual(updated?.status, "applied")
        XCTAssertNotNil(updated?.appliedAt)

        let missing = await router.handle(name: "markApplied", body: ["jobId": "nope"])
        XCTAssertEqual(missing["status"] as? Int, 404)
    }

    func testAnswerFromBankAndSkip() async throws {
        try AnswerBankStore(db).upsert(AnswerBankEntry(
            key: "tell_us_about_yourself", label: "", keywords: [], value: "I am Jane."))
        var r = result(await router.handle(name: "answer",
                                           body: ["question": "Tell us about yourself"]))
        XCTAssertEqual(r["value"] as? String, "I am Jane.")
        XCTAssertEqual(r["source"] as? String, "answer_bank")
        XCTAssertEqual(r["confidence"] as? Double, 1.0)

        r = result(await router.handle(name: "answer",
                                       body: ["question": "zorp glim flurb"]))
        XCTAssertEqual(r["value"] as? String, "")
        XCTAssertEqual(r["source"] as? String, "skip")
        XCTAssertEqual(r["confidence"] as? Double, 0.0)
    }

    func testSaveAnswerUpsertsAndBecomesMatchable() async throws {
        let body: [String: Any] = [
            "key": "security_clearance",
            "label": "Clearance?",
            "keywords": ["security", "clearance"],
            "value": "Active Secret clearance.",
        ]
        let r = result(await router.handle(name: "saveAnswer", body: body))
        XCTAssertEqual(r["ok"] as? Bool, true)
        let rows = try AnswerBankStore(db).all()
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].keywordList, ["security", "clearance"])

        let answer = result(await router.handle(
            name: "answer", body: ["question": "Do you hold a security clearance?"]))
        XCTAssertEqual(answer["value"] as? String, "Active Secret clearance.")
    }
}
