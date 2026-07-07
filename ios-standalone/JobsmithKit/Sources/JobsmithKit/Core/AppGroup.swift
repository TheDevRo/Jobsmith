import Foundation

/// Shared container for the app, the Safari extension, and the share
/// extension. Everything cross-process (database, config, generated
/// documents, the active-job handoff) lives under this container.
public enum AppGroup {
    public static let identifier = "group.com.thedevro.jobsmith.standalone"

    public static var containerURL: URL {
        if let url = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: identifier) {
            return url
        }
        // No App Group container — typically an unsigned build (the entitlement
        // gets stripped when code signing is disabled) or a provisioning gap.
        // Fall back to the process's own sandbox so the app still launches
        // rather than trapping. Cross-process sharing with the Safari/Share
        // extensions is disabled in this mode; a properly signed build carries
        // the entitlement and never reaches this path.
        _ = warnedAboutFallback
        return localFallbackURL
    }

    /// Local, single-process stand-in for the shared container when the App
    /// Group is unavailable. Application Support is per-app and persists across
    /// launches; the temporary directory is a last resort.
    private static let localFallbackURL: URL = {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory,
                                                 in: .userDomainMask,
                                                 appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        let url = base.appendingPathComponent("JobsmithLocalContainer", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }()

    /// Emit the missing-entitlement warning exactly once (static-let init runs
    /// once, thread-safe).
    private static let warnedAboutFallback: Void = {
        NSLog("[Jobsmith] App Group container unavailable for %@; using a local sandbox fallback. Extension data sharing is disabled — sign the build with the App Group entitlement to enable it.", identifier)
    }()

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
