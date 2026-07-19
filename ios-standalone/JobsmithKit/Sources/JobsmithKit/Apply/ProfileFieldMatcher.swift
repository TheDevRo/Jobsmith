import Foundation

/// Deterministic profile→field matching, ported from
/// `backend/auto_apply/field_matcher.py`.
///
/// Runs BEFORE the answer bank and the LLM in FieldMapper.map. Matches the
/// common ~90% of application-form fields (contact info, address, links,
/// salary, work authorization, EEO, education, availability) against the
/// user profile with regex/keyword rules.
///
/// Matching signals, in priority order:
///   1. The HTML `autocomplete` attribute (exact token — highest precision).
///   2. Ordered regex rules over a normalized haystack built from the field's
///      label, name, placeholder, id, autocomplete and extra_context.
///
/// The iOS `Profile` now models the same middle_name / street_address_2 /
/// country / EEO answers desktop does, so those getters read the profile
/// directly. Fields the iOS profile still lacks behave as the Python defaults:
/// the ATS password is empty, over_18 is "Yes". EEO getters still fall back to
/// the decline option (or "Prefer not to answer") when the profile value is
/// empty.
public enum ProfileFieldMatcher {

    // Types a rule may apply to. "select" covers native selects AND combobox
    // widgets (the snapshot maps both to "select").
    static let texty: Set<String> = ["text", "email", "tel", "url", "number",
                                     "textarea", "select", "date", "password"]
    static let choice: Set<String> = ["select", "radio", "checkbox", "text"]

    /// Options that mean "decline to answer" on EEO widgets.
    static let declineHints = [
        "prefer not", "decline", "do not wish", "don't wish", "dont wish",
        "not wish", "choose not", "rather not", "no answer", "not to say",
        "not specified", "don't want", "do not want", "not disclose",
    ]

    /// Placeholder options that must never be picked during fuzzy fallback.
    static let placeholderRe = regex(#"^(select|choose|please|pick)\b|^[-–—.\s]*$"#)

    static let usStates: [String: String] = [
        "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
        "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
        "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
        "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
        "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
        "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
        "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
        "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
        "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
        "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
        "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
        "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
        "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
    ]
    static let usStatesRev: [String: String] = {
        var rev: [String: String] = [:]
        for (abbr, full) in usStates { rev[full] = abbr }
        return rev
    }()

    static let countryAliases: [(canonical: String, aliases: [String])] = [
        ("united states", ["usa", "us", "united states of america", "america", "u s a", "u s"]),
        ("united kingdom", ["uk", "great britain", "england"]),
        ("canada", ["ca"]),
    ]

    /// Degree strings ("BS Computer Science") → the education-level buckets
    /// ATS dropdowns actually offer ("Bachelor's Degree").
    static let degreeLevels: [(re: NSRegularExpression, expansions: [String])] = [
        (regex(#"\b(ph\.?d|doctor)"#), ["phd", "doctorate", "doctoral degree"]),
        (regex(#"\bmba\b"#), ["mba", "master's degree", "masters"]),
        (regex(#"\b(ms|m\.?s\.?c?|ma|m\.a|master)\b"#), ["master's degree", "masters", "master"]),
        (regex(#"\b(bs|b\.?s\.?c?|ba|b\.a|bachelor)\b"#), ["bachelor's degree", "bachelors", "bachelor"]),
        (regex(#"\b(associate|a\.?a\.?s?)\b"#), ["associate's degree", "associate degree", "associate"]),
    ]

    // ------------------------------------------------------------------
    // Text helpers
    // ------------------------------------------------------------------

    static func regex(_ pattern: String) -> NSRegularExpression {
        // Rules are compile-time constants; a bad pattern is a programmer error.
        try! NSRegularExpression(pattern: pattern)
    }

    static func searches(_ re: NSRegularExpression, _ s: String) -> Bool {
        re.firstMatch(in: s, range: NSRange(s.startIndex..<s.endIndex, in: s)) != nil
    }

    static func norm(_ s: String) -> String {
        let cleaned = Rx.replaceAll(#"[^a-z0-9+#.\s]"#, in: s.lowercased(), with: " ")
        return Rx.replaceAll(#"\s+"#, in: cleaned, with: " ")
            .trimmingCharacters(in: .whitespaces)
    }

    /// Split camelCase so Workday-style ids ("workExperience-1--startDate")
    /// become rule-matchable words. Applied to haystacks only — option labels
    /// like "PhD" must not be split.
    static func decamel(_ s: String) -> String {
        Rx.replaceAll("(?<=[a-z0-9])(?=[A-Z])", in: s, with: " ")
    }

    static func tokens(_ s: String) -> Set<String> {
        Set(norm(s).split(separator: " ").map(String.init))
    }

    /// Return [value, alias1, ...] for state/country/degree style values.
    static func expandCandidates(_ value: String) -> [String] {
        let v = norm(value)
        var out = [value]
        if let full = usStates[v] {
            out.append(full)
        } else if let abbr = usStatesRev[v] {
            out.append(abbr)
        }
        for (canonical, aliases) in countryAliases {
            if v == canonical {
                out.append(contentsOf: aliases)
            } else if aliases.contains(v) {
                out.append(canonical)
            }
        }
        for (re, expansions) in degreeLevels where searches(re, v) {
            out.append(contentsOf: expansions)
            break
        }
        return out
    }

    /// 0-100 similarity between a desired value and an option label.
    static func optionScore(want: String, opt: String) -> Int {
        let w = norm(want), t = norm(opt)
        if w.isEmpty || t.isEmpty { return 0 }
        if w == t { return 100 }
        let wt = tokens(w), tt = tokens(t)
        if w == "yes" || w == "no" {
            if w == "yes" {
                if Rx.first(#"^y(es)?\b"#, in: t) != nil { return 90 }
                return tt.contains("yes") ? 80 : 0
            }
            if Rx.first(#"^n(o)?\b"#, in: t) != nil { return 90 }
            return tt.intersection(["no", "not", "none", "never"]).isEmpty ? 0 : 75
        }
        if wt == tt { return 95 }
        if wt.isSubset(of: tt) { return max(60, 88 - (tt.count - wt.count)) }
        if tt.isSubset(of: wt) { return max(60, 80 - (wt.count - tt.count)) }
        let inter = wt.intersection(tt).count
        if inter > 0 {
            let jac = Double(inter) / Double(wt.union(tt).count)
            let bonus = (t.contains(w) || w.contains(t)) ? 25 : 0
            // Python round() is banker's rounding.
            return Int((40 * jac).rounded(.toNearestOrEven)) + bonus
        }
        return 0
    }

    /// Pick the option text that best matches `value`, or nil.
    public static func bestOption(value: String, options: [String],
                                  threshold: Int = 55) -> String? {
        guard !value.isEmpty, !options.isEmpty else { return nil }
        let candidates = expandCandidates(value)
        var best: String?
        var bestScore = 0
        for opt in options {
            if searches(placeholderRe, norm(opt)) { continue }
            let score = candidates.map { optionScore(want: $0, opt: opt) }.max() ?? 0
            // Prefer shorter options on ties (avoids "No" → "Not applicable"-style grabs).
            if score > bestScore || (score == bestScore && best != nil && opt.count < best!.count) {
                best = opt
                bestScore = score
            }
        }
        return bestScore >= threshold ? best : nil
    }

    static func declineOption(_ options: [String]?) -> String? {
        for opt in options ?? [] {
            let t = norm(opt)
            if declineHints.contains(where: { t.contains($0) }) { return opt }
        }
        return nil
    }

    // ------------------------------------------------------------------
    // Repeating-section entry index
    // ------------------------------------------------------------------

    /// Which work-history / education entry a field belongs to.
    ///
    /// Greenhouse/Rails array names ("...[educations_attributes][1][school]")
    /// are 0-based; Workday-style separator ids ("workExperience-2--company")
    /// are 1-based. Fields with no index belong to entry 0.
    static func entryIndex(_ f: FieldDescriptor) -> Int {
        for src in [f.name, f.fieldId] {
            if let g = Rx.first(#"\[(\d{1,2})\]"#, in: src), let d = g[1], let n = Int(d) {
                return n
            }
            if let g = Rx.first(#"(?:^|[._\-])(\d{1,2})(?:[._\-]|$)"#, in: src),
               let d = g[1], let n = Int(d) {
                return max(0, n - 1)
            }
        }
        return 0
    }

    static func expAt(_ p: Profile, _ f: FieldDescriptor) -> WorkExperience? {
        let idx = entryIndex(f)
        return p.experience.indices.contains(idx) ? p.experience[idx] : nil
    }

    static func eduAt(_ p: Profile, _ f: FieldDescriptor) -> Education? {
        let idx = entryIndex(f)
        return p.education.indices.contains(idx) ? p.education[idx] : nil
    }

    // ------------------------------------------------------------------
    // Value getters
    // ------------------------------------------------------------------

    /// Rough total years across all work history entries — port of
    /// UserProfile.years_of_experience().
    public static func yearsOfExperience(_ p: Profile) -> Int {
        let formats = ["yyyy-MM", "yyyy-MM-dd", "MM/dd/yyyy", "MMMM yyyy", "MMM yyyy", "yyyy"]
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        func parse(_ dateStr: String) -> Date? {
            let s = String(dateStr.trimmingCharacters(in: .whitespaces).prefix(10))
            for fmt in formats {
                df.dateFormat = fmt
                if let d = df.date(from: s) { return d }
            }
            return nil
        }
        var total = 0
        for exp in p.experience {
            guard let start = parse(exp.startDate) else { continue }
            let end: Date
            if ["present", "current", "now"].contains(exp.endDate.lowercased()) {
                end = Date()
            } else if let e = parse(exp.endDate) {
                end = e
            } else {
                continue
            }
            let days = Int(floor(end.timeIntervalSince(start) / 86_400))
            total += max(0, days / 365)
        }
        return total
    }

    typealias Getter = @Sendable (Profile, FieldDescriptor, String) -> String

    static func nameParts(_ p: Profile) -> [String] {
        p.fullName.split(separator: " ").map(String.init)
    }

    static let firstName: Getter = { p, _, _ in nameParts(p).first ?? "" }
    static let lastName: Getter = { p, _, _ in
        let parts = nameParts(p)
        return parts.count > 1 ? parts.last! : ""
    }

    static let phoneCountryCode: Getter = { p, _, _ in
        if let g = Rx.first(#"^\s*(\+\d{1,3})"#, in: p.phone), let code = g[1] {
            return code
        }
        // Port of desktop _phone_country_code: +1 only for a US profile;
        // for another country an unknown code is left to the LLM rather
        // than confidently misfilled.
        let c = norm(p.country)
        return ["united states", "usa", "us", ""].contains(c) ? "+1" : ""
    }

    static let countryGetter: Getter = { p, _, _ in
        p.country.isEmpty ? "United States" : p.country
    }

    static let city: Getter = { p, _, _ in
        if !p.city.isEmpty { return p.city }
        return p.location.split(separator: ",", omittingEmptySubsequences: false)
            .first.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
    }

    static let state: Getter = { p, _, _ in
        if !p.state.isEmpty { return p.state }
        let parts = p.location.split(separator: ",", omittingEmptySubsequences: false)
        return parts.count > 1 ? parts[1].trimmingCharacters(in: .whitespaces) : ""
    }

    static let location: Getter = { p, _, _ in
        if !p.location.isEmpty { return p.location }
        if !p.city.isEmpty && !p.state.isEmpty { return "\(p.city), \(p.state)" }
        return ""
    }

    static let yearsExperienceGetter: Getter = { p, _, _ in
        p.experience.isEmpty ? "" : String(yearsOfExperience(p))
    }

    static let currentCompany: Getter = { p, f, _ in expAt(p, f)?.company ?? "" }
    static let currentTitle: Getter = { p, f, _ in expAt(p, f)?.title ?? "" }
    static let expStart: Getter = { p, f, _ in expAt(p, f)?.startDate ?? "" }
    static let expEnd: Getter = { p, f, _ in expAt(p, f)?.endDate ?? "" }
    static let expDescription: Getter = { p, f, _ in
        guard let e = expAt(p, f), !e.bullets.isEmpty else { return "" }
        return e.bullets.map { "• \($0)" }.joined(separator: "\n")
    }

    static let school: Getter = { p, f, _ in eduAt(p, f)?.school ?? "" }
    static let degree: Getter = { p, f, _ in eduAt(p, f)?.degree ?? "" }
    static let gradYear: Getter = { p, f, _ in eduAt(p, f)?.year ?? "" }

    static let skillsGetter: Getter = { p, _, _ in
        p.skills.isEmpty ? "" : p.skills.joined(separator: ", ")
    }

    /// Hispanic/Latino yes-no derived from race_ethnicity — mirrors Python's
    /// `_hispanic`. Empty when unset, so the EEO decline fallback kicks in.
    static let hispanic: Getter = { p, _, _ in
        let r = norm(p.raceEthnicity)
        if r.isEmpty { return "" }
        return (r.contains("hispanic") || r.contains("latin")) ? "Yes" : "No"
    }

    static func attr(_ get: @escaping @Sendable (Profile) -> String) -> Getter {
        { p, _, _ in get(p) }
    }

    static func const(_ value: String) -> Getter {
        { _, _, _ in value }
    }

    // ------------------------------------------------------------------
    // Rules
    // ------------------------------------------------------------------

    struct Rule: @unchecked Sendable {
        let key: String
        let re: NSRegularExpression
        let getter: Getter
        let negRe: NSRegularExpression?
        let types: Set<String>
        let eeo: Bool
        let optionsOnly: Bool
        let confidence: Double
    }

    static func rule(_ key: String, _ pattern: String, _ getter: @escaping Getter,
                     negative: String = "", types: Set<String> = texty,
                     eeo: Bool = false, optionsOnly: Bool = false,
                     confidence: Double = 0.95) -> Rule {
        Rule(key: key, re: regex(pattern), getter: getter,
             negRe: negative.isEmpty ? nil : regex(negative),
             types: types, eeo: eeo, optionsOnly: optionsOnly, confidence: confidence)
    }

    /// Order matters — first matching rule with a non-empty resolvable value wins.
    static let rules: [Rule] = [
        // --- Names (specific before generic "name") ---
        rule("first_name", #"\b(first name|given name|fname|forename)\b"#, firstName),
        rule("last_name", #"\b(last name|family name|surname|lname)\b"#, lastName),
        rule("middle_name", #"\bmiddle (name|initial)\b"#, attr { $0.middleName }),
        rule("preferred_name", #"\b(preferred name|nickname|goes by|known as)\b"#, firstName),
        rule("full_name",
             #"\b(full name|legal name|your name|candidate name|applicant name|complete name)\b|^name$"#,
             attr { $0.fullName },
             negative: #"\b(user ?name|company|employer|school|university|referr|recruiter|reference|manager|contact person|father|mother|emergency)\b"#),

        // --- Contact ---
        rule("email", #"\be ?mail\b"#, attr { $0.email }),
        rule("phone_country_code", #"\b(country code|phone code|dial code)\b"#, phoneCountryCode),
        rule("phone_type", #"\b(phone (device )?type|device type)\b"#, const("Mobile"), confidence: 0.8),
        rule("phone", #"\b(phone|mobile|cell|telephone|contact number)\b"#, attr { $0.phone },
             negative: #"\bext(ension)?\b"#),

        // --- Address ---
        rule("address_line2", #"\b(address line ?2|line ?2|apt|apartment|suite|unit number|unit)\b"#,
             attr { $0.streetAddress2 }),
        rule("street_address", #"\b(street address|address line ?1|home address|mailing address|street|address)\b"#,
             attr { $0.streetAddress }, negative: #"\b(email|line ?2|country|city|state|zip|postal|web)\b"#),
        rule("city", #"\b(city|town|municipality)\b"#, city),
        rule("zip", #"\b(zip|postal( code)?|postcode)\b"#, attr { $0.zipCode }),
        rule("state", #"\b(state|province|county)\b"#, state,
             negative: #"\b(united states|statement)\b"#),
        rule("country", #"\bcountry\b"#, countryGetter, negative: #"\bcountry code\b"#),
        rule("location", #"\b(location|city and state|where (are you|do you) (located|based|live|reside))\b"#,
             location),

        // --- Links ---
        rule("linkedin", #"\blinked ?in\b"#, attr { $0.linkedin }),
        rule("github", #"\bgit ?hub\b"#, attr { $0.github }),
        rule("portfolio", #"\b(portfolio|personal (web ?site|site|url)|website|web ?site url|other url)\b"#,
             attr { $0.portfolio }, negative: #"\b(company|employer) (web ?site|url)\b"#),

        // --- Repeating work-history / education entries ---
        // Must precede the availability rules: an employment/education "Start
        // Date" must never be answered with available_start. First-match-wins
        // means an empty getter value also acts as a guard (falls to the LLM
        // instead of a wrong deterministic fill).
        rule("exp_company",
             #"(?=.*\b(employments?|work ?experience|work ?history|previous ?employer)\b)(?=.*\b(company|employer)\b)"#,
             currentCompany),
        rule("exp_title",
             #"(?=.*\b(employments?|work ?experience|work ?history)\b)(?=.*\b(title|role|position)\b)"#,
             currentTitle),
        rule("exp_start",
             #"(?=.*\b(employments?|work ?experience|work ?history)\b)(?=.*\b(start|from)\b)"#,
             expStart),
        rule("exp_end",
             #"(?=.*\b(employments?|work ?experience|work ?history)\b)(?=.*\b(end|until|to date)\b)"#,
             expEnd),
        rule("exp_description",
             #"(?=.*\b(employments?|work ?experience|work ?history|position)\b)(?=.*\b(description|duties|responsibilities)\b)"#,
             expDescription),
        rule("edu_end",
             #"(?=.*\b(educations?|school|university|degree)\b)(?=.*\b(end|graduation|completion|to date)\b)"#,
             gradYear),
        rule("edu_start",  // no start-year in the profile — block availability misfill
             #"(?=.*\b(educations?|school|university|degree)\b)(?=.*\bstart\b)"#,
             const("")),

        // --- Compensation / availability ---
        rule("salary", #"\b(salary|compensation|desired pay|expected pay|pay (rate|expectation|requirement)|rate of pay|hourly rate)\b"#,
             attr { $0.desiredSalary }),
        rule("notice_period", #"\b(notice period|notice required|weeks? (of )?notice|current notice)\b"#,
             attr { $0.noticePeriod }),
        rule("start_date", #"\b(start date|earliest (possible )?(start|date)|available to start|availability date|date available|when (can|could) you start)\b"#,
             attr { $0.availableStart }),

        // --- Work authorization / screening ---
        rule("work_auth", #"\b(work authorization|authoriz(ed|ation) to work|legally (authorized|eligible|able|permitted)|eligible to work|right to work|work permit|lawfully (work|employed)|work eligibility)\b"#,
             attr { $0.workAuthorization }, types: choice),
        rule("sponsorship", #"\bsponsor"#, attr { $0.sponsorshipRequired }, types: choice),
        rule("over_18", #"\b(at least 18|18 (years|or older)|over 18|minimum age|legal age|age requirement)\b"#,
             const("Yes"), types: choice),
        rule("agree_terms", #"\b(i (agree|certify|acknowledge|consent|confirm|accept)|terms (and|&) conditions|privacy (policy|notice)|certify that|acknowledge)\b"#,
             const("Yes"), types: ["checkbox"], confidence: 0.55),

        // --- EEO / demographics. The profile now carries these answers; when a
        // value is set it resolves against the field's options (or fills the
        // text), and when it's empty `eeo: true` routes to the decline option
        // ("Prefer not to answer" for free-text). ---
        rule("hispanic", #"\b(hispanic|latino|latinx)\b"#, hispanic, types: choice, eeo: true),
        rule("gender", #"\bgender\b|\bsex\b"#, attr { $0.gender },
             negative: #"\b(orientation|sexual)\b"#, types: choice, eeo: true),
        rule("race", #"\b(race|ethnic)"#, attr { $0.raceEthnicity }, types: choice, eeo: true),
        rule("veteran", #"\b(veteran|military|armed forces|uniformed service)\b"#,
             attr { $0.veteranStatus }, types: choice, eeo: true),
        rule("disability", #"\bdisab"#, attr { $0.disabilityStatus }, types: choice, eeo: true),
        rule("eeo_decline_only", #"\b(sexual orientation|lgbtq|transgender|pronoun)\b"#,
             const(""), types: choice, eeo: true, optionsOnly: true),

        // --- Experience / education ---
        rule("years_experience", #"\b(years of (\w+ )?experience|experience in years|how many years)\b"#,
             yearsExperienceGetter,
             // Skill-specific ("years of experience with Python") must not be
             // answered with total career years — leave those to the LLM.
             negative: #"\b(with|using)\b|experience in (?!years)\w"#),
        rule("current_company", #"\b(current (employer|company)|most recent (employer|company)|present employer|company name|employer name)\b"#,
             currentCompany),
        rule("current_title", #"\b((current|most recent|present) (job )?(title|role|position)|job title)\b"#,
             currentTitle),
        rule("school", #"\b(school|university|college|alma mater|institution)\b"#, school),
        rule("degree", #"\b(degree|education level|highest (level of )?education|qualification)\b"#, degree),
        rule("grad_year", #"\bgraduat"#, gradYear),
        rule("skills", #"\bskills?\b"#, skillsGetter,
             negative: #"\b(why|describe|how|what makes)\b"#,
             types: ["text", "textarea"]),

        // --- Credentials (never let the LLM near these; the iOS profile
        // stores no ATS password, so the empty getter skips the field) ---
        rule("password", #"\bpassword\b"#, const(""), types: ["password", "text"]),
    ]

    /// autocomplete attribute → rule key (exact token match, checked first).
    static let autocompleteMap: [String: String] = [
        "given-name": "first_name",
        "additional-name": "middle_name",
        "family-name": "last_name",
        "name": "full_name",
        "email": "email",
        "tel": "phone",
        "tel-national": "phone",
        "tel-country-code": "phone_country_code",
        "street-address": "street_address",
        "address-line1": "street_address",
        "address-line2": "address_line2",
        "address-level2": "city",
        "address-level1": "state",
        "postal-code": "zip",
        "country": "country",
        "country-name": "country",
        "organization": "current_company",
        "organization-title": "current_title",
        "url": "portfolio",
        "new-password": "password",
        "current-password": "password",
    ]

    static let rulesByKey: [String: Rule] = {
        var byKey: [String: Rule] = [:]
        for r in rules { byKey[r.key] = r }
        return byKey
    }()

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    /// Deterministically resolve as many fields as possible from the profile.
    ///
    /// Returns {field_id: FieldValue} for resolved fields only. Unresolved
    /// fields are simply absent — the caller sends them on to the answer bank
    /// and the LLM.
    public static func matchProfileFields(profile: Profile,
                                          fields: [FieldDescriptor]) -> [String: FieldValue] {
        var out: [String: FieldValue] = [:]
        for f in fields {
            if let fv = matchOne(profile: profile, field: f) {
                out[f.fieldId] = fv
            }
        }
        return out
    }

    static func findRule(ftype: String, field f: FieldDescriptor, hay: String) -> Rule? {
        for r in rules {
            if !r.types.contains(ftype) { continue }
            if r.optionsOnly && (f.options ?? []).isEmpty { continue }
            if !searches(r.re, hay) { continue }
            if let neg = r.negRe, searches(neg, hay) { continue }
            return r
        }
        return nil
    }

    /// Resolve a single field, or nil when no rule applies / no safe value.
    public static func matchOne(profile: Profile, field f: FieldDescriptor) -> FieldValue? {
        let ftype = (f.fieldType.isEmpty ? "text" : f.fieldType).lowercased()
        if ftype == "file" {
            return nil  // handled by the dedicated file phase
        }

        // Two-pass haystack: the field's own label/name/placeholder first;
        // extra_context (fieldset legend / section heading) only as a fallback —
        // group context is shared across sibling fields and must not outvote a
        // field's own label (e.g. a "Sponsorship?" field inside a
        // "Work Authorization" section).
        let ownParts = [f.label, f.name, f.placeholder, f.fieldId, f.autocomplete]
            .filter { !$0.isEmpty }
        let hayOwn = norm(decamel(ownParts.joined(separator: " ")))
        let fullParts = [hayOwn, decamel(f.extraContext)].filter { !$0.isEmpty }
        let hayFull = norm(fullParts.joined(separator: " "))
        if hayFull.isEmpty { return nil }

        var matched: Rule?
        var hay = hayOwn
        let ac = f.autocomplete.trimmingCharacters(in: .whitespaces).lowercased()
        if let key = autocompleteMap[ac], let r = rulesByKey[key] {
            matched = r
            if !r.types.contains(ftype) && ftype != "select" {
                matched = nil
            }
        }

        if matched == nil && !hayOwn.isEmpty {
            matched = findRule(ftype: ftype, field: f, hay: hayOwn)
        }
        if matched == nil && hayFull != hayOwn {
            matched = findRule(ftype: ftype, field: f, hay: hayFull)
            hay = hayFull
        }

        guard let rule = matched else { return nil }

        var value = rule.getter(profile, f, hay)
            .trimmingCharacters(in: .whitespacesAndNewlines)

        // Resolve against the field's options so we always emit a clickable choice.
        let options = f.options ?? []
        if !options.isEmpty {
            var resolved = value.isEmpty ? nil : bestOption(value: value, options: options)
            if resolved == nil && rule.eeo {
                resolved = declineOption(options)
            }
            guard let picked = resolved else {
                return nil  // nothing safe to click — let the LLM try
            }
            value = picked
        } else if value.isEmpty {
            if rule.eeo && (ftype == "text" || ftype == "textarea") {
                value = "Prefer not to answer"
            } else {
                return nil
            }
        }

        let action = !options.isEmpty ? "select" : (ftype == "checkbox" ? "check" : "fill")
        return FieldValue(fieldId: f.fieldId, value: value, action: action,
                          confidence: rule.confidence, source: "profile")
    }
}
