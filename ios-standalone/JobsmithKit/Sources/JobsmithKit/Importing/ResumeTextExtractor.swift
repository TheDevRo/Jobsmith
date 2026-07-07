import Foundation
import PDFKit
import ZIPFoundation

/// Plain-text extraction from an imported résumé — port of
/// `resume_parser.extract_text`. Supports .pdf (PDFKit), .docx (unzip +
/// document.xml) and .txt / plain text.
public enum ResumeTextExtractor {
    public struct UnreadableFileError: LocalizedError {
        let message: String
        public var errorDescription: String? { message }
    }

    public static func extract(filename: String, data: Data) throws -> String {
        let name = filename.lowercased().trimmingCharacters(in: .whitespaces)

        if name.hasSuffix(".pdf") {
            guard let doc = PDFDocument(data: data) else {
                throw UnreadableFileError(message: "Could not read PDF")
            }
            let pages = (0..<doc.pageCount).map { doc.page(at: $0)?.string ?? "" }
            return pages.joined(separator: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }

        if name.hasSuffix(".docx") {
            return try extractDocx(data)
        }

        if name.hasSuffix(".txt") || name.hasSuffix(".md") || name.hasSuffix(".text")
            || name.isEmpty {
            return String(decoding: data, as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }

        throw UnreadableFileError(
            message: "Unsupported file type: \(filename). Upload a PDF, DOCX, or TXT, "
                + "or paste the résumé text instead.")
    }

    /// Unzip the OPC package and pull run text from word/document.xml.
    /// Table cell paragraphs flow through in document order, which is close
    /// enough to python-docx's paragraphs-then-tables walk for LLM input.
    static func extractDocx(_ data: Data) throws -> String {
        let archive: Archive
        do {
            archive = try Archive(data: data, accessMode: .read)
        } catch {
            throw UnreadableFileError(message: "Could not read DOCX: not a zip archive")
        }
        guard let entry = archive["word/document.xml"] else {
            throw UnreadableFileError(message: "Could not read DOCX: no document.xml")
        }
        var xml = Data()
        _ = try archive.extract(entry) { xml.append($0) }

        let collector = DocxTextCollector()
        let parser = XMLParser(data: xml)
        parser.delegate = collector
        guard parser.parse() else {
            throw UnreadableFileError(message: "Could not read DOCX: malformed document.xml")
        }
        return collector.text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// Collects `<w:t>` run text, breaking lines at paragraph ends and marking
/// tabs/breaks the way python-docx renders `p.text`.
private final class DocxTextCollector: NSObject, XMLParserDelegate {
    private(set) var text = ""
    private var inRunText = false

    func parser(_ parser: XMLParser, didStartElement name: String, namespaceURI: String?,
                qualifiedName: String?, attributes: [String: String]) {
        switch name {
        case "w:t": inRunText = true
        case "w:tab": text += "\t"
        case "w:br", "w:cr": text += "\n"
        default: break
        }
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) {
        if inRunText { text += string }
    }

    func parser(_ parser: XMLParser, didEndElement name: String, namespaceURI: String?,
                qualifiedName: String?) {
        switch name {
        case "w:t": inRunText = false
        case "w:p": text += "\n"
        default: break
        }
    }
}
