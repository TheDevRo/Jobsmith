import Foundation

/// The parsed result of one LLM scoring response, before it becomes a
/// `FitResult`. Deliberately dependency-free (no Job/Profile/AIEngine) so this
/// file compiles standalone into the cross-language host tool.
public struct ParsedScore: Equatable, Sendable {
    public var score: Double
    public var reasoning: String
    /// Sanitized skill/keyword breakdown as canonical JSON (sorted keys), or
    /// nil when nothing usable survived sanitization.
    public var matchReportJSON: String?

    public init(score: Double, reasoning: String, matchReportJSON: String? = nil) {
        self.score = score; self.reasoning = reasoning
        self.matchReportJSON = matchReportJSON
    }
}

/// Port of the desktop's `ai_engine.parse_score_response` fallback chain.
///
/// CONFORMANCE-TESTED: tests/test_crosslang_ai.py drives this parser (via
/// tools/ai-crosslang) and the Python original over the shared fixtures in
/// tests/fixtures/ai_score_responses.json and requires identical output. If
/// you change ANY step here, change backend/ai_engine.py identically and add
/// a fixture capturing the new behaviour.
///
/// Chain: strict JSON → first {...} blob → "score": N regex → any 0-100 scan.
public enum ScoreResponseParser {
    static let titleAlignments: Set<String> = ["strong", "partial", "weak"]

    public static func parse(_ raw: String) -> ParsedScore? {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        // Try parsing as JSON first
        if let data = LenientJSON.decodeObject(text),
           let score = LenientJSON.doubleValue(data["score"]) {
            return ParsedScore(score: score,
                               reasoning: data["reasoning"] as? String ?? "",
                               matchReportJSON: sanitizedMatchReportJSON(data))
        }
        // Salvage attempt — models often wrap JSON in prose or code fences
        if let groups = Rx.first("\\{.*\\}", in: text, options: [.dotMatchesLineSeparators]),
           let blob = groups[0],
           let data = LenientJSON.decodeObject(blob),
           let score = LenientJSON.doubleValue(data["score"]) {
            return ParsedScore(score: score,
                               reasoning: data["reasoning"] as? String ?? "",
                               matchReportJSON: sanitizedMatchReportJSON(data))
        }
        // Regex fallback 1 — look for a number after "score"
        if let groups = Rx.first("\"score\"\\s*:\\s*(\\d+)", in: text),
           let digits = groups[1], let score = Double(digits) {
            let reasoning = Rx.first("\"reasoning\"\\s*:\\s*\"([^\"]+)\"", in: text)?[1] ?? text
            return ParsedScore(score: score, reasoning: reasoning)
        }
        // Regex fallback 2 — scan for any integer 0-100 in the text
        if let score = LenientJSON.firstNumber(in: text) {
            return ParsedScore(score: score,
                               reasoning: "(Score parsed from raw response) \(String(text.prefix(300)))")
        }
        return nil
    }

    /// Coerce the LLM's structured match output into a clean report; nil when
    /// nothing usable survives (callers treat that as score/reasoning only).
    /// Mirrors `ai_engine._sanitize_match_report`.
    static func sanitizeMatchReport(_ data: [String: Any]) -> [String: Any]? {
        func strList(_ key: String, _ cap: Int) -> [String] {
            guard let raw = data[key] as? [Any] else { return [] }
            var out: [String] = []
            for item in raw {
                if let s = item as? String {
                    let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { out.append(String(trimmed.prefix(80))) }
                }
                if out.count >= cap { break }
            }
            return out
        }

        var report: [String: Any] = [
            "matched_skills": strList("matched_skills", 12),
            "missing_skills": strList("missing_skills", 12),
            "matched_soft_skills": strList("matched_soft_skills", 8),
            "missing_soft_skills": strList("missing_soft_skills", 8),
            "keywords": strList("keywords", 15),
        ]
        if let alignment = data["title_alignment"] as? String, titleAlignments.contains(alignment) {
            report["title_alignment"] = alignment
        } else {
            report["title_alignment"] = NSNull()
        }

        let usable = ["matched_skills", "missing_skills", "keywords"]
            .contains { !(report[$0] as? [String] ?? []).isEmpty }
        return usable ? report : nil
    }

    static func sanitizedMatchReportJSON(_ data: [String: Any]) -> String? {
        guard let report = sanitizeMatchReport(data),
              let json = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]) else {
            return nil
        }
        return String(data: json, encoding: .utf8)
    }
}
