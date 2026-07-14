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

// MARK: - Shared letterhead

/// Contact bits as (display text, url?) in display order — port of
/// resume_generator.py `_contact_entries`.
func contactEntries(_ profile: Profile, includeLinks: Bool) -> [(text: String, url: String?)] {
    var entries: [(text: String, url: String?)] = []
    for value in [profile.email, profile.phone, profile.location] where !value.isEmpty {
        entries.append((value, nil))
    }
    guard includeLinks else { return entries }
    for url in [profile.linkedin, profile.portfolio, profile.github] where !url.isEmpty {
        var clean = url.replacingOccurrences(of: #"^https?://"#, with: "",
                                             options: .regularExpression)
        while clean.hasSuffix("/") { clean.removeLast() }
        if !clean.isEmpty {
            let full = url.hasPrefix("http") ? url : "https://\(clean)"
            entries.append((clean, full))
        }
    }
    return entries
}

/// A short accent bar (the Ledger stub) as its own paragraph — port of
/// `_add_stub_rule`. A paragraph border spans the full text column, so a short
/// bar is drawn by pushing the right indent in until only `width` remains.
func stubRule(_ s: DocStyle, width: Double, color: String, size: String,
              before: Double = 2, after: Double = 4) -> DocxParagraph {
    var p = DocxParagraph()
    p.spacingBeforePt = before
    p.spacingAfterPt = after
    p.rightIndentInches = max(0, s.contentWidth - width)
    p.setBottomBorder(color: color, size: size)
    return p
}

/// Renders the name + contact letterhead shared by resumes and cover letters —
/// port of `_add_letterhead_docx`. Honors name alignment, small caps, the
/// Banner band, and the name rule (single / double / stub) so both documents in
/// a pair look like one set.
func addLetterhead(to doc: inout DocxDocument, profile: Profile, style s: DocStyle,
                   includeLinks: Bool) {
    let align = s.nameAlign == "center" ? "center" : nil

    // Padding inside the band — paragraph shading doesn't cover the space
    // before/after, so the band needs its own shaded lines. A single space
    // (rather than an empty run) keeps the line height real in the PDF path too.
    func shadedSpacer(_ heightPt: Double) {
        var pad = DocxParagraph()
        pad.spacingBeforePt = 0; pad.spacingAfterPt = 0
        pad.lineSpacingMultiple = 1.0
        pad.shadingFill = s.accent
        pad.run(" ", RunStyle(font: s.bodyFont, sizePt: heightPt, colorHex: s.accent))
        doc.add(pad)
    }

    if s.banner { shadedSpacer(5) }

    var namePara: DocxParagraph?
    if !profile.fullName.isEmpty {
        var p = DocxParagraph()
        p.alignment = align
        p.spacingBeforePt = 0; p.spacingAfterPt = 2
        p.lineSpacingMultiple = 1.0
        if s.banner { p.shadingFill = s.accent }
        let display = s.nameUppercase ? profile.fullName.uppercased() : profile.fullName
        p.run(display, RunStyle(font: s.nameFont, sizePt: s.nameSize, bold: s.nameBold,
                                smallCaps: s.nameSmallCaps, colorHex: s.nameColor,
                                letterSpacingPt: s.nameLetterSpacing))
        namePara = p
    }

    let entries = contactEntries(profile, includeLinks: includeLinks)
    if entries.isEmpty {
        if let namePara { doc.add(namePara) }
    } else {
        // Compact runs the contact line onto the name paragraph itself.
        let inline = s.contactInline && namePara != nil
        var contactPara: DocxParagraph
        if inline {
            contactPara = namePara!
            contactPara.run("   ", RunStyle(font: s.bodyFont, sizePt: s.contactSize,
                                            colorHex: s.contactColor))
        } else {
            if let namePara { doc.add(namePara) }
            contactPara = DocxParagraph()
            contactPara.alignment = align
            contactPara.spacingBeforePt = 0; contactPara.spacingAfterPt = 2
            contactPara.lineSpacingMultiple = 1.0
            if s.banner { contactPara.shadingFill = s.accent }
        }

        let contactStyle = RunStyle(font: s.bodyFont, sizePt: s.contactSize,
                                    colorHex: s.contactColor)
        for (index, entry) in entries.enumerated() {
            if index > 0 { contactPara.run(s.contactSeparator, contactStyle) }
            if let url = entry.url, s.hyperlinks, !s.banner {
                contactPara.link(url, text: entry.text,
                                 style: RunStyle(font: s.bodyFont, sizePt: s.contactSize,
                                                 colorHex: s.accentColor))
            } else {
                contactPara.run(entry.text, contactStyle)
            }
        }
        doc.add(contactPara)
    }

    if s.banner {
        shadedSpacer(6)
        return
    }

    guard s.nameRule else { return }
    if s.nameRuleStyle == "stub" {
        doc.add(stubRule(s, width: 0.55, color: s.nameRuleColor, size: s.nameRuleSize,
                         before: 3, after: 6))
    } else {
        var rule = DocxParagraph()
        rule.spacingBeforePt = 4; rule.spacingAfterPt = 4
        rule.setBottomBorder(color: s.nameRuleColor, size: s.nameRuleSize,
                             val: s.nameRuleStyle == "double" ? "double" : "single")
        doc.add(rule)
    }
}

// MARK: - Resume

/// Port of resume_generator.py::generate_resume_docx — same section order,
/// spacing, and preset-driven styling. Single column, real text, no tables,
/// images, or text boxes in any style.
public enum ResumeDocxGenerator {
    /// Render the resume straight to `.docx` bytes.
    public static func generate(content: DocResumeContent, profile: Profile,
                                style stylePreset: HonestyConfig.Style,
                                accent: HonestyConfig.ResumeAccent = .default) throws -> Data {
        try build(content: content, profile: profile, style: stylePreset,
                  accent: accent).render()
    }

    /// Build the format-agnostic layout model. Both the `.docx` writer and the
    /// PDF renderer consume this, so the two outputs stay structurally identical.
    public static func build(content: DocResumeContent, profile: Profile,
                             style stylePreset: HonestyConfig.Style,
                             accent: HonestyConfig.ResumeAccent = .default) -> DocxDocument {
        let s = DocStyle.resolve(style: stylePreset, accent: accent)
        var doc = DocxDocument()
        doc.margins = s.margins
        doc.defaultLineSpacing = s.lineSpacing
        let bodyFont = s.bodyFont
        let bodySize = s.bodySize

        // -- Letterhead: name, contact, rule or band --
        addLetterhead(to: &doc, profile: profile, style: s, includeLinks: true)

        func sectionHeader(_ title: String) {
            var p = DocxParagraph()
            p.spacingBeforePt = s.sectionGap; p.spacingAfterPt = 3
            // Small caps keeps the mixed-case text and renders it as caps; the
            // other styles upper-case the text outright.
            p.run(s.headerSmallCaps ? title : title.uppercased(),
                  RunStyle(font: bodyFont, sizePt: s.headerSize, bold: true,
                           allCaps: !s.headerSmallCaps, smallCaps: s.headerSmallCaps,
                           colorHex: s.headerColor, letterSpacingPt: s.headerLetterSpacing))
            // Never leave a header stranded at the foot of a page.
            p.keepNext = true
            if s.headerUnderline && s.headerRuleStyle != "stub" {
                p.setBottomBorder(color: s.headerUnderlineColor, size: s.headerUnderlineSize)
            }
            doc.add(p)
            if s.headerUnderline && s.headerRuleStyle == "stub" {
                var stub = stubRule(s, width: 0.30, color: s.headerUnderlineColor,
                                    size: s.headerUnderlineSize, before: 1, after: 4)
                stub.keepNext = true
                doc.add(stub)
            }
        }

        let bodyStyle = RunStyle(font: bodyFont, sizePt: bodySize, colorHex: DocStyle.dark)
        let markerStyle = RunStyle(font: bodyFont, sizePt: s.bulletMarkerSize,
                                   colorHex: s.bulletMarkerColor)

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
                        p.run(category + ": ", RunStyle(font: bodyFont, sizePt: bodySize,
                                                        bold: true, colorHex: DocStyle.dark))
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
                p.run(all.joined(separator: s.skillsSeparator), bodyStyle)
                doc.add(p)
            }
        }

        // -- Experience --
        if !content.experiences.isEmpty {
            sectionHeader("Professional Experience")
            // Inline layout uses the full text width; stacked keeps the 6.1" stop.
            let inlineRightTab = s.contentWidth
            let stackedRightTab = 6.1
            let companyAccent = s.companyStyle == "accent"
            let datesStyle = RunStyle(font: bodyFont, sizePt: s.datesSize, italic: true,
                                      colorHex: DocStyle.gray)

            for entry in content.experiences {
                if s.entryLayout == "inline" {
                    // One-line header: "Title  ·  Company                Dates"
                    var head = DocxParagraph()
                    head.spacingBeforePt = 6; head.spacingAfterPt = 2
                    head.keepNext = true
                    head.run(entry.title, RunStyle(font: bodyFont, sizePt: s.titleSize,
                                                   bold: true, colorHex: DocStyle.black))
                    if !entry.company.isEmpty {
                        head.run(s.entrySeparator,
                                 RunStyle(font: bodyFont, sizePt: bodySize,
                                          colorHex: companyAccent ? s.accentColor : DocStyle.gray))
                        head.run(entry.company,
                                 RunStyle(font: bodyFont, sizePt: bodySize,
                                          bold: companyAccent,
                                          italic: s.companyStyle == "italic",
                                          colorHex: companyAccent ? s.accentColor : DocStyle.dark))
                    }
                    if !entry.dates.isEmpty {
                        head.rightTabStopInches = inlineRightTab
                        head.tab()
                        head.run(entry.dates, datesStyle)
                    }
                    doc.add(head)
                } else {
                    // Stacked: title/dates on line 1, company in italic on line 2
                    var titleP = DocxParagraph()
                    titleP.spacingBeforePt = 6; titleP.spacingAfterPt = 0
                    titleP.keepNext = true
                    titleP.run(entry.title, RunStyle(font: bodyFont, sizePt: s.titleSize,
                                                     bold: true, colorHex: DocStyle.black))
                    if !entry.dates.isEmpty {
                        titleP.rightTabStopInches = stackedRightTab
                        titleP.tab()
                        titleP.run(entry.dates, datesStyle)
                    }
                    doc.add(titleP)
                    if !entry.company.isEmpty {
                        var companyP = DocxParagraph()
                        companyP.spacingBeforePt = 0; companyP.spacingAfterPt = 2
                        companyP.keepNext = true
                        companyP.run(entry.company, RunStyle(font: bodyFont, sizePt: bodySize,
                                                             italic: true, colorHex: DocStyle.gray))
                        doc.add(companyP)
                    }
                }

                // Bullets — marker glyph and color come from the style preset.
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
                p.run(entry.degree, RunStyle(font: bodyFont, sizePt: bodySize,
                                             bold: true, colorHex: DocStyle.black))
                if !entry.school.isEmpty {
                    var text = "  —  \(entry.school)"
                    if !entry.year.isEmpty { text += "  (\(entry.year))" }
                    p.run(text, RunStyle(font: bodyFont, sizePt: bodySize, colorHex: DocStyle.gray))
                } else if !entry.year.isEmpty {
                    p.run("  (\(entry.year))", RunStyle(font: bodyFont, sizePt: bodySize,
                                                        colorHex: DocStyle.gray))
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
                          RunStyle(font: bodyFont, sizePt: bodySize, bold: true,
                                   colorHex: DocStyle.black))
                let position = ref.position.trimmingCharacters(in: .whitespaces)
                if !position.isEmpty {
                    nameP.run("  —  \(position)", RunStyle(font: bodyFont, sizePt: bodySize,
                                                           italic: true, colorHex: DocStyle.gray))
                }
                doc.add(nameP)
                let bits = [ref.email, ref.phone]
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                    .filter { !$0.isEmpty }
                if !bits.isEmpty {
                    var p = DocxParagraph()
                    p.spacingBeforePt = 0; p.spacingAfterPt = 2
                    p.run(bits.joined(separator: s.contactSeparator),
                          RunStyle(font: bodyFont, sizePt: s.datesSize, colorHex: DocStyle.dark))
                    doc.add(p)
                }
            }
        }

        return doc
    }
}

// MARK: - Cover letter

/// Port of resume_generator.py::generate_cover_letter_docx — same visual style
/// as the resume, so a recruiter opening both files sees one matched set.
public enum CoverLetterDocxGenerator {
    public static func generate(content: String, profile: Profile,
                                jobTitle: String, company: String,
                                style stylePreset: HonestyConfig.Style = .ledger,
                                accent: HonestyConfig.ResumeAccent = .default,
                                date: Date = Date()) throws -> Data {
        try build(content: content, profile: profile, jobTitle: jobTitle,
                  company: company, style: stylePreset, accent: accent, date: date).render()
    }

    public static func build(content: String, profile: Profile,
                             jobTitle: String, company: String,
                             style stylePreset: HonestyConfig.Style = .ledger,
                             accent: HonestyConfig.ResumeAccent = .default,
                             date: Date = Date()) -> DocxDocument {
        var s = DocStyle.resolve(style: stylePreset, accent: accent)
        // A letter always breathes more than a resume — 1" side margins
        // regardless of the preset's tighter resume margins.
        s.margins = (s.banner ? 0.8 : 1.0, 1.0, 1.0, 1.0)

        var doc = DocxDocument()
        doc.margins = s.margins
        doc.defaultLineSpacing = s.lineSpacing
        let font = s.bodyFont

        // Candidate letterhead — identical treatment to the resume.
        addLetterhead(to: &doc, profile: profile, style: s, includeLinks: false)

        let formatter = DateFormatter()
        formatter.dateFormat = "MMMM d, yyyy"
        formatter.locale = Locale(identifier: "en_US")
        var dateP = DocxParagraph()
        dateP.spacingBeforePt = 10; dateP.spacingAfterPt = 12
        dateP.run(formatter.string(from: date),
                  RunStyle(font: font, sizePt: 11, colorHex: DocStyle.gray))
        doc.add(dateP)

        var refP = DocxParagraph()
        refP.spacingBeforePt = 0; refP.spacingAfterPt = 12
        refP.run("Re: \(jobTitle) at \(company)",
                 RunStyle(font: font, sizePt: 11, bold: true, colorHex: DocStyle.black))
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
        nameP.run(profile.fullName,
                  RunStyle(font: font, sizePt: 11, bold: true, colorHex: DocStyle.black))
        doc.add(nameP)

        return doc
    }
}
