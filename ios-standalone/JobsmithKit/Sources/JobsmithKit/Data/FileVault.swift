import Foundation

/// Generated document storage in the App Group container:
/// documents/{jobId}/resume.docx, cover_letter.docx, resume.pdf, …
/// Both the app and the Share extension read from here.
public enum FileVault {
    public enum Kind: String, Sendable {
        case resume = "resume"
        case coverLetter = "cover_letter"
    }

    public enum Format: String, Codable, Sendable, CaseIterable {
        case pdf, docx

        public var mime: String {
            switch self {
            case .docx: return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            case .pdf: return "application/pdf"
            }
        }

        /// Menu/label text for the format picker.
        public var label: String {
            switch self {
            case .pdf: return "PDF"
            case .docx: return "Word (.docx)"
            }
        }
    }

    public static func url(jobId: String, kind: Kind, format: Format) -> URL {
        let dir = AppGroup.documentsDirectory.appendingPathComponent(jobId, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("\(kind.rawValue).\(format.rawValue)")
    }

    public static func write(_ data: Data, jobId: String, kind: Kind, format: Format) throws -> URL {
        let target = url(jobId: jobId, kind: kind, format: format)
        try data.write(to: target, options: .atomic)
        return target
    }

    public static func read(jobId: String, kind: Kind, format: Format) -> Data? {
        try? Data(contentsOf: url(jobId: jobId, kind: kind, format: format))
    }

    /// User-facing filename, e.g. "Jane_Doe_Acme_resume.docx".
    public static func exportFilename(name: String, company: String, kind: Kind, format: Format) -> String {
        func slug(_ text: String) -> String {
            text.components(separatedBy: CharacterSet.alphanumerics.inverted)
                .filter { !$0.isEmpty }
                .joined(separator: "_")
        }
        let parts = [slug(name), slug(company), kind.rawValue].filter { !$0.isEmpty }
        return parts.joined(separator: "_") + "." + format.rawValue
    }

    public static func deleteDocuments(jobId: String) {
        let dir = AppGroup.documentsDirectory.appendingPathComponent(jobId, isDirectory: true)
        try? FileManager.default.removeItem(at: dir)
    }
}
