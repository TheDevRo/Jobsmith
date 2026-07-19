import Foundation

/// Text search + job-board selection for a *displayed* job list (the Inbox
/// deck and Pipeline sections). Distinct from `JobFilters`, which decides at
/// fetch time whether a job is admitted at all — this only narrows what's
/// already stored and shown.
public enum JobListFilter {

    /// Jobs matching `query` (substring over title, company, location, board
    /// name, and tags) and — when `boards` is non-empty — restricted to those
    /// source slugs. An empty `boards` set means "all boards".
    public static func apply(_ jobs: [Job], query: String, boards: Set<String>) -> [Job] {
        var result = jobs
        if !boards.isEmpty {
            result = result.filter { boards.contains($0.source) }
        }
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return result }
        return result.filter { job in
            job.title.lowercased().contains(q)
                || job.company.lowercased().contains(q)
                || job.location.lowercased().contains(q)
                || SourceCatalog.displayName(for: job.source).lowercased().contains(q)
                || job.tagList.contains { $0.lowercased().contains(q) }
        }
    }

    /// The standing pay gate for the Inbox deck. Unlike the fetch-time
    /// min-salary filter (which only rejects jobs *proven* to pay under the
    /// floor, and rejects them permanently), this runs over stored jobs on
    /// every display, so both settings flip instantly and retroactively:
    ///
    /// - `minSalary` hides jobs whose stated pay annualizes below the floor —
    ///   the same lenient rule the fetch gate applies, re-applied here so jobs
    ///   stored before the floor was raised disappear too.
    /// - `requireStatedPay` additionally hides jobs with no stated pay or an
    ///   unknown pay period. It is ignored without a floor (the settings UI
    ///   only offers it as a companion toggle).
    ///
    /// Both hide reasons are counted separately (`hiddenNoPay` for strict-mode
    /// hides, `hiddenBelowFloor` for stated pay under the floor) so the UI can
    /// badge every hidden job with its reason — the deck count plus the badge
    /// must always add back up to the full inbox, or the gate reads as jobs
    /// silently going missing.
    public static func applyPayFilter(_ jobs: [Job], minSalary: Int?,
                                      requireStatedPay: Bool)
        -> (jobs: [Job], hiddenNoPay: Int, hiddenBelowFloor: Int) {
        guard let minSalary, minSalary != 0 else { return (jobs, 0, 0) }
        var hiddenNoPay = 0
        var hiddenBelowFloor = 0
        let kept = jobs.filter { job in
            guard let annual = JobFilters.statedAnnualPay(salaryMin: job.salaryMin,
                                                          salaryMax: job.salaryMax,
                                                          salaryPeriod: job.salaryPeriod) else {
                if requireStatedPay { hiddenNoPay += 1; return false }
                return true
            }
            if annual < minSalary { hiddenBelowFloor += 1; return false }
            return true
        }
        return (kept, hiddenNoPay, hiddenBelowFloor)
    }

    /// Distinct source slugs present in `jobs`, ordered by display name — the
    /// options offered in the "Job board" filter menu.
    public static func availableBoards(in jobs: [Job]) -> [String] {
        Set(jobs.map(\.source)).sorted {
            SourceCatalog.displayName(for: $0)
                .localizedCaseInsensitiveCompare(SourceCatalog.displayName(for: $1)) == .orderedAscending
        }
    }
}
