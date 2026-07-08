import XCTest
import JobsmithKit

// MARK: - Shared fixtures

func aiTestJob(title: String = "iOS Engineer", company: String = "Acme",
               description: String = "Build delightful iOS apps in Swift.") -> Job {
    Job(from: NormalizedJob(source: "test", externalId: "j1", title: title,
                            company: company, description: description))
}

func aiTestProfile() -> Profile {
    Profile(
        fullName: "Jane Doe",
        summary: "Security-minded software engineer.",
        skills: ["Python", "AWS"],
        experience: [
            WorkExperience(title: "Engineer", company: "Acme",
                           startDate: "Jan 2020", endDate: "Present",
                           bullets: ["Built X", "Led Y"]),
        ],
        education: [Education(degree: "B.S. CS", school: "State U", year: "2015")],
        certifications: ["Security+"],
        references: [Reference(name: "Confidential Referee", position: "Boss",
                               email: "referee@secret.example", phone: "555-0100")])
}

private let allTemplateVars: [String: String] = [
    "job_title": "iOS Engineer", "job_company": "Acme",
    "job_description": "Build apps", "profile_summary": "Name: Jane",
    "role_lines": "Role 0: Engineer", "answer_lines": "- Direction: mobile",
    "directions": "- Target companies: Fortune 500",
    "keywords": "swift", "liked": "Acme", "excluded": "None",
    "soc_hints": "15-1252 Software Developers",
    "honesty_instruction": "TAILORING DIRECTIVE: test",
    "keyword_targets": "KEYWORD TARGETS: test",
    "tone_instruction": "TONE: test", "fabrication_guard": "Guard: test",
    "user_instructions": "tighten it", "current_resume": "SUMMARY\nOld",
    "current_letter": "Dear Team,", "resume_text": "SUMMARY\nNew",
    "cover_letter_text": "Dear Team, new", "resume": "resume text here",
    "questions": "- Why us?", "max_words": "80",
    "apply_instruction": "Click Apply", "mode_step_8": "8. Stop before submit.",
    "mode_step_9": "9. Report.", "mode_rule": "Never submit.",
    "extra_rules": "None.", "file_line": "No resume file available.",
    "candidate_data": "Name: Jane", "summary": "Engineer.",
]

// MARK: - Prompt registry

final class PromptRegistryTests: XCTestCase {
    func testUnknownPlaceholderStaysLiteral() {
        let out = PromptRegistry.render(template: "Hello {name}, {unknown} and {Not_Lower}",
                                        ["name": "Jane"])
        XCTAssertEqual(out, "Hello Jane, {unknown} and {Not_Lower}")
    }

    func testJSONBracesInBodiesUntouched() {
        let out = PromptRegistry.render("select_resume_experiences", allTemplateVars,
                                        config: AppConfig())
        XCTAssertTrue(out.contains(#"{"scores": [{"index": <int>, "score": <0-100>}, ...]}"#))
        XCTAssertTrue(out.contains("Title: iOS Engineer"))
    }

    func testOverrideWins() {
        var config = AppConfig()
        config.promptOverrides["score_job_fit"] = "OVERRIDE {job_title}!"
        let out = PromptRegistry.render("score_job_fit", ["job_title": "Dev"], config: config)
        XCTAssertEqual(out, "OVERRIDE Dev!")
    }

    func testBlankOverrideIgnored() {
        var config = AppConfig()
        config.promptOverrides["score_job_fit"] = "   \n  "
        let out = PromptRegistry.render("score_job_fit", ["job_title": "Dev"], config: config)
        XCTAssertTrue(out.contains("You are a career advisor AI."))
    }

    func testAllTemplatesRenderFully() {
        XCTAssertEqual(PromptRegistry.templateIds.count, 16)
        for id in PromptRegistry.templateIds {
            XCTAssertNotNil(PromptRegistry.defaultTemplate(id), "missing default for \(id)")
            let out = PromptRegistry.render(id, allTemplateVars, config: AppConfig())
            XCTAssertFalse(out.isEmpty, "\(id) rendered empty")
            XCTAssertNil(out.range(of: "\\{[a-z][a-z0-9_]*\\}", options: .regularExpression),
                         "\(id) left an unsubstituted placeholder: \(out.prefix(200))")
        }
    }
}

// MARK: - Directives

final class DirectivesTests: XCTestCase {
    func testHonestInstructionVerbatim() {
        let expected = """
TAILORING DIRECTIVE:
Tailor the resume to highlight genuinely relevant experience. Do not add, invent, or exaggerate anything. Reorder and reword only.
- You may ONLY use experience, education, and certifications from the CANDIDATE PROFILE below.
- NEVER invent, fabricate, or add jobs, companies, degrees, or certifications that are not in the candidate's profile.
- NEVER add the target job/company to the experience section — the candidate is APPLYING there.
- NEVER change, merge, or consolidate dates. Copy them EXACTLY as given.
"""
        XCTAssertEqual(Directives.honestyInstruction(.honest), expected)
    }

    func testAllHonestyLevelsDistinct() {
        let all = HonestyConfig.Level.allCases.map { Directives.honestyInstruction($0) }
        XCTAssertEqual(Set(all).count, 4)
        XCTAssertTrue(Directives.honestyInstruction(.fabricated)
            .contains("You may invent specific achievements with plausible metrics"))
    }

    func testFabricationGuards() {
        XCTAssertTrue(Directives.reviseFabricationGuard(.honest)
            .hasPrefix("Critical: Only use facts present in the candidate profile."))
        XCTAssertEqual(
            Directives.reviseFabricationGuard(.fabricated),
            "Apply the user's instruction aggressively. You may invent specific achievements with plausible metrics, add skills/tools, and upgrade responsibilities to satisfy the request. Keep everything internally consistent and believable; preserve the existing timeline (companies and rough date ranges).")
        XCTAssertEqual(Set(HonestyConfig.Level.allCases.map { Directives.reviseFabricationGuard($0) }).count, 4)
    }

    func testToneInstructions() {
        XCTAssertEqual(
            Directives.toneInstruction(.professional),
            "TONE: Write in a formal, corporate tone. Use complete sentences, no contractions (use 'I am' not 'I'm'). Maintain professional distance while still being engaging.")
        XCTAssertEqual(Set(HonestyConfig.Tone.allCases.map { Directives.toneInstruction($0) }).count, 3)
    }
}

// MARK: - Profile summary block

final class ProfileSummaryTests: XCTestCase {
    func testExactFormat() {
        let expected = """
Name: Jane Doe
Summary: Security-minded software engineer.
Skills: Python, AWS

EXPERIENCE (1 separate roles — each has its own dates, do NOT merge them):
  Role 1:
    Title: Engineer
    Company: Acme
    Dates: Jan 2020 - Present
    - Built X
    - Led Y

Education: B.S. CS from State U (2015)
Certifications: Security+
"""
        XCTAssertEqual(Directives.profileSummary(aiTestProfile()), expected)
    }

    func testReferencesNeverIncluded() {
        let summary = Directives.profileSummary(aiTestProfile())
        XCTAssertFalse(summary.contains("Confidential Referee"))
        XCTAssertFalse(summary.contains("referee@secret.example"))
        XCTAssertFalse(summary.contains("555-0100"))
    }

    func testExperienceOverrideAndSkippedUntitledRoles() {
        let roles = [
            WorkExperience(title: "", company: "Ghost Co"),
            WorkExperience(title: "Analyst", company: "Beta",
                           startDate: "2018", endDate: "2019", bullets: []),
        ]
        let summary = Directives.profileSummary(aiTestProfile(), experiences: roles)
        XCTAssertTrue(summary.contains("EXPERIENCE (2 separate roles"))
        XCTAssertFalse(summary.contains("Ghost Co"))
        XCTAssertTrue(summary.contains("  Role 2:\n    Title: Analyst"))
        XCTAssertFalse(summary.contains("Role 1:"))
    }
}

// MARK: - Lenient JSON

final class LenientJSONTests: XCTestCase {
    func testDirectObject() {
        XCTAssertEqual(LenientJSON.parseObject(#"{"a": 1}"#)?["a"] as? Int, 1)
    }

    func testFencedJSON() {
        let text = """
        ```json
        {"a": [1, 2], "b": "x"}
        ```
        """
        let obj = LenientJSON.parseObject(text)
        XCTAssertEqual(obj?["b"] as? String, "x")
    }

    func testTrailingTextSalvage() {
        let text = "Sure! Here's the result:\n{\"score\": 42, \"nested\": {\"ok\": true}}\nHope that helps"
        let obj = LenientJSON.parseObject(text)
        XCTAssertEqual(obj?["score"] as? Int, 42)
    }

    func testGarbageReturnsNil() {
        XCTAssertNil(LenientJSON.parseObject("no json to be found"))
        XCTAssertNil(LenientJSON.parseObject("{broken"))
    }

    func testFirstNumber() {
        XCTAssertEqual(LenientJSON.firstNumber(in: "I'd rate this 85 out of 100"), 85)
        XCTAssertEqual(LenientJSON.firstNumber(in: "100"), 100)
        XCTAssertNil(LenientJSON.firstNumber(in: "no digits here"))
    }
}

// MARK: - Scoring

final class ScoringServiceTests: XCTestCase {
    private let goodJSON = """
{"score": 88, "reasoning": "Great fit", "matched_skills": ["Swift", "iOS"], \
"missing_skills": ["Kotlin"], "matched_soft_skills": ["Communication"], \
"missing_soft_skills": [], "title_alignment": "strong", "keywords": ["Swift", "SwiftUI"]}
"""

    func testCleanJSONResponse() async {
        let mock = MockAIEngine()
        mock.register("career advisor AI", .text(goodJSON))
        let result = await ScoringService.score(job: aiTestJob(), profile: aiTestProfile(),
                                                config: AppConfig(), engine: mock)
        XCTAssertEqual(result.score, 88)
        XCTAssertEqual(result.reasoning, "Great fit")
        let report = LenientJSON.parseObject(result.matchReportJSON ?? "")
        XCTAssertEqual(report?["matched_skills"] as? [String], ["Swift", "iOS"])
        XCTAssertEqual(report?["title_alignment"] as? String, "strong")
        XCTAssertEqual(mock.requests.count, 1)
        XCTAssertEqual(mock.requests[0].maxTokens, 1200)
        XCTAssertEqual(mock.requests[0].tier, .fast)
    }

    func testListCapsSanitized() async {
        let many = (1...20).map { "\"Skill\($0)\"" }.joined(separator: ", ")
        let mock = MockAIEngine()
        mock.register("career advisor AI",
                      .text("{\"score\": 50, \"reasoning\": \"r\", \"matched_skills\": [\(many)]}"))
        let result = await ScoringService.score(job: aiTestJob(), profile: aiTestProfile(),
                                                config: AppConfig(), engine: mock)
        let report = LenientJSON.parseObject(result.matchReportJSON ?? "")
        XCTAssertEqual((report?["matched_skills"] as? [String])?.count, 12)
    }

    func testEmbeddedObjectSalvage() async {
        let mock = MockAIEngine()
        mock.register("career advisor AI", .text("Here you go: \(goodJSON) — enjoy!"))
        let result = await ScoringService.score(job: aiTestJob(), profile: aiTestProfile(),
                                                config: AppConfig(), engine: mock)
        XCTAssertEqual(result.score, 88)
        XCTAssertNotNil(result.matchReportJSON)
    }

    func testRegexScoreSalvage() async {
        let mock = MockAIEngine()
        mock.register("career advisor AI",
                      .text("The verdict — \"score\": 72, \"reasoning\": \"Decent overlap\" and so on"))
        let result = await ScoringService.score(job: aiTestJob(), profile: aiTestProfile(),
                                                config: AppConfig(), engine: mock)
        XCTAssertEqual(result.score, 72)
        XCTAssertEqual(result.reasoning, "Decent overlap")
        XCTAssertNil(result.matchReportJSON)
    }

    func testNumberScanFallback() async {
        let mock = MockAIEngine()
        mock.register("career advisor AI", .text("I would rate this candidate 85 out of 100."))
        let result = await ScoringService.score(job: aiTestJob(), profile: aiTestProfile(),
                                                config: AppConfig(), engine: mock)
        XCTAssertEqual(result.score, 85)
        XCTAssertTrue(result.reasoning.hasPrefix("(Score parsed from raw response)"))
    }

    func testUnparseableReturnsZero() async {
        let mock = MockAIEngine()
        mock.register("career advisor AI", .text("no idea, sorry"))
        let result = await ScoringService.score(job: aiTestJob(), profile: aiTestProfile(),
                                                config: AppConfig(), engine: mock)
        XCTAssertEqual(result.score, 0.0)
        XCTAssertTrue(result.reasoning.hasPrefix("ERROR: Could not parse score"))
    }

    func testRetryOnceAtLowTemperature() async {
        let mock = MockAIEngine()
        mock.register("career advisor AI", .failure("connection reset"), .text(goodJSON))
        let result = await ScoringService.score(job: aiTestJob(), profile: aiTestProfile(),
                                                config: AppConfig(), engine: mock)
        XCTAssertEqual(mock.requests.count, 2)
        XCTAssertEqual(mock.requests[0].temperature, 0.7)
        XCTAssertEqual(mock.requests[1].temperature, 0.3)
        XCTAssertEqual(result.score, 88)
    }

    func testBothCallsFailing() async {
        let mock = MockAIEngine()
        mock.register("career advisor AI", .failure("down"), .failure("still down"))
        let result = await ScoringService.score(job: aiTestJob(), profile: aiTestProfile(),
                                                config: AppConfig(), engine: mock)
        XCTAssertEqual(mock.requests.count, 2)
        XCTAssertEqual(result.score, 0.0)
        XCTAssertTrue(result.reasoning.hasPrefix("AI error:"))
    }
}

// MARK: - Title suggestions

final class TitleSuggestionServiceTests: XCTestCase {
    func testParsesTitlesWithReasonsAtStrongTier() async {
        let mock = MockAIEngine()
        mock.register("Recommend job titles", .text("""
        {"titles": [{"title": "Backend Engineer", "reason": "Matches your Python work"}, \
        {"title": "Platform Engineer", "reason": ""}, "Site Reliability Engineer"]}
        """))
        let result = try? await TitleSuggestionService.suggest(
            profile: aiTestProfile(), preferences: [], config: AppConfig(), engine: mock)
        XCTAssertEqual(result?.map(\.title),
                       ["Backend Engineer", "Platform Engineer", "Site Reliability Engineer"])
        XCTAssertEqual(result?.first?.reason, "Matches your Python work")
        XCTAssertEqual(mock.requests.first?.tier, .strong)
    }

    func testDedupesAndSalvagesEmbeddedJSON() {
        let text = "Sure! {\"titles\": [\"Data Engineer\", \"data engineer\", {\"title\": \"ML Engineer\"}]} hope that helps"
        XCTAssertEqual(TitleSuggestionService.parse(text).map(\.title),
                       ["Data Engineer", "ML Engineer"])
    }

    func testPreferencesBecomeAnswerLinesDroppingBlanks() async {
        let mock = MockAIEngine()
        mock.register("Recommend job titles", .text("{\"titles\": [\"X\"]}"))
        _ = try? await TitleSuggestionService.suggest(
            profile: aiTestProfile(),
            preferences: [TitlePreference(label: "Seniority", value: "Senior"),
                          TitlePreference(label: "Avoid", value: "  ")],
            config: AppConfig(), engine: mock)
        let prompt = mock.requests.first?.user ?? ""
        XCTAssertTrue(prompt.contains("- Seniority: Senior"))
        XCTAssertFalse(prompt.contains("Avoid:"), "blank answers are dropped")
    }

    func testEmptyOnUnparseable() {
        XCTAssertTrue(TitleSuggestionService.parse("no json here").isEmpty)
    }
}

// MARK: - Resume experience selection

final class RoleSelectionTests: XCTestCase {
    private func fiveRoles() -> [WorkExperience] {
        [
            WorkExperience(title: "A", company: "PinCo", startDate: "2013", endDate: "2015", pinned: true),
            WorkExperience(title: "B", company: "NowCo", startDate: "2022", endDate: "Present"),
            WorkExperience(title: "C", company: "MidCo", startDate: "2017", endDate: "2019"),
            WorkExperience(title: "D", company: "LateCo", startDate: "2019", endDate: "2021"),
            WorkExperience(title: "E", company: "OldPin", startDate: "2008", endDate: "2010", pinned: true),
        ]
    }

    func testNoCapReturnsAllWithoutEngineCall() async {
        let mock = MockAIEngine()
        let out = await TailoringService.selectResumeExperiences(
            fiveRoles(), job: aiTestJob(), maxEntries: nil, config: AppConfig(), engine: mock)
        XCTAssertEqual(out.map(\.title), ["A", "B", "C", "D", "E"])
        XCTAssertEqual(mock.requests.count, 0)
    }

    func testPinnedAlwaysIncludedAndSortedPresentFirst() async {
        let mock = MockAIEngine()
        // Unpinned indices: 0=B, 1=C, 2=D. Rank C highest.
        mock.register("ranking past job roles",
                      .text(#"{"scores": [{"index": 1, "score": 95}, {"index": 0, "score": 10}, {"index": 2, "score": 20}]}"#))
        let out = await TailoringService.selectResumeExperiences(
            fiveRoles(), job: aiTestJob(), maxEntries: 3, config: AppConfig(), engine: mock)
        // Pinned A + E always in; C won the single free slot; end-date desc.
        XCTAssertEqual(out.map(\.title), ["C", "A", "E"])
        XCTAssertEqual(mock.requests.count, 1)
        XCTAssertEqual(mock.requests[0].tier, .utility)
        XCTAssertEqual(mock.requests[0].temperature, 0.2)
        XCTAssertEqual(mock.requests[0].maxTokens, 400)
        XCTAssertTrue(mock.requests[0].user.contains("Role 0: B at NowCo"))
        XCTAssertFalse(mock.requests[0].user.contains("PinCo"))
    }

    func testEngineFailureFallsBackToOriginalOrder() async {
        let mock = MockAIEngine() // nothing registered → complete throws
        let out = await TailoringService.selectResumeExperiences(
            fiveRoles(), job: aiTestJob(), maxEntries: 3, config: AppConfig(), engine: mock)
        // Fallback picks first unpinned (B); Present role sorts first.
        XCTAssertEqual(out.map(\.title), ["B", "A", "E"])
    }

    func testPinnedExceedingCapAllKept() async {
        let mock = MockAIEngine()
        var roles = fiveRoles()
        roles[1].pinned = true
        roles[2].pinned = true // 4 pinned, cap 2
        let out = await TailoringService.selectResumeExperiences(
            roles, job: aiTestJob(), maxEntries: 2, config: AppConfig(), engine: mock)
        XCTAssertEqual(out.map(\.title), ["B", "C", "A", "E"])
        XCTAssertEqual(mock.requests.count, 0)
    }

    func testRegexSalvageOfRoleScores() async {
        let mock = MockAIEngine()
        mock.register("ranking past job roles",
                      .text(#"Scores below: "index": 2, "score": 90 then "index": 0, "score": 5"#))
        let out = await TailoringService.selectResumeExperiences(
            fiveRoles(), job: aiTestJob(), maxEntries: 3, config: AppConfig(), engine: mock)
        XCTAssertEqual(out.map(\.title), ["D", "A", "E"])
    }
}

// MARK: - Tailoring

final class TailoringServiceTests: XCTestCase {
    func testTailorResumePromptAssembly() async throws {
        var config = AppConfig()
        config.honesty.level = .embellished
        let mock = MockAIEngine()
        mock.register("expert resume writer", .text("  SUMMARY\nTailored.  "))
        let out = try await TailoringService.tailorResume(
            job: aiTestJob(), profile: aiTestProfile(), config: config, engine: mock)
        XCTAssertEqual(out, "SUMMARY\nTailored.")
        let prompt = mock.requests[0].user
        XCTAssertTrue(prompt.contains("Tailor the resume aggressively."))
        XCTAssertTrue(prompt.contains("Name: Jane Doe"))
        XCTAssertTrue(prompt.contains("Title: iOS Engineer"))
        XCTAssertFalse(prompt.contains("Confidential Referee"))
        XCTAssertEqual(mock.requests[0].tier, .strong)
        XCTAssertEqual(mock.requests[0].temperature, 0.7)
    }

    func testTailorResumeKeywordTargetsAndRetry() async throws {
        let report = #"{"matched_skills": ["Python"], "missing_skills": ["Kotlin"], "keywords": ["cloud"]}"#
        let mock = MockAIEngine()
        mock.register("expert resume writer", .failure("busy"), .text("SUMMARY\nOK"))
        let out = try await TailoringService.tailorResume(
            job: aiTestJob(), profile: aiTestProfile(), config: AppConfig(),
            engine: mock, matchReportJSON: report)
        XCTAssertEqual(out, "SUMMARY\nOK")
        XCTAssertEqual(mock.requests.count, 2)
        XCTAssertEqual(mock.requests[1].temperature, 0.5)
        let prompt = mock.requests[0].user
        XCTAssertTrue(prompt.contains("KEYWORD TARGETS (from ATS gap analysis of this posting):"))
        XCTAssertTrue(prompt.contains("- Candidate HAS these skills the job requires — feature them prominently, using this exact wording: Python"))
        XCTAssertTrue(prompt.contains("- Candidate LACKS these required skills: Kotlin."))
    }

    func testCoverLetterEmptyCompanyFallback() async throws {
        let mock = MockAIEngine()
        mock.register("expert cover letter writer", .text("Dear Hiring Team, ..."))
        _ = try await TailoringService.coverLetter(
            job: aiTestJob(company: ""), profile: aiTestProfile(),
            config: AppConfig(), engine: mock)
        let prompt = mock.requests[0].user
        XCTAssertTrue(prompt.contains("Address the letter to the hiring team at the company"))
        XCTAssertTrue(prompt.contains("TONE: Write in a formal, corporate tone."))
    }

    func testReviseResumePerCallTierAndHonesty() async throws {
        let mock = MockAIEngine()
        mock.register("expert resume editor", .text("SUMMARY\nRevised"))
        let out = try await TailoringService.reviseResume(
            currentResume: "SUMMARY\nOld", instructions: "punchier bullets",
            job: aiTestJob(), profile: aiTestProfile(), config: AppConfig(),
            engine: mock, tier: .fast, honestyLevel: .fabricated)
        XCTAssertEqual(out, "SUMMARY\nRevised")
        XCTAssertEqual(mock.requests[0].tier, .fast)
        XCTAssertEqual(mock.requests[0].temperature, 0.5)
        let prompt = mock.requests[0].user
        XCTAssertTrue(prompt.contains("Apply the user's instruction aggressively."))
        XCTAssertTrue(prompt.contains("punchier bullets"))
        XCTAssertTrue(prompt.contains("SUMMARY\nOld"))
    }

    func testReviseCoverLetterIncludesTone() async throws {
        var config = AppConfig()
        config.honesty.coverLetterTone = .enthusiastic
        let mock = MockAIEngine()
        mock.register("expert cover letter editor", .text("Dear Team, revised."))
        _ = try await TailoringService.reviseCoverLetter(
            currentLetter: "Dear Team, old.", instructions: "more energy",
            job: aiTestJob(), profile: aiTestProfile(), config: config, engine: mock)
        let prompt = mock.requests[0].user
        XCTAssertTrue(prompt.contains("TONE: Show genuine excitement and energy"))
        XCTAssertTrue(prompt.contains("Dear Team, old."))
        XCTAssertEqual(mock.requests[0].tier, .strong) // default aiEditTier
    }

    func testEmbellishmentLogFabricatedWarning() async {
        let mock = MockAIEngine()
        mock.register("Compare the original profile data", .text(
            #"{"resume_changes": [{"field": "skills", "original": "a", "modified": "b"}, {"field": "bad"}], "cover_letter_changes": []}"#))
        let log = await TailoringService.embellishmentLog(
            profile: aiTestProfile(), resumeText: "SUMMARY\nR", coverLetterText: "Dear",
            honestyLevel: .fabricated, config: AppConfig(), engine: mock)
        XCTAssertEqual(log.honestyLevel, "fabricated")
        XCTAssertEqual(log.resumeChanges,
                       [EmbellishmentChange(field: "skills", original: "a", modified: "b")])
        XCTAssertTrue(log.coverLetterChanges.isEmpty)
        XCTAssertEqual(log.warning, "This application contains fabricated content. Review before interviews.")
        XCTAssertEqual(mock.requests[0].temperature, 0.2)
        XCTAssertFalse(log.generatedAt.isEmpty)
    }

    func testEmbellishmentLogAlwaysValidOnBadOutput() async {
        let mock = MockAIEngine()
        mock.register("Compare the original profile data", .text("not json at all"))
        let log = await TailoringService.embellishmentLog(
            profile: aiTestProfile(), resumeText: "R", coverLetterText: "C",
            honestyLevel: .honest, config: AppConfig(), engine: mock)
        XCTAssertEqual(log.honestyLevel, "honest")
        XCTAssertTrue(log.resumeChanges.isEmpty)
        XCTAssertTrue(log.coverLetterChanges.isEmpty)
        XCTAssertNil(log.warning)
    }

    func testCustomAnswers() async {
        let mock = MockAIEngine()
        mock.register("custom application questions", .text(#"{"Why us?": "Because Swift."}"#))
        let answers = await TailoringService.customAnswers(
            job: aiTestJob(), profile: aiTestProfile(), questions: ["Why us?"],
            config: AppConfig(), engine: mock)
        XCTAssertEqual(answers["Why us?"], "Because Swift.")
        XCTAssertTrue(mock.requests[0].user.contains("- Why us?"))
    }

    func testCustomAnswersFallbackToEmpty() async {
        let mock = MockAIEngine()
        mock.register("custom application questions", .text("cannot help"))
        let answers = await TailoringService.customAnswers(
            job: aiTestJob(), profile: aiTestProfile(), questions: ["Q1?", "Q2?"],
            config: AppConfig(), engine: mock)
        XCTAssertEqual(answers, ["Q1?": "", "Q2?": ""])
    }
}

// MARK: - Connection test

final class ConnectionTests: XCTestCase {
    func testConnectionProbe() async {
        let mock = MockAIEngine()
        mock.setModels(["llama-3.1-8b", "qwen2.5"])
        let ok = await mock.testConnection(config: AIConfig())
        XCTAssertTrue(ok.connected)
        XCTAssertEqual(ok.models, ["llama-3.1-8b", "qwen2.5"])

        mock.setModels([], error: "refused")
        let bad = await mock.testConnection(config: AIConfig())
        XCTAssertFalse(bad.connected)
        XCTAssertNotNil(bad.error)
    }
}
