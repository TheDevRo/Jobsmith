import Foundation

/// Extractive Apply Assist pass 4 (Local AI model beta). Port of
/// `backend/auto_apply/extractive/`: fields left after passes 1-3 are answered
/// from the profile only (a form option, a verbatim profile value, or a date
/// computation), chosen by an NLI model behind a confidence gate. Essays go to
/// the LLM and come back flagged as AI drafts; anything else is left blank.
public enum Extractive {
    // Fact categories. Sensitive (EEO/legal) questions may only read EXPLICIT facts.
    static let legal = "legal", eeo = "eeo", contact = "contact", logistics = "logistics",
               role = "role", edu = "edu", cert = "cert", skill = "skill", summary = "summary",
               bank = "bank", computed = "computed"
    static let explicit: Set<String> = [legal, eeo, summary]

    /// One atomic profile item: `text` is what the NLI model reads, `value` is
    /// what gets typed into the form, unchanged.
    struct Fact: Equatable, Sendable {
        let key: String
        let text: String
        let value: String
        let category: String

        var declines: Bool { category == eeo && search(factDeclineRe, value) }
    }

    /// Year and month the date math counts "Present" as.
    struct Today: Sendable {
        let year: Int, month: Int
        init(year: Int, month: Int) { self.year = year; self.month = month }
        init(_ date: Date) {
            let c = Calendar(identifier: .gregorian).dateComponents([.year, .month], from: date)
            self.init(year: c.year ?? 2000, month: c.month ?? 1)
        }
        var index: Int { year * 12 + month - 1 }
    }

    static let factDeclineRe = rx(#"\b(decline|prefer not|don'?t wish|do not wish|not to answer|do not want|don'?t want|not disclose|rather not)\b"#)

    // MARK: - Date math

    static let monthAbbrevs = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    static let monthNames = ["January", "February", "March", "April", "May", "June", "July",
                             "August", "September", "October", "November", "December"]

    /// "2021-04", "2021-04-15", "04/2021", "April 2021", "2021", "Present" ->
    /// months since year 0 (nil if unparseable).
    static func parseMonth(_ raw: String, _ today: Today) -> Int? {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if ["present", "current", "now", "today", ""].contains(s) { return s.isEmpty ? nil : today.index }
        if let m = fullmatch(#"(\d{4})-(\d{1,2})(?:-\d{1,2})?"#, s) { return int(m[1]) * 12 + int(m[2]) - 1 }
        if let m = fullmatch(#"(\d{1,2})/(?:\d{1,2}/)?(\d{4})"#, s) { return int(m[2]) * 12 + int(m[1]) - 1 }
        if let m = fullmatch(#"([a-z]{3})[a-z]*\.? (\d{4})"#, s),
           let i = monthAbbrevs.firstIndex(of: m[1] ?? "") {
            return int(m[2]) * 12 + i
        }
        if let m = fullmatch(#"(\d{4})"#, s) { return int(m[1]) * 12 }  // a bare year counts from January
        return nil
    }

    /// [start, end] month indexes, end inclusive; nil if a date is unusable or reversed.
    static func roleSpan(_ r: WorkExperience, _ today: Today) -> (Int, Int)? {
        guard let a = parseMonth(r.startDate, today),
              let b = parseMonth(r.endDate.isEmpty ? "Present" : r.endDate, today), b >= a else { return nil }
        return (a, b)
    }

    /// Total months covered, overlaps merged, gaps excluded. nil if any role can't be dated.
    static func mergedMonths(_ roles: [WorkExperience], _ today: Today) -> Int? {
        var spans: [(Int, Int)] = []
        for r in roles {
            guard let s = roleSpan(r, today) else { return nil }  // refuse to guess from an undatable role
            spans.append(s)
        }
        var total = 0
        var cur: (Int, Int)?
        for (a, b) in spans.sorted(by: { $0 < $1 }) {
            if let c = cur, a <= c.1 + 1 {
                cur = (c.0, max(c.1, b))
            } else {
                if let c = cur { total += c.1 - c.0 + 1 }
                cur = (a, b)
            }
        }
        if let c = cur { total += c.1 - c.0 + 1 }
        return total
    }

    /// Uncovered month ranges between the first start and the last end.
    static func gaps(_ roles: [WorkExperience], _ today: Today) -> [(Int, Int)] {
        var out: [(Int, Int)] = []
        var end: Int?
        for (a, b) in roles.compactMap({ roleSpan($0, today) }).sorted(by: { $0 < $1 }) {
            if let e = end, a > e + 1 { out.append((e + 1, a - 1)) }
            end = end.map { max($0, b) } ?? b
        }
        return out
    }

    static func mentions(_ r: WorkExperience, _ term: String) -> Bool {
        let hay = ([r.title] + r.bullets).joined(separator: " ").lowercased()
        let pattern = "(?<![a-z0-9])" + NSRegularExpression.escapedPattern(for: term.lowercased()) + "(?![a-z0-9])"
        return search(rx(pattern, caseInsensitive: false), hay)
    }

    /// Years of experience (merged, fractional). terms nil = all roles, else the
    /// roles mentioning any term. nil when no role qualifies or dates are unusable.
    static func yearsWith(_ p: Profile, _ terms: [String]?, _ today: Today) -> Double? {
        let roles = terms.map { t in p.experience.filter { r in t.contains { mentions(r, $0) } } } ?? p.experience
        guard !roles.isEmpty, let m = mergedMonths(roles, today) else { return nil }
        return Double(m) / 12
    }

    // MARK: - Fact store

    static func yesNo(_ v: String) -> Bool? {
        switch v.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "yes", "y", "true": return true
        case "no", "n", "false": return false
        default: return nil
        }
    }

    static func monthName(_ s: String) -> String {
        guard let m = fullmatch(#"(\d{4})-(\d{1,2})"#, s.trimmingCharacters(in: .whitespacesAndNewlines)),
              (1...12).contains(int(m[2])) else { return s }
        return "\(monthNames[int(m[2]) - 1]) \(m[1] ?? "")"
    }

    static func sentences(_ text: String) -> [String] {
        split(text, rx(#"(?<=[.!?])\s+"#))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }

    /// All facts for one application. The iOS profile has no "over 18" field;
    /// it is "Yes", as in the LLM prompt's profile text.
    static func buildFacts(_ p: Profile, bank: [String: String] = [:], today: Today, over18: String = "Yes") -> [Fact] {
        var facts: [Fact] = []
        func add(_ key: String, _ text: String, _ value: String, _ cat: String) {
            let v = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !v.isEmpty { facts.append(Fact(key: key, text: text, value: v, category: cat)) }
        }

        // Contact / links
        add("full_name", "The candidate's name is \(p.fullName).", p.fullName, contact)
        add("email", "The candidate's email address is \(p.email).", p.email, contact)
        add("phone", "The candidate's phone number is \(p.phone).", p.phone, contact)
        add("location", "The candidate lives in \(p.location).", p.location, contact)
        add("city", "The candidate's city is \(p.city).", p.city, contact)
        add("state", "The candidate's state is \(p.state).", p.state, contact)
        add("zip", "The candidate's zip code is \(p.zipCode).", p.zipCode, contact)
        add("street", "The candidate's street address is \(p.streetAddress).", p.streetAddress, contact)
        add("country", "The candidate lives in the country \(p.country).", p.country, contact)
        add("linkedin", "The candidate's LinkedIn profile URL is \(p.linkedin).", p.linkedin, contact)
        add("github", "The candidate's GitHub URL is \(p.github).", p.github, contact)
        add("portfolio", "The candidate's personal website and portfolio URL is \(p.portfolio).", p.portfolio, contact)

        // Legal / eligibility: a clear sentence when the value is an unambiguous yes/no.
        if let wa = yesNo(p.workAuthorization) {
            add("work_authorization", wa ? "The candidate is legally authorized to work in the United States."
                : "The candidate is not authorized to work in the United States.", p.workAuthorization, legal)
        } else {
            add("work_authorization", "The candidate's work authorization is: \(p.workAuthorization).", p.workAuthorization, legal)
        }
        if let sp = yesNo(p.sponsorshipRequired) {
            add("sponsorship", sp ? "The candidate will require visa sponsorship to work."
                : "The candidate does not require visa sponsorship, now or in the future.", p.sponsorshipRequired, legal)
        }
        if let o18 = yesNo(over18) {
            add("over_18", o18 ? "The candidate is at least 18 years old." : "The candidate is under 18 years old.", over18, legal)
        }

        // EEO: only what the profile states. A decline is stated as a decline.
        for (key, label, v) in [("gender", "gender", p.gender), ("race", "race/ethnicity", p.raceEthnicity),
                                ("veteran", "veteran status", p.veteranStatus),
                                ("disability", "disability status", p.disabilityStatus)] {
            if !v.isEmpty && search(factDeclineRe, v) {
                add(key, "The candidate declines to disclose their \(label).", v, eeo)
            } else {
                add(key, "The candidate's \(label) is: \(v).", v, eeo)
            }
        }

        // Logistics
        add("salary", "The candidate's desired salary is \(p.desiredSalary).", p.desiredSalary, logistics)
        add("available_start", "The candidate can start: \(p.availableStart).", p.availableStart, logistics)
        add("notice_period", "The candidate's notice period is \(p.noticePeriod).", p.noticePeriod, logistics)

        // Roles
        for (i, r) in p.experience.enumerated() {
            let end = r.endDate.isEmpty ? "Present" : r.endDate
            let cur = ["present", "current", "now"].contains(end.trimmingCharacters(in: .whitespacesAndNewlines).lowercased())
            let when = "from \(monthName(r.startDate)) to \(cur ? "present" : monthName(r.endDate))"
            if i == 0 && cur {
                add("current_company", "The candidate currently works at \(r.company).", r.company, role)
                add("current_title", "The candidate's current job title is \(r.title).", r.title, role)
            }
            add("role\(i).company", "The candidate worked at \(r.company) as \(r.title) \(when).", r.company, role)
            add("role\(i).title", "The candidate held the job title \(r.title) at \(r.company) \(when).", r.title, role)
            for (j, b) in r.bullets.enumerated() {
                add("role\(i).bullet\(j)", "At \(r.company), the candidate: \(b).", b, role)
            }
        }

        // Education / certifications / skills
        for (i, e) in p.education.enumerated() {
            add("edu\(i).degree", "The candidate earned a \(e.degree) degree from \(e.school) in \(e.year).", e.degree, edu)
            add("edu\(i).school", "The candidate studied at \(e.school).", e.school, edu)
            add("edu\(i).year", "The candidate graduated in \(e.year).", e.year, edu)
        }
        for (i, c) in p.certifications.enumerated() {
            add("cert\(i)", "The candidate holds the \(c) certification.", c, cert)
        }
        for (i, s) in p.skills.enumerated() {
            add("skill\(i)", "The candidate has experience with \(s).", s, skill)
        }

        // Summary, one sentence per fact (keeps premises short for the 512-token limit)
        for (i, s) in sentences(p.summary).enumerated() {
            add("summary\(i)", "The candidate says: \(s)", s, summary)
        }

        // Computed
        if let yrs = yearsWith(p, nil, today) {
            add("years_total", "The candidate has \(Int(yrs)) years of work experience.", String(Int(yrs)), computed)
        }
        if !p.experience.isEmpty {
            let longGaps = gaps(p.experience, today).filter { $0.1 - $0.0 + 1 > 6 }
            add("gaps", longGaps.isEmpty
                ? "The candidate has had no gaps in employment longer than 6 months since their first listed job."
                : "The candidate has had a gap in employment longer than 6 months.",
                longGaps.isEmpty ? "No" : "Yes", computed)
        }

        // Answer bank (placeholders never count as answers). Sorted: a Swift
        // dictionary has no stable order and fact order breaks retrieval ties.
        for (k, v) in bank.sorted(by: { $0.key < $1.key }) where !v.isEmpty && !(v.hasPrefix("<") && v.hasSuffix(">")) {
            add("bank.\(k)", "The candidate's saved answer to '\(k.replacingOccurrences(of: "_", with: " "))' is: \(v)", v, Extractive.bank)
        }
        return facts
    }

    // MARK: - Regex helpers (Python `re` semantics the port relies on)

    static func rx(_ pattern: String, caseInsensitive: Bool = true) -> NSRegularExpression {
        // Patterns are constants or escaped terms; a bad one is a programmer error.
        try! NSRegularExpression(pattern: pattern, options: caseInsensitive ? [.caseInsensitive] : [])
    }

    static func search(_ re: NSRegularExpression, _ s: String) -> Bool {
        re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }

    /// Groups of the first match ([0] = whole match), like `re.search`.
    static func match(_ re: NSRegularExpression, _ s: String) -> [String?]? {
        guard let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) else { return nil }
        return (0..<m.numberOfRanges).map { Range(m.range(at: $0), in: s).map { String(s[$0]) } }
    }

    /// `re.fullmatch`, case-sensitive like the Python call sites.
    static func fullmatch(_ pattern: String, _ s: String) -> [String?]? {
        match(rx(#"\A(?:"# + pattern + #")\z"#, caseInsensitive: false), s)
    }

    /// `re.split` without capture groups.
    static func split(_ s: String, _ re: NSRegularExpression) -> [String] {
        var out: [String] = []
        var cursor = s.startIndex
        for m in re.matches(in: s, range: NSRange(s.startIndex..., in: s)) {
            guard let r = Range(m.range, in: s) else { continue }
            out.append(String(s[cursor..<r.lowerBound]))
            cursor = r.upperBound
        }
        out.append(String(s[cursor...]))
        return out
    }

    static func int(_ s: String?) -> Int { Int(s ?? "") ?? 0 }
}
