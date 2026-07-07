import Foundation

/// Weighted keyword matcher over the answer bank, ported from
/// `backend/auto_apply/answer_bank.py`.
///
/// Built-in question patterns are compiled into the binary; their VALUES come
/// from AnswerBankStore rows with the built-in keys. A row whose value is
/// wrapped in <...> (or a missing/empty row) counts as unset. Every other row
/// in the store is a custom entry scored by its own keyword list.
///
/// Score rubric (0-100):
///   100 — any exact phrase found verbatim in the question
///    80 — all keywords present
///    60 — >=60% of keywords present
///    40 — any keyword present (below threshold — never returned)
public struct AnswerBankMatcher: Sendable {
    /// Minimum score required to return a match (0-100 scale).
    public static let minMatchScore = 60

    let store: AnswerBankStore

    public init(store: AnswerBankStore) { self.store = store }
    public init(_ db: AppDatabase) { self.store = AnswerBankStore(db) }

    // ------------------------------------------------------------------
    // Built-in patterns — ported verbatim from _KEY_PATTERNS
    // ------------------------------------------------------------------

    struct KeyPattern: Sendable {
        let key: String
        let exact: [String]
        let keywords: [String]
    }

    static let keyPatterns: [KeyPattern] = [
        KeyPattern(
            key: "tell_us_about_yourself",
            exact: ["tell us about yourself", "tell me about yourself", "introduce yourself",
                    "about yourself", "tell us about you", "describe yourself",
                    "who are you", "professional background", "brief introduction"],
            keywords: ["about", "yourself", "introduce", "background", "yourself"]),
        KeyPattern(
            key: "why_this_role",
            exact: ["why this role", "why this position", "why do you want", "why are you interested",
                    "interest in this", "what attracts you", "motivation for applying",
                    "why apply", "why our company", "why do you want to work here",
                    "why are you a good fit"],
            keywords: ["why", "interest", "motivation", "attract", "role", "position"]),
        KeyPattern(
            key: "challenging_project",
            exact: ["challenging project", "difficult project", "overcome a challenge",
                    "challenging situation", "describe a challenge", "tell us about a challenge",
                    "obstacle you faced", "difficult situation"],
            keywords: ["challenge", "challenging", "difficult", "obstacle", "overcome"]),
        KeyPattern(
            key: "greatest_strength",
            exact: ["greatest strength", "top strength", "strongest skill", "best quality",
                    "key strength", "what is your strength", "describe your strengths",
                    "what are your strengths"],
            keywords: ["strength", "strengths", "strongest", "best quality"]),
        KeyPattern(
            key: "greatest_weakness",
            exact: ["greatest weakness", "area for improvement", "biggest weakness",
                    "development area", "something to improve", "what is your weakness",
                    "describe your weakness", "what are your weaknesses"],
            keywords: ["weakness", "weaknesses", "improve", "development area"]),
        KeyPattern(
            key: "career_goal",
            exact: ["career goal", "where do you see yourself", "5 years", "five years",
                    "career aspiration", "long-term goal", "career objective",
                    "professional goal", "future plans"],
            keywords: ["career", "goal", "aspiration", "future", "years", "objective"]),
        KeyPattern(
            key: "salary_expectation",
            exact: ["salary expectation", "salary requirement", "desired salary",
                    "compensation expectation", "pay expectation", "expected salary",
                    "what salary", "salary range", "compensation range"],
            keywords: ["salary", "compensation", "pay", "expected", "range"]),
        KeyPattern(
            key: "cover_letter",
            exact: ["cover letter", "why should we hire", "why should we choose you",
                    "why should we select you"],
            keywords: ["cover", "letter", "hire", "choose"]),
        KeyPattern(
            key: "how_did_you_hear",
            exact: ["how did you hear", "how did you find", "where did you hear",
                    "where did you find", "how did you learn about", "referral source",
                    "source of application", "who referred you"],
            keywords: ["hear", "find", "source", "referral", "learn"]),
    ]

    public static var builtinKeys: [String] { keyPatterns.map(\.key) }

    /// Seed snippets shipped with the project. Users fill in the <…> placeholders.
    static let seeds: [String: String] = [
        "tell_us_about_yourself":
            "<Replace with your professional summary — 2-3 sentences covering your "
            + "background, core skills, and what you're looking for.>",
        "why_this_role":
            "<Replace with why this specific role interests you — mention the company "
            + "mission, the technology, or the team focus.>",
        "challenging_project":
            "<Replace with a STAR-format story: Situation, Task, Action, Result — "
            + "a concrete project where you overcame a real technical or organisational challenge.>",
        "greatest_strength":
            "<Replace with one or two genuine strengths supported by a brief example.>",
        "greatest_weakness":
            "<Replace with an honest weakness plus the concrete steps you are taking to improve.>",
        "career_goal":
            "<Replace with your 3-5 year career goal, connected to the role you are applying for.>",
        "salary_expectation":
            "<Replace with your salary expectation or a range, or leave blank to pull "
            + "from profile.desired_salary.>",
        "cover_letter":
            "<Replace with a reusable cover-letter body.  The orchestrator will prepend "
            + "a personalised opening and close.>",
        "how_did_you_hear":
            "<Replace with how you usually find postings — e.g. LinkedIn, Indeed, "
            + "or Company website.>",
    ]

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    public struct Match: Equatable, Sendable {
        public let key: String
        public let value: String
        public let score: Int
    }

    /// Insert the 9 built-in seed placeholder rows when the store is empty.
    public func seedIfEmpty() throws {
        guard try store.all().isEmpty else { return }
        for pattern in Self.keyPatterns {
            let label = pattern.key.replacingOccurrences(of: "_", with: " ").capitalized
            try store.upsert(AnswerBankEntry(key: pattern.key, label: label,
                                             keywords: pattern.keywords,
                                             value: Self.seeds[pattern.key] ?? ""))
        }
    }

    /// Weighted keyword lookup. Returns the best match at score >=
    /// `minMatchScore` whose value is set (placeholders are never returned).
    public func findBestMatch(question: String) -> Match? {
        let (key, score, value) = bestCandidate(question)
        guard score >= Self.minMatchScore, let key, let value else { return nil }
        return Match(key: key, value: value, score: score)
    }

    /// Port of Python `score_question` — best-match diagnostics for tests/UI.
    /// `value` is nil when the matched key is unset or still a placeholder.
    public func scoreQuestion(_ question: String) -> (matchedKey: String?, score: Int, value: String?) {
        let (key, score, value) = bestCandidate(question)
        if score < Self.minMatchScore {
            return (nil, score, nil)
        }
        return (key, score, value)
    }

    /// All snippets (built-in + custom) as a flat key→value dict, port of
    /// `all_snippets`: built-in rows keep their raw value (placeholders
    /// included); custom rows are included only when set and non-placeholder.
    public func allSnippets() -> [String: String] {
        var result: [String: String] = [:]
        let builtins = Set(Self.builtinKeys)
        for entry in rows() {
            if builtins.contains(entry.key) {
                result[entry.key] = entry.value
            } else if !entry.key.isEmpty, !entry.value.isEmpty,
                      !Self.isPlaceholder(entry.value) {
                result[entry.key] = entry.value
            }
        }
        return result
    }

    // ------------------------------------------------------------------
    // Internals
    // ------------------------------------------------------------------

    static func isPlaceholder(_ value: String) -> Bool {
        value.hasPrefix("<") && value.hasSuffix(">")
    }

    private func rows() -> [AnswerBankEntry] {
        (try? store.all()) ?? []
    }

    /// Shared core of find_best_match/score_question: (bestKey, bestScore,
    /// bestValue-with-placeholder-semantics).
    private func bestCandidate(_ question: String) -> (String?, Int, String?) {
        let q = question.lowercased()
        let entries = rows()
        var byKey: [String: String] = [:]
        for e in entries { byKey[e.key] = e.value }

        var bestScore = 0
        var bestKey: String?
        var bestValue: String?

        // --- built-in keys ---
        for pattern in Self.keyPatterns {
            let score = Self.score(question: q, exactPhrases: pattern.exact,
                                   keywords: pattern.keywords)
            if score > bestScore {
                bestScore = score
                bestKey = pattern.key
                // Row value; placeholder or empty → treated as unset.
                if let v = byKey[pattern.key], !v.isEmpty, !Self.isPlaceholder(v) {
                    bestValue = v
                } else {
                    bestValue = nil
                }
            }
        }

        // --- custom keys (keywords-only scoring) ---
        let builtins = Set(Self.builtinKeys)
        for entry in entries where !builtins.contains(entry.key) {
            let kws = entry.keywordList.map { $0.lowercased() }
            guard !kws.isEmpty else { continue }
            let score = Self.score(question: q, exactPhrases: [], keywords: kws)
            if score > bestScore {
                bestScore = score
                bestKey = entry.key
                let v = entry.value
                bestValue = (!v.isEmpty && !Self.isPlaceholder(v)) ? v : nil
            }
        }

        return (bestKey, bestScore, bestValue)
    }

    /// Port of `_score_question`: 100 exact phrase, 80 all keywords,
    /// 60 for >=60% of keywords, 40 for any keyword, else 0.
    public static func score(question qLower: String, exactPhrases: [String],
                             keywords: [String]) -> Int {
        for phrase in exactPhrases where qLower.contains(phrase) {
            return 100
        }
        guard !keywords.isEmpty else { return 0 }
        let matched = keywords.filter { qLower.contains($0) }.count
        let ratio = Double(matched) / Double(keywords.count)
        if ratio >= 1.0 { return 80 }
        if ratio >= 0.6 { return 60 }
        if matched > 0 { return 40 }
        return 0
    }
}
