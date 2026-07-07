import Foundation

/// Tolerant JSON recovery for LLM output, ported from the desktop fallback
/// chains (ai_engine / resume_parser).
public enum LenientJSON {
    /// Direct JSON → strip ```json fences → first `{...}` via greedy regex → nil.
    public static func parseObject(_ text: String) -> [String: Any]? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if let obj = decodeObject(trimmed) { return obj }
        if let groups = Rx.first("```(?:json)?\\s*(\\{.*\\})\\s*```", in: trimmed,
                                 options: [.dotMatchesLineSeparators]),
           let fenced = groups[1], let obj = decodeObject(fenced) {
            return obj
        }
        if let groups = Rx.first("\\{.*\\}", in: trimmed, options: [.dotMatchesLineSeparators]),
           let raw = groups[0], let obj = decodeObject(raw) {
            return obj
        }
        return nil
    }

    /// Strict single-pass JSON object decode.
    public static func decodeObject(_ text: String) -> [String: Any]? {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) else { return nil }
        return obj as? [String: Any]
    }

    /// First integer 0-100 in the text (Python `\b([0-9]{1,2}|100)\b` scan).
    public static func firstNumber(in text: String) -> Double? {
        guard let groups = Rx.first("\\b([0-9]{1,2}|100)\\b", in: text),
              let digits = groups[1] else { return nil }
        return Double(digits)
    }

    /// Numeric coercion matching Python `float(value)` over JSON values.
    public static func doubleValue(_ value: Any?) -> Double? {
        if let number = value as? NSNumber { return number.doubleValue }
        if let string = value as? String {
            return Double(string.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return nil
    }

    /// String coercion matching Python `str(value)` (nil/NSNull → "").
    public static func stringValue(_ value: Any?) -> String {
        switch value {
        case nil, is NSNull: return ""
        case let string as String: return string
        case let number as NSNumber: return number.stringValue
        default: return "\(value!)"
        }
    }
}

/// Minimal NSRegularExpression helpers shared by the AI layer.
enum Rx {
    /// Capture groups of the first match ([0] = whole match); nil if no match.
    static func first(_ pattern: String, in text: String,
                      options: NSRegularExpression.Options = []) -> [String?]? {
        guard let re = try? NSRegularExpression(pattern: pattern, options: options) else { return nil }
        let ns = text as NSString
        guard let m = re.firstMatch(in: text, range: NSRange(location: 0, length: ns.length)) else {
            return nil
        }
        return (0..<m.numberOfRanges).map { i in
            let r = m.range(at: i)
            return r.location == NSNotFound ? nil : ns.substring(with: r)
        }
    }

    /// First match with its range in the source string (Python re.search).
    static func firstWithRange(_ pattern: String, in text: String,
                               options: NSRegularExpression.Options = [])
        -> (range: Range<String.Index>, groups: [String?])? {
        guard let re = try? NSRegularExpression(pattern: pattern, options: options) else { return nil }
        let ns = text as NSString
        guard let m = re.firstMatch(in: text, range: NSRange(location: 0, length: ns.length)),
              let range = Range(m.range, in: text) else { return nil }
        let groups = (0..<m.numberOfRanges).map { i -> String? in
            let r = m.range(at: i)
            return r.location == NSNotFound ? nil : ns.substring(with: r)
        }
        return (range, groups)
    }

    /// Capture groups of every match.
    static func all(_ pattern: String, in text: String,
                    options: NSRegularExpression.Options = []) -> [[String?]] {
        guard let re = try? NSRegularExpression(pattern: pattern, options: options) else { return [] }
        let ns = text as NSString
        return re.matches(in: text, range: NSRange(location: 0, length: ns.length)).map { m in
            (0..<m.numberOfRanges).map { i in
                let r = m.range(at: i)
                return r.location == NSNotFound ? nil : ns.substring(with: r)
            }
        }
    }

    /// Replace every match with a template ("$1" for group 1).
    static func replaceAll(_ pattern: String, in text: String, with template: String,
                           options: NSRegularExpression.Options = []) -> String {
        guard let re = try? NSRegularExpression(pattern: pattern, options: options) else { return text }
        let ns = text as NSString
        return re.stringByReplacingMatches(in: text, range: NSRange(location: 0, length: ns.length),
                                           withTemplate: template)
    }
}
