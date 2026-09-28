import XCTest
@testable import JobsmithKit

/// Setup Assistant (step 0) plumbing: provider presets in step with desktop,
/// the plain-English error mapper (desktop `describe_ai_error` twin), the
/// onboardingComplete migration, URL clean-up, and the 1-token ping. Offline:
/// the ping goes through a stub URLProtocol.
final class SetupAssistantTests: XCTestCase {

    // MARK: Presets

    /// #filePath is ios-standalone/KitTests/<this file>: three pops reach the repo root.
    private var desktopProvidersJSON: URL {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<3 { url.deleteLastPathComponent() }
        return url.appendingPathComponent("backend/ai_providers.json")
    }

    func testPresetsMatchDesktop() throws {
        let data = try Data(contentsOf: desktopProvidersJSON)
        let rows = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [[String: String]])
        XCTAssertEqual(AIProviderPreset.all.map(\.name), rows.map { $0["name"] ?? "" })
        XCTAssertEqual(AIProviderPreset.all.map(\.baseURL), rows.map { $0["base_url"] ?? "" })
        XCTAssertEqual(AIProviderPreset.all.map(\.keyURL), rows.map { $0["key_url"] ?? "" })
        XCTAssertEqual(AIProviderPreset.all.count, 12)
    }

    func testURLNormalisation() {
        XCTAssertEqual(AIProviderPreset.normalize("rig.local/v1/chat/completions/"), "https://rig.local/v1")
        XCTAssertEqual(AIProviderPreset.normalize(" http://192.0.2.5:1234/v1/ "), "http://192.0.2.5:1234/v1")
        XCTAssertEqual(AIProviderPreset.normalize(""), "")
        XCTAssertTrue(AIProviderPreset.lacksV1("http://192.0.2.5:1234"))
        XCTAssertFalse(AIProviderPreset.lacksV1("https://lmstudio.example/v1"))
        XCTAssertFalse(AIProviderPreset.lacksV1(""))
    }

    func testNonChatFilter() {
        for id in ["text-embedding-3-small", "whisper-1", "tts-1", "x/rerank-v2", "omni-moderation", "dall-e-3", "gpt-image-1"] {
            XCTAssertTrue(AIProviderPreset.isNonChat(id), id)
        }
        XCTAssertFalse(AIProviderPreset.isNonChat("meta-llama/llama-3.3-70b-instruct:free"))
    }

    // MARK: Error mapper (same codes + wording as desktop)

    func testErrorMapper() {
        let base = "https://api.example.com/v1"
        func d(_ e: Error, onDevice: Bool = false) -> String {
            let r = AIErrorMapper.describe(e, baseURL: base, onDevice: onDevice)
            return "\(r.code)|\(r.message)"
        }
        XCTAssertEqual(d(AIEngineError.httpStatus(401, "")), "auth|That API key was rejected")
        XCTAssertEqual(d(AIEngineError.httpStatus(403, "")), "auth|That API key was rejected")
        XCTAssertEqual(d(AIEngineError.httpStatus(404, "")), "model|That model is not available on this account")
        XCTAssertEqual(d(AIEngineError.httpStatus(400, #"{"error":{"code":"model_not_found"}}"#)),
                       "model|That model is not available on this account")
        XCTAssertEqual(d(AIEngineError.httpStatus(402, "")), "credit|Your provider account has no credit")
        XCTAssertEqual(d(AIEngineError.httpStatus(429, #"{"error":{"code":"insufficient_quota"}}"#)),
                       "credit|Your provider account has no credit")
        XCTAssertEqual(d(AIEngineError.httpStatus(429, "")), "rate_limit|The provider is rate-limiting; try again in a minute")
        XCTAssertEqual(d(AIEngineError.unreachable("dns")), "unreachable|Could not reach the server at api.example.com")
        XCTAssertEqual(d(AIEngineError.interrupted("timeout")), "unreachable|Could not reach the server at api.example.com")
        XCTAssertEqual(d(AIEngineError.invalidBaseURL("")), "no_url|Enter the server address first")
        XCTAssertEqual(d(AIEngineError.unreachable("Apple Intelligence is turned off"), onDevice: true),
                       "unavailable|Could not reach the server: Apple Intelligence is turned off")
    }

    // MARK: onboardingComplete + new-install defaults

    private func decode(_ config: AppConfig, dropping key: String) throws -> AppConfig {
        var obj = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(config)) as? [String: Any])
        obj.removeValue(forKey: key)
        return try JSONDecoder().decode(AppConfig.self, from: JSONSerialization.data(withJSONObject: obj))
    }

    func testOnboardingCompleteMigration() throws {
        var existing = AppConfig()
        existing.profile.fullName = "Existing User"
        XCTAssertTrue(try decode(existing, dropping: "onboardingComplete").onboardingComplete,
                      "an upgraded install with a profile is not re-prompted")
        XCTAssertFalse(try decode(AppConfig(), dropping: "onboardingComplete").onboardingComplete,
                       "a fresh install sees the wizard")
        existing.onboardingComplete = false
        let roundTrip = try JSONDecoder().decode(AppConfig.self, from: JSONEncoder().encode(existing))
        XCTAssertFalse(roundTrip.onboardingComplete, "an explicit value wins over the migration")
    }

    func testNewInstallHasNoLocalhostDefault() {
        XCTAssertEqual(AIConfig().baseURL, "")
        XCTAssertEqual(AIConfig().provider, "")
        XCTAssertEqual(AppConfig().setupMode, "")
    }

    func testProviderAndSetupModeRoundTrip() throws {
        var c = AppConfig()
        c.ai.provider = "OpenRouter"
        c.setupMode = "cloud"
        let back = try JSONDecoder().decode(AppConfig.self, from: JSONEncoder().encode(c))
        XCTAssertEqual(back.ai.provider, "OpenRouter")
        XCTAssertEqual(back.setupMode, "cloud")
    }

    // MARK: pingChat

    override func tearDown() {
        URLProtocol.unregisterClass(PingStub.self)
        PingStub.status = 200
        PingStub.lastBody = nil
        super.tearDown()
    }

    func testPingChatSendsOneTokenToTheChosenModel() async throws {
        URLProtocol.registerClass(PingStub.self)
        var ai = AIConfig(baseURL: "https://stub.invalid/v1", apiKey: "k")
        ai.strongModel = "something-else"
        try await OpenAICompatibleEngine().pingChat(model: "picked/model:free", config: ai)
        let body = try XCTUnwrap(PingStub.lastBody)
        XCTAssertEqual(body["model"] as? String, "picked/model:free")
        XCTAssertEqual(body["max_tokens"] as? Int, 1)
        XCTAssertEqual((body["messages"] as? [[String: String]])?.first?["content"], "ping")
    }

    func testPingChatThrowsTypedHTTPError() async {
        URLProtocol.registerClass(PingStub.self)
        PingStub.status = 401
        do {
            try await OpenAICompatibleEngine().pingChat(model: "m", config: AIConfig(baseURL: "https://stub.invalid/v1"))
            XCTFail("expected a 401")
        } catch {
            XCTAssertEqual(AIErrorMapper.describe(error, baseURL: "https://stub.invalid/v1").code, "auth")
        }
    }

    func testRouterSendsTheSentinelOnDevice() async throws {
        let endpoint = MockAIEngine(), device = MockAIEngine()
        let router = EngineRouter(endpoint: endpoint, onDevice: device)
        try await router.pingChat(model: AIConfig.onDeviceModelID, config: AIConfig())
        try await router.pingChat(model: "cloud-model", config: AIConfig())
        XCTAssertEqual(device.pingedModels, [AIConfig.onDeviceModelID])
        XCTAssertEqual(endpoint.pingedModels, ["cloud-model"])
    }
}

/// Answers every request with `status` and records the JSON body.
final class PingStub: URLProtocol {
    nonisolated(unsafe) static var status = 200
    nonisolated(unsafe) static var lastBody: [String: Any]?

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "stub.invalid" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var data = request.httpBody
        if data == nil, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buf = [UInt8](repeating: 0, count: 65536)
            let n = stream.read(&buf, maxLength: buf.count)
            data = Data(buf.prefix(max(n, 0)))
        }
        if let data { Self.lastBody = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] }
        let response = HTTPURLResponse(url: request.url!, statusCode: Self.status, httpVersion: "HTTP/1.1", headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(#"{"choices":[{"message":{"content":"p"}}]}"#.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

/// Résumé chunking for Apple's 8,000-character input cap — same rule as desktop
/// tests/test_setup_assistant.py::test_resume_chunked_for_apple_8k_engine.
final class ResumeChunkingTests: XCTestCase {
    static func longResume() -> String {
        var roles: [String] = []
        for i in 0..<12 {
            let bullets = (0..<9).map { "- Delivered outcome \(i).\($0) " + String(repeating: "x", count: 160) }.joined(separator: "\n")
            roles.append("Engineer \(i) at Company\(i)\n2010 - 2012\n\(bullets)\n")
        }
        return "Jane Real\njane@real.dev\n\nSUMMARY\nBuilds things.\n\nEXPERIENCE\n" + roles.joined(separator: "\n")
            + "\nEDUCATION\nBSc Computer Science, State University, 2009\n\nSKILLS\nPython, Go, SQL\n"
    }

    /// Rejects any prompt over 8,000 characters, like the real on-device model.
    final class Fake8k: AIEngine, @unchecked Sendable {
        var sizes: [Int] = []
        func listModels(config: AIConfig) async throws -> [String] { [] }
        func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
            sizes.append(req.user.count)
            if req.user.count > 8000 { throw AIEngineError.refused("too long") }
            var out: [String: Any] = ["experience": [[String: String]](), "education": [[String: String]](), "skills": [String]()]
            if req.user.contains("jane@real.dev") { out["full_name"] = "Jane Real"; out["email"] = "jane@real.dev" }
            out["experience"] = (0..<12).filter { req.user.contains("Engineer \($0) at Company\($0)") }
                .map { ["title": "Engineer \($0)", "company": "Company\($0)"] }
            if req.user.contains("State University") {
                out["education"] = [["degree": "BSc Computer Science", "school": "State University"]]
            }
            if req.user.contains("Python, Go") { out["skills"] = ["Python", "Go", "python"] }
            return String(data: try JSONSerialization.data(withJSONObject: out), encoding: .utf8)!
        }
    }

    func testTwentyThousandCharsThroughAnEightThousandCharEngine() async {
        let text = Self.longResume()
        XCTAssertGreaterThanOrEqual(text.count, 20000)
        var config = AppConfig()
        config.ai.strongModel = AIConfig.onDeviceModelID
        let engine = Fake8k()
        let result = await ResumeProfileParser.parse(text: text, config: config, engine: engine)
        XCTAssertGreaterThanOrEqual(engine.sizes.count, 3)
        XCTAssertLessThanOrEqual(engine.sizes.max() ?? 0, 8000)
        XCTAssertEqual(result.profile.fullName, "Jane Real")
        XCTAssertEqual(result.profile.experience.map(\.title), (0..<12).map { "Engineer \($0)" })
        XCTAssertEqual(result.profile.education.first?.school, "State University")
        XCTAssertEqual(result.profile.skills, ["Python", "Go"])
        XCTAssertFalse(result.warnings.contains { $0.contains("only the first part") })
    }

    func testChunksRespectTheLimit() {
        let chunks = ResumeProfileParser.chunk(Self.longResume(), limit: 7000)
        XCTAssertTrue(chunks.allSatisfy { $0.count <= 7000 })
        XCTAssertEqual(chunks.joined().components(separatedBy: "Engineer 11 at Company11").count, 2)
    }

    struct Off: AIEngine {
        func listModels(config: AIConfig) async throws -> [String] { [] }
        func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
            throw AIEngineError.unreachable("Apple Intelligence is turned off")
        }
    }

    func testParseFailureSurfacesTheRealError() async {
        var config = AppConfig()
        config.ai.strongModel = AIConfig.onDeviceModelID
        let result = await ResumeProfileParser.parse(text: "Jane\njane@x.dev", config: config, engine: Off())
        XCTAssertTrue(result.profile.isEmpty)
        XCTAssertTrue(result.warnings.first?.contains("Apple Intelligence is turned off") == true, result.warnings.first ?? "")
    }
}
