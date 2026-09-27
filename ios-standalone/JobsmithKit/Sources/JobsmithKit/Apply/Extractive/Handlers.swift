import Foundation

/// Per-kind handlers, the confidence gate, and pass 4 itself. Port of
/// `backend/auto_apply/extractive/handlers.py` and `__init__.py`.
///
/// Source guarantee per handler:
///   choice -> value is one of field.options          (origin "option")
///   text   -> value equals a Fact.value verbatim      (origin "fact:<key>")
///   number -> value computed from role dates          (origin "computed")
///   essay  -> LLM text, flagged for review            (origin "essay")
extension Extractive {
    enum Kind: String, Sendable { case choice, text, number, essay, skip }

    struct Decision: Sendable {
        var value = ""
        var kind = Kind.skip
        var origin = "none"  // option | fact:<key> | computed | essay | none
        var confidence = 0.0
        var reason = ""
        var filled: Bool { !value.isEmpty }
    }

    // Tuned on the prototype's tuning half: the most conservative config within
    // 1 point of the best. Sensitive (legal/EEO) questions use tStrict.
    static let t = 0.8, m = 0.0, tStrict = 0.99, topK = 5

    static let essayRe = rx(#"^(why|describe|tell us|tell me|explain|what makes|share|is there anything|anything else)\b|cover letter"#)
    static let countRe = rx(#"\bhow many\b"#)
    static let yearsRe = rx(#"\byears?\b.*\bexperience\b|\bexperience\b.*\byears?\b"#)
    static let sensitiveRe = rx(
        #"sponsor|visa|h-?1b|citizen|permanent resident|green card|authori[sz]|eligib|legally|i-9|criminal|convict|felony|"#
        + #"arrest|disab|veteran|military|served|gender|\bsex\b|race|ethnic|hispanic|latin|orientation|pronoun|clearance|"#
        + #"background check|drug|non-?compete|non-?solicit|terminat|relative|at least 18|\bage\b"#)
    static let placeholderRe = rx(#"^\s*(select|choose|please|pick|--)|^[-–—.\s]*$"#)
    static let optionDeclineRe = rx(#"decline|prefer not|don'?t wish|do not wish|not to answer|do not want|rather not|not to say|not disclose"#)
    /// Words that make a years question generic (all roles) rather than about one skill.
    static let totalTerms: Set<String> = ["professional", "work", "total", "overall", "full-time", "full time", "paid", ""]

    static func classify(_ f: FieldDescriptor) -> Kind {
        let type = f.fieldType.isEmpty ? "text" : f.fieldType.lowercased()
        if ["file", "password", "date", "hidden"].contains(type) { return .skip }
        if search(yearsRe, f.label) || search(countRe, f.label) { return .number }
        if !(f.options ?? []).isEmpty { return .choice }
        if type == "textarea" || search(essayRe, f.label.trimmingCharacters(in: .whitespacesAndNewlines)) { return .essay }
        if type == "checkbox" { return .skip }  // a bare consent/attestation box: always the user's call
        return .text
    }

    static func isSensitive(_ f: FieldDescriptor) -> Bool { search(sensitiveRe, questionText(f)) }

    /// Fill with the top candidate iff its score >= t and it beats the runner-up by >= m.
    static func gate(_ scores: [(String, Double)], t: Double, m: Double) -> (String?, String) {
        guard !scores.isEmpty else { return (nil, "no candidates") }
        let ranked = scores.enumerated().sorted { $0.1.1 != $1.1.1 ? $0.1.1 > $1.1.1 : $0.0 < $1.0 }.map(\.1)
        let (best, p1) = ranked[0]
        let p2 = ranked.count > 1 ? ranked[1].1 : 0
        if p1 < t { return (nil, "top \(f2(p1)) < T \(f2(t))") }
        if p1 - p2 < m || p1 == p2 {  // a tie never "beats" the runner-up, even with m = 0
            return (nil, p1 != p2 ? "margin \(f2(p1 - p2)) < M \(f2(m))" : "tie with runner-up")
        }
        return (best, "top \(f2(p1)), margin \(f2(p1 - p2))")
    }

    static func f2(_ x: Double) -> String { String(format: "%.2f", x) }

    static func hyp(_ q: String, _ answer: String) -> String { "The answer to '\(q)' is '\(answer)'." }

    /// Python dict semantics: first insertion fixes the position, later writes replace the value.
    static func ordered(_ items: [(String, Double)]) -> [(String, Double)] {
        var out: [(String, Double)] = []
        var at: [String: Int] = [:]
        for (k, v) in items {
            if let i = at[k] { out[i].1 = v } else { at[k] = out.count; out.append((k, v)) }
        }
        return out
    }

    /// Score every option against one premise made of all retrieved facts; the gate decides.
    static func choice(_ f: FieldDescriptor, _ facts: [Fact], _ nli: any NLIScorer, t: Double, m: Double) throws -> Decision {
        let q = f.label.isEmpty ? questionText(f) : f.label
        let declined = facts.contains { $0.declines }
        let cands = (f.options ?? []).filter { !search(placeholderRe, $0) && (declined || !search(optionDeclineRe, $0)) }
        var d = Decision(kind: .choice)
        guard !facts.isEmpty, !cands.isEmpty else {
            d.reason = facts.isEmpty ? "no facts retrieved" : "no answerable options"
            return d
        }
        let premise = facts.map(\.text).joined(separator: " ")
        let probs = try nli.entail(cands.map { NLIPair(premise, hyp(q, $0)) })
        let scores = ordered(Array(zip(cands, probs)))
        let (best, reason) = gate(scores, t: t, m: m)
        d.reason = reason
        if let best {
            d.value = best
            d.origin = "option"
            d.confidence = scores.first { $0.0 == best }!.1
        }
        return d
    }

    static func formatOK(_ type: String, _ v: String) -> Bool {
        switch type.lowercased() {
        case "email": return v.contains("@")
        case "url": return v.hasPrefix("http")
        case "tel": return v.filter(\.isNumber).count >= 7
        default: return true
        }
    }

    /// Pick the fact whose value answers the question (each judged against its own fact); entered verbatim.
    static func text(_ f: FieldDescriptor, _ facts: [Fact], _ nli: any NLIScorer, t: Double, m: Double) throws -> Decision {
        let q = f.label.isEmpty ? questionText(f) : f.label
        // A sentence is not a short-text answer.
        let cands = facts.filter { $0.value.unicodeScalars.count <= 80 && formatOK(f.fieldType, $0.value) }
        var d = Decision(kind: .text)
        guard !cands.isEmpty else {
            d.reason = "no short fact values"
            return d
        }
        let probs = try nli.entail(cands.map { NLIPair($0.text, hyp(q, $0.value)) })
        var best: [(String, Double, Fact)] = []  // best fact per value, first-seen order
        for (x, p) in zip(cands, probs) {
            if let i = best.firstIndex(where: { $0.0 == x.value }) {
                if p > best[i].1 { best[i] = (x.value, p, x) }
            } else if p > -1 {
                best.append((x.value, p, x))
            }
        }
        let (winner, reason) = gate(best.map { ($0.0, $0.1) }, t: t, m: m)
        d.reason = reason
        if let winner, let hit = best.first(where: { $0.0 == winner }) {
            d.value = hit.2.value
            d.origin = "fact:\(hit.2.key)"
            d.confidence = hit.1
        }
        return d
    }

    /// Skill a years question asks about; nil = all roles.
    static func skillTerms(_ label: String) -> [String]? {
        let m = match(rx(#"\b(?:with|in|using)\s+(.+?)\s*\??$"#), label) ?? match(rx("years of (.+?) experience"), label)
        let term = (m?[1] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if totalTerms.contains(term) { return nil }
        return split(term, rx("/|,| or | and ", caseInsensitive: false))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }

    /// Option text -> [lo, hi) range in years; nil when the option isn't a numeric range.
    static func bucket(_ opt: String) -> (Double, Double)? {
        let o = opt.lowercased().replacingOccurrences(of: "–", with: "-")
        if fullmatch(#"\s*(none|0|no experience)\s*(years?)?\s*"#, o) != nil { return (0, 0) }  // never chosen
        if let g = match(rx(#"less than (\d+)"#), o) { return (0, Double(int(g[1]))) }
        if let g = match(rx(#"(?:more than|over) (\d+)"#), o) { return (Double(int(g[1])) + 1e-9, .infinity) }
        if let g = match(rx(#"(\d+)\s*\+"#), o) { return (Double(int(g[1])), .infinity) }
        // "3-5 years" covers 3.0 up to (not incl.) 6.0 when floored
        if let g = match(rx(#"(\d+)\s*-\s*(\d+)"#), o) { return (Double(int(g[1])), Double(int(g[2]) + 1)) }
        return nil
    }

    static func number(_ f: FieldDescriptor, _ p: Profile, _ today: Today) -> Decision {
        var d = Decision(kind: .number)
        guard search(yearsRe, f.label) else {
            d.reason = "only years can be computed; other counts are left for the user (NLI can't compare numbers)"
            return d
        }
        let terms = skillTerms(f.label)
        guard let yrs = yearsWith(p, terms, today) else {
            d.reason = terms.map { "no dated role mentions \($0)" } ?? "no dated roles"
            return d
        }
        let n = floor(yrs)
        let options = f.options ?? []
        if options.isEmpty {
            (d.value, d.origin, d.confidence, d.reason) = (String(Int(n)), "computed", 1, "\(f2(yrs)) years -> \(Int(n))")
            return d
        }
        let hits = options.filter { o in bucket(o).map { $0 != (0, 0) && $0.0 <= n && n < $0.1 } ?? false }
        if hits.count == 1 {
            (d.value, d.origin, d.confidence, d.reason) = (hits[0], "computed", 1, "\(f2(yrs)) years -> \(hits[0])")
        } else {
            d.reason = "\(f2(yrs)) years matches \(hits.count) options"
        }
        return d
    }

    static let refusalRe = rx(#"\b(cannot|can't|unable to|not able to|am not able to) (answer|provide|generate)|no (answer|information)"#
                              + #" (can|is|regarding)|not (explicitly )?stated in the (candidate )?profile|must return an empty"#)

    /// Plain essay text from the answer prompt, or "" when the model returned nothing usable:
    /// unwraps ["..."] / {"answer": ...}; a refusal is not an essay and goes back to the user.
    static func cleanEssay(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        s = Rx.replaceAll("<think>.*?</think>", in: s, with: "", options: [.dotMatchesLineSeparators])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if s.hasPrefix("```") {
            s = s.trimmingCharacters(in: CharacterSet(charactersIn: "`"))
            if s.hasPrefix("json") { s.removeFirst(4) }
            s = s.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if s.hasPrefix("[") || s.hasPrefix("{"),
           let v = try? JSONSerialization.jsonObject(with: Data(s.utf8), options: [.fragmentsAllowed]) {
            let items = v as? [Any] ?? [v]
            s = items.map { x in
                if let d = x as? [String: Any] { return d["answer"].map { "\($0)" } ?? "" }
                return "\(x)"
            }.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return search(refusalRe, s) ? "" : s
    }

    /// Every non-essay field: choice / text via NLI, years via date math, the rest skipped.
    static func decide(_ f: FieldDescriptor, _ p: Profile, _ facts: [Fact], _ nli: any NLIScorer, _ today: Today) throws -> Decision {
        let kind = classify(f)
        if kind == .skip { return Decision(kind: kind, reason: "field type left for the user") }
        if kind == .number { return number(f, p, today) }
        let sensitive = isSensitive(f)
        let threshold = sensitive ? max(t, tStrict) : t
        let top = topFacts(f, facts, k: topK, sensitive: sensitive).map(\.0)
        var d = try (kind == .choice ? choice : text)(f, top, nli, threshold, m)
        if sensitive { d.reason = "[strict] " + d.reason }
        return d
    }

    /// Existing `source` values only, so fill.js needs no change: extractive fills are
    /// "profile", essays "llm_generated" at confidence 0.5 (the AI-draft marker), the rest "skip".
    static func fieldValue(_ f: FieldDescriptor, _ d: Decision) -> FieldValue {
        guard d.filled else {
            return FieldValue(fieldId: f.fieldId, value: "", action: "skip", confidence: d.confidence, source: "skip")
        }
        return FieldValue(fieldId: f.fieldId, value: d.value, action: (f.options ?? []).isEmpty ? "fill" : "select",
                          confidence: d.confidence, source: d.origin == "essay" ? "llm_generated" : "profile")
    }

    /// Writes one essay answer; nil when no LLM is reachable or it refuses.
    public typealias EssayWriter = @Sendable (FieldDescriptor) async -> String?

    /// Pass 4 for `fields`. The NLI work runs while essays are drafted concurrently on the LLM.
    /// Throws when the NLI model fails, so the caller can fall back to the LLM path.
    static func fill(profile: Profile, fields: [FieldDescriptor], bank: [String: String], nli: any NLIScorer,
                     today: Today, essay: @escaping EssayWriter) async throws -> [FieldValue] {
        let facts = buildFacts(profile, bank: bank, today: today)
        let essays = fields.filter { classify($0) == .essay }
        async let drafts: [String: Decision] = withTaskGroup(of: (String, Decision).self) { group in
            for f in essays {
                group.addTask {
                    var d = Decision(kind: .essay)
                    guard let raw = await essay(f) else {
                        d.reason = "essay LLM failed"
                        return (f.fieldId, d)
                    }
                    d.value = cleanEssay(raw)
                    if d.filled {
                        (d.origin, d.confidence, d.reason) = ("essay", 0.5, "LLM draft, needs review")
                    } else {
                        d.reason = "LLM gave no usable essay: \(raw.prefix(120))"
                    }
                    return (f.fieldId, d)
                }
            }
            var out: [String: Decision] = [:]
            for await (id, d) in group { out[id] = d }
            return out
        }
        // Model inference blocks; keep it off the cooperative pool the essays await on.
        let decided = try await Task.detached(priority: .userInitiated) {
            var out: [String: Decision] = [:]
            for f in fields where classify(f) != .essay { out[f.fieldId] = try decide(f, profile, facts, nli, today) }
            return out
        }.value
        let byEssay = await drafts
        return fields.map { f in fieldValue(f, decided[f.fieldId] ?? byEssay[f.fieldId] ?? Decision()) }
    }
}
