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
                                                    profile: sampleProfile(), style: .standard)
        let xml = try documentXML(from: data)

        // Name uppercased with letter spacing (standard preset).
        XCTAssertTrue(xml.contains("JANE DOE"))
        // Section headers in fixed order.
        for header in ["SUMMARY", "TECHNICAL SKILLS", "PROFESSIONAL EXPERIENCE", "EDUCATION", "CERTIFICATIONS", "REFERENCES"] {
            XCTAssertTrue(xml.contains(header), "missing section \(header)")
        }
        // Markdown bold stripped from summary.
        XCTAssertTrue(xml.contains("Engineer with 8 years of experience."))
        XCTAssertFalse(xml.contains("**"))
        // XML-escaped ampersand in bullet text.
        XCTAssertTrue(xml.contains("Built things &amp; shipped them"))
        // Right tab stop for dates (stacked layout: 6.1in = 8784 twips).
        XCTAssertTrue(xml.contains("w:pos=\"8784\""))
        // Header underline borders (standard preset).
        XCTAssertTrue(xml.contains("<w:pBdr><w:bottom w:val=\"single\""))
        // References appended.
        XCTAssertTrue(xml.contains("Pat Ref"))
        // Valid archive contains all four OPC parts.
        let archive = try XCTUnwrap(Archive(data: data, accessMode: .read))
        for part in ["[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/_rels/document.xml.rels"] {
            XCTAssertNotNil(archive[part], "missing \(part)")
        }
    }

    func testModernPresetUsesInlineLayoutAndHyperlinks() throws {
        let data = try ResumeDocxGenerator.generate(content: sampleContent(),
                                                    profile: sampleProfile(), style: .modern)
        let xml = try documentXML(from: data)
        // Inline layout separator between title and company.
        XCTAssertTrue(xml.contains("  ·  "))
        // Hyperlinked contact entry with relationship id.
        XCTAssertTrue(xml.contains("<w:hyperlink r:id=\"rId100\">"))
        XCTAssertTrue(xml.contains("linkedin.com/in/janedoe"))
        // Modern: no header underline on section headers, but name rule exists.
        XCTAssertTrue(xml.contains("Aptos"))
        // Name NOT uppercased in modern.
        XCTAssertTrue(xml.contains(">Jane Doe<"))
    }

    func testCategorizedSkills() throws {
        var content = sampleContent()
        content.skillsText = "Languages: Python, Swift\nCloud: AWS, GCP"
        let data = try ResumeDocxGenerator.generate(content: content,
                                                    profile: sampleProfile(), style: .minimal)
        let xml = try documentXML(from: data)
        XCTAssertTrue(xml.contains("Languages: "))
        XCTAssertTrue(xml.contains("Times New Roman"))
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

    #if canImport(UIKit)
    /// The PDF renderer consumes the same layout model as the .docx writer, so
    /// the resume text round-trips back out through PDFKit extraction.
    func testResumePDFIsValidAndRoundTripsText() throws {
        let doc = ResumeDocxGenerator.build(content: sampleContent(),
                                            profile: sampleProfile(), style: .standard)
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
    #endif

    func testDocumentFormatDefaultsToPDF() {
        XCTAssertEqual(HonestyConfig().documentFormat, .pdf)
        XCTAssertEqual(FileVault.Format.pdf.label, "PDF")
        XCTAssertEqual(FileVault.Format.docx.label, "Word (.docx)")
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
                                                    profile: profile, style: .standard)

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
