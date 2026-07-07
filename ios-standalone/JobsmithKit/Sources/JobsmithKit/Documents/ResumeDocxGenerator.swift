import Foundation

/// Parsed-content input for document generation. Kept generator-local so the
/// docs module doesn't depend on the AI module; ResumeTextParser output maps
/// onto this via a small adapter at the call site.
public struct DocResumeContent: Sendable {
    public var summary: String
    /// Raw skills section text (may be categorized "Category: a, b" lines).
    public var skillsText: String
    public var experiences: [DocExperience]
    public var education: [DocEducation]
    public var certifications: [String]

    public init(summary: String = "", skillsText: String = "",
                experiences: [DocExperience] = [], education: [DocEducation] = [],
                certifications: [String] = []) {
        self.summary = summary; self.skillsText = skillsText
        self.experiences = experiences; self.education = education
        self.certifications = certifications
    }
}

public struct DocExperience: Sendable {
    public var title: String
    public var company: String
    public var dates: String
    public var bullets: [String]

    public init(title: String, company: String = "", dates: String = "", bullets: [String] = []) {
        self.title = title; self.company = company; self.dates = dates; self.bullets = bullets
    }
}

public struct DocEducation: Sendable {
    public var degree: String
    public var school: String
    public var year: String

    public init(degree: String, school: String = "", year: String = "") {
        self.degree = degree; self.school = school; self.year = year
    }
}

private func stripMarkdownBold(_ text: String) -> String {
    text.replacingOccurrences(of: #"\*\*(.+?)\*\*"#, with: "$1", options: .regularExpression)
}

/// Port of resume_generator.py::generate_resume_docx — same section order,
/// spacing, and preset-driven styling.
public enum ResumeDocxGenerator {
    public static func generate(content: DocResumeContent, profile: Profile,
                                style stylePreset: HonestyConfig.Style) throws -> Data {
        let s = DocStyle.preset(stylePreset)
        var doc = DocxDocument()
        doc.margins = s.margins
        let bodyFont = s.bodyFont

        // -- Name --
        if !profile.fullName.isEmpty {
            var p = DocxParagraph()
            p.alignment = "center"
            p.spacingBeforePt = 0; p.spacingAfterPt = 2
            if let ls = s.lineSpacing { p.lineSpacingMultiple = ls }
            let display = s.nameUppercase ? profile.fullName.uppercased() : profile.fullName
            p.run(display, RunStyle(font: s.nameFont, sizePt: s.nameSize, bold: true,
                                    colorHex: s.nameColor, letterSpacingPt: s.nameLetterSpacing))
            doc.add(p)
        }

        // -- Contact line: email | phone | location | linkedin | portfolio | github --
        var contactEntries: [(text: String, url: String?)] = []
        for value in [profile.email, profile.phone, profile.location] where !value.isEmpty {
            contactEntries.append((value, nil))
        }
        for url in [profile.linkedin, profile.portfolio, profile.github] where !url.isEmpty {
            var clean = url.replacingOccurrences(of: #"^https?://"#, with: "", options: .regularExpression)
            while clean.hasSuffix("/") { clean.removeLast() }
            if !clean.isEmpty {
                let full = url.hasPrefix("http") ? url : "https://\(clean)"
                contactEntries.append((clean, full))
            }
        }
        if !contactEntries.isEmpty {
            var p = DocxParagraph()
            p.alignment = "center"
            p.spacingBeforePt = 0; p.spacingAfterPt = 2
            let grayStyle = RunStyle(font: bodyFont, sizePt: 9, colorHex: DocStyle.gray)
            for (index, entry) in contactEntries.enumerated() {
                if index > 0 { p.run("  |  ", grayStyle) }
                if let url = entry.url, s.hyperlinks {
                    p.link(url, text: entry.text,
                           style: RunStyle(font: bodyFont, sizePt: 9, colorHex: s.accentColor))
                } else {
                    p.run(entry.text, grayStyle)
                }
            }
            doc.add(p)
        }

        // -- Rule under header --
        if s.nameRule {
            var p = DocxParagraph()
            p.spacingBeforePt = 4; p.spacingAfterPt = 4
            p.bottomBorder = (s.nameRuleColor, s.nameRuleSize)
            doc.add(p)
        }

        func sectionHeader(_ title: String) {
            var p = DocxParagraph()
            p.spacingBeforePt = 10; p.spacingAfterPt = 3
            p.run(title.uppercased(), RunStyle(font: bodyFont, sizePt: s.headerSize, bold: true,
                                               allCaps: true, colorHex: s.headerColor,
                                               letterSpacingPt: s.headerLetterSpacing))
            if s.headerUnderline {
                p.bottomBorder = (s.headerUnderlineColor, s.headerUnderlineSize)
            }
            doc.add(p)
        }

        let bodyStyle = RunStyle(font: bodyFont, sizePt: 10.5, colorHex: DocStyle.dark)
        let markerStyle = RunStyle(font: bodyFont, sizePt: s.bulletMarkerSize, colorHex: s.bulletMarkerColor)

        // -- Summary --
        let summary = stripMarkdownBold(content.summary).trimmingCharacters(in: .whitespacesAndNewlines)
        if !summary.isEmpty {
            sectionHeader("Summary")
            var p = DocxParagraph()
            p.spacingBeforePt = 2; p.spacingAfterPt = 4
            p.run(summary, bodyStyle)
            doc.add(p)
        }

        // -- Skills (categorized "Category: a, b" lines vs flat comma list) --
        var skillsText = stripMarkdownBold(content.skillsText)
        skillsText = skillsText.replacingOccurrences(of: #"(?m)^[-*•]\s*"#, with: "", options: .regularExpression)
        let skillLines = skillsText.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        if !skillLines.isEmpty {
            sectionHeader("Technical Skills")
            let categorized = skillLines.contains { $0.contains(":") }
            if categorized {
                for line in skillLines {
                    var p = DocxParagraph()
                    p.spacingBeforePt = 1; p.spacingAfterPt = 1
                    if let colon = line.firstIndex(of: ":") {
                        let category = String(line[..<colon]).trimmingCharacters(in: .whitespaces)
                        let skills = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
                        p.run(category + ": ", RunStyle(font: bodyFont, sizePt: 10.5, bold: true, colorHex: DocStyle.dark))
                        p.run(skills, bodyStyle)
                    } else {
                        p.run(line, bodyStyle)
                    }
                    doc.add(p)
                }
            } else {
                let all = skillLines.flatMap { $0.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) } }
                    .filter { !$0.isEmpty }
                var p = DocxParagraph()
                p.spacingBeforePt = 2; p.spacingAfterPt = 4
                p.run(all.joined(separator: ", "), bodyStyle)
                doc.add(p)
            }
        }

        // -- Experience --
        if !content.experiences.isEmpty {
            sectionHeader("Professional Experience")
            let (top, bottom, left, right) = s.margins
            _ = (top, bottom)
            let inlineRightTab = 8.5 - left - right
            let stackedRightTab = 6.1

            for entry in content.experiences {
                if s.entryLayout == "inline" {
                    var head = DocxParagraph()
                    head.spacingBeforePt = 6; head.spacingAfterPt = 2
                    head.run(entry.title, RunStyle(font: bodyFont, sizePt: 11, bold: true, colorHex: DocStyle.black))
                    if !entry.company.isEmpty {
                        head.run("  ·  ", RunStyle(font: bodyFont, sizePt: 10.5, colorHex: s.accentColor))
                        head.run(entry.company, bodyStyle)
                    }
                    if !entry.dates.isEmpty {
                        head.rightTabStopInches = inlineRightTab
                        head.tab()
                        head.run(entry.dates, RunStyle(font: bodyFont, sizePt: 10, italic: true, colorHex: DocStyle.gray))
                    }
                    doc.add(head)
                } else {
                    var titleP = DocxParagraph()
                    titleP.spacingBeforePt = 6; titleP.spacingAfterPt = 0
                    titleP.run(entry.title, RunStyle(font: bodyFont, sizePt: 11, bold: true, colorHex: DocStyle.black))
                    if !entry.dates.isEmpty {
                        titleP.rightTabStopInches = stackedRightTab
                        titleP.tab()
                        titleP.run(entry.dates, RunStyle(font: bodyFont, sizePt: 10, italic: true, colorHex: DocStyle.gray))
                    }
                    doc.add(titleP)
                    if !entry.company.isEmpty {
                        var companyP = DocxParagraph()
                        companyP.spacingBeforePt = 0; companyP.spacingAfterPt = 2
                        companyP.run(entry.company, RunStyle(font: bodyFont, sizePt: 10.5, italic: true, colorHex: DocStyle.gray))
                        doc.add(companyP)
                    }
                }

                for bullet in entry.bullets {
                    var p = DocxParagraph()
                    p.spacingBeforePt = 1; p.spacingAfterPt = 1
                    p.leftIndentInches = 0.25
                    p.hangingIndentInches = 0.2
                    p.run(s.bulletMarker, markerStyle)
                    p.run(bullet, bodyStyle)
                    doc.add(p)
                }
            }
        }

        // -- Education --
        if !content.education.isEmpty {
            sectionHeader("Education")
            for entry in content.education {
                var p = DocxParagraph()
                p.spacingBeforePt = 3; p.spacingAfterPt = 1
                p.run(entry.degree, RunStyle(font: bodyFont, sizePt: 10.5, bold: true, colorHex: DocStyle.black))
                if !entry.school.isEmpty {
                    var text = "  —  \(entry.school)"
                    if !entry.year.isEmpty { text += "  (\(entry.year))" }
                    p.run(text, RunStyle(font: bodyFont, sizePt: 10.5, colorHex: DocStyle.gray))
                } else if !entry.year.isEmpty {
                    p.run("  (\(entry.year))", RunStyle(font: bodyFont, sizePt: 10.5, colorHex: DocStyle.gray))
                }
                doc.add(p)
            }
        }

        // -- Certifications --
        let certs = content.certifications
            .map { stripMarkdownBold($0).trimmingCharacters(in: CharacterSet(charactersIn: "-*•– ")) }
            .filter { !$0.isEmpty }
        if !certs.isEmpty {
            sectionHeader("Certifications")
            for cert in certs {
                var p = DocxParagraph()
                p.spacingBeforePt = 1; p.spacingAfterPt = 1
                p.leftIndentInches = 0.25
                p.hangingIndentInches = 0.2
                p.run(s.bulletMarker, markerStyle)
                p.run(cert, bodyStyle)
                doc.add(p)
            }
        }

        // -- References: appended verbatim from profile, never sent to AI --
        let validRefs = profile.references.filter { !$0.name.trimmingCharacters(in: .whitespaces).isEmpty }
        if !validRefs.isEmpty {
            sectionHeader("References")
            for ref in validRefs {
                var nameP = DocxParagraph()
                nameP.spacingBeforePt = 4; nameP.spacingAfterPt = 1
                nameP.run(ref.name.trimmingCharacters(in: .whitespaces),
                          RunStyle(font: bodyFont, sizePt: 10.5, bold: true, colorHex: DocStyle.black))
                let position = ref.position.trimmingCharacters(in: .whitespaces)
                if !position.isEmpty {
                    nameP.run("  —  \(position)", RunStyle(font: bodyFont, sizePt: 10.5, italic: true, colorHex: DocStyle.gray))
                }
                doc.add(nameP)
                let bits = [ref.email, ref.phone]
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                    .filter { !$0.isEmpty }
                if !bits.isEmpty {
                    var p = DocxParagraph()
                    p.spacingBeforePt = 0; p.spacingAfterPt = 2
                    p.run(bits.joined(separator: "  |  "), RunStyle(font: bodyFont, sizePt: 10, colorHex: DocStyle.dark))
                    doc.add(p)
                }
            }
        }

        return try doc.render()
    }
}

/// Port of resume_generator.py::generate_cover_letter_docx.
public enum CoverLetterDocxGenerator {
    public static func generate(content: String, profile: Profile,
                                jobTitle: String, company: String,
                                date: Date = Date()) throws -> Data {
        var doc = DocxDocument()
        doc.margins = (1, 1, 1, 1)
        let font = "Calibri"

        if !profile.fullName.isEmpty {
            var p = DocxParagraph()
            p.alignment = "center"
            p.spacingBeforePt = 0; p.spacingAfterPt = 2
            p.run(profile.fullName.uppercased(),
                  RunStyle(font: font, sizePt: 16, bold: true, colorHex: DocStyle.black, letterSpacingPt: 1.5))
            doc.add(p)
        }

        let contact = [profile.email, profile.phone, profile.location].filter { !$0.isEmpty }
        if !contact.isEmpty {
            var p = DocxParagraph()
            p.alignment = "center"
            p.spacingBeforePt = 0; p.spacingAfterPt = 2
            p.run(contact.joined(separator: "  |  "), RunStyle(font: font, sizePt: 9, colorHex: DocStyle.gray))
            doc.add(p)
        }

        var rule = DocxParagraph()
        rule.spacingBeforePt = 4; rule.spacingAfterPt = 8
        rule.bottomBorder = ("2B5797", "6")
        doc.add(rule)

        let formatter = DateFormatter()
        formatter.dateFormat = "MMMM d, yyyy"
        formatter.locale = Locale(identifier: "en_US")
        var dateP = DocxParagraph()
        dateP.spacingBeforePt = 6; dateP.spacingAfterPt = 12
        dateP.run(formatter.string(from: date), RunStyle(font: font, sizePt: 11, colorHex: DocStyle.dark))
        doc.add(dateP)

        var refP = DocxParagraph()
        refP.spacingBeforePt = 0; refP.spacingAfterPt = 12
        refP.run("Re: \(jobTitle) at \(company)", RunStyle(font: font, sizePt: 11, bold: true, colorHex: DocStyle.black))
        doc.add(refP)

        var clean = stripMarkdownBold(content)
        clean = clean.replacingOccurrences(of: #"(?m)^#+\s*"#, with: "", options: .regularExpression)

        let bodyStyle = RunStyle(font: font, sizePt: 11, colorHex: DocStyle.dark)
        for rawParagraph in clean.components(separatedBy: "\n\n") {
            let paragraph = rawParagraph.trimmingCharacters(in: .whitespacesAndNewlines)
            if paragraph.isEmpty { continue }
            let lower = paragraph.lowercased()
            // The generator adds its own closing.
            if lower.hasPrefix("sincerely") || lower.hasPrefix("best regards")
                || lower.hasPrefix("regards") || lower.hasPrefix("thank you") {
                continue
            }
            var p = DocxParagraph()
            p.spacingBeforePt = 0; p.spacingAfterPt = 6
            p.run(paragraph, bodyStyle)
            doc.add(p)
        }

        doc.add(DocxParagraph())
        var closing = DocxParagraph()
        closing.spacingBeforePt = 6; closing.spacingAfterPt = 2
        closing.run("Sincerely,", bodyStyle)
        doc.add(closing)

        var nameP = DocxParagraph()
        nameP.spacingBeforePt = 2; nameP.spacingAfterPt = 0
        nameP.run(profile.fullName, RunStyle(font: font, sizePt: 11, bold: true, colorHex: DocStyle.black))
        doc.add(nameP)

        return try doc.render()
    }
}
