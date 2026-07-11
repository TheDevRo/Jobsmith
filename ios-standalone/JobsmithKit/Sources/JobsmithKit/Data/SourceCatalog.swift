import Foundation

/// Human-facing display names for job source slugs (the `source` field on
/// `Job`/`NormalizedJob`, e.g. "linkedin"). Single source of truth shared by
/// the sort menu, job rows, and Settings so a slug renders identically
/// everywhere. Unknown/empty slugs fall back to a capitalized best effort.
public enum SourceCatalog {
    private static let displayNames: [String: String] = [
        "remoteok": "RemoteOK",
        "weworkremotely": "WeWorkRemotely",
        "arbeitnow": "Arbeitnow",
        "greenhouse": "Greenhouse",
        "lever": "Lever",
        "ashby": "Ashby",
        "workable": "Workable",
        "recruitee": "Recruitee",
        "adzuna": "Adzuna",
        "usajobs": "USAJobs",
        "linkedin": "LinkedIn",
        "indeed": "Indeed",
    ]

    /// Pretty name for a source slug, e.g. "linkedin" → "LinkedIn". Unknown
    /// slugs are capitalized; blank/empty renders as "Unknown".
    public static func displayName(for source: String) -> String {
        let slug = source.trimmingCharacters(in: .whitespaces).lowercased()
        if slug.isEmpty { return "Unknown" }
        return displayNames[slug] ?? source.trimmingCharacters(in: .whitespaces).capitalized
    }
}
