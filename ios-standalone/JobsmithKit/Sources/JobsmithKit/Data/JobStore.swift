import Foundation
import GRDB

/// Typed query layer over the jobs table.
public struct JobStore: Sendable {
    let db: AppDatabase

    public init(_ db: AppDatabase) { self.db = db }

    /// Result of an upsert pass, mirroring the desktop counters.
    public struct UpsertSummary: Sendable, Equatable {
        public var inserted = 0
        public var updated = 0
        public init() {}
    }

    /// Port of the desktop `upsert_job`: insert new jobs; on
    /// (source, externalId) duplicates, backfill only empty fields and never
    /// regress status. Also tracks repost sightings.
    @discardableResult
    public func upsert(_ normalized: [NormalizedJob]) throws -> UpsertSummary {
        var summary = UpsertSummary()
        let now = ISO8601DateFormatter().string(from: Date())
        try db.writer.write { dbc in
            for var item in normalized {
                // Feeds that ship no structured salary often still state a rate
                // in the description prose ("$25–30/hr"). Recover it once here so
                // every source and the manual paste path benefit uniformly.
                if item.salaryMin == nil, item.salaryMax == nil,
                   let derived = JobFilters.parseSalaryFromText(item.description) {
                    item.salaryMin = derived.min
                    item.salaryMax = derived.max
                    item.salaryPeriod = derived.period
                }
                if var existing = try Job
                    .filter(Column("source") == item.source && Column("externalId") == item.externalId)
                    .fetchOne(dbc) {
                    var changed = false
                    if !item.description.isEmpty && existing.description.isEmpty {
                        existing.description = item.description; changed = true
                    }
                    if let min = item.salaryMin, min != 0, existing.salaryMin == nil {
                        existing.salaryMin = min; changed = true
                    }
                    if let max = item.salaryMax, max != 0, existing.salaryMax == nil {
                        existing.salaryMax = max; changed = true
                    }
                    if let period = item.salaryPeriod, period != "unknown", !period.isEmpty,
                       existing.salaryPeriod == "unknown" || existing.salaryPeriod.isEmpty {
                        existing.salaryPeriod = period; changed = true
                    }
                    if !item.tags.isEmpty && existing.tagList.isEmpty {
                        existing.tags = (try? String(data: JSONEncoder().encode(item.tags), encoding: .utf8)) ?? "[]"
                        changed = true
                    }
                    if item.isEasyApply && !existing.isEasyApply {
                        existing.isEasyApply = true; changed = true
                    }
                    if item.applyType != "unknown" && existing.applyType == "unknown" {
                        existing.applyType = item.applyType; changed = true
                    }
                    existing.lastSeen = now
                    existing.timesSeen += 1
                    try existing.update(dbc)
                    if changed { summary.updated += 1 }
                } else {
                    // Re-discovery guard: a job the user hard-deleted stays
                    // deleted even if a later fetch re-finds the same posting.
                    if !item.externalId.isEmpty,
                       try Int.fetchOne(dbc,
                            sql: "SELECT 1 FROM deleted_jobs WHERE sync_id = ?",
                            arguments: ["\(item.source):\(item.externalId)"]) != nil {
                        continue
                    }
                    try Job(from: item).insert(dbc)
                    summary.inserted += 1
                }
            }
        }
        return summary
    }

    public func job(id: String) throws -> Job? {
        try db.writer.read { try Job.fetchOne($0, key: id) }
    }

    public func inbox(limit: Int = 100) throws -> [Job] {
        try db.writer.read {
            try Job.filter(Column("triage") == "new")
                .order(Column("fitScore").desc, Column("dateDiscovered").desc)
                .limit(limit)
                .fetchAll($0)
        }
    }

    public func jobs(triage: String? = nil, status: String? = nil) throws -> [Job] {
        try db.writer.read { dbc in
            var request = Job.all()
            if let triage { request = request.filter(Column("triage") == triage) }
            if let status { request = request.filter(Column("status") == status) }
            return try request
                .order(Column("fitScore").desc, Column("dateDiscovered").desc)
                .fetchAll(dbc)
        }
    }

    public func setTriage(_ triage: String, jobId: String) throws {
        try db.writer.write {
            try $0.execute(sql: "UPDATE jobs SET triage = ? WHERE id = ?",
                           arguments: [triage, jobId])
        }
    }

    public func setStatus(_ status: String, jobId: String) throws {
        try db.writer.write {
            try $0.execute(sql: "UPDATE jobs SET status = ? WHERE id = ?",
                           arguments: [status, jobId])
        }
    }

    public func setScore(jobId: String, score: Double, reasoning: String, matchReport: String?) throws {
        try db.writer.write {
            try $0.execute(
                sql: "UPDATE jobs SET fitScore = ?, fitReasoning = ?, matchReport = ? WHERE id = ?",
                arguments: [score, reasoning, matchReport, jobId])
        }
    }

    public func setSalaryEstimate(jobId: String, json: String?) throws {
        try db.writer.write {
            try $0.execute(sql: "UPDATE jobs SET salaryEstimate = ? WHERE id = ?",
                           arguments: [json, jobId])
        }
    }

    public func setEmbellishmentLog(jobId: String, log: String) throws {
        try db.writer.write {
            try $0.execute(sql: "UPDATE jobs SET embellishmentLog = ? WHERE id = ?",
                           arguments: [log, jobId])
        }
    }

    public func knownExternalIDs(source: String) throws -> Set<String> {
        try db.writer.read {
            let rows = try String.fetchAll(
                $0, sql: "SELECT externalId FROM jobs WHERE source = ?", arguments: [source])
            return Set(rows)
        }
    }

    public func delete(jobId: String) throws {
        try db.writer.write { dbc in
            // Record a durable tombstone (same "{source}:{externalId}" sync id
            // the sync layer uses) so the deletion propagates to other devices
            // and a later fetch can't silently re-discover the same posting.
            if let job = try Job.fetchOne(dbc, key: jobId), !job.externalId.isEmpty {
                try dbc.execute(
                    sql: "INSERT INTO deleted_jobs (sync_id, deleted_at) VALUES (?, ?) "
                       + "ON CONFLICT(sync_id) DO UPDATE SET deleted_at = excluded.deleted_at",
                    arguments: ["\(job.source):\(job.externalId)", Self.syncNow()])
            }
            _ = try Job.deleteOne(dbc, key: jobId)
        }
    }

    /// Deletion clock in the sync layer's exact format (RFC3339 UTC, ms, 'Z')
    /// so `deleted_jobs.deleted_at` compares correctly against a change
    /// record's `updated_at` under last-writer-wins. Matches SyncEngine.isoMs.
    static func syncNow() -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'"
        return f.string(from: Date())
    }

    public struct Stats: Sendable, Equatable {
        public var totalJobs = 0
        public var newInInbox = 0
        public var pendingReview = 0
        public var appliedTotal = 0
        public var averageFitScore: Double? = nil
        public init() {}
    }

    public func stats() throws -> Stats {
        try db.writer.read { dbc in
            var stats = Stats()
            stats.totalJobs = try Job.fetchCount(dbc)
            stats.newInInbox = try Job.filter(Column("triage") == "new").fetchCount(dbc)
            stats.pendingReview = try Application.filter(Column("status") == "pending_review").fetchCount(dbc)
            stats.appliedTotal = try Application.filter(Column("status") == "applied").fetchCount(dbc)
            stats.averageFitScore = try Double.fetchOne(
                dbc, sql: "SELECT AVG(fitScore) FROM jobs WHERE fitScore IS NOT NULL")
            return stats
        }
    }
}
