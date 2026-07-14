import Foundation
import GRDB

public struct FetchProgress: Sendable, Equatable {
    public var sourcesTotal: Int
    public var sourcesDone: Int
    public var jobsFound: Int
    public var detail: String
    public var blocked: [String] = []
    public var timedOut: [String] = []
    public var failed: [String] = []
    public var suspect: [String] = []
    /// Sources that rejected our credentials (401/403) — a wrong API key, not
    /// an empty board.
    public var authFailed: [String] = []
    /// Sources cut off mid-fetch (suspension, cancellation, dropped network).
    /// Not an error: their partial results are saved and the run is resumable.
    public var interrupted: [String] = []
    /// Raw jobs each finished source returned (pre-dedup, pre-filter). Populated
    /// incrementally so the UI can show "Greenhouse — 104 found" live.
    public var perSourceFound: [String: Int] = [:]
    /// How many of a source's raw jobs its global filters (keywords, age,
    /// salary, location) then dropped. `found − filtered` is what it contributed
    /// to the shortlist, before cross-source dedup.
    public var perSourceFiltered: [String: Int] = [:]
}

public struct FetchSummary: Sendable, Equatable {
    /// Jobs each source returned (pre-dedup/filter). 0 for failed sources.
    public var perSource: [String: Int] = [:]
    public var blocked: [String] = []
    public var timedOut: [String] = []
    public var failed: [String] = []
    /// Sources that rejected our credentials (401/403). Kept apart from `failed`
    /// so the UI can say "check the API key" instead of "had trouble" — and so a
    /// bad key can never masquerade as "no jobs today".
    public var authFailed: [String] = []
    /// Sources that have returned jobs before but now hit 3+ consecutive
    /// zero-job runs — their parser or API may have silently broken.
    public var suspect: [String] = []
    /// Sources cut off mid-fetch by the app being suspended, the task being
    /// cancelled, or the network dropping. Whatever they checkpointed is already
    /// saved and the run resumes from their cursor, so this is *not* an error to
    /// show the user — it's the "still to finish" list.
    public var interrupted: [String] = []
    public var inserted = 0
    public var updated = 0
    public init() {}

    /// True when the run stopped short and still has sources left to finish.
    public var isIncomplete: Bool { !interrupted.isEmpty }
}

/// Runs job sources concurrently, each under its own timeout, then dedups,
/// filters, and upserts — the Swift twin of Python `fetch_all_jobs`. A single
/// source failing, timing out, or being bot-blocked doesn't affect the others.
///
/// Results are persisted **as each source delivers them**, not pooled until every
/// source has finished. That is what makes a long search survivable on iOS: the
/// app gets roughly 30 seconds of background execution after the user leaves,
/// while LinkedIn alone budgets minutes. When the window closes mid-run,
/// everything already checkpointed is in the database and the run resumes from
/// its cursor instead of being thrown away.
public actor FetchPipeline {
    struct SourceTimeoutError: Error {}

    static let zeroStreakThreshold = 3

    private var continuations: [UUID: AsyncStream<FetchProgress>.Continuation] = [:]

    // Per-run accumulators. A FetchPipeline is constructed per run; these back
    // both the live progress events and the final summary, which must agree even
    // when the run is cut short.
    private var dedup = IncrementalDeduplicator()
    /// source → sync keys it delivered (pre-filter). Counting distinct keys
    /// rather than deliveries keeps the tally honest when LinkedIn re-delivers a
    /// job to attach its description.
    private var foundKeys: [String: Set<String>] = [:]
    /// source → sync keys that survived the global filters.
    private var keptKeys: [String: Set<String>] = [:]
    private var inserted = 0
    private var updated = 0
    private var sourcesTotal = 0
    private var doneCount = 0
    private var blocked: [String] = []
    private var timedOut: [String] = []
    private var failed: [String] = []
    private var authFailed: [String] = []
    private var interrupted: [String] = []
    private var suspect: [String] = []

    public init() {}

    /// Progress events for UI. Each call returns an independent stream; the
    /// stream finishes when `run` completes.
    public func progressUpdates() -> AsyncStream<FetchProgress> {
        AsyncStream { continuation in
            let id = UUID()
            continuations[id] = continuation
            continuation.onTermination = { [weak self] _ in
                Task { await self?.removeContinuation(id) }
            }
        }
    }

    private func removeContinuation(_ id: UUID) {
        continuations.removeValue(forKey: id)
    }

    private func emit(_ detail: String) {
        let progress = FetchProgress(
            sourcesTotal: sourcesTotal, sourcesDone: doneCount,
            jobsFound: foundKeys.values.reduce(0) { $0 + $1.count },
            detail: detail,
            blocked: blocked, timedOut: timedOut, failed: failed, suspect: suspect,
            authFailed: authFailed, interrupted: interrupted,
            perSourceFound: foundKeys.mapValues(\.count),
            perSourceFiltered: foundKeys.reduce(into: [:]) { out, entry in
                out[entry.key] = entry.value.count - (keptKeys[entry.key]?.count ?? 0)
            })
        for continuation in continuations.values { continuation.yield(progress) }
    }

    private func finishStreams() {
        for continuation in continuations.values { continuation.finish() }
        continuations.removeAll()
    }

    /// Fetch from `sources` (empty = all registered), applying global filters and
    /// dedup and upserting into `jobStore` as results arrive.
    ///
    /// `timeoutBudget` caps every source's own timeout — a `BGAppRefreshTask`
    /// gets roughly 30 seconds total, far less than the 60–300s some sources
    /// allow themselves, so the background tiers pass a budget rather than
    /// letting iOS kill the task mid-write.
    ///
    /// `runStore`/`runID` and `cursors` carry a resumable run: each cursor is
    /// handed back to the source that produced it, and every checkpoint updates
    /// the run record so a later attempt picks up where this one stopped.
    public func run(config: AppConfig, sources: [String] = [],
                    jobStore: JobStore,
                    timeoutBudget: Duration? = nil,
                    runStore: SearchRunStore? = nil,
                    runID: String? = nil,
                    cursors: [String: String] = [:]) async -> FetchSummary {
        let requested = Set(sources.map { $0.lowercased() })
        let ids = SourceRegistry.allIDs.filter { requested.isEmpty || requested.contains($0) }
        sourcesTotal = ids.count

        emit("Fetching from \(ids.count) sources in parallel (\(ids.joined(separator: ", ")))...")

        await withTaskGroup(of: (String, Result<Void, Error>).self) { group in
            for id in ids {
                guard let source = SourceRegistry.source(for: id) else { continue }
                let knownIDs = Self.knownIDs(for: id, jobStore: jobStore)
                let declared = type(of: source).timeout
                let timeout = timeoutBudget.map { min(declared, $0) } ?? declared
                let cursor = cursors[id]

                group.addTask { [weak self] in
                    do {
                        try await Self.withTimeout(timeout) {
                            _ = try await source.fetchJobs(
                                config: config, knownExternalIDs: knownIDs,
                                resumeCursor: cursor
                            ) { jobs, cursor in
                                await self?.deliver(source: id, jobs: jobs, cursor: cursor,
                                                    config: config, jobStore: jobStore,
                                                    runStore: runStore, runID: runID)
                            }
                        }
                        return (id, .success(()))
                    } catch {
                        // A source the *caller* cancelled — the background window
                        // closing, the user hitting Stop — is interrupted, not
                        // broken: its checkpoints are saved and it gets resumed.
                        // The pipeline's own timeout is a genuine timeout.
                        if !(error is SourceTimeoutError),
                           Task.isCancelled || TransientNetwork.isTransient(error) {
                            return (id, .failure(SourceInterruptedError(String(describing: error))))
                        }
                        return (id, .failure(error))
                    }
                }
            }

            for await (name, result) in group {
                finish(source: name, result: result, jobStore: jobStore,
                       runStore: runStore, runID: runID)
            }
        }

        emit(interrupted.isEmpty
             ? "Saved \(inserted) new jobs"
             : "Paused after \(inserted) new jobs — \(interrupted.count) source(s) left")
        finishStreams()

        var summary = FetchSummary()
        summary.perSource = foundKeys.mapValues(\.count)
        for id in ids where summary.perSource[id] == nil { summary.perSource[id] = 0 }
        summary.blocked = blocked
        summary.timedOut = timedOut
        summary.failed = failed
        summary.authFailed = authFailed
        summary.interrupted = interrupted
        summary.suspect = suspect
        summary.inserted = inserted
        summary.updated = updated
        return summary
    }

    /// Persist one source's incremental delivery. Runs on the actor, so the
    /// dedup state and counters stay serialized across concurrent sources.
    private func deliver(source: String, jobs: [NormalizedJob], cursor: String?,
                         config: AppConfig, jobStore: JobStore,
                         runStore: SearchRunStore?, runID: String?) {
        // Per-source tallies are computed on the source's OWN batch, before
        // cross-source dedup, so they stay attributable to it: a job dropped
        // because another board got there first is not this source's filter loss.
        for job in jobs {
            foundKeys[source, default: []].insert(Self.syncKey(job))
        }
        for job in JobFilters.applyGlobalFilters(jobs, search: config.search) {
            keptKeys[source, default: []].insert(Self.syncKey(job))
        }

        // Dedup, then filter — the order the pooled version used.
        let admitted = dedup.admit(jobs)
        let toUpsert = JobFilters.applyGlobalFilters(admitted, search: config.search)
        var insertedNow = 0
        if !toUpsert.isEmpty, let result = try? jobStore.upsert(toUpsert) {
            insertedNow = result.inserted
            inserted += result.inserted
            updated += result.updated
        }

        if let runStore, let runID {
            try? runStore.saveCursor(id: runID, source: source, cursor: cursor)
            try? runStore.addInserted(id: runID, delta: insertedNow)
        }

        emit("\(doneCount)/\(sourcesTotal) sources done")
    }

    /// Record a source's terminal outcome.
    private func finish(source name: String, result: Result<Void, Error>, jobStore: JobStore,
                        runStore: SearchRunStore?, runID: String?) {
        doneCount += 1
        var reachedEnd = true
        switch result {
        case .success:
            let count = foundKeys[name]?.count ?? 0
            let streak = recordSourceResult(name, count: count, jobStore: jobStore)
            if streak >= Self.zeroStreakThreshold { suspect.append(name) }
        case .failure(let error):
            switch error {
            case is SourceInterruptedError:
                interrupted.append(name)
                reachedEnd = false
            case is SourceTimeoutError: timedOut.append(name)
            case is SourceBlockedError: blocked.append(name)
            case is SourceAuthError: authFailed.append(name)
            default: failed.append(name)
            }
        }
        // Only a source that reached a terminal state is struck off the run. An
        // interrupted one stays on the list, so the resume picks it back up.
        if reachedEnd, let runStore, let runID {
            try? runStore.markSourceComplete(id: runID, source: name)
        }
        emit(doneCount == sourcesTotal
             ? "All \(sourcesTotal) sources finished"
             : "\(doneCount)/\(sourcesTotal) sources done")
    }

    private static func syncKey(_ job: NormalizedJob) -> String {
        job.source + ":" + job.externalId
    }

    /// Postings a source already holds, so it can skip re-fetching them.
    ///
    /// Greenhouse emits both greenhouse and lever records, so it consumes both
    /// key spaces. LinkedIn uses this to skip the detail-page scrape — by far its
    /// slowest and most 429-prone phase — but only for jobs whose description it
    /// actually captured: a posting whose detail fetch was cut short is stored
    /// with an empty description and must stay eligible for another try.
    private static func knownIDs(for id: String, jobStore: JobStore) -> Set<String> {
        switch id {
        case GreenhouseSource.id:
            return ((try? jobStore.knownExternalIDs(source: "greenhouse")) ?? [])
                .union((try? jobStore.knownExternalIDs(source: "lever")) ?? [])
        case LinkedInSource.id:
            return (try? jobStore.externalIDsWithDescription(source: "linkedin")) ?? []
        default:
            return []
        }
    }

    /// Persist a source's run outcome to the source_stats table and return
    /// its consecutive zero-job streak. Sources that have never returned jobs
    /// report a streak of 0 — an unconfigured source is not a broken one.
    private func recordSourceResult(_ name: String, count: Int, jobStore: JobStore) -> Int {
        let now = ISO8601DateFormatter().string(from: Date())
        let result = try? jobStore.db.writer.write { db -> Int in
            let row = try Row.fetchOne(
                db, sql: "SELECT consecutiveZero, everReturned FROM source_stats WHERE source = ?",
                arguments: [name])
            var zeroStreak = 0
            var everReturned = false
            if count > 0 {
                zeroStreak = 0
                everReturned = true
            } else {
                var prevZero = 0
                if let row {
                    let storedZero: Int? = row["consecutiveZero"]
                    let storedEver: Bool? = row["everReturned"]
                    prevZero = storedZero ?? 0
                    everReturned = storedEver ?? false
                }
                zeroStreak = prevZero + 1
            }
            try db.execute(sql: """
                INSERT INTO source_stats (source, lastCount, consecutiveZero, everReturned, lastRun)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    lastCount = excluded.lastCount,
                    consecutiveZero = excluded.consecutiveZero,
                    everReturned = excluded.everReturned,
                    lastRun = excluded.lastRun
                """, arguments: [name, count, zeroStreak, everReturned, now])
            return everReturned ? zeroStreak : 0
        }
        return result ?? 0
    }

    /// Race an operation against a deadline; loser is cancelled.
    static func withTimeout<T: Sendable>(
        _ timeout: Duration,
        _ operation: @escaping @Sendable () async throws -> T
    ) async throws -> T {
        try await withThrowingTaskGroup(of: T.self) { group in
            group.addTask { try await operation() }
            group.addTask {
                try await Task.sleep(for: timeout)
                throw SourceTimeoutError()
            }
            defer { group.cancelAll() }
            return try await group.next()!
        }
    }
}
