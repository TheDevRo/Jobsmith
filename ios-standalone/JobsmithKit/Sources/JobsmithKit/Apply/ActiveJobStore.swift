import Foundation

/// The app↔extension "active job" handoff: when the user opens a job's apply
/// page from the app, the job context is written here so the Share extension
/// can bind its scan to the right job.
public struct ActiveJob: Codable, Equatable, Sendable {
    public var jobId: String
    public var title: String
    public var company: String
    public var url: String
    public var savedAt: String

    public init(jobId: String, title: String = "", company: String = "",
                url: String = "",
                savedAt: String = ISO8601DateFormatter().string(from: Date())) {
        self.jobId = jobId; self.title = title; self.company = company
        self.url = url; self.savedAt = savedAt
    }

    public init(job: Job) {
        self.init(jobId: job.id, title: job.title, company: job.company, url: job.url)
    }
}

/// Atomic JSON read/write of the active job at AppGroup.activeJobURL
/// ({jobId, title, company, url, savedAt}).
public struct ActiveJobStore: Sendable {
    public let fileURL: URL

    public init(fileURL: URL = AppGroup.activeJobURL) {
        self.fileURL = fileURL
    }

    public func read() -> ActiveJob? {
        guard let data = try? Data(contentsOf: fileURL) else { return nil }
        return try? JSONDecoder().decode(ActiveJob.self, from: data)
    }

    public func write(_ job: ActiveJob) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(job)
        try data.write(to: fileURL, options: .atomic)
    }

    public func clear() {
        try? FileManager.default.removeItem(at: fileURL)
    }
}
