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
        if text.count > maxChars {
            text = String(text.prefix(maxChars))
            warnings.append("The text was long; only the first part was parsed. Review fields carefully.")
        }

        let prompt = PromptRegistry.render(promptKey, ["resume": text], config: config)
        let request = CompletionRequest(user: prompt, tier: .strong,
                                        temperature: 0.1, maxTokens: 4096)
        let rawText: String
        do {
            rawText = try await engine.complete(request, config: config.ai)
                .trimmingCharacters(in: .whitespacesAndNewlines)
        } catch {
            return ResumeParseResult(
                profile: Profile(),
                warnings: ["AI extraction failed (\(String(describing: error))). Fill the form manually."])
        }

        guard let data = LenientJSON.parseObject(rawText) else {
            return ResumeParseResult(
                profile: Profile(),
                warnings: ["Could not extract structured data automatically. Fill the form manually or try again."])
        }

        let profile = sanitize(data)
        if profile.fullName.isEmpty && profile.experience.isEmpty {
            warnings.append("Little structured data was found — double-check every field below.")
        }
        return ResumeParseResult(profile: profile, warnings: warnings)
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
