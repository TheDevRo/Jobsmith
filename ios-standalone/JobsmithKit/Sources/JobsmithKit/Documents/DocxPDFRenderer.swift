#if canImport(UIKit)
import UIKit

/// Renders the shared `DocxDocument` layout model to a PDF so PDF and `.docx`
/// output stay structurally identical — same sections, fonts, spacing, rules,
/// bullets, and the Banner band — instead of maintaining two separate layouts.
///
/// Paints onto a US-Letter page with UIKit text drawing (NSStringDrawing),
/// which honors paragraph alignment, indents, and right tab stops and handles
/// the coordinate flip for us.
///
/// Fonts: unlike the desktop PDF path (which embeds Lato / PT Serif TTFs for
/// ReportLab), this uses CoreText system faces — Georgia and Arial/Helvetica
/// ship with iOS, and the Office-only faces (Calibri, Aptos) map to Helvetica
/// Neue. Swiss stays on Helvetica, which is the design, matching Python.
public enum DocxPDFRenderer {
    /// US-Letter, 72 pt per inch.
    private static let pageWidth: CGFloat = 612
    private static let pageHeight: CGFloat = 792

    public static func render(_ doc: DocxDocument) -> Data {
        let (top, bottom, left, right) = doc.margins
        let marginTop = CGFloat(top) * 72
        let marginBottom = CGFloat(bottom) * 72
        let marginLeft = CGFloat(left) * 72
        let contentWidth = pageWidth - CGFloat(left + right) * 72
        let contentBottom = pageHeight - marginBottom

        let bounds = CGRect(x: 0, y: 0, width: pageWidth, height: pageHeight)
        let renderer = UIGraphicsPDFRenderer(bounds: bounds, format: UIGraphicsPDFRendererFormat())

        return renderer.pdfData { ctx in
            ctx.beginPage()
            var cursorY = marginTop

            for paragraph in doc.paragraphs {
                cursorY += CGFloat(paragraph.spacingBeforePt)

                let attr = attributedString(for: paragraph)
                var textHeight: CGFloat = 0
                if attr.length > 0 {
                    let rect = attr.boundingRect(
                        with: CGSize(width: contentWidth, height: .greatestFiniteMagnitude),
                        options: [.usesLineFragmentOrigin, .usesFontLeading], context: nil)
                    textHeight = ceil(rect.height)
                }

                // Break to a new page when the block won't fit — but only if the
                // page already holds content, so an oversized single block can't
                // loop forever.
                if cursorY + textHeight > contentBottom && cursorY > marginTop {
                    ctx.beginPage()
                    cursorY = marginTop
                }

                // Banner band: paragraph shading, painted behind the text and
                // through the trailing space so consecutive shaded paragraphs
                // form one continuous band.
                if let fill = paragraph.shadingFill {
                    let band = CGRect(x: marginLeft, y: cursorY, width: contentWidth,
                                      height: textHeight + CGFloat(paragraph.spacingAfterPt))
                    color(fill).setFill()
                    UIBezierPath(rect: band).fill()
                }

                if attr.length > 0 {
                    attr.draw(with: CGRect(x: marginLeft, y: cursorY,
                                           width: contentWidth, height: textHeight),
                              options: [.usesLineFragmentOrigin, .usesFontLeading], context: nil)
                }

                if let border = paragraph.bottomBorder {
                    // A right indent shortens the rule — that's how the stub
                    // accent bars are drawn (same trick as the DOCX path).
                    let ruleWidth = contentWidth - CGFloat(paragraph.rightIndentInches ?? 0) * 72
                    // OOXML border sizes are eighths of a point.
                    let thickness = max(0.4, CGFloat(Double(border.size) ?? 8) / 8)
                    var lineY = cursorY + textHeight + 1
                    color(border.color).setStroke()

                    // "double" draws the thin twin rule the Executive style uses.
                    let passes = border.val == "double" ? 2 : 1
                    for pass in 0..<passes {
                        let path = UIBezierPath()
                        let y = lineY + CGFloat(pass) * (thickness + 1.5)
                        path.move(to: CGPoint(x: marginLeft, y: y))
                        path.addLine(to: CGPoint(x: marginLeft + max(0, ruleWidth), y: y))
                        path.lineWidth = thickness
                        path.stroke()
                    }
                    lineY += CGFloat(passes - 1) * (thickness + 1.5)
                    cursorY = lineY + 1
                }

                cursorY += textHeight + CGFloat(paragraph.spacingAfterPt)
            }
        }
    }

    private static func attributedString(for paragraph: DocxParagraph) -> NSAttributedString {
        let style = NSMutableParagraphStyle()
        if paragraph.alignment == "center" { style.alignment = .center }
        if let left = paragraph.leftIndentInches {
            style.headIndent = CGFloat(left) * 72
            style.firstLineHeadIndent = CGFloat(left - (paragraph.hangingIndentInches ?? 0)) * 72
        }
        if let right = paragraph.rightIndentInches { style.tailIndent = -CGFloat(right) * 72 }
        if let multiple = paragraph.lineSpacingMultiple { style.lineHeightMultiple = CGFloat(multiple) }
        if let tab = paragraph.rightTabStopInches {
            style.tabStops = [NSTextTab(textAlignment: .right, location: CGFloat(tab) * 72)]
        }

        let result = NSMutableAttributedString()
        for content in paragraph.runs {
            switch content {
            case .tab:
                result.append(NSAttributedString(string: "\t", attributes: [.paragraphStyle: style]))
            case .text(let text, let runStyle):
                result.append(run(text, runStyle, style, underline: false))
            case .hyperlink(_, let text, let runStyle):
                result.append(run(text, runStyle, style, underline: true))
            }
        }
        return result
    }

    private static func run(_ text: String, _ s: RunStyle, _ paragraph: NSParagraphStyle,
                            underline: Bool) -> NSAttributedString {
        var attrs: [NSAttributedString.Key: Any] = [
            .font: font(for: s),
            .foregroundColor: color(s.colorHex),
            .paragraphStyle: paragraph,
        ]
        if s.letterSpacingPt != 0 { attrs[.kern] = CGFloat(s.letterSpacingPt) }
        if underline { attrs[.underlineStyle] = NSUnderlineStyle.single.rawValue }
        // Small caps has no cheap CoreText equivalent across arbitrary system
        // faces, so — exactly like the desktop ReportLab path — the text is
        // upper-cased instead. With the preset's letter-spacing it reads the same.
        let display = (s.allCaps || s.smallCaps) ? text.uppercased() : text
        return NSAttributedString(string: display, attributes: attrs)
    }

    private static func font(for s: RunStyle) -> UIFont {
        let size = CGFloat(s.sizePt)
        let base = UIFont(name: mappedFontName(s.font), size: size) ?? UIFont.systemFont(ofSize: size)
        var traits: UIFontDescriptor.SymbolicTraits = []
        if s.bold { traits.insert(.traitBold) }
        if s.italic { traits.insert(.traitItalic) }
        if !traits.isEmpty, let desc = base.fontDescriptor.withSymbolicTraits(traits) {
            return UIFont(descriptor: desc, size: size)
        }
        return base
    }

    /// The presets name Office fonts; map the ones iOS doesn't ship to close
    /// system equivalents so the PDF still reads as intended. Georgia (Executive)
    /// and Helvetica (Swiss) are real system faces and are used as-is.
    private static func mappedFontName(_ name: String) -> String {
        switch name.lowercased() {
        case "calibri", "aptos": return "Helvetica Neue"
        case "arial", "helvetica": return "Helvetica"
        case "georgia": return "Georgia"
        case "times new roman": return "Times New Roman"
        default: return name
        }
    }

    private static func color(_ hex: String) -> UIColor {
        var value: UInt64 = 0
        Scanner(string: hex).scanHexInt64(&value)
        return UIColor(red: CGFloat((value >> 16) & 0xff) / 255,
                       green: CGFloat((value >> 8) & 0xff) / 255,
                       blue: CGFloat(value & 0xff) / 255, alpha: 1)
    }
}
#endif
