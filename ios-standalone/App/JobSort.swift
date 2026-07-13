import Foundation
import JobsmithKit

/// User-selectable ordering for job lists (Inbox deck, Pipeline sections).
/// Persisted per-device via @AppStorage under `AppStorageKey.jobSort`.
///
/// Job-board selection is a *filter*, not a sort — see `JobFilter`.
enum JobSort: String, CaseIterable, Identifiable {
    case bestBets, bestMatch, newest, salary, company

    var id: String { rawValue }

    var label: String {
        switch self {
        case .bestBets: return "Best bets"
        case .bestMatch: return "Best match"
        case .newest: return "Newest"
        case .salary: return "Salary"
        case .company: return "Company A–Z"
        }
    }

    var systemImage: String {
        switch self {
        case .bestBets: return "target"
        case .bestMatch: return "flame"
        case .newest: return "clock"
        case .salary: return "dollarsign.circle"
        case .company: return "textformat"
        }
    }

    /// Stable reorder of `jobs` for this option. Ties fall back to newest so
    /// the order is deterministic regardless of the source query.
    ///
    /// `conversion` (how often each source has actually replied to you) is only
    /// consulted by `.bestBets`; the other options ignore it.
    func sorted(_ jobs: [Job], conversion: [String: Double] = [:]) -> [Job] {
        switch self {
        case .bestBets:
            // Fit is only part of the story — a perfect match on a board that
            // never replies is a worse bet than a good match on one that does.
            return DigestRanker.rank(jobs, conversion: conversion)
        case .bestMatch:
            // Unscored jobs (nil) sink below scored ones.
            return jobs.sorted { ($0.fitScore ?? -1, $0.dateDiscovered) > ($1.fitScore ?? -1, $1.dateDiscovered) }
        case .newest:
            return jobs.sorted { $0.dateDiscovered > $1.dateDiscovered }
        case .salary:
            return jobs.sorted { (Self.salaryKey($0), $0.dateDiscovered) > (Self.salaryKey($1), $1.dateDiscovered) }
        case .company:
            return jobs.sorted { Self.companyKey($0) < Self.companyKey($1) }
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
}

enum AppStorageKey {
    static let jobSort = "jobSort"
    /// Set once the first-search reassurance toast has been shown.
    static let hasSeenSearchTip = "hasSeenSearchTip"
}
