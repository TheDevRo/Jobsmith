import Foundation
import GRDB

/// GRDB database wrapper shared by the app and its extensions via the
/// App Group container (WAL mode, safe for cross-process access).
public struct AppDatabase: Sendable {
    public let writer: any DatabaseWriter

    public init(_ writer: any DatabaseWriter) throws {
        self.writer = writer
        try Self.migrator.migrate(writer)
    }

    /// The shared on-disk database in the App Group container.
    public static func shared() throws -> AppDatabase {
        var config = Configuration()
        config.busyMode = .timeout(5)
        let pool = try DatabasePool(path: AppGroup.databaseURL.path, configuration: config)
        return try AppDatabase(pool)
    }

    /// In-memory database for tests and previews.
    public static func inMemory() throws -> AppDatabase {
        try AppDatabase(DatabaseQueue())
    }

    static var migrator: DatabaseMigrator {
        var migrator = DatabaseMigrator()

        migrator.registerMigration("v1") { db in
            try db.create(table: "jobs") { t in
                t.primaryKey("id", .text)
                t.column("source", .text).notNull()
                t.column("externalId", .text).notNull()
                t.column("title", .text).notNull()
                t.column("company", .text).notNull().defaults(to: "")
                t.column("location", .text).notNull().defaults(to: "")
                t.column("url", .text).notNull().defaults(to: "")
                t.column("description", .text).notNull().defaults(to: "")
                t.column("salaryMin", .integer)
                t.column("salaryMax", .integer)
                t.column("salaryPeriod", .text).notNull().defaults(to: "unknown")
                t.column("tags", .text).notNull().defaults(to: "[]")
                t.column("datePosted", .text).notNull().defaults(to: "")
                t.column("dateDiscovered", .text).notNull()
                t.column("status", .text).notNull().defaults(to: "discovered")
                t.column("fitScore", .double)
                t.column("fitReasoning", .text)
                t.column("matchReport", .text)
                t.column("isRemote", .boolean).notNull().defaults(to: false)
                t.column("isEasyApply", .boolean).notNull().defaults(to: false)
                t.column("applyType", .text).notNull().defaults(to: "unknown")
                t.column("embellishmentLog", .text)
                t.column("triage", .text).notNull().defaults(to: "new")
                t.column("lastSeen", .text)
                t.column("timesSeen", .integer).notNull().defaults(to: 1)
                t.column("salaryEstimate", .text)
                t.uniqueKey(["source", "externalId"])
            }
            try db.create(indexOn: "jobs", columns: ["status"])
            try db.create(indexOn: "jobs", columns: ["triage"])
            try db.create(indexOn: "jobs", columns: ["fitScore"])

            try db.create(table: "applications") { t in
                t.primaryKey("id", .text)
                t.column("jobId", .text).notNull()
                    .references("jobs", onDelete: .cascade)
                t.column("resumeContent", .text).notNull().defaults(to: "")
                t.column("coverLetterContent", .text).notNull().defaults(to: "")
                t.column("resumeDocxPath", .text)
                t.column("coverDocxPath", .text)
                t.column("customAnswers", .text).notNull().defaults(to: "{}")
                t.column("status", .text).notNull().defaults(to: "pending_review")
                t.column("honestyLevel", .text).notNull().defaults(to: "honest")
                t.column("stylePreset", .text).notNull().defaults(to: "standard")
                t.column("appliedAt", .text)
                t.column("createdAt", .text).notNull()
                t.column("updatedAt", .text).notNull()
            }
            try db.create(indexOn: "applications", columns: ["status"])
            try db.create(indexOn: "applications", columns: ["jobId"])

            try db.create(table: "activity_log") { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("timestamp", .text).notNull()
                t.column("action", .text).notNull()
                t.column("details", .text).notNull().defaults(to: "")
                t.column("jobId", .text)
            }

            try db.create(table: "answer_bank") { t in
                t.primaryKey("key", .text)
                t.column("label", .text).notNull().defaults(to: "")
                t.column("keywords", .text).notNull().defaults(to: "[]")
                t.column("value", .text).notNull().defaults(to: "")
                t.column("updatedAt", .text).notNull()
            }

            try db.create(table: "source_stats") { t in
                t.primaryKey("source", .text)
                t.column("lastCount", .integer).notNull().defaults(to: 0)
                t.column("consecutiveZero", .integer).notNull().defaults(to: 0)
                t.column("everReturned", .boolean).notNull().defaults(to: false)
                t.column("lastRun", .text)
            }

            try db.create(table: "geo_cache") { t in
                t.primaryKey("location", .text)
                t.column("geoId", .text).notNull()
            }

            try db.create(table: "ai_cache") { t in
                t.primaryKey("key", .text)
                t.column("value", .text).notNull()
                t.column("createdAt", .text).notNull()
            }
        }

        // Durable deletion tombstones. A hard-deleted job records its sync key
        // ("{source}:{externalId}") here so the deletion propagates through
        // folder sync and a later fetch can't silently re-discover it.
        migrator.registerMigration("v2_deleted_jobs") { db in
            try db.create(table: "deleted_jobs") { t in
                t.primaryKey("sync_id", .text)
                t.column("deleted_at", .text).notNull()
            }
        }

        return migrator
    }
}
