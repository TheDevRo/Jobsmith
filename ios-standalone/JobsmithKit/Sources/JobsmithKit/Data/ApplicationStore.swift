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
