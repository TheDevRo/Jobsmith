import Foundation

/// Job-fit score from the local NLI model, used when the scoring LLM is
/// unavailable. Port of `backend/nli/fit.py` (the NLI scoring bench's
/// equal-weight mode): keyword-bearing requirement lines from the posting, each
/// judged "is it met?" against the profile; score = mean P(met).
extension LocalNLI {
    static let maxLines = 20
    public static let reasoningPrefix = "Scored by the local model (beta)"
    static let keywordRe = Extractive.rx(
        #"\b(experience|years|degree|bachelor|master|proficien|knowledge|skill|familiar|certif|ability|"#
        + #"required|must|prefer|expertise|background)"#)
    static let bulletRe = Extractive.rx(#"^[\s•\-\*·▪◦●]+"#)
    static let lineSplitRe = Extractive.rx(#"\n+|(?<=[.!?;])\s+(?=[A-Z•\-\*])"#, caseInsensitive: false)

    /// Candidate requirement lines: split on newlines and sentence ends, 25-300 chars, keyword-bearing.
    static func requirementLines(_ description: String) -> [String] {
        let lines = Extractive.split(description, lineSplitRe)
            .map { bulletRe.stringByReplacingMatches(in: $0, range: NSRange($0.startIndex..., in: $0), withTemplate: "")
                .trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { (25...300).contains($0.unicodeScalars.count) }
        return Array(lines.filter { Extractive.search(keywordRe, $0) }.prefix(maxLines))
    }

    /// [whole profile] plus the pieces it splits into when it is too long for one premise.
    static func profilePieces(_ p: Profile) -> [String] {
        let skills = p.skills.joined(separator: ", ")
        let roles = p.experience.filter { !$0.title.isEmpty }
            .map { "\($0.title) at \($0.company), \($0.startDate)-\($0.endDate.isEmpty ? "present" : $0.endDate)" }
        let edu = p.education.filter { !$0.degree.isEmpty }
            .map { "\($0.degree) \($0.school)".trimmingCharacters(in: .whitespacesAndNewlines) }.joined(separator: ", ")
        let certs = p.certifications.joined(separator: ", ")
        let summary = p.summary.trimmingCharacters(in: .whitespacesAndNewlines)
        let whole = "The candidate's skills: \(skills). Experience: \(roles.joined(separator: "; ")). Education: \(edu). "
            + "Certifications: \(certs). \(summary)"
        var pieces = [summary, "The candidate's skills: \(skills).", "Education: \(edu)."]
        pieces += roles.map { "The candidate worked as \($0)." }
        if !certs.isEmpty { pieces.append("Certifications: \(certs).") }
        return [whole] + pieces.filter { !$0.trimmingCharacters(in: CharacterSet(charactersIn: " .:")).isEmpty }
    }

    /// The fit result, or nil when the posting has no requirement lines to judge.
    public static func fitScore(job: Job, profile: Profile, nli: any NLIScorer) throws -> FitResult? {
        let lines = requirementLines(job.description)
        guard !lines.isEmpty else { return nil }
        let hyps = lines.map { "The candidate meets this job requirement: \($0)" }
        let all = profilePieces(profile)
        var premises = [all[0]]
        if hyps.contains(where: { (nli.countTokens(NLIPair(all[0], $0)) ?? 0) > 512 }) {
            premises = Array(all.dropFirst())  // too long for one pass: judge each piece, best piece per line
        }
        let per = try premises.map { p in try nli.entail(hyps.map { NLIPair(p, $0) }) }
        let met = hyps.indices.map { i in per.map { $0[i] }.max() ?? 0 }
        let score = (1000 * met.reduce(0, +) / Double(met.count)).rounded() / 10
        let yes = zip(lines, met).filter { $0.1 > 0.5 }.map(\.0)
        let no = zip(lines, met).filter { $0.1 <= 0.5 }.map(\.0)
        let report: [String: Any] = ["matched_skills": yes, "missing_skills": no, "keywords": [String]()]
        return FitResult(score: score,
                         reasoning: "\(reasoningPrefix): meets \(yes.count) of \(lines.count) requirement lines.",
                         matchReportJSON: ScoreResponseParser.sanitizedMatchReportJSON(report))
    }
}
