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

    /// Distinct source slugs present in `jobs`, ordered by display name — the
    /// options offered in the "Job board" filter menu.
    public static func availableBoards(in jobs: [Job]) -> [String] {
        Set(jobs.map(\.source)).sorted {
            SourceCatalog.displayName(for: $0)
                .localizedCaseInsensitiveCompare(SourceCatalog.displayName(for: $1)) == .orderedAscending
        }
    }
}
