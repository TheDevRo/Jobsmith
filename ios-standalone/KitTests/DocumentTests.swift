import XCTest
import ZIPFoundation
import JobsmithKit

final class DocumentTests: XCTestCase {
    private func sampleProfile() -> Profile {
        Profile(fullName: "Jane Doe", email: "jane@example.com", phone: "555-555-5555",
                location: "Denver, CO", linkedin: "https://linkedin.com/in/janedoe",
                summary: "unused here", skills: [],
                references: [Reference(name: "Pat Ref", position: "Manager",
                                       email: "pat@example.com", phone: "555-555-5556")])
    }

    private func sampleContent() -> DocResumeContent {
        DocResumeContent(
            summary: "Engineer with **8 years** of experience.",
            skillsText: "Python, Swift, Docker",
            experiences: [DocExperience(title: "Senior Engineer", company: "Acme",
                                        dates: "Jan 2022 - Present",
                                        bullets: ["Built things & shipped them", "Led a team"])],
            education: [DocEducation(degree: "B.S. Computer Science", school: "State U", year: "2019")],
            certifications: ["AWS Solutions Architect"])
    }

    private func documentXML(from data: Data) throws -> String {
        let archive = try XCTUnwrap(Archive(data: data, accessMode: .read))
        let entry = try XCTUnwrap(archive["word/document.xml"])
        var xml = Data()
        _ = try archive.extract(entry) { xml.append($0) }
        return String(decoding: xml, as: UTF8.self)
    }

    func testResumeDocxStructure() throws {
        let data = try ResumeDocxGenerator.generate(content: sampleContent(),
                                                    profile: sampleProfile(), style: .ledger)
        let xml = try documentXML(from: data)

        // Ledger keeps the name mixed-case.
        XCTAssertTrue(xml.contains(">Jane Doe<"))
        // Section headers in fixed order.
        for header in ["SUMMARY", "TECHNICAL SKILLS", "PROFESSIONAL EXPERIENCE", "EDUCATION", "CERTIFICATIONS", "REFERENCES"] {
            XCTAssertTrue(xml.contains(header), "missing section \(header)")
        }
        // Markdown bold stripped from summary.
        XCTAssertTrue(xml.contains("Engineer with 8 years of experience."))
        XCTAssertFalse(xml.contains("**"))
        // XML-escaped ampersand in bullet text.
        XCTAssertTrue(xml.contains("Built things &amp; shipped them"))
        // Right tab stop for dates (inline layout, 6.9in text column = 9936 twips).
        XCTAssertTrue(xml.contains("w:pos=\"9936\""))
        // Header rules present.
        XCTAssertTrue(xml.contains("<w:pBdr><w:bottom w:val=\"single\""))
        // Section headers are kept with the content that follows them.
        XCTAssertTrue(xml.contains("<w:keepNext/>"))
        // References appended.
        XCTAssertTrue(xml.contains("Pat Ref"))
        // Valid archive contains all four OPC parts.
        let archive = try XCTUnwrap(Archive(data: data, accessMode: .read))
        for part in ["[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/_rels/document.xml.rels"] {
            XCTAssertNotNil(archive[part], "missing \(part)")
        }
    }

    func testLedgerPresetUsesInlineLayoutAndHyperlinks() throws {
        let data = try ResumeDocxGenerator.generate(content: sampleContent(),
                                                    profile: sampleProfile(), style: .ledger)
        let xml = try documentXML(from: data)
        // Inline layout separator between title and company.
        XCTAssertTrue(xml.contains("  ·  "))
        // Hyperlinked contact entry with relationship id.
        XCTAssertTrue(xml.contains("<w:hyperlink r:id=\"rId100\">"))
        XCTAssertTrue(xml.contains("linkedin.com/in/janedoe"))
        // Calibri, not Aptos — Aptos is Microsoft 365-only and substitutes to a
        // serif everywhere else, which would wreck this style's bold-sans identity.
        XCTAssertTrue(xml.contains("Calibri"))
        XCTAssertFalse(xml.contains("Aptos"))
        // Ledger's default accent (navy) colors headers, stubs and company names.
        XCTAssertTrue(xml.contains("w:val=\"1F3A5F\""))
        // Stub bars: an empty bordered paragraph with a big right indent.
        XCTAssertTrue(xml.contains("w:right="))
    }

    func testExecutiveIsSerifSmallCapsWithDoubleRuleAndIgnoresAccent() throws {
        let data = try ResumeDocxGenerator.generate(content: sampleContent(),
                                                    profile: sampleProfile(),
                                                    style: .executive, accent: .burgundy)
        let xml = try documentXML(from: data)
        XCTAssertTrue(xml.contains("Georgia"))
        XCTAssertTrue(xml.contains("<w:smallCaps/>"))
        XCTAssertTrue(xml.contains("<w:bottom w:val=\"double\""))
        // accent_locked: the user's burgundy must not leak in.
        XCTAssertFalse(xml.contains("6D1F2C"))
    }

    func testBannerUsesParagraphShadingNotATable() throws {
        let data = try ResumeDocxGenerator.generate(content: sampleContent(),
                                                    profile: sampleProfile(), style: .banner)
        let xml = try documentXML(from: data)
        // The band is paragraph shading on real text — the ATS-safe way.
        XCTAssertTrue(xml.contains("<w:shd w:val=\"clear\" w:color=\"auto\" w:fill=\"1F2D42\"/>"))
        // Banner drops contact hyperlinks (they'd be unreadable on the band).
        XCTAssertFalse(xml.contains("<w:hyperlink"))
    }

    func testAccentRecolorsUnlockedStylesOnly() throws {
        let forest = DocStyle.resolve(style: .ledger, accent: .forest)
        XCTAssertEqual(forest.accent, "1F4D3A")
        XCTAssertEqual(forest.headerColor, "1F4D3A")
        XCTAssertEqual(forest.nameRuleColor, "1F4D3A")
        // company_style is an enum value, not a color — it must survive the
        // accent-sentinel substitution intact.
        XCTAssertEqual(forest.companyStyle, "accent")
        // Locked presets keep their own ink.
        XCTAssertEqual(DocStyle.resolve(style: .swiss, accent: .forest).accent, "3A3F47")
        XCTAssertEqual(DocStyle.resolve(style: .executive, accent: .plum).accent, "17202B")
        // "default" keeps the preset's own accent.
        XCTAssertEqual(DocStyle.resolve(style: .ledger, accent: .default).accent, "1F3A5F")
    }

    /// The non-negotiable invariant: every style is single-column real text.
    func testEveryStyleStaysSingleColumnRealText() throws {
        for style in HonestyConfig.Style.allCases {
            let data = try ResumeDocxGenerator.generate(content: sampleContent(),
                                                        profile: sampleProfile(), style: style)
            let xml = try documentXML(from: data)
            XCTAssertFalse(xml.contains("<w:tbl"), "\(style) emitted a table")
            XCTAssertFalse(xml.contains("<w:drawing"), "\(style) emitted a drawing")
            XCTAssertFalse(xml.contains("<w:txbxContent"), "\(style) emitted a text box")
            XCTAssertFalse(xml.contains("<w:pict"), "\(style) emitted a picture")
            XCTAssertFalse(xml.contains("<w:cols w:num"), "\(style) emitted multiple columns")
            // Real text survived.
            XCTAssertTrue(xml.contains("Senior Engineer"), "\(style) lost the job title")
            // Word rejects malformed document.xml outright ("unreadable content").
            let parser = XMLParser(data: Data(xml.utf8))
            XCTAssertTrue(parser.parse(), "\(style) produced malformed XML: \(parser.parserError as Any)")
        }
        // Only Banner paints a band, and it does so with paragraph shading.
        let banner = try documentXML(from: try ResumeDocxGenerator.generate(
            content: sampleContent(), profile: sampleProfile(), style: .banner))
        XCTAssertTrue(banner.contains("<w:shd"))
        let swiss = try documentXML(from: try ResumeDocxGenerator.generate(
            content: sampleContent(), profile: sampleProfile(), style: .swiss))
        XCTAssertFalse(swiss.contains("<w:shd"))
    }

    func testCategorizedSkills() throws {
        var content = sampleContent()
        content.skillsText = "Languages: Python, Swift\nCloud: AWS, GCP"
        let data = try ResumeDocxGenerator.generate(content: content,
                                                    profile: sampleProfile(), style: .swiss)
        let xml = try documentXML(from: data)
        XCTAssertTrue(xml.contains("Languages: "))
        XCTAssertTrue(xml.contains("Arial"))
    }

    /// Compact joins a flat skills list with pipes; Swiss with middots.
    func testSkillsSeparatorFollowsPreset() throws {
        let compact = try documentXML(from: try ResumeDocxGenerator.generate(
            content: sampleContent(), profile: sampleProfile(), style: .compact))
        XCTAssertTrue(compact.contains("Python | Swift | Docker"))
        let swiss = try documentXML(from: try ResumeDocxGenerator.generate(
            content: sampleContent(), profile: sampleProfile(), style: .swiss))
        XCTAssertTrue(swiss.contains("Python  ·  Swift  ·  Docker"))
    }

    func testCoverLetterSkipsAIClosings() throws {
        let content = """
        Dear Hiring Team,

        I am excited to apply for this role.

        Thank you for your consideration.

        Sincerely,
        Someone Else
        """
        let data = try CoverLetterDocxGenerator.generate(content: content,
                                                         profile: sampleProfile(),
                                                         jobTitle: "Engineer", company: "Acme",
                                                         date: Date(timeIntervalSince1970: 1_750_000_000))
        let xml = try documentXML(from: data)
        XCTAssertTrue(xml.contains("Dear Hiring Team,"))
        XCTAssertTrue(xml.contains("Re: Engineer at Acme"))
        // AI closing paragraphs skipped; our own "Sincerely," appended once.
        XCTAssertNil(xml.range(of: "Someone Else"))
        XCTAssertEqual(xml.components(separatedBy: "Sincerely,").count - 1, 1)
        XCTAssertTrue(xml.contains("Jane Doe"))
    }

    /// The cover letter shares the resume's letterhead and typography.
    func testCoverLetterHonorsStylePreset() throws {
        let executive = try documentXML(from: try CoverLetterDocxGenerator.generate(
            content: "Dear Hiring Team,\n\nI would be a great fit.",
            profile: sampleProfile(), jobTitle: "Engineer", company: "Acme",
            style: .executive))
        XCTAssertTrue(executive.contains("Georgia"))
        XCTAssertTrue(executive.contains("<w:smallCaps/>"))
        XCTAssertTrue(executive.contains("<w:bottom w:val=\"double\""))
        // Cover letters never carry the resume's contact links.
        XCTAssertFalse(executive.contains("linkedin.com"))

        let banner = try documentXML(from: try CoverLetterDocxGenerator.generate(
            content: "Dear Hiring Team,\n\nI would be a great fit.",
            profile: sampleProfile(), jobTitle: "Engineer", company: "Acme",
            style: .banner, accent: .burgundy))
        // Banner is not accent-locked, so the user's burgundy drives the band.
        XCTAssertTrue(banner.contains("<w:shd w:val=\"clear\" w:color=\"auto\" w:fill=\"6D1F2C\"/>"))
        XCTAssertFalse(banner.contains("<w:tbl"))
    }

    #if canImport(UIKit)
    /// The PDF renderer consumes the same layout model as the .docx writer, so
    /// the resume text round-trips back out through PDFKit extraction.
    func testResumePDFIsValidAndRoundTripsText() throws {
        let doc = ResumeDocxGenerator.build(content: sampleContent(),
                                            profile: sampleProfile(), style: .ledger)
        let data = DocxPDFRenderer.render(doc)

        // Real PDF (magic header) and non-trivial.
        XCTAssertTrue(data.starts(with: Array("%PDF".utf8)))
        XCTAssertGreaterThan(data.count, 1000)

        let text = try ResumeTextExtractor.extract(filename: "resume.pdf", data: data)
        XCTAssertTrue(text.contains("Senior Engineer"))
        XCTAssertTrue(text.contains("Built things"))
        // Markdown bold stripped, same as the .docx path.
        XCTAssertTrue(text.contains("8 years"))
        XCTAssertFalse(text.contains("**"))
    }

    func testCoverLetterPDFIsValid() throws {
        let doc = CoverLetterDocxGenerator.build(
            content: "Dear Hiring Team,\n\nI would be a great fit.",
            profile: sampleProfile(), jobTitle: "Engineer", company: "Acme",
            date: Date(timeIntervalSince1970: 1_750_000_000))
        let data = DocxPDFRenderer.render(doc)
        XCTAssertTrue(data.starts(with: Array("%PDF".utf8)))
        let text = try ResumeTextExtractor.extract(filename: "cover.pdf", data: data)
        XCTAssertTrue(text.contains("Re: Engineer at Acme"))
    }

    /// Every preset must render a PDF whose text still extracts — including
    /// Banner (shaded band) and Executive (small caps → upper-cased).
    func testEveryStylePDFRoundTripsText() throws {
        for style in HonestyConfig.Style.allCases {
            let doc = ResumeDocxGenerator.build(content: sampleContent(),
                                                profile: sampleProfile(), style: style)
            let data = DocxPDFRenderer.render(doc)
            XCTAssertTrue(data.starts(with: Array("%PDF".utf8)), "\(style) is not a PDF")
            let text = try ResumeTextExtractor.extract(filename: "resume.pdf", data: data)
            XCTAssertTrue(text.contains("Senior Engineer"), "\(style) lost the job title")
            XCTAssertTrue(text.contains("Built things"), "\(style) lost a bullet")
        }
    }

    // MARK: - Style preview (the settings picker)

    func testStylePreviewRendersEveryStyle() throws {
        for style in HonestyConfig.Style.allCases {
            for accent in HonestyConfig.ResumeAccent.allCases {
                let data = StylePreviewSample.pdf(style: style, accent: accent)
                XCTAssertTrue(data.starts(with: Array("%PDF".utf8)),
                              "\(style)/\(accent) is not a PDF")
                XCTAssertGreaterThan(data.count, 1000,
                                     "\(style)/\(accent) rendered an empty page")
            }
        }
    }

    /// A specimen that skipped a section would hide that section's styling, so
    /// the sample has to exercise everything a style can touch.
    func testStylePreviewSampleShowsEverySection() throws {
        let data = StylePreviewSample.pdf(style: .ledger)
        let text = try ResumeTextExtractor.extract(filename: "resume.pdf", data: data)
        XCTAssertTrue(text.contains("Morgan Reyes"))        // letterhead
        XCTAssertTrue(text.contains("Northwind Logistics")) // accent company name
        XCTAssertTrue(text.contains("2021 - Present"))      // right-aligned dates
        XCTAssertTrue(text.contains("Oregon State"))        // education
        XCTAssertTrue(text.contains("Tableau"))             // skills + certifications
    }

    // MARK: - PDF import reading order

    /// Régression: importing a PDF résumé used to lose every employment date.
    ///
    /// `PDFPage.string` treats a right-aligned date column as a separate column
    /// and emits it at the *end of the page*, detached from the roles, while
    /// gluing the first bullet onto the entry line. The extracted text went
    /// straight to the AI, which had no way to reattach dates to jobs.
    func testPDFImportKeepsDatesOnTheirEntryLine() throws {
        let pdf = StylePreviewSample.pdf(style: .ledger)
        let text = try ResumeTextExtractor.extract(filename: "resume.pdf", data: pdf)
        let lines = text.split(separator: "\n").map(String.init)

        let senior = try XCTUnwrap(lines.first { $0.contains("Senior Data Analyst") },
                                   "lost the entry line entirely")
        XCTAssertTrue(senior.contains("2021 - Present"),
                      "dates detached from their role: \(senior)")
        XCTAssertFalse(senior.contains("Cut freight spend"),
                       "first bullet glued onto the entry line: \(senior)")

        let second = try XCTUnwrap(lines.first { $0.contains("Data Analyst · Cascade") })
        XCTAssertTrue(second.contains("2018 - 2021"), "dates detached: \(second)")

        // Nothing may be stranded at the foot of the page.
        XCTAssertFalse(lines.last?.contains("2021") ?? false,
                       "dates stranded at the end of the page: \(lines.last ?? "")")

        // Bullets keep their own lines, in order.
        XCTAssertTrue(lines.contains { $0.hasPrefix("•") && $0.contains("Cut freight spend") })
    }

    /// Every style must survive the round trip, not just the one style whose
    /// geometry happened to cooperate.
    func testPDFImportReadingOrderHoldsForEveryStyle() throws {
        for style in HonestyConfig.Style.allCases {
            let pdf = StylePreviewSample.pdf(style: style)
            let text = try ResumeTextExtractor.extract(filename: "resume.pdf", data: pdf)
            let entry = try XCTUnwrap(
                text.split(separator: "\n").map(String.init)
                    .first { $0.contains("Senior Data Analyst") },
                "\(style): lost the entry line")
            XCTAssertTrue(entry.contains("2021 - Present"),
                          "\(style): dates detached from their role — got '\(entry)'")
        }
    }
    #endif

    func testDocumentFormatDefaultsToPDF() {
        XCTAssertEqual(HonestyConfig().documentFormat, .pdf)
        XCTAssertEqual(FileVault.Format.pdf.label, "PDF")
        XCTAssertEqual(FileVault.Format.docx.label, "Word (.docx)")
    }

    // MARK: - Config

    func testResumeStyleDefaultsToLedger() {
        XCTAssertEqual(HonestyConfig().resumeStyle, .ledger)
        XCTAssertEqual(HonestyConfig().resumeAccent, .default)
    }

    /// Configs persisted before the five-style lineup must keep working —
    /// the retired names map forward instead of failing to decode.
    func testLegacyResumeStylesDecodeToTheirReplacements() throws {
        func decode(_ json: String) throws -> HonestyConfig {
            try JSONDecoder().decode(HonestyConfig.self, from: Data(json.utf8))
        }
        XCTAssertEqual(try decode(#"{"resumeStyle":"modern"}"#).resumeStyle, .ledger)
        XCTAssertEqual(try decode(#"{"resumeStyle":"standard"}"#).resumeStyle, .ledger)
        XCTAssertEqual(try decode(#"{"resumeStyle":"minimal"}"#).resumeStyle, .swiss)
        // New names round-trip; an unknown name falls back to the default.
        XCTAssertEqual(try decode(#"{"resumeStyle":"banner"}"#).resumeStyle, .banner)
        XCTAssertEqual(try decode(#"{"resumeStyle":"nonsense"}"#).resumeStyle, .ledger)
        // A config with no style/accent keys at all still decodes.
        XCTAssertEqual(try decode("{}").resumeStyle, .ledger)
        XCTAssertEqual(try decode("{}").resumeAccent, .default)
        // Accent decodes leniently too.
        XCTAssertEqual(try decode(#"{"resumeAccent":"forest"}"#).resumeAccent, .forest)
        XCTAssertEqual(try decode(#"{"resumeAccent":"chartreuse"}"#).resumeAccent, .default)
        // And the same mapping backs the persisted application.style_preset.
        XCTAssertEqual(HonestyConfig.Style.fromPersisted("minimal"), .swiss)
        XCTAssertEqual(HonestyConfig.Style.fromPersisted("standard"), .ledger)
    }

    func testOnlyExecutiveAndSwissAreMonochrome() {
        XCTAssertEqual(HonestyConfig.Style.allCases.filter(\.isMonochrome), [.executive, .swiss])
        for style in HonestyConfig.Style.allCases {
            XCTAssertEqual(DocStyle.preset(style).accentLocked, style.isMonochrome,
                           "\(style): accentLocked disagrees with isMonochrome")
        }
    }
}

final class ResumeTextExtractorTests: XCTestCase {
    func testDocxRoundTrip() throws {
        let content = DocResumeContent(
            summary: "Engineer with eight years of experience.",
            skillsText: "Python, Swift, Docker",
            experiences: [DocExperience(title: "Senior Engineer", company: "Acme",
                                        dates: "Jan 2022 - Present",
                                        bullets: ["Built things", "Led a team"])],
            education: [DocEducation(degree: "B.S. Computer Science",
                                     school: "State U", year: "2019")],
            certifications: [])
        let profile = Profile(fullName: "Jane Doe", email: "jane@example.com",
                              phone: "555-555-5555", location: "Denver, CO")
        let docx = try ResumeDocxGenerator.generate(content: content,
                                                    profile: profile, style: .ledger)

        let text = try ResumeTextExtractor.extract(filename: "resume.docx", data: docx)
        XCTAssertTrue(text.contains("Engineer with eight years of experience."))
        XCTAssertTrue(text.contains("Senior Engineer"))
        XCTAssertTrue(text.contains("Built things"))
        // Paragraphs come back on separate lines.
        XCTAssertTrue(text.contains("\n"))
    }

    func testPlainTextPassthrough() throws {
        let text = try ResumeTextExtractor.extract(
            filename: "resume.txt", data: Data("Jane Doe\nEngineer".utf8))
        XCTAssertEqual(text, "Jane Doe\nEngineer")
        // No filename → treated as pasted plain text.
        XCTAssertEqual(try ResumeTextExtractor.extract(filename: "", data: Data("hi".utf8)), "hi")
    }

    func testUnsupportedExtensionThrows() {
        XCTAssertThrowsError(try ResumeTextExtractor.extract(filename: "resume.pages",
                                                             data: Data()))
        XCTAssertThrowsError(try ResumeTextExtractor.extract(filename: "resume.docx",
                                                             data: Data("not a zip".utf8)))
    }
}
