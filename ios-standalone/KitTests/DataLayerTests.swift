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

    func testUpsertRecoversHourlyRateFromDescription() throws {
        let store = try makeStore()
        try store.upsert([sample(externalId: "hr-1",
                                 description: "Great team! Pay is $28 - $34 per hour.")])
        let job = try store.jobs()[0]
        XCTAssertEqual(job.salaryMin, 28)
        XCTAssertEqual(job.salaryMax, 34)
        XCTAssertEqual(job.salaryPeriod, "hourly")
    }

    func testUpsertDoesNotOverrideStructuredSalaryWithText() throws {
        let store = try makeStore()
        // Structured salary present → the description text is not consulted.
        try store.upsert([sample(externalId: "sal-1", description: "Also mentions $28/hr.",
                                 salaryMin: 120000)])
        let job = try store.jobs()[0]
        XCTAssertEqual(job.salaryMin, 120000)
        XCTAssertEqual(job.salaryPeriod, "unknown")
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

    /// Clearing the inbox (soft-deleting every triage=="new" job) must leave
    /// pipeline rows — any other triage — completely untouched.
    func testDeletingAllInboxJobsLeavesPipelineIntact() throws {
        let store = try makeStore()
        try store.upsert([sample(externalId: "in-1"), sample(externalId: "in-2"),
                          sample(externalId: "kept")])
        let kept = try store.jobs().first { $0.externalId == "kept" }!
        try store.setTriage("shortlisted", jobId: kept.id)

        for job in try store.inbox() { try store.delete(jobId: job.id) }

        XCTAssertTrue(try store.inbox().isEmpty)
        let shortlisted = try store.jobs(triage: "shortlisted")
        XCTAssertEqual(shortlisted.map(\.externalId), ["kept"])
        // Soft delete: the rows survive (hidden) so a re-fetch of the same
        // postings can't resurrect them into the inbox.
        try store.upsert([sample(externalId: "in-1")])
        XCTAssertTrue(try store.inbox().isEmpty)
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

    // MARK: - Saved endpoints (switchable AI connections)

    func testSavedEndpointsSurviveConfigRoundTrip() async throws {
        let url = try tempConfigURL()
        let store = ConfigStore(fileURL: url)
        var config = await store.load()
        config.ai.savedEndpoints = [
            .init(name: "LM Studio", baseURL: "https://lmstudio.example/v1", apiKey: "",
                  strongModel: "qwen3.5-9b-mtp"),
            .init(name: "OpenRouter", baseURL: "https://openrouter.ai/api/v1", apiKey: "sk-or-x",
                  strongModel: "deepseek/deepseek-chat"),
        ]
        try await store.save(config)

        let loaded = await ConfigStore(fileURL: url).load()
        XCTAssertEqual(loaded.ai.savedEndpoints.map(\.name), ["LM Studio", "OpenRouter"])
        XCTAssertEqual(loaded.ai.savedEndpoints[1].apiKey, "sk-or-x")
    }

    /// A config written before the field existed decodes to no presets.
    func testConfigWithoutSavedEndpointsDecodesEmpty() throws {
        let json = #"{"ai": {"baseURL": "http://x/v1"}}"#
        let config = try JSONDecoder().decode(AppConfig.self, from: Data(json.utf8))
        XCTAssertEqual(config.ai.baseURL, "http://x/v1")
        XCTAssertTrue(config.ai.savedEndpoints.isEmpty)
    }

    func testCaptureAndApplySwitchTheLiveConnection() {
        var ai = AIConfig()
        ai.baseURL = "https://lmstudio.example/v1"; ai.apiKey = ""
        ai.strongModel = "qwen3.5-9b-mtp"; ai.fastModel = "qwen3.5-9b-mtp"
        let lmstudio = ai.capture(name: "LM Studio")

        let openrouter = AIConfig.SavedEndpoint(
            name: "OpenRouter", baseURL: "https://openrouter.ai/api/v1", apiKey: "sk-or-x",
            strongModel: "deepseek/deepseek-chat", fastModel: "meta-llama/llama-3.3-70b")
        ai.savedEndpoints = [lmstudio, openrouter]

        ai.apply(openrouter)
        XCTAssertEqual(ai.baseURL, "https://openrouter.ai/api/v1")
        XCTAssertEqual(ai.apiKey, "sk-or-x")
        XCTAssertEqual(ai.strongModel, "deepseek/deepseek-chat")
        XCTAssertEqual(ai.activeSavedEndpoint()?.name, "OpenRouter")

        ai.apply(lmstudio)
        XCTAssertEqual(ai.model(for: .strong), "qwen3.5-9b-mtp")
        XCTAssertEqual(ai.activeSavedEndpoint()?.name, "LM Studio")
    }

    func testApplyKeepsOnDeviceTierAssignments() {
        // "Score on-device" is a routing choice about the phone, not about any
        // endpoint — switching servers must not silently move that work back
        // to the network.
        var ai = AIConfig()
        ai.fastModel = AIConfig.onDeviceModelID
        ai.apply(.init(name: "OpenRouter", baseURL: "https://openrouter.ai/api/v1",
                       apiKey: "k", strongModel: "big", fastModel: "medium"))
        XCTAssertEqual(ai.fastModel, AIConfig.onDeviceModelID)
        XCTAssertEqual(ai.strongModel, "big")
    }

    // MARK: - Tolerant decode + corruption handling (REL-10)

    private func tempConfigURL() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("config-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("config.json")
    }

    /// An unknown extra key (written by a newer build) plus missing optional
    /// sections must not cost the user the values that *are* there.
    func testUnknownKeysAndMissingSectionsSurvive() async throws {
        let url = try tempConfigURL()
        let json = """
        {
          "profile": {"fullName": "Jane Doe", "skills": ["Swift"], "quantumField": 42},
          "search": {"keywords": ["ios"], "somethingNew": true},
          "ai": {"baseURL": "http://10.0.0.2:1234/v1"},
          "retiredSection": {"a": 1}
        }
        """
        try Data(json.utf8).write(to: url)

        let config = await ConfigStore(fileURL: url).load()
        XCTAssertEqual(config.profile.fullName, "Jane Doe")
        XCTAssertEqual(config.profile.skills, ["Swift"])
        XCTAssertEqual(config.search.keywords, ["ios"])
        XCTAssertEqual(config.ai.baseURL, "http://10.0.0.2:1234/v1")
        // Absent sections fall back to their defaults rather than failing.
        XCTAssertEqual(config.honesty.level, .honest)
        XCTAssertEqual(config.apiKeys.adzunaAppID, "")
    }

    /// One malformed section must not take the rest of the config with it.
    func testMalformedSectionFallsBackWithoutLosingOthers() async throws {
        let url = try tempConfigURL()
        let json = """
        {
          "profile": {"fullName": "Jane Doe"},
          "search": "this should be an object",
          "honesty": {"level": "tailored"}
        }
        """
        try Data(json.utf8).write(to: url)

        let config = await ConfigStore(fileURL: url).load()
        XCTAssertEqual(config.profile.fullName, "Jane Doe")
        XCTAssertEqual(config.honesty.level, .tailored)
        XCTAssertEqual(config.search.keywords, [])  // defaulted, not fatal
    }

    /// A config file that is not JSON at all must be preserved, not overwritten
    /// with defaults — the old behaviour silently wiped profile, keys and all.
    func testCorruptFileIsPreservedAndReported() async throws {
        let url = try tempConfigURL()
        let garbage = Data("}{ not json at all".utf8)
        try garbage.write(to: url)

        let store = ConfigStore(fileURL: url)
        let config = await store.load()
        XCTAssertTrue(config.profile.isEmpty)

        let warning = await store.loadWarning
        XCTAssertNotNil(warning)
        // The original is untouched and a copy is kept for post-mortem.
        XCTAssertEqual(try Data(contentsOf: url), garbage)
        let corruptURL = url.deletingLastPathComponent()
            .appendingPathComponent("config.corrupt.json")
        XCTAssertEqual(try Data(contentsOf: corruptURL), garbage)
    }

    /// SEC-08: the LinkedIn session cookie is a live credential — it round-trips
    /// through the secret store and must not be left in the plaintext JSON.
    func testLinkedInCookieDoesNotLandInPlaintextJSON() async throws {
        let url = try tempConfigURL()
        let secrets = FakeSecretStore()
        let store = ConfigStore(fileURL: url, secrets: secrets)

        var config = await store.load()
        config.apiKeys.linkedInCookie = "AQEDATest_li_at_value"
        config.apiKeys.adzunaAppID = "public-id"
        try await store.save(config)

        let onDisk = try String(contentsOf: url, encoding: .utf8)
        XCTAssertFalse(onDisk.contains("AQEDATest_li_at_value"))
        XCTAssertTrue(onDisk.contains("public-id"))
        XCTAssertEqual(secrets.get(.linkedInCookie), "AQEDATest_li_at_value")

        // ...but a fresh store still reads it back, out of the secret store.
        let reloaded = await ConfigStore(fileURL: url, secrets: secrets).load()
        XCTAssertEqual(reloaded.apiKeys.linkedInCookie, "AQEDATest_li_at_value")
        XCTAssertEqual(reloaded.apiKeys.adzunaAppID, "public-id")
    }

    /// A cookie written in plaintext by an older build migrates into the secret
    /// store on first load, and is stripped from the file.
    func testLegacyPlaintextCookieMigrates() async throws {
        let url = try tempConfigURL()
        try Data(#"{"apiKeys": {"linkedInCookie": "legacy_cookie_value"}}"#.utf8).write(to: url)

        let secrets = FakeSecretStore()
        let loaded = await ConfigStore(fileURL: url, secrets: secrets).load()
        XCTAssertEqual(loaded.apiKeys.linkedInCookie, "legacy_cookie_value")
        XCTAssertEqual(secrets.get(.linkedInCookie), "legacy_cookie_value")

        let onDisk = try String(contentsOf: url, encoding: .utf8)
        XCTAssertFalse(onDisk.contains("legacy_cookie_value"))
    }

    /// AI bearer keys are live credentials too: `ai.apiKey` and every saved
    /// endpoint's key round-trip through the secret store, and must not sit in
    /// the plaintext JSON — while the in-memory config (what settings-sync
    /// reads) keeps carrying them.
    func testAIKeysDoNotLandInPlaintextJSON() async throws {
        let url = try tempConfigURL()
        let secrets = FakeSecretStore()
        let store = ConfigStore(fileURL: url, secrets: secrets)

        var config = await store.load()
        config.ai.apiKey = "sk-live-SECRET"
        config.ai.baseURL = "http://x/v1"
        config.ai.savedEndpoints = [
            .init(id: "ep-1", name: "OpenRouter", baseURL: "https://openrouter.ai/api/v1",
                  apiKey: "sk-or-ENDPOINT"),
        ]
        try await store.save(config)

        let onDisk = try String(contentsOf: url, encoding: .utf8)
        XCTAssertFalse(onDisk.contains("sk-live-SECRET"))
        XCTAssertFalse(onDisk.contains("sk-or-ENDPOINT"))
        // Non-secret AI fields stay in the file. (JSONEncoder escapes "/", so
        // match a slash-free substring of the endpoint URL.)
        XCTAssertTrue(onDisk.contains("openrouter.ai"))
        XCTAssertEqual(secrets.get(.aiAPIKey), "sk-live-SECRET")
        XCTAssertEqual(secrets.get(.savedEndpointAPIKey("ep-1")), "sk-or-ENDPOINT")

        // A fresh store rehydrates both keys from the secret store.
        let reloaded = await ConfigStore(fileURL: url, secrets: secrets).load()
        XCTAssertEqual(reloaded.ai.apiKey, "sk-live-SECRET")
        XCTAssertEqual(reloaded.ai.savedEndpoints.first?.apiKey, "sk-or-ENDPOINT")
    }

    /// Deleting a saved endpoint must clear its Keychain entry — no orphaned
    /// bearer token left behind for a preset the user removed.
    func testDeletingSavedEndpointClearsItsSecret() async throws {
        let url = try tempConfigURL()
        let secrets = FakeSecretStore()
        let store = ConfigStore(fileURL: url, secrets: secrets)

        var config = await store.load()
        config.ai.savedEndpoints = [
            .init(id: "ep-1", name: "A", baseURL: "https://a/v1", apiKey: "key-a"),
            .init(id: "ep-2", name: "B", baseURL: "https://b/v1", apiKey: "key-b"),
        ]
        try await store.save(config)
        XCTAssertEqual(secrets.get(.savedEndpointAPIKey("ep-1")), "key-a")

        // Remove the first endpoint.
        config.ai.savedEndpoints.removeAll { $0.id == "ep-1" }
        try await store.save(config)

        XCTAssertNil(secrets.get(.savedEndpointAPIKey("ep-1")))
        XCTAssertEqual(secrets.get(.savedEndpointAPIKey("ep-2")), "key-b")
    }

    /// A legacy config with plaintext AI keys migrates them into the secret
    /// store on first load and strips them from the file.
    func testLegacyPlaintextAIKeysMigrate() async throws {
        let url = try tempConfigURL()
        let json = #"{"ai": {"apiKey": "legacy-ai-key", "savedEndpoints": [{"id": "ep-9", "name": "X", "baseURL": "https://x/v1", "apiKey": "legacy-ep-key", "strongModel": "", "fastModel": "", "utilityModel": ""}]}}"#
        try Data(json.utf8).write(to: url)

        let secrets = FakeSecretStore()
        let loaded = await ConfigStore(fileURL: url, secrets: secrets).load()
        XCTAssertEqual(loaded.ai.apiKey, "legacy-ai-key")
        XCTAssertEqual(loaded.ai.savedEndpoints.first?.apiKey, "legacy-ep-key")
        XCTAssertEqual(secrets.get(.aiAPIKey), "legacy-ai-key")
        XCTAssertEqual(secrets.get(.savedEndpointAPIKey("ep-9")), "legacy-ep-key")

        let onDisk = try String(contentsOf: url, encoding: .utf8)
        XCTAssertFalse(onDisk.contains("legacy-ai-key"))
        XCTAssertFalse(onDisk.contains("legacy-ep-key"))
    }

    /// The documented fallback: when the Keychain is unavailable (an unsigned
    /// sideload with no App Group), the cookie stays in the JSON rather than
    /// being dropped — the feature keeps working, just less privately.
    func testCookieFallsBackToPlaintextWhenSecretStoreUnavailable() async throws {
        let url = try tempConfigURL()
        let secrets = FakeSecretStore(available: false)
        let store = ConfigStore(fileURL: url, secrets: secrets)

        var config = await store.load()
        config.apiKeys.linkedInCookie = "fallback_cookie"
        try await store.save(config)

        let onDisk = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(onDisk.contains("fallback_cookie"))

        let reloaded = await ConfigStore(fileURL: url, secrets: secrets).load()
        XCTAssertEqual(reloaded.apiKeys.linkedInCookie, "fallback_cookie")
    }
}

/// In-memory `SecretStore`. The real Keychain can't be exercised from a hostless
/// test bundle: those load into the shared `xctest` process, which has no
/// application-identifier entitlement, so `SecItemAdd` returns
/// errSecMissingEntitlement. `available: false` reproduces exactly that.
final class FakeSecretStore: SecretStore, @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [SecretKey: String] = [:]
    private let available: Bool

    init(available: Bool = true) { self.available = available }

    func get(_ key: SecretKey) -> String? {
        lock.lock(); defer { lock.unlock() }
        return available ? storage[key] : nil
    }

    @discardableResult
    func set(_ value: String, for key: SecretKey) -> Bool {
        guard available else { return false }
        lock.lock(); defer { lock.unlock() }
        if value.isEmpty { storage[key] = nil } else { storage[key] = value }
        return true
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
