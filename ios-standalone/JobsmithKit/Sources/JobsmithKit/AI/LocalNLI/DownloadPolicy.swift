import Foundation
import Network

/// "Is this download about to use cellular (or another metered link)?" — the
/// Quick match / Local match downloads ask once before spending hundreds of MB.
public enum DownloadPolicy {
    private static let monitor: NWPathMonitor = {
        let m = NWPathMonitor()
        m.start(queue: DispatchQueue(label: "jobsmith.download-policy"))
        return m
    }()

    /// Start watching early (app launch) so the first answer is not a guess.
    public static func start() { _ = monitor }

    public static var isExpensiveNetwork: Bool {
        #if DEBUG
        if CommandLine.arguments.contains("-SimulateCellular") { return true }
        #endif
        return monitor.currentPath.isExpensive
    }
}
