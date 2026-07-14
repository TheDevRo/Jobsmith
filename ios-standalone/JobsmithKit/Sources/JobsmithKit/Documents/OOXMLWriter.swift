import Foundation

/// Minimal WordprocessingML builder covering exactly the subset the desktop
/// resume_generator.py uses: paragraphs + runs with fonts/size/bold/italic/
/// caps/small-caps/color/letter-spacing, paragraph spacing, hanging and right
/// indents, right tab stops, bottom borders (single or double), paragraph
/// shading, keep-with-next, line spacing, and external hyperlinks.
/// No tables, no images, no text boxes, no multi-column sections — every style
/// stays single-column real text, which is what keeps the DOCX ATS-parseable.
public struct OOXML {
    /// Twips: 1 inch = 1440.
    static func twips(inches: Double) -> Int { Int((inches * 1440).rounded()) }
    /// w:sz is half-points; w:spacing (paragraph) is twentieths of a point.
    static func halfPoints(_ pt: Double) -> Int { Int((pt * 2).rounded()) }
    static func twentieths(_ pt: Double) -> Int { Int((pt * 20).rounded()) }

    static func escape(_ text: String) -> String {
        text.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
    }
}

public struct RunStyle: Sendable {
    public var font: String
    public var sizePt: Double
    public var bold: Bool
    public var italic: Bool
    public var allCaps: Bool
    /// Renders lowercase letters as smaller capitals, keeping the source text
    /// mixed-case (Executive's name and section headers).
    public var smallCaps: Bool
    /// RRGGBB hex, no '#'.
    public var colorHex: String
    public var letterSpacingPt: Double

    public init(font: String, sizePt: Double, bold: Bool = false, italic: Bool = false,
                allCaps: Bool = false, smallCaps: Bool = false,
                colorHex: String = "333333", letterSpacingPt: Double = 0) {
        self.font = font; self.sizePt = sizePt; self.bold = bold
        self.italic = italic; self.allCaps = allCaps; self.smallCaps = smallCaps
        self.colorHex = colorHex; self.letterSpacingPt = letterSpacingPt
    }

    var xml: String {
        // CT_RPr child order matters: rFonts → b → i → caps → smallCaps →
        // color → spacing → sz.
        var props = "<w:rFonts w:ascii=\"\(font)\" w:hAnsi=\"\(font)\"/>"
        if bold { props += "<w:b/>" }
        if italic { props += "<w:i/>" }
        if allCaps { props += "<w:caps/>" }
        if smallCaps { props += "<w:smallCaps/>" }
        props += "<w:color w:val=\"\(colorHex)\"/>"
        if letterSpacingPt != 0 { props += "<w:spacing w:val=\"\(OOXML.twentieths(letterSpacingPt))\"/>" }
        props += "<w:sz w:val=\"\(OOXML.halfPoints(sizePt))\"/><w:szCs w:val=\"\(OOXML.halfPoints(sizePt))\"/>"
        return "<w:rPr>\(props)</w:rPr>"
    }
}

enum RunContent {
    case text(String, RunStyle)
    case tab
    case hyperlink(url: String, text: String, style: RunStyle)
}

public struct DocxParagraph {
    var alignment: String?          // "center"
    var spacingBeforePt: Double = 0
    var spacingAfterPt: Double = 2
    var lineSpacingMultiple: Double?
    var leftIndentInches: Double?
    var hangingIndentInches: Double?
    /// Pushes the right edge of the text column (and therefore of a bottom
    /// border) inward — this is how the short "stub" accent bars are drawn.
    var rightIndentInches: Double?
    var rightTabStopInches: Double?
    /// A paragraph border spans the paragraph's text column.
    /// `val` is the OOXML border style: "single" or "double".
    var bottomBorder: (color: String, size: String, val: String)?
    /// Solid background fill (RRGGBB) — the Banner band. Presentation-only in
    /// OOXML: the runs underneath stay ordinary text, so ATS extraction is
    /// unaffected. NOT a table and NOT an image.
    var shadingFill: String?
    /// Keeps the paragraph on the same page as the next one, so a section
    /// header is never stranded at the foot of a page.
    var keepNext: Bool = false
    var runs: [RunContent] = []

    public init() {}

    /// Convenience for the common single-line border.
    mutating func setBottomBorder(color: String, size: String, val: String = "single") {
        bottomBorder = (color, size, val)
    }

    public mutating func run(_ text: String, _ style: RunStyle) {
        runs.append(.text(text, style))
    }

    public mutating func tab() { runs.append(.tab) }

    public mutating func link(_ url: String, text: String, style: RunStyle) {
        runs.append(.hyperlink(url: url, text: text, style: style))
    }

    /// Renders paragraph XML; hyperlink relationship ids are allocated
    /// through `relationships`.
    func xml(relationships: DocxRelationships) -> String {
        // CT_PPr child order matters:
        // keepNext → pBdr → shd → tabs → spacing → ind → jc.
        var pPr = ""
        if keepNext { pPr += "<w:keepNext/>" }
        if let border = bottomBorder {
            pPr += "<w:pBdr><w:bottom w:val=\"\(border.val)\" w:sz=\"\(border.size)\" w:space=\"1\" w:color=\"\(border.color)\"/></w:pBdr>"
        }
        if let fill = shadingFill {
            pPr += "<w:shd w:val=\"clear\" w:color=\"auto\" w:fill=\"\(fill)\"/>"
        }
        if let tab = rightTabStopInches {
            pPr += "<w:tabs><w:tab w:val=\"right\" w:pos=\"\(OOXML.twips(inches: tab))\"/></w:tabs>"
        }
        var spacing = "<w:spacing w:before=\"\(OOXML.twentieths(spacingBeforePt))\" w:after=\"\(OOXML.twentieths(spacingAfterPt))\""
        if let multiple = lineSpacingMultiple {
            spacing += " w:line=\"\(Int((multiple * 240).rounded()))\" w:lineRule=\"auto\""
        }
        spacing += "/>"
        pPr += spacing
        if leftIndentInches != nil || hangingIndentInches != nil || rightIndentInches != nil {
            var ind = "<w:ind"
            if let left = leftIndentInches { ind += " w:left=\"\(OOXML.twips(inches: left))\"" }
            if let right = rightIndentInches { ind += " w:right=\"\(OOXML.twips(inches: right))\"" }
            if let hanging = hangingIndentInches { ind += " w:hanging=\"\(OOXML.twips(inches: hanging))\"" }
            ind += "/>"
            pPr += ind
        }
        if let alignment { pPr += "<w:jc w:val=\"\(alignment)\"/>" }

        var body = ""
        for content in runs {
            switch content {
            case .text(let text, let style):
                body += "<w:r>\(style.xml)<w:t xml:space=\"preserve\">\(OOXML.escape(text))</w:t></w:r>"
            case .tab:
                body += "<w:r><w:tab/></w:r>"
            case .hyperlink(let url, let text, let style):
                let rid = relationships.addHyperlink(url)
                let underlined = style.xml.replacingOccurrences(of: "</w:rPr>", with: "<w:u w:val=\"single\"/></w:rPr>")
                body += "<w:hyperlink r:id=\"\(rid)\"><w:r>\(underlined)<w:t xml:space=\"preserve\">\(OOXML.escape(text))</w:t></w:r></w:hyperlink>"
            }
        }
        return "<w:p><w:pPr>\(pPr)</w:pPr>\(body)</w:p>"
    }
}

/// Collects hyperlink relationships for word/_rels/document.xml.rels.
final class DocxRelationships {
    private(set) var hyperlinks: [(id: String, url: String)] = []

    func addHyperlink(_ url: String) -> String {
        let id = "rId\(hyperlinks.count + 100)"
        hyperlinks.append((id, url))
        return id
    }

    var relsXML: String {
        var entries = ""
        for link in hyperlinks {
            entries += "<Relationship Id=\"\(link.id)\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink\" Target=\"\(OOXML.escape(link.url))\" TargetMode=\"External\"/>"
        }
        return "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">\(entries)</Relationships>"
    }
}

/// Assembles a complete .docx: builds document.xml from paragraphs and zips
/// the OPC container.
public struct DocxDocument {
    public var paragraphs: [DocxParagraph] = []
    /// top, bottom, left, right in inches.
    public var margins: (Double, Double, Double, Double) = (1, 1, 1, 1)
    /// Line spacing applied to every added paragraph that doesn't set its own —
    /// stands in for python-docx's `styles['Normal'].paragraph_format`.
    /// Set it before adding paragraphs.
    public var defaultLineSpacing: Double?

    public init() {}

    public mutating func add(_ paragraph: DocxParagraph) {
        var paragraph = paragraph
        if paragraph.lineSpacingMultiple == nil { paragraph.lineSpacingMultiple = defaultLineSpacing }
        paragraphs.append(paragraph)
    }

    public func render() throws -> Data {
        let relationships = DocxRelationships()
        let body = paragraphs.map { $0.xml(relationships: relationships) }.joined()
        let (top, bottom, left, right) = margins
        let sectPr = """
        <w:sectPr><w:pgSz w:w="12240" w:h="15840"/>\
        <w:pgMar w:top="\(OOXML.twips(inches: top))" w:right="\(OOXML.twips(inches: right))" \
        w:bottom="\(OOXML.twips(inches: bottom))" w:left="\(OOXML.twips(inches: left))" \
        w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>
        """
        let documentXML = """
        <?xml version="1.0" encoding="UTF-8" standalone="yes"?>\
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" \
        xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\
        <w:body>\(body)\(sectPr)</w:body></w:document>
        """
        return try DocxArchive.package(documentXML: documentXML,
                                       documentRelsXML: relationships.relsXML)
    }
}
