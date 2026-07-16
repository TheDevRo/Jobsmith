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
        // The db, -wal and -shm files hold every posting and application. New
        // files inherit the directory's protection class; set it explicitly too
        // so databases created by an earlier build are upgraded in place. Not
        // `.complete` — background fetch/scoring must open it while locked.
        for path in [AppGroup.databaseURL.path,
                     AppGroup.databaseURL.path + "-wal",
                     AppGroup.databaseURL.path + "-shm"] {
            try? FileManager.default.setAttributes(
                [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
                ofItemAtPath: path)
        }
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

        // Post-apply outcome tracking (desktop parity). `outcome` is a derived
        // cache — the truth is the append-only application_events history, which
        // is what syncs. See SyncEngine.appEventSnapshot and the desktop's
        // backend/sync/entities.py::ApplicationEventAdapter.
        migrator.registerMigration("v3_outcomes") { db in
            try db.alter(table: "applications") { t in
                t.add(column: "outcome", .text).notNull().defaults(to: "awaiting")
                t.add(column: "outcomeUpdatedAt", .text)
            }
            try db.create(table: "application_events") { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("applicationId", .text).notNull()
                    .references("applications", onDelete: .cascade)
                t.column("fromOutcome", .text)
                t.column("toOutcome", .text).notNull()
                t.column("occurredAt", .text).notNull()
                t.column("note", .text)
                // user: picked in the UI | rule: aged out | email: parsed inbox
                t.column("source", .text).notNull().defaults(to: "user")
            }
            try db.create(indexOn: "application_events", columns: ["applicationId"])
        }

        // Reminder dates. Sync as their own `application_schedule` entity — on
        // `application` they'd be clobbered by any unrelated edit from the other
        // device, same reason the outcome doesn't live there.
        migrator.registerMigration("v4_reminders") { db in
            try db.alter(table: "applications") { t in
                t.add(column: "followUpAt", .text)
                t.add(column: "interviewAt", .text)
            }
        }

        // A search that can outlive the app. iOS grants ~30 seconds of execution
        // once the user leaves, but a LinkedIn search budgets minutes — so a run
        // records which sources it still owes and how far each got, and a later
        // attempt (foreground return, or a BGProcessingTask) finishes the job
        // instead of starting over. Device-local: a half-finished run means
        // nothing on the other device, so this never syncs.
        migrator.registerMigration("v5_search_runs") { db in
            try db.create(table: "search_runs") { t in
                t.primaryKey("id", .text)
                t.column("startedAt", .text).notNull()
                // running | interrupted | complete
                t.column("state", .text).notNull().defaults(to: "running")
                // JSON [String]
                t.column("requestedSources", .text).notNull().defaults(to: "[]")
                t.column("completedSources", .text).notNull().defaults(to: "[]")
                // JSON {source: opaque cursor JSON}
                t.column("cursors", .text).notNull().defaults(to: "{}")
                t.column("insertedSoFar", .integer).notNull().defaults(to: 0)
            }
            try db.create(indexOn: "search_runs", columns: ["state"])
        }

        // How many times a LinkedIn detail scrape has failed to produce a
        // description for a job. Bounds the retry worklist: without it, a job
        // whose detail page never yields (page layout change, sustained
        // blocking) is re-scraped on every single run, forever — 2026-07-15
        // that backlog was making each search grind through its full detail
        // budget for nothing. Device-local, like search_runs: the other device
        // runs its own scrapes and keeps its own score.
        migrator.registerMigration("v7_detail_attempts") { db in
            try db.create(table: "detail_attempts") { t in
                t.column("source", .text).notNull()
                t.column("externalId", .text).notNull()
                t.column("attempts", .integer).notNull().defaults(to: 0)
                t.column("lastAttemptAt", .text)
                t.primaryKey(["source", "externalId"])
            }
        }

        return migrator
    }
}
