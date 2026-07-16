import Foundation
import GRDB

/// A stored job listing. Mirrors the desktop `jobs` table with one mobile
/// addition: `triage` drives the swipe-to-triage inbox.
public struct Job: Codable, Equatable, Sendable, Identifiable,
                   FetchableRecord, PersistableRecord {
    public static let databaseTableName = "jobs"

    public var id: String
    public var source: String
    public var externalId: String
    public var title: String
    public var company: String
    public var location: String
    public var url: String
    public var description: String
    public var salaryMin: Int?
    public var salaryMax: Int?
    public var salaryPeriod: String
    /// JSON-encoded [String] — kept as a string column for desktop parity.
    public var tags: String
    public var datePosted: String
    public var dateDiscovered: String
    /// discovered → tailoring → review → applied | manual
    public var status: String
    public var fitScore: Double?
    public var fitReasoning: String?
    /// JSON match report from scoring (matched/missing skills, alignment).
    public var matchReport: String?
    public var isRemote: Bool
    public var isEasyApply: Bool
    /// easy_apply | quick_apply | external | unknown
    public var applyType: String
    public var embellishmentLog: String?
    /// new | shortlisted | dismissed — inbox triage state (mobile-only).
    public var triage: String
    public var lastSeen: String?
    public var timesSeen: Int
    /// JSON salary estimate (market data), distinct from stated salary.
    public var salaryEstimate: String?

    public init(from normalized: NormalizedJob) {
        self.id = UUID().uuidString
        self.source = normalized.source
        self.externalId = normalized.externalId
        self.title = normalized.title
        self.company = normalized.company
        self.location = normalized.location
        self.url = normalized.url
        self.description = normalized.description
        self.salaryMin = normalized.salaryMin
        self.salaryMax = normalized.salaryMax
        self.salaryPeriod = normalized.salaryPeriod ?? "unknown"
        self.tags = (try? String(data: JSONEncoder().encode(normalized.tags), encoding: .utf8)) ?? "[]"
        self.datePosted = normalized.datePosted
        self.dateDiscovered = ISO8601DateFormatter().string(from: Date())
        self.status = "discovered"
        self.fitScore = nil
        self.fitReasoning = nil
        self.matchReport = nil
        self.isRemote = normalized.isRemote
        self.isEasyApply = normalized.isEasyApply
        self.applyType = normalized.applyType
        self.embellishmentLog = nil
        self.triage = "new"
        self.lastSeen = nil
        self.timesSeen = 1
        self.salaryEstimate = nil
    }

    public var tagList: [String] {
        (try? JSONDecoder().decode([String].self, from: Data(tags.utf8))) ?? []
    }
}

/// Fetch-time job shape — the Swift twin of the Python normalized job dict
/// every source module returns.
public struct NormalizedJob: Codable, Equatable, Sendable {
    public var source: String
    public var externalId: String
    public var title: String
    public var company: String
    public var location: String
    public var url: String
    public var description: String
    public var salaryMin: Int?
    public var salaryMax: Int?
    public var salaryPeriod: String?
    public var tags: [String]
    public var datePosted: String
    public var isRemote: Bool
    public var isEasyApply: Bool
    public var applyType: String

    public init(source: String, externalId: String, title: String,
                company: String = "", location: String = "", url: String = "",
                description: String = "", salaryMin: Int? = nil,
                salaryMax: Int? = nil, salaryPeriod: String? = nil,
                tags: [String] = [], datePosted: String = "",
                isRemote: Bool = false, isEasyApply: Bool = false,
                applyType: String = "unknown") {
        self.source = source; self.externalId = externalId; self.title = title
        self.company = company; self.location = location; self.url = url
        self.description = description; self.salaryMin = salaryMin
        self.salaryMax = salaryMax; self.salaryPeriod = salaryPeriod
        self.tags = tags; self.datePosted = datePosted
        self.isRemote = isRemote; self.isEasyApply = isEasyApply
        self.applyType = applyType
    }

    /// Back to fetch shape from a stored row. A resumed search reads the jobs it
    /// still owes work on — LinkedIn postings whose detail page it never got to —
    /// straight out of the database, so an interrupted run doesn't have to
    /// re-run its search phase just to rebuild the list.
    public init(from job: Job) {
        self.source = job.source
        self.externalId = job.externalId
        self.title = job.title
        self.company = job.company
        self.location = job.location
        self.url = job.url
        self.description = job.description
        self.salaryMin = job.salaryMin
        self.salaryMax = job.salaryMax
        self.salaryPeriod = job.salaryPeriod
        self.tags = job.tagList
        self.datePosted = job.datePosted
        self.isRemote = job.isRemote
        self.isEasyApply = job.isEasyApply
        self.applyType = job.applyType
    }
}

public struct Application: Codable, Equatable, Sendable, Identifiable,
                           FetchableRecord, PersistableRecord {
    public static let databaseTableName = "applications"

    public var id: String
    public var jobId: String
    public var resumeContent: String
    public var coverLetterContent: String
    public var resumeDocxPath: String?
    public var coverDocxPath: String?
    public var customAnswers: String
    /// pending_review → applied | manual
    public var status: String
    public var honestyLevel: String
    public var stylePreset: String
    public var appliedAt: String?
    public var createdAt: String
    public var updatedAt: String
    /// What the employer did after submission — orthogonal to `status`, which
    /// only tracks getting the application *out*. Derived from the
    /// `application_events` history; never written directly by sync.
    public var outcome: String
    public var outcomeUpdatedAt: String?
    /// Reminder dates. Like `outcome`, these sync as their own entity rather than
    /// as fields on the application — see SyncEngine.scheduleSnapshot.
    public var followUpAt: String?
    public var interviewAt: String?

    public init(jobId: String, resumeContent: String, coverLetterContent: String,
                honestyLevel: String, stylePreset: String) {
        let now = ISO8601DateFormatter().string(from: Date())
        self.id = UUID().uuidString
        self.jobId = jobId
        self.resumeContent = resumeContent
        self.coverLetterContent = coverLetterContent
        self.resumeDocxPath = nil
        self.coverDocxPath = nil
        self.customAnswers = "{}"
        self.status = "pending_review"
        self.honestyLevel = honestyLevel
        self.stylePreset = stylePreset
        self.appliedAt = nil
        self.createdAt = now
        self.updatedAt = now
        self.outcome = ApplicationOutcome.awaiting.rawValue
        self.outcomeUpdatedAt = nil
        self.followUpAt = nil
        self.interviewAt = nil
    }
}

/// The post-apply funnel. Mirrors the desktop's `database.VALID_OUTCOMES` —
/// keep the two in step, they travel over the same sync wire.
public enum ApplicationOutcome: String, CaseIterable, Sendable {
    case awaiting, noResponse = "no_response", screening, interview, offer,
         rejected, withdrawn

    public var label: String {
        switch self {
        case .awaiting:   return "Awaiting response"
        case .noResponse: return "No response"
        case .screening:  return "Screening"
        case .interview:  return "Interview"
        case .offer:      return "Offer"
        case .rejected:   return "Rejected"
        case .withdrawn:  return "Withdrawn"
        }
    }

    /// Stages that mean the employer engaged, in the order they're reached.
    public static let funnelStages: [ApplicationOutcome] = [.screening, .interview, .offer]

    public var isResponse: Bool { self != .awaiting && self != .noResponse }
}

/// One immutable outcome transition. Append-only: events are never edited, which
/// is what lets two devices merge their histories as a plain union instead of
/// last-writer-wins clobbering each other (see SyncEngine).
public struct ApplicationEvent: Codable, Equatable, Sendable, Identifiable,
                                FetchableRecord, MutablePersistableRecord {
    public static let databaseTableName = "application_events"

    public var id: Int64?
    public var applicationId: String
    public var fromOutcome: String?
    public var toOutcome: String
    public var occurredAt: String
    public var note: String?
    public var source: String

    public init(applicationId: String, fromOutcome: String?, toOutcome: String,
                occurredAt: String, note: String? = nil, source: String = "user") {
        self.id = nil
        self.applicationId = applicationId
        self.fromOutcome = fromOutcome
        self.toOutcome = toOutcome
        self.occurredAt = occurredAt
        self.note = note
        self.source = source
    }

    public mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}

public struct ActivityEntry: Codable, Equatable, Sendable, Identifiable,
                             FetchableRecord, MutablePersistableRecord {
    public static let databaseTableName = "activity_log"

    public var id: Int64?
    public var timestamp: String
    public var action: String
    public var details: String
    public var jobId: String?

    public init(action: String, details: String, jobId: String? = nil) {
        self.id = nil
        self.timestamp = ISO8601DateFormatter().string(from: Date())
        self.action = action
        self.details = details
        self.jobId = jobId
    }

    public mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}

public struct AnswerBankEntry: Codable, Equatable, Sendable, Identifiable,
                               FetchableRecord, PersistableRecord {
    public static let databaseTableName = "answer_bank"

    public var id: String { key }
    public var key: String
    public var label: String
    /// JSON-encoded [String] of match keywords.
    public var keywords: String
    public var value: String
    public var updatedAt: String

    public init(key: String, label: String, keywords: [String], value: String) {
        self.key = key
        self.label = label
        self.keywords = (try? String(data: JSONEncoder().encode(keywords), encoding: .utf8)) ?? "[]"
        self.value = value
        self.updatedAt = ISO8601DateFormatter().string(from: Date())
    }

    public var keywordList: [String] {
        (try? JSONDecoder().decode([String].self, from: Data(keywords.utf8))) ?? []
    }
}
