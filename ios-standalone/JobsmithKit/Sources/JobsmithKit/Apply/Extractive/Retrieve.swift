import Foundation

/// Narrow the fact store to the top-k facts for one field: lexical overlap + an
/// alias table. Port of `backend/auto_apply/extractive/retrieve.py`.
extension Extractive {
    static let stopWords = Set("""
        a an the and or of to in on for with at by is are was were be been you your yours do does did have has had
        any this that these those what which who whom how when where why can could would will shall should may might must
        we our us i me my it its as from if than then there their they them currently current please select choose
        candidate candidate's s
        """.split(whereSeparator: { $0 == " " || $0 == "\n" }).map(String.init))

    /// (question regex, fact regex over "key text"). A hit adds `aliasBonus` to that fact's score.
    static let aliases: [(NSRegularExpression, NSRegularExpression)] = [
        (#"authori[sz]|eligib|legally|right to work|proof .*work|i-9"#, #"^work_authorization|^sponsorship"#),
        (#"sponsor|visa|h-?1b|citizen|permanent resident|green card|authorization status"#, #"^sponsorship|^work_authorization|visa|citizen"#),
        (#"\b18\b|\bage\b"#, #"^over_18"#),
        (#"gender|\bsex\b|\bman\b|\bwoman\b"#, #"^gender"#),
        (#"race|ethnic|hispanic|latin"#, #"^race"#),
        (#"veteran|military|served|armed forces|army|navy"#, #"^veteran|army|navy|military|veteran"#),
        (#"disab"#, #"^disability"#),
        (#"relocat|move to"#, #"relocat"#),
        (#"remote|hybrid|on-?site|office|work arrangement"#, #"remote|hybrid|on-site|relocat"#),
        (#"travel"#, #"travel"#),
        (#"salary|compensation|\bpay\b"#, #"^salary"#),
        (#"\bstart\b|available"#, #"^available_start"#),
        (#"notice"#, #"^notice_period"#),
        (#"locat|\blive\b|based|city|time ?zone|metropolitan|\barea\b|reside"#, #"^location|^city|^state"#),
        (#"employer|company|work(?:s|ing)? (?:currently|today)|currently work|where do you work"#, #"^current_company"#),
        (#"title|position|\brole\b"#, #"^current_title"#),
        (#"school|college|university|institution"#, #"^edu\d+\.school"#),
        (#"degree|education|bachelor|master|field of study|major"#, #"^edu\d+\.degree"#),
        (#"graduat"#, #"^edu\d+\.year"#),
        (#"certif"#, #"^cert\d+"#),
        (#"linkedin"#, #"^linkedin"#),
        (#"github"#, #"^github"#),
        (#"website|portfolio|personal site"#, #"^portfolio"#),
        (#"e-?mail"#, #"^email"#),
        (#"phone|mobile|cell"#, #"^phone"#),
        (#"manag|direct reports|supervis|people|team"#, #"\b(led|managed|supervised|manage)\b.*\b\d+\b|\bteam of\b"#),
        (#"\bgaps?\b|career break"#, #"^gaps|career break"#),
        (#"years|experience"#, #"^years_total"#),
    ].map { (rx($0.0), rx($0.1)) }
    static let aliasBonus = 3.0
    static let wordRe = rx(#"[a-z0-9+#]+(?:[.-][a-z0-9]+)*"#, caseInsensitive: false)

    static func stem(_ w: String) -> String {
        for suf in ["ing", "ed", "es", "s"] where w.count > suf.count + 3 && w.hasSuffix(suf) {
            return String(w.dropLast(suf.count))
        }
        return w
    }

    static func tokens(_ text: String) -> [String] {
        let s = text.lowercased()
        return wordRe.matches(in: s, range: NSRange(s.startIndex..., in: s))
            .compactMap { Range($0.range, in: s).map { String(s[$0]) } }
            .filter { !stopWords.contains($0) }.map(stem)
    }

    static func questionText(_ f: FieldDescriptor) -> String {
        let q = [f.label, f.extraContext, f.placeholder].filter { !$0.isEmpty }.joined(separator: " ")
        return q.isEmpty ? f.name : q
    }

    /// Top-k (fact, score) with score > 0. Sensitive questions only see explicit profile answers.
    static func topFacts(_ field: FieldDescriptor, _ facts: [Fact], k: Int = 5, sensitive: Bool = false) -> [(Fact, Double)] {
        rank(questionText(field), facts.filter { !sensitive || explicit.contains($0.category) }, k: k)
    }

    /// Top-k (fact, score) with score > 0 for the query text q.
    static func rank(_ q: String, _ pool: [Fact], k: Int = 5) -> [(Fact, Double)] {
        guard !pool.isEmpty else { return [] }
        var seen = Set<String>()
        let qTokens = tokens(q).filter { seen.insert($0).inserted }  // Counter keys, first-seen order
        var df: [String: Int] = [:]
        let factTokens = pool.map { Set(tokens($0.text)) }
        for ft in factTokens { for t in ft { df[t, default: 0] += 1 } }
        let n = Double(pool.count)
        var scored: [(Fact, Double, Int)] = []
        for (i, f) in pool.enumerated() {
            var s = 0.0
            for t in qTokens where factTokens[i].contains(t) { s += log(1 + n / Double(df[t]!)) }
            let hay = "\(f.key) \(f.text)"
            if aliases.contains(where: { search($0.0, q) && search($0.1, hay) }) { s += aliasBonus }
            if s > 0 { scored.append((f, s, i)) }
        }
        scored.sort { $0.1 != $1.1 ? $0.1 > $1.1 : $0.2 < $1.2 }  // stable, like Python's sort
        return scored.prefix(k).map { ($0.0, $0.1) }
    }
}
