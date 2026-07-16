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

    /// Whether `id` may be fetched at all under this config, feature flags
    /// included. Distinct from the user's per-source toggle: a source can be
    /// switched on in `enabledSources` and still be off the table here.
    public static func isAvailable(_ id: String, config: AppConfig) -> Bool {
        id == LinkedInSource.id ? LinkedInFeature.isEnabled(config) : true
    }

    /// The sources a run should actually use: registered, switched on by the
    /// user, and available. The single answer to "what are we about to fetch".
    ///
    /// LinkedIn's on/off state is `linkedInEnabled` alone, not membership in
    /// `enabledSources` — settings sync strips "linkedin" from the set on every
    /// import (it folds the id into the flag), so requiring membership here
    /// silently disabled LinkedIn after a sync while the Settings toggle,
    /// which reads the flag, still showed it on.
    public static func enabledIDs(for config: AppConfig) -> [String] {
        allIDs.filter { id in
            guard isAvailable(id, config: config) else { return false }
            return id == LinkedInSource.id || config.search.enabledSources.contains(id)
        }
    }

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
