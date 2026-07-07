import Foundation

/// Port of resume_generator.py `_STYLES` — three presets, single-column,
/// real text, no images/tables (ATS-safe).
public struct DocStyle: Sendable {
    public let bodyFont: String
    public let nameFont: String
    public let nameSize: Double
    public let nameUppercase: Bool
    public let nameLetterSpacing: Double
    public let nameColor: String
    public let accentColor: String
    public let headerSize: Double
    public let headerColor: String
    public let headerUnderline: Bool
    public let headerUnderlineColor: String
    public let headerUnderlineSize: String
    public let headerLetterSpacing: Double
    public let bulletMarker: String
    public let bulletMarkerSize: Double
    public let bulletMarkerColor: String
    public let nameRule: Bool
    public let nameRuleColor: String
    public let nameRuleSize: String
    /// top, bottom, left, right (inches)
    public let margins: (Double, Double, Double, Double)
    public let lineSpacing: Double?
    /// "stacked" | "inline"
    public let entryLayout: String
    public let hyperlinks: Bool

    // Shared ink colors (hex, no '#').
    public static let black = "222222"
    public static let dark = "333333"
    public static let gray = "666666"

    public static func preset(_ style: HonestyConfig.Style) -> DocStyle {
        switch style {
        case .standard:
            return DocStyle(
                bodyFont: "Calibri", nameFont: "Calibri", nameSize: 20,
                nameUppercase: true, nameLetterSpacing: 2, nameColor: black,
                accentColor: "2B5797", headerSize: 11, headerColor: "2B5797",
                headerUnderline: true, headerUnderlineColor: "2B5797",
                headerUnderlineSize: "4", headerLetterSpacing: 0,
                bulletMarker: "▸  ", bulletMarkerSize: 8, bulletMarkerColor: "2B5797",
                nameRule: true, nameRuleColor: "2B5797", nameRuleSize: "8",
                margins: (0.5, 0.5, 0.7, 0.7), lineSpacing: nil,
                entryLayout: "stacked", hyperlinks: false)
        case .minimal:
            return DocStyle(
                bodyFont: "Times New Roman", nameFont: "Times New Roman", nameSize: 16,
                nameUppercase: false, nameLetterSpacing: 0, nameColor: black,
                accentColor: dark, headerSize: 11, headerColor: dark,
                headerUnderline: true, headerUnderlineColor: "999999",
                headerUnderlineSize: "4", headerLetterSpacing: 0,
                bulletMarker: "-  ", bulletMarkerSize: 10.5, bulletMarkerColor: dark,
                nameRule: true, nameRuleColor: "999999", nameRuleSize: "4",
                margins: (0.75, 0.75, 1.0, 1.0), lineSpacing: nil,
                entryLayout: "stacked", hyperlinks: false)
        case .modern:
            return DocStyle(
                bodyFont: "Aptos", nameFont: "Aptos", nameSize: 22,
                nameUppercase: false, nameLetterSpacing: 0, nameColor: "1F1F1F",
                accentColor: "1F3A5F", headerSize: 10, headerColor: "1F1F1F",
                headerUnderline: false, headerUnderlineColor: "1F3A5F",
                headerUnderlineSize: "4", headerLetterSpacing: 1.5,
                bulletMarker: "•  ", bulletMarkerSize: 10.5, bulletMarkerColor: dark,
                nameRule: true, nameRuleColor: "1F3A5F", nameRuleSize: "6",
                margins: (0.6, 0.6, 0.8, 0.8), lineSpacing: 1.15,
                entryLayout: "inline", hyperlinks: true)
        }
    }
}
