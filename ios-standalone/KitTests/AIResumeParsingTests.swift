import XCTest
import JobsmithKit

// MARK: - Resume text parsing (resume_generator.py port)

final class ResumeTextParserTests: XCTestCase {
    func testCanonicalFormat() {
        let content = """
SUMMARY
Seasoned engineer with a decade of shipping.

SKILLS
Python, AWS, Docker

EXPERIENCE
Title: Senior Engineer
Company: Acme Corp
Dates: Jan 2020 - Present
- Did a thing
- Did another

Title: Engineer
Company: Beta LLC
Dates: 2018 - 2020
- Built stuff

EDUCATION
Degree: B.S. Computer Science
School: State University
Year: 2019

CERTIFICATIONS
- AWS SAA
- Security+
"""
        let parsed = ResumeTextParser.parse(content)
        XCTAssertEqual(parsed.summary, "Seasoned engineer with a decade of shipping.")
        XCTAssertEqual(parsed.skills, ["Python", "AWS", "Docker"])
        XCTAssertEqual(parsed.experiences, [
            ParsedExperienceEntry(title: "Senior Engineer", company: "Acme Corp",
                                  dates: "Jan 2020 - Present",
                                  bullets: ["Did a thing", "Did another"]),
            ParsedExperienceEntry(title: "Engineer", company: "Beta LLC",
                                  dates: "2018 - 2020", bullets: ["Built stuff"]),
        ])
        XCTAssertEqual(parsed.education,
                       [ParsedEducationEntry(degree: "B.S. Computer Science",
                                             school: "State University", year: "2019")])
        XCTAssertEqual(parsed.certifications, ["AWS SAA", "Security+"])
    }

    func testHeaderVariantsCaseAndMarkdownTolerant() {
        let content = """
## Professional Summary:
Veteran analyst.

Core Competencies
SIEM, IDS

Work Experience:
Title: Analyst
Company: SecCo
Dates: 2021 - Present
- Hunted threats

Academic Background
Degree: B.A. History
School: Yale
Year: 2010

Licenses & Certifications
- CISSP

Projects
Built a honeypot.

Achievements
Employee of the year.
"""
        let parsed = ResumeTextParser.parse(content)
        XCTAssertEqual(parsed.summary, "Veteran analyst.")
        XCTAssertEqual(parsed.skills, ["SIEM", "IDS"])
        XCTAssertEqual(parsed.experiences.first?.title, "Analyst")
        XCTAssertEqual(parsed.education.first?.school, "Yale")
        XCTAssertEqual(parsed.certifications, ["CISSP"])
        XCTAssertEqual(parsed.projects, "Built a honeypot.")
        XCTAssertEqual(parsed.awards, "Employee of the year.")
    }

    func testMoreHeaderVariants() {
        let sections = ResumeTextParser.parseSections("""
About Me
Hi there.

TECHNICAL SKILLS
Go

EMPLOYMENT HISTORY
Title: Dev
Company: X
Dates: 2020

EDUCATIONAL BACKGROUND
Degree: BS

CERTIFICATES
- One
""")
        XCTAssertEqual(sections["summary"], "Hi there.")
        XCTAssertEqual(sections["skills"], "Go")
        XCTAssertNotNil(sections["experience"])
        XCTAssertNotNil(sections["education"])
        XCTAssertNotNil(sections["certifications"])
    }

    func testPipeEntryFormat() {
        let entries = ResumeTextParser.parseExperienceEntries("""
Senior Engineer | Acme | Jan 2020 - Present
- Shipped things
""")
        XCTAssertEqual(entries, [
            ParsedExperienceEntry(title: "Senior Engineer", company: "Acme",
                                  dates: "Jan 2020 - Present", bullets: ["Shipped things"]),
        ])
    }

    func testAtNotation() {
        let entries = ResumeTextParser.parseExperienceEntries("""
Engineer at Acme (2020-2023)
- one
Manager at BigCo
- two
""")
        XCTAssertEqual(entries, [
            ParsedExperienceEntry(title: "Engineer", company: "Acme",
                                  dates: "2020-2023", bullets: ["one"]),
            ParsedExperienceEntry(title: "Manager", company: "BigCo",
                                  dates: "", bullets: ["two"]),
        ])
    }

    func testCommaParenNotation() {
        let entries = ResumeTextParser.parseExperienceEntries(
            "Staff Engineer, Initech (2019 - 2022)\n- fixed the printer")
        XCTAssertEqual(entries, [
            ParsedExperienceEntry(title: "Staff Engineer", company: "Initech",
                                  dates: "2019 - 2022", bullets: ["fixed the printer"]),
        ])
    }

    func testOrphanBulletsAndFallbackTitle() {
        let entries = ResumeTextParser.parseExperienceEntries("""
- orphan bullet
Some freeform header
- attached bullet
""")
        XCTAssertEqual(entries.count, 2)
        XCTAssertEqual(entries[0], ParsedExperienceEntry(bullets: ["orphan bullet"]))
        XCTAssertEqual(entries[1], ParsedExperienceEntry(title: "Some freeform header",
                                                         bullets: ["attached bullet"]))
    }

    func testMarkdownBoldStripped() {
        let entries = ResumeTextParser.parseExperienceEntries("""
**Title: Lead Dev**
Company: Acme
Dates: 2022 - Present
- **bold** win
""")
        XCTAssertEqual(entries.first?.title, "Lead Dev")
        XCTAssertEqual(entries.first?.bullets, ["bold win"])
    }

    func testEducationFreeformVariants() {
        XCTAssertEqual(
            ResumeTextParser.parseEducationEntries("B.S. Computer Science, Stanford University, 2020"),
            [ParsedEducationEntry(degree: "B.S. Computer Science",
                                  school: "Stanford University", year: "2020")])
        XCTAssertEqual(
            ResumeTextParser.parseEducationEntries("B.S. Computer Science | Stanford University | 2020"),
            [ParsedEducationEntry(degree: "B.S. Computer Science",
                                  school: "Stanford University", year: "2020")])
        XCTAssertEqual(
            ResumeTextParser.parseEducationEntries("M.S. Data Science, MIT (2021)"),
            [ParsedEducationEntry(degree: "M.S. Data Science", school: "MIT", year: "2021")])
        // No year → whole line kept as the degree field
        XCTAssertEqual(
            ResumeTextParser.parseEducationEntries("B.A. History, Yale"),
            [ParsedEducationEntry(degree: "B.A. History, Yale", school: "", year: "")])
    }

    func testNoHeadersBecomesSummary() {
        let parsed = ResumeTextParser.parse("Just a freeform paragraph with no headers.")
        XCTAssertEqual(parsed.summary, "Just a freeform paragraph with no headers.")
        XCTAssertTrue(parsed.experiences.isEmpty)
        XCTAssertTrue(parsed.skills.isEmpty)
    }

    func testCategorizedSkillsKeptAsLines() {
        let parsed = ResumeTextParser.parse("""
SKILLS
Security: SIEM, IDS
Cloud: AWS, GCP
""")
        XCTAssertEqual(parsed.skills, ["Security: SIEM, IDS", "Cloud: AWS, GCP"])
    }
}

// MARK: - Profile extraction (resume_parser.py port)

final class ResumeProfileParserTests: XCTestCase {
    func testHappyPathWithFencedJSON() async {
        let mock = MockAIEngine()
        mock.register("résumé parser", .text("""
```json
{"full_name": "Jane Doe", "email": "j@x.com", "location": "Austin, TX",
 "summary": "Engineer.", "skills": ["Swift", "Python"],
 "experience": [
   {"title": "Dev", "company": "Acme", "start_date": "2020", "end_date": "", "bullets": ["Did stuff"]},
   {"title": "", "company": "", "start_date": "", "end_date": "", "bullets": []}
 ],
 "education": [{"degree": "BS", "school": "U", "year": "2019"}],
 "certifications": [{"name": "Security+", "issuer": "CompTIA"}]}
```
"""))
        let result = await ResumeProfileParser.parse(text: "JANE DOE\nresume body",
                                                     config: AppConfig(), engine: mock)
        XCTAssertTrue(result.warnings.isEmpty)
        XCTAssertEqual(result.profile.fullName, "Jane Doe")
        XCTAssertEqual(result.profile.email, "j@x.com")
        XCTAssertEqual(result.profile.skills, ["Swift", "Python"])
        // Empty experience row dropped; blank end_date becomes Present.
        XCTAssertEqual(result.profile.experience.count, 1)
        XCTAssertEqual(result.profile.experience[0].endDate, "Present")
        XCTAssertEqual(result.profile.experience[0].bullets, ["Did stuff"])
        XCTAssertEqual(result.profile.education.count, 1)
        // Object-shaped certification flattened to one display string.
        XCTAssertEqual(result.profile.certifications.count, 1)
        XCTAssertTrue(result.profile.certifications[0].contains("Security+"))
        XCTAssertTrue(result.profile.certifications[0].contains("CompTIA"))
    }

    func testRequestParameters() async {
        let mock = MockAIEngine()
        mock.register("résumé parser", .text("{}"))
        _ = await ResumeProfileParser.parse(text: "short resume", config: AppConfig(), engine: mock)
        XCTAssertEqual(mock.requests.count, 1)
        XCTAssertEqual(mock.requests[0].tier, .strong)
        XCTAssertEqual(mock.requests[0].temperature, 0.1)
        XCTAssertEqual(mock.requests[0].maxTokens, 4096)
        XCTAssertTrue(mock.requests[0].user.contains("short resume"))
    }

    func testEmptyTextShortCircuits() async {
        let mock = MockAIEngine()
        let result = await ResumeProfileParser.parse(text: "   ", config: AppConfig(), engine: mock)
        XCTAssertEqual(result.warnings, ["No résumé text to parse."])
        XCTAssertTrue(result.profile.isEmpty)
        XCTAssertEqual(mock.requests.count, 0)
    }

    func testLongTextTruncatedWithWarning() async {
        let mock = MockAIEngine()
        mock.register("résumé parser", .text("{}"))
        let long = String(repeating: "a", count: 17000)
        let result = await ResumeProfileParser.parse(text: long, config: AppConfig(), engine: mock)
        XCTAssertTrue(result.warnings.contains(
            "The text was long; only the first part was parsed. Review fields carefully."))
        XCTAssertFalse(mock.requests[0].user.contains(String(repeating: "a", count: 16001)))
        XCTAssertTrue(mock.requests[0].user.contains(String(repeating: "a", count: 16000)))
    }

    func testUnparseableOutputNeverThrows() async {
        let mock = MockAIEngine()
        mock.register("résumé parser", .text("I cannot parse this resume, sorry."))
        let result = await ResumeProfileParser.parse(text: "resume", config: AppConfig(), engine: mock)
        XCTAssertTrue(result.profile.isEmpty)
        XCTAssertEqual(result.warnings,
                       ["Could not extract structured data automatically. Fill the form manually or try again."])
    }

    func testEngineFailureNeverThrows() async {
        let mock = MockAIEngine() // nothing registered → throws inside
        let result = await ResumeProfileParser.parse(text: "resume", config: AppConfig(), engine: mock)
        XCTAssertTrue(result.profile.isEmpty)
        XCTAssertEqual(result.warnings.count, 1)
        XCTAssertTrue(result.warnings[0].hasPrefix("AI extraction failed ("))
        XCTAssertTrue(result.warnings[0].hasSuffix("). Fill the form manually."))
    }

    func testLittleDataWarning() async {
        let mock = MockAIEngine()
        mock.register("résumé parser", .text(#"{"email": "x@y.com"}"#))
        let result = await ResumeProfileParser.parse(text: "resume", config: AppConfig(), engine: mock)
        XCTAssertEqual(result.warnings,
                       ["Little structured data was found — double-check every field below."])
        XCTAssertEqual(result.profile.email, "x@y.com")
    }

    func testStringListCoercionFromDelimitedString() async {
        let mock = MockAIEngine()
        mock.register("résumé parser", .text(#"{"full_name": "J", "skills": "Python, AWS; Docker"}"#))
        let result = await ResumeProfileParser.parse(text: "resume", config: AppConfig(), engine: mock)
        XCTAssertEqual(result.profile.skills, ["Python", "AWS", "Docker"])
    }
}
