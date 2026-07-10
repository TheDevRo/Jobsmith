import Foundation
import JobsmithKit

/// User-selectable ordering for job lists (Inbox deck, Pipeline sections).
/// Persisted per-device via @AppStorage under `AppStorageKey.jobSort`.
enum JobSort: String, CaseIterable, Identifiable {
    case bestMatch, newest, salary, company, board

    var id: String { rawValue }

    var label: String {
        switch self {
        case .bestMatch: return "Best match"
        case .newest: return "Newest"
        case .salary: return "Salary"
        case .company: return "Company A–Z"
        case .board: return "Job board"
        }
    }

    var systemImage: String {
        switch self {
        case .bestMatch: return "flame"
        case .newest: return "clock"
        case .salary: return "dollarsign.circle"
        case .company: return "textformat"
        case .board: return "rectangle.stack"
        }
    }

    /// Stable reorder of `jobs` for this option. Ties fall back to newest so
    /// the order is deterministic regardless of the source query.
    func sorted(_ jobs: [Job]) -> [Job] {
        switch self {
        case .bestMatch:
            // Unscored jobs (nil) sink below scored ones.
            return jobs.sorted { ($0.fitScore ?? -1, $0.dateDiscovered) > ($1.fitScore ?? -1, $1.dateDiscovered) }
        case .newest:
            return jobs.sorted { $0.dateDiscovered > $1.dateDiscovered }
        case .salary:
            return jobs.sorted { (Self.salaryKey($0), $0.dateDiscovered) > (Self.salaryKey($1), $1.dateDiscovered) }
        case .company:
            return jobs.sorted { Self.companyKey($0) < Self.companyKey($1) }
        case .board:
            // Group by job board (A–Z); within a board, newest first.
            return jobs.sorted {
                let a = Self.boardKey($0), b = Self.boardKey($1)
                return a == b ? $0.dateDiscovered > $1.dateDiscovered : a < b
            }
        }
    }

    /// Highest known compensation: stated salary first, else the market
    /// estimate midpoint, else 0 (sorts last).
    private static func salaryKey(_ job: Job) -> Int {
        if let stated = job.salaryMax ?? job.salaryMin { return stated }
        if let raw = job.salaryEstimate,
           let est = try? JSONDecoder().decode(SalaryEstimate.self, from: Data(raw.utf8)) {
            return est.p50 ?? est.p75
        }
        return 0
    }

    /// Case-insensitive company name; blanks sort to the very end.
    private static func companyKey(_ job: Job) -> String {
        let name = job.company.trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? "\u{10FFFF}" : name.lowercased()
    }

    /// Case-insensitive source/board slug (e.g. "arbeitnow"); blanks sort last.
    private static func boardKey(_ job: Job) -> String {
        let name = job.source.trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? "\u{10FFFF}" : name.lowercased()
    }
}

enum AppStorageKey {
    static let jobSort = "jobSort"
}
