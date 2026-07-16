import XCTest
@testable import JobsmithKit

/// Regression tests for the on-device routing bugs: a tier assigned to
/// Apple's on-device model must actually stay on-device — no silent endpoint
/// fallback in the router, no retry escaping to the cloud strong model, and
/// no settings-sync import quietly reverting the sentinel to another device's
/// endpoint model.
final class OnDeviceRoutingTests: XCTestCase {

    /// Deterministic engine: replays a script of results, records every request.
    final class ScriptedEngine: AIEngine, @unchecked Sendable {
        private let lock = NSLock()
        private var script: [Result<String, Error>]
        private(set) var recorded: [CompletionRequest] = []

        init(_ script: [Result<String, Error>]) { self.script = script }

        var requests: [CompletionRequest] {
            lock.lock(); defer { lock.unlock() }
            return recorded
        }

        func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
            lock.lock(); defer { lock.unlock() }
            recorded.append(req)
            guard !script.isEmpty else { return "{\"score\": 1, \"reasoning\": \"\"}" }
            return try script.removeFirst().get()
        }

        func listModels(config: AIConfig) async throws -> [String] { [] }
    }

    private func onDeviceConfig() -> AppConfig {
        var config = AppConfig()
        config.ai.fastModel = AIConfig.onDeviceModelID
        config.ai.strongModel = "cloud-strong"
        return config
    }

    private func job() -> Job {
        Job(from: NormalizedJob(source: "greenhouse", externalId: "1",
                                title: "Engineer", company: "Acme"))
    }

    // MARK: EngineRouter

    func testOnDeviceTierNeverFallsBackToEndpoint() async {
        let endpoint = ScriptedEngine([])
        let onDevice = ScriptedEngine([.failure(AIEngineError.unreachable("Apple Intelligence is turned off"))])
        let router = EngineRouter(endpoint: endpoint, onDevice: onDevice)
        let req = CompletionRequest(user: "p", tier: .fast, temperature: 0.7, maxTokens: 100)

        do {
            _ = try await router.complete(req, config: onDeviceConfig().ai)
            XCTFail("expected the on-device error to propagate")
        } catch let error as AIEngineError {
            guard case .unreachable(let detail) = error else {
                return XCTFail("unexpected error: \(error)")
            }
            XCTAssertTrue(detail.contains("Apple Intelligence"))
        } catch {
            XCTFail("unexpected error type: \(error)")
        }
        // The whole point: the user's endpoint must never see this request.
        XCTAssertTrue(endpoint.requests.isEmpty)
        XCTAssertEqual(onDevice.requests.count, 1)
    }

    func testEndpointTierRoutesToEndpoint() async throws {
        let endpoint = ScriptedEngine([.success("ok")])
        let onDevice = ScriptedEngine([])
        let router = EngineRouter(endpoint: endpoint, onDevice: onDevice)
        var config = AppConfig()
        config.ai.fastModel = "small-cloud"
        let req = CompletionRequest(user: "p", tier: .fast, temperature: 0.7, maxTokens: 100)

        let out = try await router.complete(req, config: config.ai)
        XCTAssertEqual(out, "ok")
        XCTAssertTrue(onDevice.requests.isEmpty)
    }

    // MARK: ScoringService

    func testScoringRetryStaysOnDevice() async throws {
        // First call fails (non-transient), retry succeeds — and must stay on
        // the fast tier when that tier is on-device, not escalate to .strong
        // (the cloud resume model).
        let engine = ScriptedEngine([
            .failure(AIEngineError.emptyResponse),
            .success("{\"score\": 77, \"reasoning\": \"retry\"}"),
        ])
        let result = try await ScoringService.score(job: job(), profile: Profile(),
                                                    config: onDeviceConfig(), engine: engine)
        XCTAssertEqual(result.score, 77)
        XCTAssertEqual(engine.requests.map(\.tier), [.fast, .fast])
    }

    func testScoringRetryEscalatesForEndpointTiers() async throws {
        let engine = ScriptedEngine([
            .failure(AIEngineError.emptyResponse),
            .success("{\"score\": 55, \"reasoning\": \"retry\"}"),
        ])
        var config = AppConfig()
        config.ai.fastModel = "small-cloud"
        config.ai.strongModel = "big-cloud"
        let result = try await ScoringService.score(job: job(), profile: Profile(),
                                                    config: config, engine: engine)
        XCTAssertEqual(result.score, 55)
        XCTAssertEqual(engine.requests.map(\.tier), [.fast, .strong])
    }

    func testRefusalSkipsRetryAndSurfacesAsRefused() async {
        let engine = ScriptedEngine([
            .failure(AIEngineError.refused("declined by safety guardrails")),
        ])
        do {
            _ = try await ScoringService.score(job: job(), profile: Profile(),
                                               config: onDeviceConfig(), engine: engine)
            XCTFail("expected ScoringError.refused")
        } catch let error as ScoringError {
            guard case .refused = error else { return XCTFail("unexpected: \(error)") }
        } catch {
            XCTFail("unexpected error type: \(error)")
        }
        // Deterministic decline — exactly one call, no retry.
        XCTAssertEqual(engine.requests.count, 1)
    }

    // MARK: SettingsSync sentinel protection

    private func configDict(fast: String, strong: String = "cloud-strong") -> [String: JSONValue] {
        ["ai": .object(["fastModel": .string(fast), "strongModel": .string(strong)])]
    }

    func testImportCannotOverwriteOnDeviceSentinel() {
        var config = configDict(fast: AIConfig.onDeviceModelID)
        SettingsSync.apply(&config, path: "ai.models.fast", value: .string("desktop-model"))
        guard case .object(let ai)? = config["ai"] else { return XCTFail("no ai section") }
        XCTAssertEqual(ai["fastModel"], .string(AIConfig.onDeviceModelID),
                       "an imported endpoint model must not undo the on-device choice")
        // The strong tier is NOT pinned — imports apply normally.
        var config2 = configDict(fast: AIConfig.onDeviceModelID)
        SettingsSync.apply(&config2, path: "ai.models.strong", value: .string("desktop-model"))
        guard case .object(let ai2)? = config2["ai"] else { return XCTFail("no ai section") }
        XCTAssertEqual(ai2["strongModel"], .string("desktop-model"))
    }

    func testDeviceLocalPathDetection() {
        let pinned = configDict(fast: AIConfig.onDeviceModelID)
        XCTAssertTrue(SettingsSync.isDeviceLocal("ai.models.fast", config: pinned))
        XCTAssertFalse(SettingsSync.isDeviceLocal("ai.models.strong", config: pinned))
        XCTAssertFalse(SettingsSync.isDeviceLocal("ai.temperature", config: pinned))
        let unpinned = configDict(fast: "small-cloud")
        XCTAssertFalse(SettingsSync.isDeviceLocal("ai.models.fast", config: unpinned))
    }
}
