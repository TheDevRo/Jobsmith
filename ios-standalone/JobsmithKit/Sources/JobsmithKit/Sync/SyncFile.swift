import Foundation

/// Errors raised by the sync transport when a change log can't be trusted.
public enum SyncError: Error, LocalizedError, Equatable {
    /// A `changes/*.jsonl` log is present (as a real file or an iCloud
    /// placeholder) but its bytes are unavailable — evicted and not yet
    /// downloaded, mid-download, or otherwise unreadable. The caller MUST abort
    /// rather than treat the log as empty: our own log is only ever written by
    /// this device, so rewriting it from "empty" would destroy history, and
    /// reading a peer's partial view would stall convergence silently.
    case logUnavailable(URL)

    public var errorDescription: String? {
        switch self {
        case .logUnavailable(let url):
            return "Sync log \"\(url.lastPathComponent)\" is still syncing from iCloud; try again in a moment."
        }
    }
}

/// Pure helpers for iCloud's "evicted to the cloud" placeholder convention.
///
/// When a file provider offloads a file to free local space, the real file
/// `NAME` disappears and a sibling placeholder `.NAME.icloud` takes its place.
/// The transport must notice that — globbing only `*.jsonl` silently skips an
/// evicted `.A1B2.jsonl.icloud`, and reading an evicted file with
/// `String(contentsOf:)` fails, which the old code mistook for "empty".
///
/// These functions are intentionally side-effect free (except `materialize`,
/// which triggers a download) and take an injectable `FileManager` so they can
/// be unit-tested without a real ubiquity container.
enum SyncFile {
    static let placeholderPrefix = "."
    static let placeholderSuffix = ".icloud"

    /// The eviction placeholder sibling for a real file URL:
    /// `changes/A1B2.jsonl` -> `changes/.A1B2.jsonl.icloud`.
    static func placeholderURL(for url: URL) -> URL {
        let name = url.lastPathComponent
        return url.deletingLastPathComponent()
            .appendingPathComponent("\(placeholderPrefix)\(name)\(placeholderSuffix)")
    }

    /// True if `name` is an iCloud eviction placeholder (`.something.icloud`).
    static func isPlaceholderName(_ name: String) -> Bool {
        name.hasPrefix(placeholderPrefix) && name.hasSuffix(placeholderSuffix)
            && name.count > placeholderPrefix.count + placeholderSuffix.count
    }

    /// The real file a placeholder stands in for:
    /// `changes/.A1B2.jsonl.icloud` -> `changes/A1B2.jsonl`. Returns the input
    /// unchanged when it is not a placeholder.
    static func realURL(forPlaceholder url: URL) -> URL {
        let name = url.lastPathComponent
        guard isPlaceholderName(name) else { return url }
        let realName = String(name.dropFirst(placeholderPrefix.count).dropLast(placeholderSuffix.count))
        return url.deletingLastPathComponent().appendingPathComponent(realName)
    }

    /// True when the real file is absent but its placeholder is present — the
    /// item has been evicted to iCloud and must be materialized before use.
    static func isEvicted(_ url: URL, fileManager: FileManager = .default) -> Bool {
        !fileManager.fileExists(atPath: url.path)
            && fileManager.fileExists(atPath: placeholderURL(for: url).path)
    }

    /// Force-download an evicted ubiquitous item, waiting bounded for the real
    /// bytes to land. Returns true when the real file is present on return
    /// (immediately true when it was never evicted). Returns false when neither
    /// the real file nor its placeholder exists, or when the download did not
    /// complete within `timeout`.
    @discardableResult
    static func materialize(_ url: URL, timeout: TimeInterval = 5,
                            fileManager: FileManager = .default) -> Bool {
        if fileManager.fileExists(atPath: url.path) { return true }
        guard fileManager.fileExists(atPath: placeholderURL(for: url).path) else { return false }
        try? fileManager.startDownloadingUbiquitousItem(at: url)
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if fileManager.fileExists(atPath: url.path) { return true }
            Thread.sleep(forTimeInterval: 0.1)
        }
        return fileManager.fileExists(atPath: url.path)
    }
}
