import Foundation
import GRDB

/// A search that may need more than one attempt to finish.
///
/// iOS gives a backgrounded app roughly 30 seconds before suspending it, and a
/// LinkedIn search alone budgets minutes. Rather than lose the run, the pipeline
/// records what it still owes — which sources haven't reached a terminal state,
/// and how far into its pagination each one got — so a later attempt can carry
/// on. `remainingSources` is the work list; `cursors` is the bookmark.
public struct SearchRun: Sendable, Equatable {
    public enum State: String, Sendable {
        /// An attempt is in flight.
        case running
        /// Cut short with work left. The next attempt picks this up.
        case interrupted
        /// Every source reached a terminal state. Nothing left to do.
        case complete
    }

    public var id: String
    public var startedAt: Date
    public var state: State
    public var requestedSources: [String]
    public var completedSources: [String]
    /// source → that source's opaque resume cursor.
    public var cursors: [String: String]
    public var insertedSoFar: Int

    /// The sources this run still owes, in their original order.
    public var remainingSources: [String] {
        let done = Set(completedSources)
        return requestedSources.filter { !done.contains($0) }
    }

    public var isFinished: Bool { remainingSources.isEmpty }
}

/// Persistence for `SearchRun`. Device-local — a half-finished run is
/// meaningless on another device, so none of this syncs.
public struct SearchRunStore: Sendable {
    /// A run older than this is abandoned rather than resumed: its listings have
    /// gone stale and its cursor points into a search page that has since moved.
    /// Starting fresh is both cheaper and more correct.
    static let maxAge: TimeInterval = 24 * 3600

    let db: AppDatabase

    public init(_ db: AppDatabase) { self.db = db }

    /// Open a run over `sources`, superseding any unfinished one.
    ///
    /// Superseding rather than refusing is deliberate: the user tapping Search
    /// is a clear instruction to search *now*, and the stale run's results are
    /// already saved — only its unfinished tail is dropped, which the new run is
    /// about to cover anyway.
    @discardableResult
    public func begin(sources: [String]) throws -> SearchRun {
        let run = SearchRun(id: UUID().uuidString, startedAt: Date(), state: .running,
                            requestedSources: sources, completedSources: [],
                            cursors: [:], insertedSoFar: 0)
        try db.writer.write { dbc in
            try dbc.execute(sql: "UPDATE search_runs SET state = 'complete' WHERE state != 'complete'")
            try dbc.execute(sql: """
                INSERT INTO search_runs (id, startedAt, state, requestedSources,
                                         completedSources, cursors, insertedSoFar)
                VALUES (?, ?, ?, ?, '[]', '{}', 0)
                """, arguments: [run.id, Self.iso.string(from: run.startedAt),
                                 run.state.rawValue, Self.encode(sources)])
        }
        return run
    }

    /// The run still awaiting work, if there is one. Stale and already-finished
    /// runs are retired here rather than handed back to a caller that would only
    /// have to re-check them.
    public func activeRun() throws -> SearchRun? {
        try db.writer.write { dbc -> SearchRun? in
            guard let row = try Row.fetchOne(
                dbc, sql: "SELECT * FROM search_runs WHERE state != 'complete' ORDER BY startedAt DESC LIMIT 1"),
                  let run = Self.decode(row) else { return nil }

            if Date().timeIntervalSince(run.startedAt) > Self.maxAge || run.isFinished {
                try dbc.execute(sql: "UPDATE search_runs SET state = 'complete' WHERE id = ?",
                                arguments: [run.id])
                return nil
            }
            return run
        }
    }

    /// Strike a source off the run — it reached a terminal state (finished,
    /// failed, blocked, timed out) and will not be retried by a resume.
    public func markSourceComplete(id: String, source: String) throws {
        try db.writer.write { dbc in
            guard let row = try Row.fetchOne(dbc, sql: "SELECT * FROM search_runs WHERE id = ?",
                                             arguments: [id]),
                  var run = Self.decode(row) else { return }
            guard !run.completedSources.contains(source) else { return }
            run.completedSources.append(source)
            try dbc.execute(sql: "UPDATE search_runs SET completedSources = ? WHERE id = ?",
                            arguments: [Self.encode(run.completedSources), id])
        }
    }

    /// Bookmark how far `source` has got. A nil cursor clears any stored one —
    /// a source that reports no cursor has no partial position worth resuming.
    public func saveCursor(id: String, source: String, cursor: String?) throws {
        try db.writer.write { dbc in
            guard let row = try Row.fetchOne(dbc, sql: "SELECT * FROM search_runs WHERE id = ?",
                                             arguments: [id]),
                  var run = Self.decode(row) else { return }
            run.cursors[source] = cursor
            try dbc.execute(sql: "UPDATE search_runs SET cursors = ? WHERE id = ?",
                            arguments: [Self.encodeDict(run.cursors), id])
        }
    }

    /// Add what a checkpoint just inserted to the run's tally.
    ///
    /// Accumulated, not assigned: a run spans several attempts, and each attempt
    /// counts only its own inserts. A resumed attempt that finds nothing new to
    /// insert — because it is finishing the *detail* phase for jobs the first
    /// attempt already stored — would otherwise reset the run's total to zero.
    public func addInserted(id: String, delta: Int) throws {
        guard delta != 0 else { return }
        try db.writer.write {
            try $0.execute(sql: "UPDATE search_runs SET insertedSoFar = insertedSoFar + ? WHERE id = ?",
                           arguments: [delta, id])
        }
    }

    public func setState(id: String, _ state: SearchRun.State) throws {
        try db.writer.write {
            try $0.execute(sql: "UPDATE search_runs SET state = ? WHERE id = ?",
                           arguments: [state.rawValue, id])
        }
    }

    public func run(id: String) throws -> SearchRun? {
        try db.writer.read { dbc in
            try Row.fetchOne(dbc, sql: "SELECT * FROM search_runs WHERE id = ?", arguments: [id])
                .flatMap(Self.decode)
        }
    }

    // MARK: - Row coding

    static let iso = ISO8601DateFormatter()

    static func encode(_ list: [String]) -> String {
        (try? String(data: JSONEncoder().encode(list), encoding: .utf8)) ?? "[]"
    }

    static func encodeDict(_ dict: [String: String]) -> String {
        (try? String(data: JSONEncoder().encode(dict), encoding: .utf8)) ?? "{}"
    }

    static func decode(_ row: Row) -> SearchRun? {
        let id: String = row["id"]
        guard let started = iso.date(from: row["startedAt"] ?? ""),
              let state = SearchRun.State(rawValue: row["state"] ?? "") else { return nil }
        let requested = (try? JSONDecoder().decode(
            [String].self, from: Data((row["requestedSources"] as String? ?? "[]").utf8))) ?? []
        let completed = (try? JSONDecoder().decode(
            [String].self, from: Data((row["completedSources"] as String? ?? "[]").utf8))) ?? []
        let cursors = (try? JSONDecoder().decode(
            [String: String].self, from: Data((row["cursors"] as String? ?? "{}").utf8))) ?? [:]
        return SearchRun(id: id, startedAt: started, state: state,
                         requestedSources: requested, completedSources: completed,
                         cursors: cursors, insertedSoFar: row["insertedSoFar"] ?? 0)
    }
}
