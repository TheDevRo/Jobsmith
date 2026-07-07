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
}
