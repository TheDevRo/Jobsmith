import CryptoKit
import Foundation
import ZIPFoundation

/// Where the Local AI model lives and what it is. Files are downloaded from a
/// model-only GitHub release (not an app release), pinned by size + SHA-256,
/// into Application Support (never the App Group: the Share extension must not
/// see or load a 450 MB model).
public enum NLIModel {
    public struct File: Sendable {
        public let name: String
        public let size: Int64
        public let sha256: String
    }

    /// DeBERTa-v3-large-mnli-fever-anli-ling-wanli (MoritzLaurer), Core ML, int8
    /// weights, compiled (.mlmodelc) and zipped. Built by the conversion script
    /// in the model notes; the tokenizer is the same file the desktop uses.
    static let revision = "deberta-v3-large-wanli-coreml-w8-fp32"
    public static let files = [
        File(name: "nli-deberta-v3-large-w8.mlmodelc.zip", size: 391_586_483,
             sha256: "632725ce1b8a0150a6e5a4d6527ebda7f053795b6040cb7ffef6c4bfb1fb08c7"),
        File(name: "tokenizer.json", size: 8_648_889,
             sha256: "7aa118770f066a74530d161c7d0b994d0629cc0ff3a0df213f184192773f960a"),
    ]
    public static let sizeBytes = files.reduce(0) { $0 + $1.size }
    public static let baseURL = URL(string: "https://github.com/TheDevRo/Jobsmith/releases/download/nli-model-v1")!
    static let modelDirName = "NLI.mlmodelc"
    static let tokenizerFile = "tokenizer.json"

    /// Parent of every revision. Tests point it at a temp directory.
    nonisolated(unsafe) static var root: URL = FileManager.default
        .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("models/nli", isDirectory: true)

    static var directory: URL { root.appendingPathComponent(revision, isDirectory: true) }

    public static var isInstalled: Bool {
        let fm = FileManager.default
        return fm.fileExists(atPath: directory.appendingPathComponent(modelDirName).path)
            && fm.fileExists(atPath: directory.appendingPathComponent(tokenizerFile).path)
    }
}

/// Download / verify / resume / delete for the Local AI model, observed by the
/// Settings screen. A download that drops keeps its resume data, so Retry
/// continues where it stopped. The model directory only ever receives verified
/// files: each download is size- and SHA-256-checked before it is moved in.
@MainActor
public final class NLIModelStore: ObservableObject {
    public static let shared = NLIModelStore()

    public enum State: Equatable, Sendable {
        case notInstalled
        case downloading(Double)  // 0-1
        case ready
        case failed(String)
    }

    @Published public private(set) var state: State
    private var job: Task<Void, Never>?
    private let session: URLSession
    private let baseURL: URL
    private let files: [NLIModel.File]

    public init(session: URLSession = .shared, baseURL: URL = NLIModel.baseURL, files: [NLIModel.File] = NLIModel.files) {
        self.session = session
        self.baseURL = baseURL
        self.files = files
        state = NLIModel.isInstalled ? .ready : .notInstalled
    }

    public var isDownloading: Bool { job != nil }

    /// Start (or resume) the download. No-op while running or when installed.
    public func install() {
        guard job == nil else { return }
        guard !NLIModel.isInstalled else { state = .ready; return }
        state = .downloading(0)
        job = Task { [weak self] in
            guard let self else { return }
            do {
                try await self.downloadAll()
                self.state = .ready
            } catch is CancellationError {
                self.state = NLIModel.isInstalled ? .ready : .notInstalled
            } catch {
                self.state = .failed(Self.describe(error))
            }
            self.job = nil
        }
    }

    /// Stop a running download; what arrived is kept for the next `install()`.
    public func cancel() { job?.cancel() }

    /// Remove every downloaded revision and partial download.
    public func delete() async {
        job?.cancel()
        await job?.value
        await NLIRuntime.shared.unload()
        try? FileManager.default.removeItem(at: NLIModel.root)
        state = .notInstalled
    }

    private func downloadAll() async throws {
        let dir = NLIModel.directory
        let fm = FileManager.default
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        var root = NLIModel.root
        var noBackup = URLResourceValues()
        noBackup.isExcludedFromBackup = true  // re-downloadable; keep it out of iCloud backups
        try? root.setResourceValues(noBackup)

        var done: Int64 = 0
        for file in files {
            let isZip = file.name.hasSuffix(".zip")
            let installed = dir.appendingPathComponent(isZip ? NLIModel.modelDirName : file.name)
            if !fm.fileExists(atPath: installed.path) {
                let base = Double(done), all = Double(max(1, total))
                let verified = try await download(file, into: dir) { [weak self] fraction in
                    self?.state = .downloading(min(1, (base + fraction * Double(file.size)) / all))
                }
                if isZip {
                    let staging = dir.appendingPathComponent("unzip", isDirectory: true)
                    try? fm.removeItem(at: staging)
                    try fm.unzipItem(at: verified, to: staging)
                    guard let unzipped = try fm.contentsOfDirectory(at: staging, includingPropertiesForKeys: nil)
                        .first(where: { $0.pathExtension == "mlmodelc" }) else {
                        throw NLIDownloadError.badArchive
                    }
                    try fm.moveItem(at: unzipped, to: installed)
                    try? fm.removeItem(at: staging)
                    try? fm.removeItem(at: verified)
                } else {
                    try fm.moveItem(at: verified, to: installed)
                }
            }
            done += file.size
            state = .downloading(Double(done) / Double(max(1, total)))
        }
    }

    private var total: Int64 { files.reduce(0) { $0 + $1.size } }

    /// One file into `dir/<name>.verified`, resuming from `<name>.resume` when present.
    private func download(_ file: NLIModel.File, into dir: URL,
                          progress: @escaping @MainActor (Double) -> Void) async throws -> URL {
        let fm = FileManager.default
        let resumeFile = dir.appendingPathComponent(file.name + ".resume")
        let dest = dir.appendingPathComponent(file.name + ".verified")
        try? fm.removeItem(at: dest)
        let resumeData = try? Data(contentsOf: resumeFile)
        let url = baseURL.appendingPathComponent(file.name)
        let session = self.session

        let box = TaskBox()
        let poll = Task { @MainActor in
            while !Task.isCancelled {
                if let t = box.task, t.countOfBytesExpectedToReceive > 0 {
                    progress(t.progress.fractionCompleted)
                }
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
        defer { poll.cancel() }

        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                let handler: @Sendable (URL?, URLResponse?, Error?) -> Void = { tmp, response, error in
                    if let error {
                        let data = (error as NSError).userInfo[NSURLSessionDownloadTaskResumeData] as? Data
                        if let data { try? data.write(to: resumeFile) } else { try? fm.removeItem(at: resumeFile) }
                        cont.resume(throwing: error)
                        return
                    }
                    if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                        try? fm.removeItem(at: resumeFile)
                        cont.resume(throwing: NLIDownloadError.http(http.statusCode))
                        return
                    }
                    do {
                        try fm.moveItem(at: tmp!, to: dest)  // before the handler returns and tmp is deleted
                        try? fm.removeItem(at: resumeFile)
                        cont.resume()
                    } catch {
                        cont.resume(throwing: error)
                    }
                }
                let task = resumeData.map { session.downloadTask(withResumeData: $0, completionHandler: handler) }
                    ?? session.downloadTask(with: url, completionHandler: handler)
                box.task = task
                task.resume()
            }
        } onCancel: {
            box.task?.cancel(byProducingResumeData: { data in try? data?.write(to: resumeFile) })
        }

        let size = (try? fm.attributesOfItem(atPath: dest.path)[.size] as? NSNumber)?.int64Value ?? -1
        let digest = try Self.sha256(dest)
        guard size == file.size, digest == file.sha256 else {
            try? fm.removeItem(at: dest)
            throw NLIDownloadError.checksum(file.name)
        }
        return dest
    }

    static func sha256(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1 << 20), !chunk.isEmpty { hasher.update(data: chunk) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static func describe(_ error: Error) -> String {
        if let e = error as? NLIDownloadError { return e.description }
        if let e = error as? URLError, e.code == .notConnectedToInternet { return "No internet connection" }
        return error.localizedDescription
    }

    private final class TaskBox: @unchecked Sendable {
        var task: URLSessionDownloadTask?
    }
}

enum NLIDownloadError: Error, CustomStringConvertible {
    case http(Int), checksum(String), badArchive

    var description: String {
        switch self {
        case .http(let code): return "The download server answered HTTP \(code)"
        case .checksum(let name): return "\(name): checksum mismatch (download corrupted or the file changed upstream)"
        case .badArchive: return "The downloaded model archive holds no Core ML model"
        }
    }
}
