import Foundation

public struct ResumeParseResult: Equatable, Sendable {
    public var profile: Profile
    public var warnings: [String]

    public init(profile: Profile, warnings: [String]) {
        self.profile = profile; self.warnings = warnings
    }
}

/// Port of `resume_parser.parse_resume`: extract a partial profile from
/// résumé-like text via the LLM, strictly extractively. Never throws — a bad
/// model response returns an empty profile plus a warning so the user can
/// still fill the form manually.
public enum ResumeProfileParser {
    static let maxChars = 16000

    public static func parse(text: String, config: AppConfig, engine: AIEngine,
                             promptKey: String = "resume_parse") async -> ResumeParseResult {
        var warnings: [String] = []
        var text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty {
            return ResumeParseResult(profile: Profile(), warnings: ["No résumé text to parse."])
        }
        let chunks: [String]
        if config.ai.usesOnDevice(for: .strong) {
            // Apple's model takes at most 8,000 characters of input, prompt included.
            let overhead = PromptRegistry.render(promptKey, ["resume": ""], config: config).count
            chunks = chunk(text, limit: min(appleChunkChars, appleInputCap - overhead - 200))
        } else {
            if text.count > maxChars {
                text = String(text.prefix(maxChars))
                warnings.append("The text was long; only the first part was parsed. Review fields carefully.")
            }
            chunks = [text]
        }

        var parts: [Profile] = []
        var badJSON: [String] = []
        for piece in chunks {
            let prompt = PromptRegistry.render(promptKey, ["resume": piece], config: config)
            let request = CompletionRequest(user: prompt, tier: .strong,
                                            temperature: 0.1, maxTokens: 4096)
            let rawText: String
            do {
                rawText = try await engine.complete(request, config: config.ai)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
            } catch {
                // The engine's own reason, in plain English where we have one.
                let d = AIErrorMapper.describe(error, baseURL: config.ai.baseURL,
                                               onDevice: config.ai.usesOnDevice(for: .strong))
                let reason = d.code == "error" || d.code == "unavailable"
                    ? error.localizedDescription : "\(d.message) (\(error.localizedDescription))"
                return ResumeParseResult(
                    profile: Profile(),
                    warnings: ["AI extraction failed: \(reason). Fill the form manually."])
            }
            guard let data = LenientJSON.parseObject(rawText) else {
                badJSON.append(String(rawText.prefix(120)))
                continue
            }
            parts.append(sanitize(data))
        }

        guard !parts.isEmpty else {
            return ResumeParseResult(
                profile: Profile(),
                warnings: ["The AI's reply was not valid JSON (it began: \"\(badJSON.first ?? "")\"). Fill the form manually or try again."])
        }
        if !badJSON.isEmpty {
            warnings.append("\(badJSON.count) of \(chunks.count) parts of the résumé could not be read — check the fields.")
        }
        let profile = merge(parts)
        if profile.fullName.isEmpty && profile.experience.isEmpty {
            warnings.append("Little structured data was found — double-check every field below.")
        }
        return ResumeParseResult(profile: profile, warnings: warnings)
    }

    // MARK: - Apple on-device chunking (mirror of desktop resume_parser.chunk_resume / merge_profiles)

    static let appleInputCap = 8000
    public static let appleChunkChars = 7000
    static let headings: Set<String> = [
        "summary", "professional summary", "profile", "objective", "experience", "work experience",
        "professional experience", "relevant experience", "employment", "employment history", "work history",
        "education", "skills", "technical skills", "core skills", "certifications", "certificates",
        "licenses", "licenses and certifications", "projects", "awards", "publications", "volunteer",
        "volunteer experience", "languages", "interests", "references",
    ]

    static func isHeading(_ line: String) -> Bool {
        var t = line.trimmingCharacters(in: .whitespaces)
        while t.hasSuffix(":") { t.removeLast() }
        t = t.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, t.count <= 40 else { return false }
        return headings.contains(t.lowercased()) || (t == t.uppercased() && t.contains { $0.isLetter })
    }

    static func pack(_ pieces: [String], limit: Int) -> [String] {
        var out: [String] = [], cur = ""
        for piece in pieces {
            if !cur.isEmpty, cur.count + piece.count > limit { out.append(cur); cur = "" }
            cur += piece
        }
        if !cur.isEmpty { out.append(cur) }
        return out
    }

    /// Lines with their newline kept, so pieces concatenate back to the text.
    static func lines(_ text: String) -> [String] {
        var out: [String] = [], cur = ""
        for ch in text { cur.append(ch); if ch == "\n" { out.append(cur); cur = "" } }
        if !cur.isEmpty { out.append(cur) }
        return out
    }

    /// An oversized section: split on blank lines (a role stays whole), a still
    /// oversized paragraph on lines, a single huge line on characters.
    static func splitHard(_ block: String, limit: Int) -> [String] {
        var paras: [String] = [], cur = ""
        for line in lines(block) {
            if line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !cur.isEmpty {
                paras.append(cur); cur = ""
            }
            cur += line
        }
        if !cur.isEmpty { paras.append(cur) }
        var pieces: [String] = []
        for para in paras {
            if para.count <= limit { pieces.append(para); continue }
            for line in lines(para) {
                var rest = Substring(line)
                while !rest.isEmpty { pieces.append(String(rest.prefix(limit))); rest = rest.dropFirst(limit) }
            }
        }
        return pack(pieces, limit: limit)
    }

    /// Split résumé text on section headings into chunks of at most `limit`
    /// characters, packing whole sections together where they fit.
    public static func chunk(_ text: String, limit: Int = appleChunkChars) -> [String] {
        var sections: [String] = [], cur = ""
        for line in lines(text) {
            if isHeading(line), !cur.isEmpty { sections.append(cur); cur = "" }
            cur += line
        }
        if !cur.isEmpty { sections.append(cur) }
        let pieces = sections.flatMap { $0.count <= limit ? [$0] : splitHard($0, limit: limit) }
        return pack(pieces, limit: limit)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    /// First non-empty scalar wins; lists concatenated and de-duplicated
    /// (skills/certs case-insensitively, experience by title+company with
    /// bullets merged, education by degree+school).
    public static func merge(_ parts: [Profile]) -> Profile {
        var out = Profile()
        func key(_ a: String, _ b: String) -> String { a.lowercased() + "\u{1}" + b.lowercased() }
        for p in parts {
            let scalars: [WritableKeyPath<Profile, String>] = [\.fullName, \.email, \.phone, \.location, \.streetAddress,
                                                            \.city, \.state, \.zipCode, \.linkedin, \.github,
                                                            \.portfolio, \.summary]
            for kp in scalars where out[keyPath: kp].isEmpty { out[keyPath: kp] = p[keyPath: kp] }
            for kp in [\Profile.skills, \Profile.certifications] as [WritableKeyPath<Profile, [String]>] {
                var seen = Set(out[keyPath: kp].map { $0.lowercased() })
                for v in p[keyPath: kp] where seen.insert(v.lowercased()).inserted { out[keyPath: kp].append(v) }
            }
            for e in p.experience {
                if let i = out.experience.firstIndex(where: { key($0.title, $0.company) == key(e.title, e.company) }) {
                    for b in e.bullets where !out.experience[i].bullets.contains(b) { out.experience[i].bullets.append(b) }
                } else {
                    out.experience.append(e)
                }
            }
            for e in p.education where !out.education.contains(where: { key($0.degree, $0.school) == key(e.degree, e.school) }) {
                out.education.append(e)
            }
        }
        return out
    }

    /// Coerce the model's JSON onto the partial Profile shape: fix types and
    /// drop empty experience/education rows. Demographic/credential/salary
    /// fields are intentionally never prefilled from a résumé.
    static func sanitize(_ raw: [String: Any]) -> Profile {
        func str(_ key: String) -> String {
            let value = raw[key]
            if let s = value as? String { return s.trimmingCharacters(in: .whitespacesAndNewlines) }
            return LenientJSON.stringValue(value)
        }

        var profile = Profile()
        profile.fullName = str("full_name")
        profile.email = str("email")
        profile.phone = str("phone")
        profile.location = str("location")
        profile.streetAddress = str("street_address")
        profile.city = str("city")
        profile.state = str("state")
        profile.zipCode = str("zip_code")
        profile.linkedin = str("linkedin")
        profile.github = str("github")
        profile.portfolio = str("portfolio")
        profile.summary = str("summary")

        profile.skills = coerceStringList(raw["skills"])
        profile.certifications = coerceStringList(raw["certifications"])

        var experience: [WorkExperience] = []
        for entry in raw["experience"] as? [Any] ?? [] {
            guard let dict = entry as? [String: Any] else { continue }
            let title = LenientJSON.stringValue(dict["title"]).trimmingCharacters(in: .whitespacesAndNewlines)
            let company = LenientJSON.stringValue(dict["company"]).trimmingCharacters(in: .whitespacesAndNewlines)
            if title.isEmpty && company.isEmpty { continue }
            let endDate = LenientJSON.stringValue(dict["end_date"])
            experience.append(WorkExperience(
                title: title, company: company,
                startDate: LenientJSON.stringValue(dict["start_date"]).trimmingCharacters(in: .whitespacesAndNewlines),
                endDate: (endDate.isEmpty ? "Present" : endDate).trimmingCharacters(in: .whitespacesAndNewlines),
                bullets: coerceStringList(dict["bullets"])))
        }
        profile.experience = experience

        var education: [Education] = []
        for entry in raw["education"] as? [Any] ?? [] {
            guard let dict = entry as? [String: Any] else { continue }
            let degree = LenientJSON.stringValue(dict["degree"]).trimmingCharacters(in: .whitespacesAndNewlines)
            let school = LenientJSON.stringValue(dict["school"]).trimmingCharacters(in: .whitespacesAndNewlines)
            if degree.isEmpty && school.isEmpty { continue }
            education.append(Education(
                degree: degree, school: school,
                year: LenientJSON.stringValue(dict["year"]).trimmingCharacters(in: .whitespacesAndNewlines)))
        }
        profile.education = education
        return profile
    }

    /// One display string per item; models sometimes return objects
    /// (e.g. {"name": "Security+", "issuer": "CompTIA"}) despite the prompt.
    static func flattenItem(_ value: Any) -> String {
        if let dict = value as? [String: Any] {
            let parts = dict.values.compactMap { v -> String? in
                guard v is String || v is NSNumber else { return nil }
                let s = LenientJSON.stringValue(v).trimmingCharacters(in: .whitespacesAndNewlines)
                return s.isEmpty ? nil : s
            }
            return parts.joined(separator: " — ")
        }
        return LenientJSON.stringValue(value).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func coerceStringList(_ value: Any?) -> [String] {
        if let list = value as? [Any] {
            return list.map(flattenItem).filter { !$0.isEmpty }
        }
        if let string = value as? String,
           !string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return string.components(separatedBy: CharacterSet(charactersIn: ",\n;"))
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
        }
        return []
    }
}
