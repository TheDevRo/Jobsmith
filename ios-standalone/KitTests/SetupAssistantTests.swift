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
