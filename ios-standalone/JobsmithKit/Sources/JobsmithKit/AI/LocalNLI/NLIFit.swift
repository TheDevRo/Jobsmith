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

    /// The profile premise is kept to this many tokens so premise + requirement line fits the
    /// model's 256-token input: 20 model runs per job. Splitting a long profile into pieces
    /// meant ~200 runs per job — over a minute on a phone, cut off by iOS mid-batch — and the
    /// NLI bench that validated this method used one compact premise too.
    static let premiseTokens = 190

    /// One compact premise: roles, education, certifications, then as many skills and summary
    /// words as fit. Mirrors `backend/nli/fit.py::premise`.
    static func premise(_ p: Profile, countTokens: (String) -> Int?) -> String {
        let skills = p.skills
        let roles = p.experience.filter { !$0.title.isEmpty }
            .map { "\($0.title) at \($0.company), \($0.startDate)-\($0.endDate.isEmpty ? "present" : $0.endDate)" }
        let edu = p.education.filter { !$0.degree.isEmpty }
            .map { "\($0.degree) \($0.school)".trimmingCharacters(in: .whitespacesAndNewlines) }.joined(separator: ", ")
        let certs = p.certifications.joined(separator: ", ")
        let words = p.summary.split(whereSeparator: \.isWhitespace).map(String.init)
        func size(_ text: String) -> Int { countTokens(text) ?? text.count / 4 }
        func build(_ nSkills: Int, _ nWords: Int) -> String {
            ("The candidate's skills: \(skills.prefix(nSkills).joined(separator: ", ")). "
             + "Experience: \(roles.joined(separator: "; ")). Education: \(edu). Certifications: \(certs). "
             + words.prefix(nWords).joined(separator: " ")).trimmingCharacters(in: .whitespaces)
        }
        func most(_ hi: Int, _ fits: (Int) -> Bool) -> Int {  // largest n in 0...hi with fits(n)
            var lo = 0, hi = hi
            while lo < hi { let mid = (lo + hi + 1) / 2; if fits(mid) { lo = mid } else { hi = mid - 1 } }
            return lo
        }
        var nSkills = skills.count
        if size(build(nSkills, 0)) > premiseTokens {  // even without the summary: trim the skills list
            nSkills = most(skills.count) { size(build($0, 0)) <= premiseTokens }
        }
        return build(nSkills, most(words.count) { size(build(nSkills, $0)) <= premiseTokens })
    }

    /// The fit result, or nil when the posting has no requirement lines to judge.
    public static func fitScore(job: Job, profile: Profile, nli: any NLIScorer) throws -> FitResult? {
        let lines = requirementLines(job.description)
        guard !lines.isEmpty else { return nil }
        let hyps = lines.map { "The candidate meets this job requirement: \($0)" }
        let prem = premise(profile) { nli.countTokens(NLIPair($0, "")) }
        let met = try nli.entail(hyps.map { NLIPair(prem, $0) })
        let score = (1000 * met.reduce(0, +) / Double(met.count)).rounded() / 10
        let yes = zip(lines, met).filter { $0.1 > 0.5 }.map(\.0)
        let no = zip(lines, met).filter { $0.1 <= 0.5 }.map(\.0)
        let report: [String: Any] = ["matched_skills": yes, "missing_skills": no, "keywords": [String]()]
        return FitResult(score: score,
                         reasoning: "\(reasoningPrefix): meets \(yes.count) of \(lines.count) requirement lines.",
                         matchReportJSON: ScoreResponseParser.sanitizedMatchReportJSON(report))
    }
}
