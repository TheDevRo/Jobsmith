import Foundation

/// Shared container for the app, the Safari extension, and the share
/// extension. Everything cross-process (database, config, generated
/// documents, the active-job handoff) lives under this container.
public enum AppGroup {
    public static let identifier = "group.com.thedevro.jobsmith.standalone"

    public static var containerURL: URL {
        guard let url = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: identifier) else {
            // Missing entitlement is a build configuration error, not a
            // recoverable runtime state.
            fatalError("App Group container unavailable — check entitlements for \(identifier)")
        }
        return url
    }

    public static var databaseDirectory: URL { subdirectory("db") }
    public static var documentsDirectory: URL { subdirectory("documents") }
    public static var importsDirectory: URL { subdirectory("imports") }

    public static var databaseURL: URL {
        databaseDirectory.appendingPathComponent("jobsmith.db")
    }

    public static var configURL: URL {
        containerURL.appendingPathComponent("config.json")
    }

    public static var activeJobURL: URL {
        containerURL.appendingPathComponent("active_job.json")
    }

    private static func subdirectory(_ name: String) -> URL {
        let url = containerURL.appendingPathComponent(name, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
}
