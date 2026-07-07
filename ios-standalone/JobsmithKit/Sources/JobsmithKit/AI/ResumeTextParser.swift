import Foundation

public struct ParsedExperienceEntry: Equatable, Sendable {
    public var title: String
    public var company: String
    public var dates: String
    public var bullets: [String]

    public init(title: String = "", company: String = "", dates: String = "",
                bullets: [String] = []) {
        self.title = title; self.company = company; self.dates = dates
        self.bullets = bullets
    }
}

public struct ParsedEducationEntry: Equatable, Sendable {
    public var degree: String
    public var school: String
    public var year: String

    public init(degree: String = "", school: String = "", year: String = "") {
        self.degree = degree; self.school = school; self.year = year
    }
}

public struct ParsedResume: Equatable, Sendable {
    public var summary: String
    public var skills: [String]
    public var experiences: [ParsedExperienceEntry]
    public var education: [ParsedEducationEntry]
    public var certifications: [String]
    public var projects: String
    public var awards: String

    public init(summary: String = "", skills: [String] = [],
                experiences: [ParsedExperienceEntry] = [],
                education: [ParsedEducationEntry] = [],
                certifications: [String] = [], projects: String = "",
                awards: String = "") {
        self.summary = summary; self.skills = skills
        self.experiences = experiences; self.education = education
        self.certifications = certifications; self.projects = projects
        self.awards = awards
    }
}

/// Tolerant parser for AI-generated resume text, ported from the parsing
/// half of `resume_generator.py` (the DOCX/PDF rendering lives elsewhere).
public enum ResumeTextParser {
    /// Every accepted header variant → canonical section key.
    static let headerMap: [String: String] = [
        // Summary variants
        "SUMMARY": "summary",
        "PROFESSIONAL SUMMARY": "summary",
        "PROFILE SUMMARY": "summary",
        "PROFILE": "summary",
        "ABOUT ME": "summary",
        // Skills variants
        "SKILLS": "skills",
        "TECHNICAL SKILLS": "skills",
        "CORE COMPETENCIES": "skills",
        "KEY SKILLS": "skills",
        "COMPETENCIES": "skills",
        // Experience variants
        "EXPERIENCE": "experience",
        "PROFESSIONAL EXPERIENCE": "experience",
        "WORK EXPERIENCE": "experience",
        "EMPLOYMENT HISTORY": "experience",
        "CAREER HISTORY": "experience",
        // Education variants
        "EDUCATION": "education",
        "EDUCATIONAL BACKGROUND": "education",
        "ACADEMIC BACKGROUND": "education",
        // Certification variants
        "CERTIFICATIONS": "certifications",
        "CERTIFICATES": "certifications",
        "LICENSES": "certifications",
        "LICENSES & CERTIFICATIONS": "certifications",
        // Misc — kept for completeness but not rendered specially
        "PROJECTS": "projects",
        "AWARDS": "awards",
        "ACHIEVEMENTS": "awards",
    ]

    public static func parse(_ content: String) -> ParsedResume {
        let sections = parseSections(content)
        return ParsedResume(
            summary: sections["summary"] ?? "",
            skills: parseSkills(sections["skills"] ?? ""),
            experiences: parseExperienceEntries(sections["experience"] ?? ""),
            education: parseEducationEntries(sections["education"] ?? ""),
            certifications: parseCertifications(sections["certifications"] ?? ""),
            projects: sections["projects"] ?? "",
            awards: sections["awards"] ?? "")
    }

    /// Split raw text into canonical sections; case-insensitive header
    /// matching with markdown prefixes and trailing colons tolerated. If no
    /// recognised headers exist the whole content becomes the summary.
    public static func parseSections(_ content: String) -> [String: String] {
        var sections: [String: String] = [:]
        var currentSection = "preamble"
        var currentLines: [String] = []

        for line in content.components(separatedBy: "\n") {
            let stripped = line.trimmingCharacters(in: .whitespacesAndNewlines)
            // Strip markdown prefix characters the AI might add despite instructions
            let cleaned = Rx.replaceAll("^[#*_`]+\\s*", in: stripped, with: "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            // Normalise: upper-case and strip trailing colon/whitespace
            let cleanedUpper = rstrip(cleaned.uppercased(), charactersIn: ": ")

            if let key = headerMap[cleanedUpper] {
                if !currentLines.isEmpty {
                    sections[currentSection] = currentLines.joined(separator: "\n")
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                }
                currentSection = key
                currentLines = []
            } else {
                currentLines.append(line)
            }
        }
        if !currentLines.isEmpty {
            sections[currentSection] = currentLines.joined(separator: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }

        sections.removeValue(forKey: "preamble")

        if sections.isEmpty {
            sections["summary"] = content.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return sections
    }

    /// Parse the experience section into structured entries. Supported
    /// formats, in priority order: Title:/Company:/Dates: prefixes,
    /// "Title | Company | Dates", "Title at Company (dates)",
    /// "Title, Company (dates)", then any line as a new entry title.
    public static func parseExperienceEntries(_ text: String) -> [ParsedExperienceEntry] {
        var entries: [ParsedExperienceEntry] = []
        var current: ParsedExperienceEntry?

        func saveCurrent() {
            if let entry = current { entries.append(entry) }
        }

        for line in text.components(separatedBy: "\n") {
            var stripped = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if stripped.isEmpty { continue }

            // Strip markdown bold markers
            stripped = Rx.replaceAll("\\*\\*(.+?)\\*\\*", in: stripped, with: "$1")

            // ── Structured prefix format (highest priority) ───────────────
            if let m = Rx.first("^Title:\\s*(.+)", in: stripped, options: [.caseInsensitive]),
               let title = m[1] {
                saveCurrent()
                current = ParsedExperienceEntry(title: title.trimmingCharacters(in: .whitespacesAndNewlines))
                continue
            }
            if current != nil,
               let m = Rx.first("^Company:\\s*(.+)", in: stripped, options: [.caseInsensitive]),
               let company = m[1] {
                current?.company = company.trimmingCharacters(in: .whitespacesAndNewlines)
                continue
            }
            if current != nil,
               let m = Rx.first("^Dates?:\\s*(.+)", in: stripped, options: [.caseInsensitive]),
               let dates = m[1] {
                current?.dates = dates.trimmingCharacters(in: .whitespacesAndNewlines)
                continue
            }

            // ── Bullet points ──────────────────────────────────────────────
            if ["-", "•", "*", "–", "▸"].contains(where: { stripped.hasPrefix($0) }) {
                let bullet = Rx.replaceAll("^[-•*–▸]+\\s*", in: stripped, with: "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if !bullet.isEmpty {
                    if current == nil {
                        // Orphan bullet before any entry — create a blank entry
                        current = ParsedExperienceEntry()
                    }
                    current?.bullets.append(bullet)
                }
                continue
            }

            // ── Freeform header lines (no structured prefix) ───────────────
            if stripped.count < 120 {
                // Format 1: "Title | Company | Dates"
                let pipeParts = stripped.components(separatedBy: "|")
                    .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                if pipeParts.count >= 2 {
                    saveCurrent()
                    current = ParsedExperienceEntry(
                        title: pipeParts[0], company: pipeParts[1],
                        dates: pipeParts.count >= 3 ? pipeParts[2] : "")
                    continue
                }

                // Format 2: "Title at Company (dates)" or "Title at Company"
                if let m = Rx.first("^(.+?)\\s+at\\s+(.+?)(?:\\s+\\(([^)]+)\\))?$",
                                    in: stripped, options: [.caseInsensitive]) {
                    saveCurrent()
                    current = ParsedExperienceEntry(
                        title: (m[1] ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
                        company: (m[2] ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
                        dates: (m[3] ?? "").trimmingCharacters(in: .whitespacesAndNewlines))
                    continue
                }

                // Format 3: "Title, Company (dates)"
                if let m = Rx.first("^(.+?),\\s+(.+?)\\s+\\(([^)]+)\\)$", in: stripped) {
                    saveCurrent()
                    current = ParsedExperienceEntry(
                        title: (m[1] ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
                        company: (m[2] ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
                        dates: (m[3] ?? "").trimmingCharacters(in: .whitespacesAndNewlines))
                    continue
                }
            }

            // ── Fallback: treat as a new entry title, preserving the raw text ─
            saveCurrent()
            current = ParsedExperienceEntry(title: stripped)
        }

        saveCurrent()
        return entries
    }

    /// Parse the education section: Degree:/School:/Year: prefixes plus
    /// freeform "Degree, School, Year", "Degree | School | Year",
    /// "Degree, School (Year)".
    public static func parseEducationEntries(_ text: String) -> [ParsedEducationEntry] {
        var entries: [ParsedEducationEntry] = []
        var current: ParsedEducationEntry?

        func extractYear(_ s: String) -> (rest: String, year: String) {
            guard let m = Rx.firstWithRange("\\b(19|20)\\d{2}\\b", in: s),
                  let whole = m.groups[0] else { return (s, "") }
            var rest = String(s[..<m.range.lowerBound]) + String(s[m.range.upperBound...])
            rest = strip(rest, charactersIn: " ,()–-")
            return (rest, whole)
        }

        for line in text.components(separatedBy: "\n") {
            var stripped = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if stripped.isEmpty { continue }

            stripped = Rx.replaceAll("\\*\\*(.+?)\\*\\*", in: stripped, with: "$1")

            // ── Structured prefix lines ────────────────────────────────────
            if let m = Rx.first("^Degree:\\s*(.+)", in: stripped, options: [.caseInsensitive]),
               let degree = m[1] {
                if let entry = current { entries.append(entry) }
                current = ParsedEducationEntry(degree: degree.trimmingCharacters(in: .whitespacesAndNewlines))
                continue
            }
            if current != nil,
               let m = Rx.first("^School:\\s*(.+)", in: stripped, options: [.caseInsensitive]),
               let school = m[1] {
                current?.school = school.trimmingCharacters(in: .whitespacesAndNewlines)
                continue
            }
            if current != nil,
               let m = Rx.first("^Year:\\s*(.+)", in: stripped, options: [.caseInsensitive]),
               let year = m[1] {
                current?.year = year.trimmingCharacters(in: .whitespacesAndNewlines)
                continue
            }

            // Skip bullet points
            if ["-", "•", "*", "–"].contains(where: { stripped.hasPrefix($0) }) { continue }

            // ── Freeform line ──────────────────────────────────────────────
            // Try pipe-separated first: "Degree | School | Year"
            let pipeParts = stripped.components(separatedBy: "|")
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            if pipeParts.count >= 2 {
                if let entry = current { entries.append(entry) }
                let (rest, year) = extractYear(pipeParts[pipeParts.count - 1])
                if !year.isEmpty {
                    let school = pipeParts.count >= 3 ? pipeParts[1] : rest
                    current = ParsedEducationEntry(degree: pipeParts[0], school: school, year: year)
                } else {
                    current = ParsedEducationEntry(degree: pipeParts[0], school: pipeParts[1], year: "")
                }
                continue
            }

            // Try "Degree, School (Year)" — year in parens
            if let m = Rx.firstWithRange("\\((\\d{4})\\)", in: stripped),
               let year = m.groups[1] {
                let withoutYear = rstrip(
                    String(stripped[..<m.range.lowerBound])
                        .trimmingCharacters(in: .whitespacesAndNewlines),
                    charactersIn: ",")
                let commaParts = splitOnce(withoutYear, on: ",")
                    .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                if commaParts.count >= 2 {
                    if let entry = current { entries.append(entry) }
                    current = ParsedEducationEntry(degree: commaParts[0], school: commaParts[1], year: year)
                    continue
                }
            }

            // "Degree, School, Year" — year as the last comma token
            let commaParts = stripped.components(separatedBy: ",")
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            if commaParts.count >= 2 {
                let (last, year) = extractYear(commaParts[commaParts.count - 1])
                if !year.isEmpty {
                    let joined = commaParts[1..<(commaParts.count - 1)].joined(separator: ",")
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    let school = joined.isEmpty ? last : joined
                    if let entry = current { entries.append(entry) }
                    current = ParsedEducationEntry(degree: commaParts[0], school: school, year: year)
                    continue
                }
            }

            // Last resort: treat the whole line as a degree field
            if let entry = current { entries.append(entry) }
            current = ParsedEducationEntry(degree: lstrip(stripped, charactersIn: "-•* "))
        }

        if let entry = current { entries.append(entry) }
        return entries
    }

    /// Skills as displayed: categorized lines kept whole, otherwise all
    /// lines flattened into one comma-separated list.
    static func parseSkills(_ text: String) -> [String] {
        var t = Rx.replaceAll("\\*\\*(.+?)\\*\\*", in: text, with: "$1")
        t = Rx.replaceAll("^[-*•]\\s*", in: t, with: "", options: [.anchorsMatchLines])
        let lines = t.components(separatedBy: "\n")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        if lines.contains(where: { $0.contains(":") }) { return lines }
        return lines.flatMap { line in
            line.components(separatedBy: ",")
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
        }
    }

    static func parseCertifications(_ text: String) -> [String] {
        text.components(separatedBy: "\n").compactMap { line in
            var l = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if l.isEmpty { return nil }
            l = Rx.replaceAll("\\*\\*(.+?)\\*\\*", in: l, with: "$1")
            let clean = lstrip(l, charactersIn: "-*•– ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return clean.isEmpty ? nil : clean
        }
    }

    // MARK: - Small string helpers (Python str.strip semantics)

    static func lstrip(_ s: String, charactersIn chars: String) -> String {
        let set = Set(chars)
        var out = Substring(s)
        while let first = out.first, set.contains(first) { out.removeFirst() }
        return String(out)
    }

    static func rstrip(_ s: String, charactersIn chars: String) -> String {
        let set = Set(chars)
        var out = Substring(s)
        while let last = out.last, set.contains(last) { out.removeLast() }
        return String(out)
    }

    static func strip(_ s: String, charactersIn chars: String) -> String {
        rstrip(lstrip(s, charactersIn: chars), charactersIn: chars)
    }

    static func splitOnce(_ s: String, on separator: Character) -> [String] {
        guard let idx = s.firstIndex(of: separator) else { return [s] }
        return [String(s[..<idx]), String(s[s.index(after: idx)...])]
    }
}
