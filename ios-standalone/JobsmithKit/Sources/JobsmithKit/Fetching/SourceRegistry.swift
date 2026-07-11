import Foundation

/// Maps source ids to fetchers, in the desktop's SOURCES order. Indeed is not
/// ported (needs a real browser + Cloudflare solver).
public enum SourceRegistry {
    private static let all: [(id: String, make: @Sendable () -> any JobSource)] = [
        (RemoteOKSource.id, { RemoteOKSource() }),
        (WeWorkRemotelySource.id, { WeWorkRemotelySource() }),
        (AdzunaSource.id, { AdzunaSource() }),
        (GreenhouseSource.id, { GreenhouseSource() }),
        (LinkedInSource.id, { LinkedInSource() }),
        (ArbeitnowSource.id, { ArbeitnowSource() }),
        (USAJobsSource.id, { USAJobsSource() }),
        (AshbySource.id, { AshbySource() }),
        (WorkableSource.id, { WorkableSource() }),
        (RecruiteeSource.id, { RecruiteeSource() }),
    ]

    public static var allIDs: [String] { all.map(\.id) }

    public static func source(for id: String) -> (any JobSource)? {
        let key = id.lowercased()
        return all.first { $0.id == key }?.make()
    }

    /// The per-source budget FetchPipeline enforces for `id`, if registered.
    public static func timeout(for id: String) -> Duration? {
        source(for: id).map { type(of: $0).timeout }
    }

    /// A worst-case estimate of how long a fetch over `ids` will take. Sources
    /// run in parallel under their own timeouts, so the ceiling is the single
    /// slowest source's budget. Empty/unknown input falls back to 60s.
    public static func estimatedDuration(for ids: [String]) -> Duration {
        let requested = ids.isEmpty ? allIDs : ids
        return requested.compactMap { timeout(for: $0) }.max() ?? .seconds(60)
    }
}
