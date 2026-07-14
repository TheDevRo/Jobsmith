import XCTest
@testable import JobsmithKit

/// An engine that fails on demand, so the two ways a scoring run can end early —
/// pause and abort — can be told apart without a network.
private final class FailingEngine: AIEngine, @unchecked Sendable {
    let error: Error
    var calls = 0

    init(_ error: Error) { self.error = error }

    func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
        calls += 1
        throw error
    }

    func listModels(config: AIConfig) async throws -> [String] { [] }
}

final class ScoringInterruptionTests: XCTestCase {
    private func job() -> Job {
        Job(from: NormalizedJob(source: "demo", externalId: "d-1", title: "Engineer",
                                company: "Acme", location: "Remote",
                                description: "Build things."))
    }

    /// The LM-Studio-left-home case, and the app-suspended-mid-call case. Neither
    /// says anything about the job or the model, so the run must be able to park
    /// and come back — not report a failure and abandon every job behind it.
    func testTransientFailureSurfacesAsInterrupted() async {
        let engine = FailingEngine(AIEngineError.interrupted("connection lost"))
        do {
            _ = try await ScoringService.score(job: job(), profile: Profile(),
                                               config: AppConfig(), engine: engine)
            XCTFail("expected ScoringError.interrupted")
        } catch let error as ScoringError {
            guard case .interrupted = error else {
                return XCTFail("expected .interrupted, got \(error)")
            }
        } catch {
            XCTFail("unexpected error: \(error)")
        }
        XCTAssertEqual(engine.calls, 1,
                       "a cut-off call gets no retry — the retry would die the same way")
    }

    /// A refused connection is a real, reportable problem: every remaining job
    /// would fail identically, so the run stops and says so. This is the behavior
    /// the resume work must NOT have softened.
    func testDeadEndpointStillAbortsWithEngineUnavailable() async {
        let engine = FailingEngine(AIEngineError.unreachable("Connection refused"))
        do {
            _ = try await ScoringService.score(job: job(), profile: Profile(),
                                               config: AppConfig(), engine: engine)
            XCTFail("expected ScoringError.engineUnavailable")
        } catch let error as ScoringError {
            guard case .engineUnavailable = error else {
                return XCTFail("expected .engineUnavailable, got \(error)")
            }
        } catch {
            XCTFail("unexpected error: \(error)")
        }
        XCTAssertEqual(engine.calls, 2, "a fatal failure still gets its one low-temperature retry")
    }

    /// Cancelling the batch (the Stop button, or the background window closing)
    /// reads as an interruption, not a fault.
    func testCancellationSurfacesAsInterrupted() async {
        let engine = FailingEngine(CancellationError())
        do {
            _ = try await ScoringService.score(job: job(), profile: Profile(),
                                               config: AppConfig(), engine: engine)
            XCTFail("expected ScoringError.interrupted")
        } catch let error as ScoringError {
            guard case .interrupted = error else {
                return XCTFail("expected .interrupted, got \(error)")
            }
        } catch {
            XCTFail("unexpected error: \(error)")
        }
    }

    /// A stored `0` means "we failed to score this", not "this is a bad job" —
    /// so an unscored job is one with no score *or* a zero. The resume worklist
    /// is derived from exactly this, which is why no separate progress record is
    /// needed to survive a suspension.
    func testUnscoredIsTheResumeWorklist() {
        var scored = job();  scored.fitScore = 82
        var zeroed = job();  zeroed.fitScore = 0
        let never = job()

        XCTAssertFalse(ScoreBatch.isUnscored(scored))
        XCTAssertTrue(ScoreBatch.isUnscored(zeroed))
        XCTAssertTrue(ScoreBatch.isUnscored(never))
        XCTAssertEqual(ScoreBatch.unscored([scored, zeroed, never]).count, 2)
    }

    /// The cap is a hard ceiling on calls per run, resumed or not.
    func testPlanRespectsCap() {
        let jobs = (0..<10).map { _ in job() }
        XCTAssertEqual(ScoreBatch.plan(candidates: jobs, cap: 3).count, 3)
        XCTAssertTrue(ScoreBatch.plan(candidates: jobs, cap: 0).isEmpty)
        XCTAssertTrue(ScoreBatch.plan(candidates: jobs, cap: -1).isEmpty)
    }
}
