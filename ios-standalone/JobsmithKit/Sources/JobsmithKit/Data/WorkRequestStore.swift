import Foundation
import GRDB

/// A request for *another device* to do work this one couldn't finish —
/// today: "score everything unscored" (`score_all`), written when a scoring
/// run parks and Desktop handoff is enabled.
///
/// The record is deliberately dumb: kind + params + pending/done. It carries
/// no job list — the fulfilling side derives "what's left" from its own copy
/// of the database, exactly like a resumed run does locally, so a request
/// can never go stale against the jobs it referred to. Results don't travel
/// on the request either; scores converge through the ordinary `job` entity.
public struct WorkRequest: Equatable, Sendable {
    public static let kindScoreAll = "score_all"

    public var id: String
    public var kind: String
    public var status: String  // pending | done
    public var requestedBy: String?
    public var requestedAt: String?
    public var completedBy: String?
    public var completedAt: String?
    /// Kind-specific knobs; for score_all: {"cap": Int, "pool": "inbox"|"pipeline"}.
    public var params: [String: JSONValue]

    public var isPending: Bool { status == "pending" }
}

public final class WorkRequestStore {
    private let db: AppDatabase

    public init(_ db: AppDatabase) {
        self.db = db
    }

    private static let isoFmt: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'"
        return f
    }()

    /// File a new pending request. Prunes retired requests first so the table
    /// (and the sync payload) can't accumulate forever — the writer cleans up
    /// after itself; a deleted row becomes an ordinary tombstone on export.
    @discardableResult
    public func create(kind: String, params: [String: JSONValue],
                       requestedBy deviceId: String,
                       now: Date = Date()) throws -> WorkRequest {
        let request = WorkRequest(
            id: UUID().uuidString, kind: kind, status: "pending",
            requestedBy: deviceId, requestedAt: Self.isoFmt.string(from: now),
            completedBy: nil, completedAt: nil, params: params)
        try db.writer.write { dbc in
            try Self.prune(dbc, now: now)
            try dbc.execute(sql: """
                INSERT INTO work_requests
                    (id, kind, status, requestedBy, requestedAt, paramsJSON)
                VALUES (?, ?, ?, ?, ?, ?)
                """, arguments: [request.id, kind, "pending", deviceId,
                                 request.requestedAt, Self.encode(params)])
        }
        return request
    }

    /// Is a request of this kind, authored by *this* device, already waiting?
    /// Guards against filing a duplicate every time the same run re-parks.
    /// Scoped to `requestedBy` on purpose: a pending row imported from another
    /// device is that peer's business — it must never suppress this device from
    /// filing its own request (that peer might be the one that's offline).
    public func hasPending(kind: String, requestedBy deviceId: String) throws -> Bool {
        try db.writer.read { dbc in
            try Row.fetchOne(dbc, sql: """
                SELECT 1 FROM work_requests
                WHERE kind = ? AND status = 'pending' AND requestedBy = ? LIMIT 1
                """, arguments: [kind, deviceId]) != nil
        }
    }

    public func all() throws -> [WorkRequest] {
        try db.writer.read { dbc in
            try Row.fetchAll(dbc, sql: "SELECT * FROM work_requests").map(Self.request(from:))
        }
    }

    /// Drop retired requests once they're old news, and pending ones nothing
    /// ever answered. Done rows linger a week so the requesting device sees
    /// the completion at least once; unanswered pending rows expire after 30
    /// days rather than commanding a desktop that comes online months later.
    static func prune(_ dbc: Database, now: Date) throws {
        let doneBefore = isoFmt.string(from: now.addingTimeInterval(-7 * 86400))
        let pendingBefore = isoFmt.string(from: now.addingTimeInterval(-30 * 86400))
        try dbc.execute(sql: """
            DELETE FROM work_requests
            WHERE (status = 'done' AND COALESCE(completedAt, requestedAt, '') < ?)
               OR (status = 'pending' AND COALESCE(requestedAt, '') < ?)
            """, arguments: [doneBefore, pendingBefore])
    }

    static func request(from row: Row) -> WorkRequest {
        WorkRequest(id: row["id"], kind: row["kind"], status: row["status"],
                    requestedBy: row["requestedBy"], requestedAt: row["requestedAt"],
                    completedBy: row["completedBy"], completedAt: row["completedAt"],
                    params: decode(row["paramsJSON"]))
    }

    static func encode(_ params: [String: JSONValue]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: JSONValue.object(params).toAny()),
              let json = String(data: data, encoding: .utf8) else { return "{}" }
        return json
    }

    static func decode(_ json: String?) -> [String: JSONValue] {
        guard let json, let data = json.data(using: .utf8),
              case .object(let obj)? = try? JSONDecoder().decode(JSONValue.self, from: data)
        else { return [:] }
        return obj
    }
}
