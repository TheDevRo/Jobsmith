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
                    // Re-discovery is handled by the existing-row branch above: a
                    // deleted job stays as a hidden row (triage='deleted'), so a
                    // re-fetch matches it as a duplicate and never regresses the
                    // decision. A genuinely new posting inserts here.
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

    /// All untriaged ("new") jobs, best-fit first. `limit` is nil by default so
    /// the whole inbox is returned — the swipe deck only renders the top few,
    /// and the count/score-all flows need the true total, not a capped 100.
    public func inbox(limit: Int? = nil) throws -> [Job] {
        try db.writer.read {
            var request = Job.filter(Column("triage") == "new")
                .order(Column("fitScore").desc, Column("dateDiscovered").desc)
            if let limit { request = request.limit(limit) }
            return try request.fetchAll($0)
        }
    }

    /// Normalized (title, company) of every role you've actually applied to.
    ///
    /// The fetch-time `Deduplicator` only removes duplicates *within one fetch*,
    /// so a repost — or the same role picked up from a second board — arrives with
    /// a new externalId and URL and looks brand new. This is the key that catches
    /// it. Location is deliberately excluded: the same role in another office is
    /// still a role you already applied to. Must match the desktop's
    /// database.normalize_identity.
    public func appliedIdentities() throws -> Set<String> {
        try db.writer.read { dbc in
            var out: Set<String> = []
            for row in try Row.fetchAll(dbc, sql: """
                SELECT DISTINCT j.title, j.company
                FROM applications a JOIN jobs j ON j.id = a.jobId
                WHERE a.status = 'applied'
                """) {
                if let key = Self.identityKey(title: row["title"], company: row["company"]) {
                    out.insert(key)
                }
            }
            return out
        }
    }

    /// nil when either half is missing — never match on a half-identity.
    public static func identityKey(title: String?, company: String?) -> String? {
        func norm(_ s: String?) -> String {
            (s ?? "")
                .lowercased()
                .replacingOccurrences(of: "[^a-z0-9]+", with: " ", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)
        }
        let t = norm(title), c = norm(company)
        return (t.isEmpty || c.isEmpty) ? nil : "\(t)|\(c)"
    }

    public func jobs(triage: String? = nil, status: String? = nil) throws -> [Job] {
        try db.writer.read { dbc in
            var request = Job.all()
            if let triage { request = request.filter(Column("triage") == triage) }
            else { request = request.filter(Column("triage") != "deleted") }  // hide soft-deleted
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

    /// Postings we already hold a description for. LinkedIn skips the detail-page
    /// scrape for these — but *not* for a job stored with an empty description,
    /// which is what a detail fetch cut short by suspension leaves behind. Using
    /// plain `knownExternalIDs` here would strand those jobs description-less
    /// forever, since every later run would consider them already known.
    public func externalIDsWithDescription(source: String) throws -> Set<String> {
        try db.writer.read {
            let rows = try String.fetchAll(
                $0, sql: "SELECT externalId FROM jobs WHERE source = ? AND description != ''",
                arguments: [source])
            return Set(rows)
        }
    }

    /// Stored postings from `source` that still have no description — the work
    /// left over when a detail phase was interrupted. Returned as fetch-shaped
    /// jobs so a resumed run can carry straight on enriching them.
    ///
    /// Jobs whose detail scrape has already failed `maxAttempts` times are left
    /// out: at that point the page is judged to never yield (layout change,
    /// posting gone), and retrying it every run is what let the backlog eat the
    /// whole detail budget. Least-tried first, so retries rotate through the
    /// backlog across runs instead of hammering the same jobs.
    public func jobsNeedingDescription(source: String, maxAttempts: Int = Int.max,
                                       limit: Int? = nil) throws -> [NormalizedJob] {
        try db.writer.read { dbc in
            try Job.fetchAll(dbc, sql: """
                SELECT jobs.* FROM jobs
                LEFT JOIN detail_attempts da
                    ON da.source = jobs.source AND da.externalId = jobs.externalId
                WHERE jobs.source = ? AND jobs.description = '' AND jobs.triage != 'deleted'
                    AND COALESCE(da.attempts, 0) < ?
                ORDER BY COALESCE(da.attempts, 0) ASC, jobs.dateDiscovered DESC
                LIMIT ?
                """, arguments: [source, maxAttempts, limit ?? -1])
                .map(NormalizedJob.init(from:))
        }
    }

    /// Count one failed detail scrape against each of `externalIds`, pushing
    /// them toward the `maxAttempts` cutoff in `jobsNeedingDescription`.
    public func recordDetailAttempts(source: String, externalIds: [String]) throws {
        guard !externalIds.isEmpty else { return }
        let now = ISO8601DateFormatter().string(from: Date())
        try db.writer.write { dbc in
            for externalId in externalIds {
                try dbc.execute(sql: """
                    INSERT INTO detail_attempts (source, externalId, attempts, lastAttemptAt)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(source, externalId) DO UPDATE SET
                        attempts = attempts + 1,
                        lastAttemptAt = excluded.lastAttemptAt
                    """, arguments: [source, externalId, now])
            }
        }
    }

    /// Soft delete: mark the job triage='deleted' so it's hidden everywhere and
    /// the removal propagates through the normal `triage` last-writer-wins path
    /// (symmetric with shortlist). The row stays to hold the 'deleted' state, so
    /// a later fetch of the same posting can't silently re-discover it.
    public func delete(jobId: String) throws {
        try db.writer.write { dbc in
            try dbc.execute(sql: "UPDATE jobs SET triage = 'deleted' WHERE id = ?",
                            arguments: [jobId])
        }
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
            stats.totalJobs = try Job.filter(Column("triage") != "deleted").fetchCount(dbc)
            stats.newInInbox = try Job.filter(Column("triage") == "new").fetchCount(dbc)
            stats.pendingReview = try Application.filter(Column("status") == "pending_review").fetchCount(dbc)
            stats.appliedTotal = try Application.filter(Column("status") == "applied").fetchCount(dbc)
            stats.averageFitScore = try Double.fetchOne(
                dbc, sql: "SELECT AVG(fitScore) FROM jobs WHERE fitScore IS NOT NULL")
            return stats
        }
    }
}
