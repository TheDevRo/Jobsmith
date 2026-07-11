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
    /// Sources that have returned jobs before but now hit 3+ consecutive
    /// zero-job runs — their parser or API may have silently broken.
    public var suspect: [String] = []
    public var inserted = 0
    public var updated = 0
    public init() {}
}

/// Runs job sources concurrently, each under its own timeout, then dedups,
/// filters, and upserts — the Swift twin of Python `fetch_all_jobs`. A single
/// source failing, timing out, or being bot-blocked doesn't affect the others.
public actor FetchPipeline {
    struct SourceTimeoutError: Error {}

    static let zeroStreakThreshold = 3

    private var continuations: [UUID: AsyncStream<FetchProgress>.Continuation] = [:]

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

    private func emit(_ progress: FetchProgress) {
        for continuation in continuations.values { continuation.yield(progress) }
    }

    private func finishStreams() {
        for continuation in continuations.values { continuation.finish() }
        continuations.removeAll()
    }

    /// Fetch from `sources` (empty = all registered), apply global filters
    /// and dedup, and upsert into `jobStore`.
    public func run(config: AppConfig, sources: [String] = [],
                    jobStore: JobStore) async -> FetchSummary {
        let requested = Set(sources.map { $0.lowercased() })
        let ids = SourceRegistry.allIDs.filter { requested.isEmpty || requested.contains($0) }

        var summary = FetchSummary()
        var collected: [NormalizedJob] = []
        var doneCount = 0
        // Running per-source tallies, surfaced on every progress event.
        var perSourceFound: [String: Int] = [:]
        var perSourceFiltered: [String: Int] = [:]

        emit(FetchProgress(sourcesTotal: ids.count, sourcesDone: 0, jobsFound: 0,
                           detail: "Fetching from \(ids.count) sources in parallel (\(ids.joined(separator: ", ")))..."))

        await withTaskGroup(of: (String, Result<[NormalizedJob], Error>).self) { group in
            for id in ids {
                guard let source = SourceRegistry.source(for: id) else { continue }
                // Only greenhouse consumes known ids (it emits both greenhouse
                // and lever records) — mirrors Python's known_ids plumbing.
                let knownIDs: Set<String>
                if id == GreenhouseSource.id {
                    knownIDs = ((try? jobStore.knownExternalIDs(source: "greenhouse")) ?? [])
                        .union((try? jobStore.knownExternalIDs(source: "lever")) ?? [])
                } else {
                    knownIDs = []
                }
                let timeout = type(of: source).timeout
                group.addTask {
                    do {
                        let jobs = try await Self.withTimeout(timeout) {
                            try await source.fetchJobs(config: config, knownExternalIDs: knownIDs)
                        }
                        return (id, .success(jobs))
                    } catch {
                        return (id, .failure(error))
                    }
                }
            }

            for await (name, result) in group {
                doneCount += 1
                switch result {
                case .success(let jobs):
                    summary.perSource[name] = jobs.count
                    collected += jobs
                    perSourceFound[name] = jobs.count
                    // Per-source filtered count: how many of THIS source's jobs
                    // the global filters drop. Computed per source (not from the
                    // merged pool) so the count is attributable and reliable;
                    // cross-source dedup is reported separately at the end.
                    let kept = JobFilters.applyGlobalFilters(jobs, search: config.search).count
                    perSourceFiltered[name] = jobs.count - kept
                    let streak = recordSourceResult(name, count: jobs.count, jobStore: jobStore)
                    if streak >= Self.zeroStreakThreshold { summary.suspect.append(name) }
                case .failure(let error):
                    summary.perSource[name] = 0
                    if error is SourceTimeoutError { summary.timedOut.append(name) }
                    else if error is SourceBlockedError { summary.blocked.append(name) }
                    else { summary.failed.append(name) }
                }
                emit(FetchProgress(sourcesTotal: ids.count, sourcesDone: doneCount,
                                   jobsFound: collected.count,
                                   detail: doneCount == ids.count
                                       ? "All \(ids.count) sources finished"
                                       : "\(doneCount)/\(ids.count) sources done",
                                   blocked: summary.blocked, timedOut: summary.timedOut,
                                   failed: summary.failed, suspect: summary.suspect,
                                   perSourceFound: perSourceFound,
                                   perSourceFiltered: perSourceFiltered))
            }
        }

        let unique = Deduplicator.dedupe(collected)
        let filtered = JobFilters.applyGlobalFilters(unique, search: config.search)
        if let upsert = try? jobStore.upsert(filtered) {
            summary.inserted = upsert.inserted
            summary.updated = upsert.updated
        }

        emit(FetchProgress(sourcesTotal: ids.count, sourcesDone: doneCount,
                           jobsFound: filtered.count,
                           detail: "Saved \(summary.inserted) new jobs",
                           blocked: summary.blocked, timedOut: summary.timedOut,
                           failed: summary.failed, suspect: summary.suspect,
                           perSourceFound: perSourceFound,
                           perSourceFiltered: perSourceFiltered))
        finishStreams()
        return summary
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
