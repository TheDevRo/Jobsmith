import Foundation

/// Candidate profile — the single source of truth the AI draws from.
/// Mirrors the `profile:` section of the desktop config.yaml.
public struct Profile: Codable, Equatable, Sendable {
    public var fullName: String
    public var email: String
    public var phone: String
    public var location: String
    public var streetAddress: String
    public var city: String
    public var state: String
    public var zipCode: String
    public var linkedin: String
    public var github: String
    public var portfolio: String
    public var desiredSalary: String
    public var workAuthorization: String
    public var sponsorshipRequired: String
    public var availableStart: String
    public var noticePeriod: String
    public var summary: String
    public var skills: [String]
    public var experience: [WorkExperience]
    public var education: [Education]
    public var certifications: [String]
    /// Appended verbatim to generated resumes — never sent to the AI.
    public var references: [Reference]

    public init(
        fullName: String = "", email: String = "", phone: String = "",
        location: String = "", streetAddress: String = "", city: String = "",
        state: String = "", zipCode: String = "", linkedin: String = "",
        github: String = "", portfolio: String = "", desiredSalary: String = "",
        workAuthorization: String = "", sponsorshipRequired: String = "",
        availableStart: String = "", noticePeriod: String = "",
        summary: String = "", skills: [String] = [],
        experience: [WorkExperience] = [], education: [Education] = [],
        certifications: [String] = [], references: [Reference] = []
    ) {
        self.fullName = fullName; self.email = email; self.phone = phone
        self.location = location; self.streetAddress = streetAddress
        self.city = city; self.state = state; self.zipCode = zipCode
        self.linkedin = linkedin; self.github = github; self.portfolio = portfolio
        self.desiredSalary = desiredSalary
        self.workAuthorization = workAuthorization
        self.sponsorshipRequired = sponsorshipRequired
        self.availableStart = availableStart; self.noticePeriod = noticePeriod
        self.summary = summary; self.skills = skills
        self.experience = experience; self.education = education
        self.certifications = certifications; self.references = references
    }

    // Tolerant decoding: a field added in a later build (or one whose payload
    // went bad) must not fail the whole Profile — that would reset the user's
    // resume data on upgrade. Missing/malformed fields fall back to defaults.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fullName = c.lenient(String.self, .fullName, "")
        email = c.lenient(String.self, .email, "")
        phone = c.lenient(String.self, .phone, "")
        location = c.lenient(String.self, .location, "")
        streetAddress = c.lenient(String.self, .streetAddress, "")
        city = c.lenient(String.self, .city, "")
        state = c.lenient(String.self, .state, "")
        zipCode = c.lenient(String.self, .zipCode, "")
        linkedin = c.lenient(String.self, .linkedin, "")
        github = c.lenient(String.self, .github, "")
        portfolio = c.lenient(String.self, .portfolio, "")
        desiredSalary = c.lenient(String.self, .desiredSalary, "")
        workAuthorization = c.lenient(String.self, .workAuthorization, "")
        sponsorshipRequired = c.lenient(String.self, .sponsorshipRequired, "")
        availableStart = c.lenient(String.self, .availableStart, "")
        noticePeriod = c.lenient(String.self, .noticePeriod, "")
        summary = c.lenient(String.self, .summary, "")
        skills = c.lenient([String].self, .skills, [])
        experience = c.lenient([WorkExperience].self, .experience, [])
        education = c.lenient([Education].self, .education, [])
        certifications = c.lenient([String].self, .certifications, [])
        references = c.lenient([Reference].self, .references, [])
    }

    enum CodingKeys: String, CodingKey {
        case fullName, email, phone, location, streetAddress, city, state, zipCode
        case linkedin, github, portfolio, desiredSalary, workAuthorization
        case sponsorshipRequired, availableStart, noticePeriod, summary, skills
        case experience, education, certifications, references
    }

    public var isEmpty: Bool {
        fullName.isEmpty && summary.isEmpty && skills.isEmpty && experience.isEmpty
    }
}

public struct WorkExperience: Codable, Equatable, Sendable, Identifiable {
    public var id: UUID
    public var title: String
    public var company: String
    public var startDate: String
    public var endDate: String
    public var bullets: [String]
    /// Pinned roles are always included on tailored resumes regardless of
    /// the LLM's relevance ranking.
    public var pinned: Bool

    public init(id: UUID = UUID(), title: String = "", company: String = "",
                startDate: String = "", endDate: String = "Present",
                bullets: [String] = [], pinned: Bool = false) {
        self.id = id; self.title = title; self.company = company
        self.startDate = startDate; self.endDate = endDate
        self.bullets = bullets; self.pinned = pinned
    }
}

public struct Education: Codable, Equatable, Sendable, Identifiable {
    public var id: UUID
    public var degree: String
    public var school: String
    public var year: String

    public init(id: UUID = UUID(), degree: String = "", school: String = "", year: String = "") {
        self.id = id; self.degree = degree; self.school = school; self.year = year
    }
}

public struct Reference: Codable, Equatable, Sendable, Identifiable {
    public var id: UUID
    public var name: String
    public var position: String
    public var email: String
    public var phone: String

    public init(id: UUID = UUID(), name: String = "", position: String = "", email: String = "", phone: String = "") {
        self.id = id; self.name = name; self.position = position
        self.email = email; self.phone = phone
    }
}
