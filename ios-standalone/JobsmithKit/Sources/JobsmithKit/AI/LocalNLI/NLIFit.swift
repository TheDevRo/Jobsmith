import Foundation

/// Job-fit score from the local NLI model, used when the scoring LLM is
/// unavailable. Port of `backend/nli/fit.py` (the NLI scoring bench's
/// equal-weight mode): keyword-bearing requirement lines from the posting, each
/// judged "is it met?" against the profile; score = mean P(met).
extension LocalNLI {
    static let maxLines = 20
    public static let reasoningPrefix = "Scored by Local match"
    static let keywordRe = Extractive.rx(
        #"\b(experience|years|degree|bachelor|master|proficien|knowledge|skill|familiar|certif|ability|"#
        + #"required|must|prefer|expertise|background)"#)
    static let bulletRe = Extractive.rx(#"^[\s•\-\*·▪◦●]+"#)
    static let lineSplitRe = Extractive.rx(#"\n+|(?<=[.!?;])\s+(?=[A-Z•\-\*])"#, caseInsensitive: false)

    /// The posting split on newlines and sentence ends, bullets stripped, 25-300 chars (no keyword filter).
    static func candidateLines(_ description: String) -> [String] {
        Extractive.split(description, lineSplitRe)
            .map { bulletRe.stringByReplacingMatches(in: $0, range: NSRange($0.startIndex..., in: $0), withTemplate: "")
                .trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { (25...300).contains($0.unicodeScalars.count) }
    }

    /// Candidate requirement lines: split on newlines and sentence ends, 25-300 chars, keyword-bearing.
    static func requirementLines(_ description: String) -> [String] {
        Array(candidateLines(description).filter { Extractive.search(keywordRe, $0) }.prefix(maxLines))
    }

    /// Premise + hypothesis are kept within this many tokens: the fixed input length of the
    /// on-device model (one model run per requirement line, no per-shape recompiles on the GPU).
    static let maxPairTokens = 256
    static let unitSplitRe = Extractive.rx(#"(?<=[.!?])\s+|\s*[\n•]+\s*"#)

    static func hypothesis(_ line: String) -> String { "The candidate meets this job requirement: \(line)" }

    /// (items that share words with `line`, best first; the rest in profile order).
    static func splitRelevant(_ line: String, _ items: [String]) -> (hits: [String], rest: [String]) {
        let pool = items.enumerated().map { Extractive.Fact(key: String($0.offset), text: $0.element, value: $0.element, category: "") }
        let hits = Extractive.rank(line, pool, k: items.count).map(\.0.value)
        return (hits, items.filter { !hits.contains($0) })
    }

    /// One premise per requirement line: roles, education and certifications always; then the
    /// skills and the summary sentences / role bullets that match the line; then the other
    /// skills and sentences, while premise + hypothesis fit `maxPairTokens`. The compact
    /// premise alone lost the long profiles' detail; per-line relevance keeps what each line
    /// needs. Mirrors `backend/nli/fit.py::line_premises`.
    static func linePremises(_ p: Profile, lines: [String], countTokens: (NLIPair) -> Int?) -> [String] {
        let roles = p.experience.filter { !$0.title.isEmpty }
            .map { "\($0.title) at \($0.company), \($0.startDate)-\($0.endDate.isEmpty ? "present" : $0.endDate)" }
        let edu = p.education.filter { !$0.degree.isEmpty }
            .map { "\($0.degree) \($0.school)".trimmingCharacters(in: .whitespacesAndNewlines) }.joined(separator: ", ")
        let certs = p.certifications.joined(separator: ", ")
        var units = Extractive.split(p.summary, unitSplitRe)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { $0.unicodeScalars.count > 2 }
        for r in p.experience {
            for b in r.bullets where !b.isEmpty {
                var t = Substring(b)
                while t.hasSuffix(".") { t = t.dropLast() }
                units.append("At \(r.company): \(t).")
            }
        }
        func size(_ prem: String, _ hyp: String) -> Int {
            countTokens(NLIPair(prem, hyp)) ?? (prem.count + hyp.count) / 4 + 3
        }
        func most(_ hi: Int, _ fits: (Int) -> Bool) -> Int {  // largest n in 0...hi with fits(n)
            var lo = 0, hi = hi
            while lo < hi { let mid = (lo + hi + 1) / 2; if fits(mid) { lo = mid } else { hi = mid - 1 } }
            return lo
        }
        return lines.map { line in
            let hyp = hypothesis(line)
            let sk = splitRelevant(line, p.skills), un = splitRelevant(line, units)
            func build(_ n: [Int]) -> String {  // matching skills, matching sentences, other skills, other sentences
                let skills = sk.hits.prefix(n[0]) + sk.rest.prefix(n[2])
                let sentences = un.hits.prefix(n[1]) + un.rest.prefix(n[3])
                return ("The candidate's skills: \(skills.joined(separator: ", ")). "
                        + "Experience: \(roles.joined(separator: "; ")). Education: \(edu). Certifications: \(certs). "
                        + sentences.joined(separator: " ")).trimmingCharacters(in: .whitespacesAndNewlines)
            }
            var n = [0, 0, 0, 0]
            for (i, hi) in [sk.hits.count, un.hits.count, sk.rest.count, un.rest.count].enumerated() {
                n[i] = most(hi) { k in var m = n; m[i] = k; return size(build(m), hyp) <= maxPairTokens }
            }
            return build(n)
        }
    }

    /// The fit result, or nil when the posting has no requirement lines to judge.
    public static func fitScore(job: Job, profile: Profile, nli: any NLIScorer) throws -> FitResult? {
        let lines = requirementLines(job.description)
        guard !lines.isEmpty else { return nil }
        let prems = linePremises(profile, lines: lines) { nli.countTokens($0) }
        let met = try nli.entail(zip(prems, lines).map { NLIPair($0, hypothesis($1)) })
        let score = (1000 * met.reduce(0, +) / Double(met.count)).rounded() / 10
        let yes = zip(lines, met).filter { $0.1 > 0.5 }.map(\.0)
        let no = zip(lines, met).filter { $0.1 <= 0.5 }.map(\.0)
        let report: [String: Any] = ["matched_skills": yes, "missing_skills": no, "keywords": [String]()]
        return FitResult(score: score,
                         reasoning: "\(reasoningPrefix): meets \(yes.count) of \(lines.count) requirement lines.",
                         matchReportJSON: ScoreResponseParser.sanitizedMatchReportJSON(report))
    }
}
