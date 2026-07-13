import Foundation
import GRDB

public struct ApplicationStore: Sendable {
    let db: AppDatabase

    public init(_ db: AppDatabase) { self.db = db }

    /// Create (or replace) the application for a job — one application per
    /// job, matching the desktop tailor flow.
    @discardableResult
    public func createOrReplace(jobId: String, resume: String, coverLetter: String,
                                honestyLevel: String, stylePreset: String) throws -> Application {
        let application = Application(jobId: jobId, resumeContent: resume,
                                      coverLetterContent: coverLetter,
                                      honestyLevel: honestyLevel, stylePreset: stylePreset)
        try db.writer.write { dbc in
            try Application.filter(Column("jobId") == jobId).deleteAll(dbc)
            try application.insert(dbc)
        }
        return application
    }

    public func application(jobId: String) throws -> Application? {
        try db.writer.read {
            try Application.filter(Column("jobId") == jobId).fetchOne($0)
        }
    }

    public func application(id: String) throws -> Application? {
        try db.writer.read { try Application.fetchOne($0, key: id) }
    }

    public func applications(status: String? = nil) throws -> [Application] {
        try db.writer.read { dbc in
            var request = Application.all()
            if let status { request = request.filter(Column("status") == status) }
            return try request.order(Column("updatedAt").desc).fetchAll(dbc)
        }
    }

    public func updateContent(id: String, resume: String?, coverLetter: String?) throws {
        let now = ISO8601DateFormatter().string(from: Date())
        try db.writer.write { dbc in
            guard var app = try Application.fetchOne(dbc, key: id) else { return }
            if let resume { app.resumeContent = resume }
            if let coverLetter { app.coverLetterContent = coverLetter }
            app.updatedAt = now
            try app.update(dbc)
        }
    }

    public func updateStatus(id: String, status: String) throws {
        let now = ISO8601DateFormatter().string(from: Date())
        try db.writer.write { dbc in
            guard var app = try Application.fetchOne(dbc, key: id) else { return }
            app.status = status
            app.updatedAt = now
            if status == "applied" { app.appliedAt = now }
            try app.update(dbc)
        }
    }

    public func setDocumentPaths(id: String, resumePath: String?, coverPath: String?) throws {
        try db.writer.write { dbc in
            guard var app = try Application.fetchOne(dbc, key: id) else { return }
            if let resumePath { app.resumeDocxPath = resumePath }
            if let coverPath { app.coverDocxPath = coverPath }
            try app.update(dbc)
        }
    }

    // MARK: outcomes

    /// Record a post-apply outcome. Writes an immutable event and refreshes the
    /// derived `outcome` column. Re-selecting the current outcome is a no-op, so
    /// the history never fills with duplicates.
    ///
    /// Note this does NOT touch `updatedAt`: that column drives the
    /// last-writer-wins clock for the `application` sync entity, and an outcome
    /// must not make this device win — and thereby overwrite — an unrelated
    /// resume edit made on the desktop. The outcome travels on its own
    /// append-only entity instead.
    /// `occurredAt` defaults to now, but is injectable: an outcome parsed from an
    /// email happened when the email arrived, not when we noticed it.
    public func recordOutcome(id: String, outcome: ApplicationOutcome,
                              note: String? = nil, source: String = "user",
                              occurredAt: String? = nil) throws {
        try db.writer.write { dbc in
            guard let app = try Application.fetchOne(dbc, key: id),
                  app.outcome != outcome.rawValue else { return }
            let stamp = try occurredAt ?? Self.nextOccurredAt(dbc, applicationId: id)
            var event = ApplicationEvent(applicationId: id, fromOutcome: app.outcome,
                                         toOutcome: outcome.rawValue,
                                         occurredAt: stamp, note: note, source: source)
            try event.insert(dbc)
            try Self.recomputeOutcome(dbc, applicationId: id)
        }
    }

    /// Now, but never at-or-before this application's latest event.
    ///
    /// Timestamps are millisecond-precision, so tapping through
    /// screening → interview lands both events in the same millisecond. Order
    /// then falls to the tiebreak, which is alphabetical by outcome — and the
    /// history would read "interview, screening", inverting what actually
    /// happened. Stepping past the last event keeps this device's own history
    /// causally ordered; the tiebreak is then only ever reached by genuinely
    /// concurrent events from *different* devices, where any consistent choice
    /// is fine. Same trick as the sync engine's nextTS().
    private static func nextOccurredAt(_ dbc: Database, applicationId: String) throws -> String {
        let candidate = isoNow()
        guard let latest = try ApplicationEvent
                .filter(Column("applicationId") == applicationId)
                .order(Column("occurredAt").desc)
                .fetchOne(dbc)?.occurredAt,
              candidate <= latest
        else { return candidate }
        guard let date = parseEventDate(latest) else { return candidate }
        return isoMs(date.addingTimeInterval(0.001))
    }

    /// Oldest first. Ordered by the event's sync identity rather than rowid so
    /// every device presents the same history (see recomputeOutcome).
    public func events(id: String) throws -> [ApplicationEvent] {
        try db.writer.read { dbc in
            try ApplicationEvent
                .filter(Column("applicationId") == id)
                .order(Column("occurredAt"), Column("toOutcome"))
                .fetchAll(dbc)
        }
    }

    /// Rebuild `applications.outcome` from the event history — the events are the
    /// truth, the column is a cache. Called after a local edit and after a sync
    /// import merges in another device's events, so both sides converge on the
    /// same latest event.
    ///
    /// The tiebreak is `toOutcome`, NOT the rowid: rowids are local insertion
    /// order, so two devices holding the same two same-millisecond events would
    /// order them differently and derive *different* outcomes — the histories
    /// converge but the answer read off them doesn't. `(occurredAt, toOutcome)` is
    /// the event's sync identity, so it is a total order every device agrees on.
    /// Must match the desktop's entities.py::recompute_outcome.
    static func recomputeOutcome(_ dbc: Database, applicationId: String) throws {
        guard let latest = try ApplicationEvent
            .filter(Column("applicationId") == applicationId)
            .order(Column("occurredAt").desc, Column("toOutcome").desc)
            .fetchOne(dbc)
        else { return }  // no history — leave the default 'awaiting' alone
        try dbc.execute(
            sql: "UPDATE applications SET outcome = ?, outcomeUpdatedAt = ? WHERE id = ?",
            arguments: [latest.toOutcome, latest.occurredAt, applicationId])
    }

    /// Funnel counts over submitted applications, read from event history so an
    /// application that interviewed and was then rejected still counts toward the
    /// stages it actually reached.
    public func funnel() throws -> (applied: Int, stages: [(ApplicationOutcome, Int)]) {
        try db.writer.read { dbc in
            let applied = try Application.filter(Column("status") == "applied").fetchCount(dbc)
            var reached: [ApplicationOutcome: Set<String>] = [:]
            let stageValues = ApplicationOutcome.funnelStages.map(\.rawValue)
            for row in try Row.fetchAll(dbc, sql: """
                SELECT e.applicationId AS appId, e.toOutcome AS stage
                FROM application_events e
                JOIN applications a ON a.id = e.applicationId
                WHERE a.status = 'applied' AND e.toOutcome IN (?, ?, ?)
                """, arguments: StatementArguments(stageValues)) {
                guard let stage = ApplicationOutcome(rawValue: row["stage"]),
                      let idx = ApplicationOutcome.funnelStages.firstIndex(of: stage)
                else { continue }
                // Reaching a stage implies the earlier ones were reached too.
                for earlier in ApplicationOutcome.funnelStages[...idx] {
                    reached[earlier, default: []].insert(row["appId"])
                }
            }
            return (applied, ApplicationOutcome.funnelStages.map { ($0, reached[$0]?.count ?? 0) })
        }
    }

    /// How often each source has actually replied to you — the signal behind the
    /// "Best bets" ranking. "Replied" means the outcome moved past awaiting /
    /// no-response; a rejection is still a reply.
    ///
    /// Sources with fewer than `DigestRanker.minConversionSample` submitted
    /// applications are omitted, so the ranker treats them as neutral rather than
    /// judging a board on one silent application. Mirrors the desktop's
    /// get_outcome_analytics response_rate.by_source.
    public func responseRateBySource() throws -> [String: Double] {
        try db.writer.read { dbc in
            var out: [String: Double] = [:]
            for row in try Row.fetchAll(dbc, sql: """
                SELECT j.source AS source,
                       COUNT(*) AS total,
                       SUM(CASE WHEN a.outcome NOT IN ('awaiting', 'no_response')
                                THEN 1 ELSE 0 END) AS responded
                FROM applications a JOIN jobs j ON j.id = a.jobId
                WHERE a.status = 'applied'
                GROUP BY j.source
                """) {
                let total: Int = row["total"]
                guard total >= DigestRanker.minConversionSample else { continue }
                let responded: Int = row["responded"] ?? 0
                out[row["source"]] = Double(responded) / Double(total)
            }
            return out
        }
    }

    // MARK: reminders

    /// Set or clear the reminder dates. `nil` means "leave alone"; pass the
    /// matching `clear` flag to unset, or you couldn't set one date without
    /// wiping the other.
    ///
    /// Like `recordOutcome`, this does NOT bump `updatedAt` — that is the LWW
    /// clock for the `application` entity, and the dates ride their own stream.
    public func setSchedule(id: String, followUpAt: String? = nil, interviewAt: String? = nil,
                            clearFollowUp: Bool = false, clearInterview: Bool = false) throws {
        try db.writer.write { dbc in
            guard var app = try Application.fetchOne(dbc, key: id) else { return }
            if clearFollowUp { app.followUpAt = nil } else if let followUpAt { app.followUpAt = followUpAt }
            if clearInterview { app.interviewAt = nil } else if let interviewAt { app.interviewAt = interviewAt }
            try app.update(dbc)
        }
    }

    /// Open applications carrying a reminder date — what the phone schedules
    /// notifications for. An employer who already rejected you needs no nudge.
    public func scheduled() throws -> [Application] {
        try db.writer.read { dbc in
            try Application
                .filter(Column("status") == "applied")
                .filter(!["rejected", "withdrawn", "offer", "no_response"].contains(Column("outcome")))
                .filter(Column("followUpAt") != nil || Column("interviewAt") != nil)
                .fetchAll(dbc)
        }
    }

    // MARK: event timestamps

    /// RFC3339 UTC, milliseconds — the same shape the sync engine emits.
    public static func isoMs(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        f.timeZone = TimeZone(identifier: "UTC")
        return f.string(from: date)
    }

    public static func isoNow() -> String { isoMs(Date()) }

    /// Parse an event stamp from either platform. iOS writes `…060Z`; the desktop
    /// writes `…060761+00:00` (microseconds, explicit offset), and no single
    /// ISO8601 formatter accepts both. Both are always UTC, so parse the fixed
    /// `yyyy-MM-dd'T'HH:mm:ss` prefix they share and add back the fractional
    /// seconds by hand.
    public static func parseEventDate(_ iso: String) -> Date? {
        guard let base = secondsParser.date(from: String(iso.prefix(19))) else { return nil }
        // ".060761+00:00" / ".060Z" -> 0.060
        var fraction: TimeInterval = 0
        let tail = iso.dropFirst(19)
        if tail.hasPrefix(".") {
            let digits = tail.dropFirst().prefix { $0.isNumber }
            if let value = Double("0." + digits) { fraction = value }
        }
        return base.addingTimeInterval(fraction)
    }

    private static let secondsParser: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()
}

public struct ActivityStore: Sendable {
    let db: AppDatabase

    public init(_ db: AppDatabase) { self.db = db }

    public func log(_ action: String, _ details: String, jobId: String? = nil) {
        var entry = ActivityEntry(action: action, details: details, jobId: jobId)
        try? db.writer.write { try entry.insert($0) }
    }

    public func recent(limit: Int = 50) throws -> [ActivityEntry] {
        try db.writer.read {
            try ActivityEntry.order(Column("id").desc).limit(limit).fetchAll($0)
        }
    }
}

public struct AnswerBankStore: Sendable {
    let db: AppDatabase

    public init(_ db: AppDatabase) { self.db = db }

    public func all() throws -> [AnswerBankEntry] {
        try db.writer.read { try AnswerBankEntry.order(Column("key")).fetchAll($0) }
    }

    public func upsert(_ entry: AnswerBankEntry) throws {
        try db.writer.write { try entry.save($0) }
    }

    public func delete(key: String) throws {
        _ = try db.writer.write { try AnswerBankEntry.deleteOne($0, key: key) }
    }
}
