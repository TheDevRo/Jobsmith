import Foundation

/// Port of resume_generator.py `_STYLE_DEFAULTS` + `_STYLES` — five presets,
/// single-column, real text, no images/tables/text boxes (ATS-safe).
///
/// All colors are 6-char hex strings without '#'. Presets may carry the
/// `DocStyle.accentSentinel` value in a *color* token; `resolve(style:accent:)`
/// substitutes the active accent hex, exactly like `_resolve_resume_style`.
public struct DocStyle: Sendable {
    // -- Type --
    public var bodyFont: String
    public var nameFont: String
    public var bodySize: Double
    public var nameSize: Double
    public var nameBold: Bool
    public var nameUppercase: Bool
    public var nameSmallCaps: Bool
    public var nameLetterSpacing: Double
    /// "left" | "center"
    public var nameAlign: String
    public var nameColor: String

    // -- Accent --
    /// The preset's own accent hex (already resolved after `resolve`).
    public var accent: String
    /// True → deliberately monochrome; the user's accent choice is ignored.
    public var accentLocked: Bool
    public var accentColor: String

    // -- Contact --
    public var contactColor: String
    public var contactSeparator: String
    /// Contact info runs onto the name line (Compact).
    public var contactInline: Bool

    // -- Section headers --
    public var headerSize: Double
    public var headerColor: String
    public var headerSmallCaps: Bool
    public var headerLetterSpacing: Double
    public var headerUnderline: Bool
    public var headerUnderlineColor: String
    /// OOXML border size, in eighths of a point.
    public var headerUnderlineSize: String
    /// "full" full-width rule | "stub" short accent bar
    public var headerRuleStyle: String
    /// Points of space before a section header.
    public var sectionGap: Double

    // -- Bullets --
    public var bulletMarker: String
    public var bulletMarkerSize: Double
    public var bulletMarkerColor: String

    // -- Letterhead rule / band --
    public var nameRule: Bool
    /// "single" | "double" | "stub"
    public var nameRuleStyle: String
    public var nameRuleColor: String
    public var nameRuleSize: String
    /// Shaded band behind the name + contact block (Banner).
    public var banner: Bool

    // -- Page --
    /// top, bottom, left, right (inches)
    public var margins: (Double, Double, Double, Double)
    public var lineSpacing: Double?

    // -- Entries --
    /// "inline" ("Title · Company ....... Dates") | "stacked"
    public var entryLayout: String
    public var entrySeparator: String
    /// "plain" | "italic" | "accent"
    public var companyStyle: String
    public var skillsSeparator: String
    public var hyperlinks: Bool

    // Shared ink colors (hex, no '#') — fixed body-text colors every style uses.
    public static let black = "222222"
    public static let dark = "333333"
    public static let gray = "666666"

    /// Sentinel a preset puts in a color token to mean "the active accent".
    public static let accentSentinel = "accent"

    /// Derived type sizes, mirroring the Python generator.
    public var contactSize: Double { max(8.0, bodySize - 1.5) }
    public var datesSize: Double { max(8.0, bodySize - 0.5) }
    public var titleSize: Double { bodySize + 0.5 }

    /// Width of the text column, in inches (US Letter).
    public var contentWidth: Double { 8.5 - margins.2 - margins.3 }

    /// Tokens every preset inherits; presets override only what differs.
    public static let defaults = DocStyle(
        bodyFont: "Calibri", nameFont: "Calibri",
        bodySize: 10.5, nameSize: 20,
        nameBold: true, nameUppercase: false, nameSmallCaps: false,
        nameLetterSpacing: 0, nameAlign: "left", nameColor: "222222",
        accent: "1F3A5F", accentLocked: false, accentColor: accentSentinel,
        contactColor: "666666", contactSeparator: "  ·  ", contactInline: false,
        headerSize: 10, headerColor: "222222", headerSmallCaps: false,
        headerLetterSpacing: 0, headerUnderline: false,
        headerUnderlineColor: "999999", headerUnderlineSize: "4",
        headerRuleStyle: "full", sectionGap: 10,
        bulletMarker: "•  ", bulletMarkerSize: 10.5, bulletMarkerColor: "333333",
        nameRule: false, nameRuleStyle: "single", nameRuleColor: "999999",
        nameRuleSize: "6", banner: false,
        margins: (0.6, 0.6, 0.75, 0.75), lineSpacing: nil,
        entryLayout: "inline", entrySeparator: "  ·  ", companyStyle: "plain",
        skillsSeparator: ", ", hyperlinks: true)

    /// The raw preset — accent sentinels unresolved. Call `resolve` instead
    /// unless you specifically want the preset's own accent.
    public static func preset(_ style: HonestyConfig.Style) -> DocStyle {
        var s = defaults
        switch style {
        // Executive — engraved-letterhead serif: centered small-caps name over
        // a thin double rule. Deliberately monochrome (accentLocked).
        case .executive:
            s.bodyFont = "Georgia"; s.nameFont = "Georgia"
            s.nameSize = 22; s.nameBold = false; s.nameSmallCaps = true
            s.nameLetterSpacing = 3; s.nameAlign = "center"; s.nameColor = "17202B"
            s.accent = "17202B"; s.accentLocked = true
            s.headerSize = 11; s.headerColor = "17202B"; s.headerSmallCaps = true
            s.headerLetterSpacing = 1.5; s.headerUnderline = true
            s.headerUnderlineColor = "C9C2B4"; s.headerUnderlineSize = "4"
            s.nameRule = true; s.nameRuleStyle = "double"
            s.nameRuleColor = "17202B"; s.nameRuleSize = "4"
            s.margins = (0.7, 0.7, 0.85, 0.85)
            s.entrySeparator = ", "; s.companyStyle = "italic"
            s.skillsSeparator = "  ·  "
            s.bulletMarker = "•  "; s.bulletMarkerSize = 9
            s.bulletMarkerColor = "17202B"

        // Ledger — bold sans with a short thick accent stub under the name and
        // stub-underlined section headers. The default; shows off accents.
        //
        // Calibri, not Aptos: Aptos ships only with Microsoft 365, and every
        // other reader (LibreOffice, older Word, Google Docs) substitutes a
        // *serif* for it — which wrecks a style whose identity is "bold sans".
        case .ledger:
            s.bodyFont = "Calibri"; s.nameFont = "Calibri"
            s.nameSize = 26; s.nameColor = "171C24"
            s.headerSize = 10; s.headerColor = accentSentinel
            s.headerLetterSpacing = 1.2; s.headerUnderline = true
            s.headerUnderlineColor = accentSentinel; s.headerUnderlineSize = "20"
            s.headerRuleStyle = "stub"
            s.nameRule = true; s.nameRuleStyle = "stub"
            s.nameRuleColor = accentSentinel; s.nameRuleSize = "28"
            s.margins = (0.6, 0.6, 0.8, 0.8)
            s.lineSpacing = 1.12
            s.companyStyle = "accent"; s.skillsSeparator = "  ·  "

        // Banner — full-width ink band behind name + contact. Paragraph shading
        // on real text (w:shd) — parses identically to plain text.
        // Calibri for the same reason as Ledger (see above).
        case .banner:
            s.bodyFont = "Calibri"; s.nameFont = "Calibri"
            s.accent = "1F2D42"
            s.nameSize = 24; s.nameColor = "FFFFFF"
            s.contactColor = "D7DEE9"
            s.banner = true
            s.headerSize = 10; s.headerColor = accentSentinel
            s.headerLetterSpacing = 1.2; s.headerUnderline = true
            s.headerUnderlineColor = accentSentinel; s.headerUnderlineSize = "12"
            s.margins = (0.5, 0.6, 0.8, 0.8)
            s.lineSpacing = 1.1
            s.entrySeparator = "  —  "; s.skillsSeparator = "  ·  "

        // Compact — 9.5pt, half-inch margins, contact on the name line,
        // pipe-separated skills. Two pages become one.
        case .compact:
            s.bodySize = 9.5; s.nameSize = 14
            s.contactInline = true
            s.accent = "37404A"
            s.headerSize = 9; s.headerColor = accentSentinel
            s.headerLetterSpacing = 1; s.headerUnderline = true
            s.headerUnderlineColor = "CCCCCC"; s.headerUnderlineSize = "4"
            s.sectionGap = 7
            s.nameRule = true; s.nameRuleColor = "999999"; s.nameRuleSize = "4"
            s.margins = (0.5, 0.5, 0.5, 0.5)
            s.lineSpacing = 1.0
            s.skillsSeparator = " | "
            s.bulletMarkerSize = 9.5

        // Swiss — no rules, no color: hierarchy from spacing, weight, and quiet
        // letter-spaced grey headers alone. Deliberately monochrome.
        case .swiss:
            s.bodyFont = "Arial"; s.nameFont = "Arial"
            s.nameSize = 21; s.nameBold = false; s.nameColor = "14181D"
            s.accent = "3A3F47"; s.accentLocked = true
            s.headerSize = 9.5; s.headerColor = "9AA0A9"
            s.headerLetterSpacing = 2.5
            s.sectionGap = 15
            s.margins = (0.9, 0.8, 0.95, 0.95)
            s.lineSpacing = 1.2
            s.entrySeparator = ", "; s.skillsSeparator = "  ·  "
            s.bulletMarker = "—  "; s.bulletMarkerColor = "6E747D"
        }
        return s
    }

    /// Port of `_resolve_resume_style` — merge the preset over the defaults and
    /// substitute the accent sentinel in every *color* token with the active
    /// accent (the user's choice unless the preset is `accentLocked`).
    ///
    /// Note `companyStyle` legitimately has "accent" as an enum value and is
    /// deliberately NOT rewritten — same carve-out as the Python `_color`
    /// key-suffix check.
    public static func resolve(style: HonestyConfig.Style,
                               accent: HonestyConfig.ResumeAccent = .default) -> DocStyle {
        var s = preset(style)
        var accentHex = s.accent
        if !s.accentLocked, let chosen = accent.hex {
            accentHex = chosen
        }
        func sub(_ token: String) -> String { token == accentSentinel ? accentHex : token }

        s.accent = accentHex
        s.accentColor = sub(s.accentColor)
        s.nameColor = sub(s.nameColor)
        s.contactColor = sub(s.contactColor)
        s.headerColor = sub(s.headerColor)
        s.headerUnderlineColor = sub(s.headerUnderlineColor)
        s.bulletMarkerColor = sub(s.bulletMarkerColor)
        s.nameRuleColor = sub(s.nameRuleColor)
        return s
    }

    /// Resolve straight from a stored config.
    public static func resolve(_ honesty: HonestyConfig) -> DocStyle {
        resolve(style: honesty.resumeStyle, accent: honesty.resumeAccent)
    }
}
