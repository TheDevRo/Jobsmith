import Foundation
import CryptoKit

/// Content-addressed document store — the Swift twin of backend/sync/documents.py.
/// Blobs live at `documents/{sha256}.{ext}`; a reference is `{"hash", "ext"}`.
/// Writes are atomic; a missing blob is non-fatal ("document syncing").
public struct DocumentStore {
    public let storeDir: URL
    public let localDir: URL?

    public init(storeDir: URL, localDir: URL? = nil) {
        self.storeDir = storeDir
        self.localDir = localDir
    }

    public static func sha256(of url: URL) throws -> String {
        let data = try Data(contentsOf: url)
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    public func filename(_ ref: [String: String]) -> String {
        if let ext = ref["ext"], !ext.isEmpty { return "\(ref["hash"] ?? "").\(ext)" }
        return ref["hash"] ?? ""
    }

    public func blobURL(_ ref: [String: String]) -> URL {
        storeDir.appendingPathComponent(filename(ref))
    }

    public func has(_ ref: [String: String]) -> Bool {
        FileManager.default.fileExists(atPath: blobURL(ref).path)
    }

    /// Copy a local file into the store (if absent) and return its reference.
    @discardableResult
    public func put(_ localURL: URL) throws -> [String: String] {
        let hash = try Self.sha256(of: localURL)
        let ext = localURL.pathExtension.lowercased()
        let ref = ["hash": hash, "ext": ext]
        let dest = blobURL(ref)
        if !FileManager.default.fileExists(atPath: dest.path) {
            try FileManager.default.createDirectory(at: storeDir, withIntermediateDirectories: true)
            let data = try Data(contentsOf: localURL)
            try data.write(to: dest, options: .atomic)
        }
        return ref
    }

    /// Copy a stored blob to `localDir/basename.<ext>`; nil if not yet synced.
    public func materialize(_ ref: [String: String], basename: String) throws -> URL? {
        guard let localDir, has(ref) else { return nil }
        try FileManager.default.createDirectory(at: localDir, withIntermediateDirectories: true)
        let ext = ref["ext"] ?? ""
        let dest = localDir.appendingPathComponent(ext.isEmpty ? basename : "\(basename).\(ext)")
        let fm = FileManager.default
        let src = blobURL(ref)
        // Unchanged since the last import: skip the copy (every sync used to
        // rewrite every document).
        if fm.contentsEqual(atPath: src.path, andPath: dest.path) { return dest }
        let tmp = dest.appendingPathExtension("tmp")
        try? fm.removeItem(at: tmp)
        try fm.copyItem(at: src, to: tmp)  // streamed; no whole-file Data in memory
        if fm.fileExists(atPath: dest.path) {
            _ = try fm.replaceItemAt(dest, withItemAt: tmp)
        } else {
            try fm.moveItem(at: tmp, to: dest)
        }
        return dest
    }
}
