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
            let pages = (0..<doc.pageCount).map { index -> String in
                guard let page = doc.page(at: index) else { return "" }
                return pageText(page)
            }
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

    // MARK: - PDF reading order

    /// Rebuild a page's text in visual reading order.
    ///
    /// `PDFPage.string` runs its own reading-order analysis, and on the layout
    /// almost every résumé uses — a job title on the left with its dates
    /// right-aligned on the same line — it treats the dates as a separate
    /// column and moves them to the *end of the page*, while gluing the first
    /// bullet onto the entry line. Employment dates then arrive detached from
    /// the roles they belong to and `ResumeTextParser` drops them: the user
    /// imports a résumé and their dates and companies come back blank.
    ///
    /// PDFKit's *line* layout is sound — it's only the order it emits lines in
    /// that's wrong — so this asks it for the line under each character, then
    /// groups those line fragments by vertical overlap and reads each group
    /// left to right. The dates rejoin the title they sit beside.
    ///
    /// Falls back to `page.string` for a page with no character geometry (a
    /// scan, say), which is no worse than before.
    static func pageText(_ page: PDFPage) -> String {
        let count = page.numberOfCharacters
        guard count > 0 else { return page.string ?? "" }

        // One selection per visual line fragment. Two fragments on the same
        // baseline (the title, and the right-aligned dates) come back as
        // separate selections — which is exactly what we want to rejoin.
        var fragments: [(rect: CGRect, text: String)] = []
        var seen = Set<String>()
        for i in 0..<count {
            let bounds = page.characterBounds(at: i)
            guard !bounds.isNull, !bounds.isInfinite else { continue }
            guard let selection = page.selectionForLine(
                at: CGPoint(x: bounds.midX, y: bounds.midY)
            ) else { continue }

            let rect = selection.bounds(for: page)
            guard !rect.isNull, !rect.isInfinite, rect.height > 0 else { continue }
            // Deduplicate on geometry, not text: two identical bullets on one
            // page are different lines and both must survive.
            let key = "\(rect.minX),\(rect.minY),\(rect.width),\(rect.height)"
            guard !seen.contains(key) else { continue }
            seen.insert(key)

            let text = (selection.string ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            fragments.append((rect, text))
        }
        guard !fragments.isEmpty else { return page.string ?? "" }

        // Group fragments that sit on the same line. Overlap rather than a
        // centre-distance test, so a 10pt date beside a 12pt title still counts
        // as one line.
        var lines: [(minY: CGFloat, maxY: CGFloat,
                     items: [(rect: CGRect, text: String)])] = []
        for fragment in fragments {
            let r = fragment.rect
            var best: Int?
            var bestRatio: CGFloat = 0
            for (i, line) in lines.enumerated() {
                let overlap = min(r.maxY, line.maxY) - max(r.minY, line.minY)
                guard overlap > 0 else { continue }
                let shorter = min(r.height, line.maxY - line.minY)
                let ratio = shorter > 0 ? overlap / shorter : 0
                if ratio >= 0.5, ratio > bestRatio {
                    best = i
                    bestRatio = ratio
                }
            }
            if let i = best {
                lines[i].items.append(fragment)
                lines[i].minY = min(lines[i].minY, r.minY)
                lines[i].maxY = max(lines[i].maxY, r.maxY)
            } else {
                lines.append((minY: r.minY, maxY: r.maxY, items: [fragment]))
            }
        }

        // Page space puts the origin at the bottom-left, so a larger y is
        // higher up the page: sort descending to read top-down.
        lines.sort { ($0.maxY, $0.minY) > ($1.maxY, $1.minY) }

        return lines.map { line in
            line.items
                .sorted { $0.rect.minX < $1.rect.minX }
                .map(\.text)
                .joined(separator: " ")
        }
        .joined(separator: "\n")
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
