import XCTest
import JobsmithKit

// Shared fixtures for the Apply layer tests.
enum ApplyFixtures {
    static func profile() -> Profile {
        Profile(
            fullName: "Jane Q Doe",
            email: "jane@example.com",
            phone: "+1 555-123-4567",
            location: "Austin, TX",
            streetAddress: "123 Main St",
            city: "",
            state: "",
            zipCode: "78701",
            linkedin: "https://linkedin.com/in/jane",
            github: "https://github.com/jane",
            portfolio: "https://jane.dev",
            desiredSalary: "$150,000",
            workAuthorization: "Yes",
            sponsorshipRequired: "No",
            availableStart: "Immediately",
            noticePeriod: "2 weeks",
            summary: "Engineer who ships.",
            skills: ["Swift", "Python"],
            experience: [WorkExperience(title: "Senior Engineer", company: "Acme",
                                        startDate: "2020-01", endDate: "Present",
                                        bullets: ["Did X", "Did Y"])],
            education: [Education(degree: "BS Computer Science",
                                  school: "State University", year: "2015")],
            certifications: []
        )
    }

    static func config() -> AppConfig {
        AppConfig(profile: profile())
    }
}

// MARK: - Answer bank scoring

final class AnswerBankMatcherTests: XCTestCase {
    private var db: AppDatabase!
    private var store: AnswerBankStore!
    private var matcher: AnswerBankMatcher!

    override func setUpWithError() throws {
        db = try AppDatabase.inMemory()
        store = AnswerBankStore(db)
        matcher = AnswerBankMatcher(store: store)
    }

    func testExactPhraseScores100() throws {
        try store.upsert(AnswerBankEntry(key: "tell_us_about_yourself", label: "",
                                         keywords: [], value: "I am Jane."))
        let result = matcher.scoreQuestion("Please tell us about yourself")
        XCTAssertEqual(result.matchedKey, "tell_us_about_yourself")
        XCTAssertEqual(result.score, 100)
        XCTAssertEqual(result.value, "I am Jane.")
        XCTAssertEqual(matcher.findBestMatch(question: "Please tell us about yourself")?.value,
                       "I am Jane.")
    }

    func testAllKeywordsScore80() throws {
        // All 5 salary_expectation keywords, no exact phrase.
        let q = "range of pay you expected as compensation salary"
        let result = matcher.scoreQuestion(q)
        XCTAssertEqual(result.matchedKey, "salary_expectation")
        XCTAssertEqual(result.score, 80)
    }

    func testSixtyPercentKeywordsScore60() throws {
        // 4 of 6 why_this_role keywords (why, interest, role, position).
        let q = "why does this position and role interest you"
        let result = matcher.scoreQuestion(q)
        XCTAssertEqual(result.matchedKey, "why_this_role")
        XCTAssertEqual(result.score, 60)
    }

    func testBelowThresholdReturnsNoMatch() throws {
        try store.upsert(AnswerBankEntry(key: "why_this_role", label: "",
                                         keywords: [], value: "Because reasons."))
        let result = matcher.scoreQuestion("describe the role")
        XCTAssertNil(result.matchedKey)
        XCTAssertEqual(result.score, 40)
        XCTAssertNil(matcher.findBestMatch(question: "describe the role"))
    }

    func testPlaceholderValueIsNeverReturned() throws {
        try matcher.seedIfEmpty()
        XCTAssertNil(matcher.findBestMatch(question: "tell us about yourself"))
        let result = matcher.scoreQuestion("tell us about yourself")
        XCTAssertEqual(result.matchedKey, "tell_us_about_yourself")
        XCTAssertEqual(result.score, 100)
        XCTAssertNil(result.value)
    }

    func testCustomEntryKeywordScoring() throws {
        try store.upsert(AnswerBankEntry(key: "remote_ok", label: "Remote?",
                                         keywords: ["remote", "work from home"],
                                         value: "Yes, fully remote."))
        let match = matcher.findBestMatch(
            question: "Are you comfortable with remote work from home?")
        XCTAssertEqual(match?.key, "remote_ok")
        XCTAssertEqual(match?.value, "Yes, fully remote.")
        XCTAssertEqual(match?.score, 80)
    }

    func testSeedIfEmptyInsertsNinePlaceholdersOnce() throws {
        try matcher.seedIfEmpty()
        XCTAssertEqual(try store.all().count, 9)
        // A user-set value survives a second seeding pass.
        try store.upsert(AnswerBankEntry(key: "tell_us_about_yourself", label: "",
                                         keywords: [], value: "Real answer"))
        try matcher.seedIfEmpty()
        XCTAssertEqual(try store.all().count, 9)
        let row = try store.all().first { $0.key == "tell_us_about_yourself" }
        XCTAssertEqual(row?.value, "Real answer")
        // allSnippets keeps built-in placeholders but hides them from matching.
        XCTAssertEqual(matcher.allSnippets().count, 9)
    }
}

// MARK: - Deterministic profile matcher

final class ProfileFieldMatcherTests: XCTestCase {
    private let profile = ApplyFixtures.profile()

    private func match(_ field: FieldDescriptor) -> FieldValue? {
        ProfileFieldMatcher.matchProfileFields(profile: profile,
                                               fields: [field])[field.fieldId]
    }

    func testEmailByLabel() {
        let fv = match(FieldDescriptor(fieldId: "e", label: "Email Address", fieldType: "email"))
        XCTAssertEqual(fv?.value, "jane@example.com")
        XCTAssertEqual(fv?.action, "fill")
        XCTAssertEqual(fv?.source, "profile")
        XCTAssertEqual(fv?.confidence, 0.95)
    }

    func testAutocompleteBeatsLabel() {
        let fv = match(FieldDescriptor(fieldId: "x", label: "User ID",
                                       fieldType: "text", autocomplete: "email"))
        XCTAssertEqual(fv?.value, "jane@example.com")
    }

    func testFirstAndLastName() {
        XCTAssertEqual(match(FieldDescriptor(fieldId: "f", label: "First Name"))?.value, "Jane")
        XCTAssertEqual(match(FieldDescriptor(fieldId: "l", label: "Last Name"))?.value, "Doe")
        XCTAssertEqual(match(FieldDescriptor(fieldId: "n", label: "Full Name"))?.value, "Jane Q Doe")
    }

    func testFullNameNegativeVeto() {
        XCTAssertNil(match(FieldDescriptor(fieldId: "ec", label: "Emergency Contact Name")))
    }

    func testPhoneAndExtensionVeto() {
        XCTAssertEqual(match(FieldDescriptor(fieldId: "p", label: "Mobile Phone",
                                             fieldType: "tel"))?.value, "+1 555-123-4567")
        XCTAssertNil(match(FieldDescriptor(fieldId: "px", label: "Phone Extension")))
    }

    func testStreetAddressAndZip() {
        XCTAssertEqual(match(FieldDescriptor(fieldId: "a", label: "Street Address"))?.value,
                       "123 Main St")
        XCTAssertEqual(match(FieldDescriptor(fieldId: "z", label: "Zip / Postal Code"))?.value,
                       "78701")
    }

    func testCityAndStateFallBackToLocation() {
        XCTAssertEqual(match(FieldDescriptor(fieldId: "c", label: "City"))?.value, "Austin")
        // State select resolves "TX" to the option text via abbreviation expansion.
        let fv = match(FieldDescriptor(fieldId: "s", label: "State", fieldType: "select",
                                       options: ["Texas", "California"]))
        XCTAssertEqual(fv?.value, "Texas")
        XCTAssertEqual(fv?.action, "select")
    }

    func testCountrySelectAliasExpansion() {
        let fv = match(FieldDescriptor(fieldId: "co", label: "Country", fieldType: "select",
                                       options: ["United States of America", "Canada"]))
        XCTAssertEqual(fv?.value, "United States of America")
    }

    func testLinks() {
        XCTAssertEqual(match(FieldDescriptor(fieldId: "li", label: "LinkedIn Profile"))?.value,
                       "https://linkedin.com/in/jane")
        XCTAssertEqual(match(FieldDescriptor(fieldId: "gh", label: "GitHub URL"))?.value,
                       "https://github.com/jane")
    }

    func testSalaryAndStartDate() {
        XCTAssertEqual(match(FieldDescriptor(fieldId: "sal", label: "Desired Salary"))?.value,
                       "$150,000")
        XCTAssertEqual(match(FieldDescriptor(fieldId: "st", label: "When can you start?"))?.value,
                       "Immediately")
    }

    func testWorkAuthAndSponsorshipSelects() {
        let auth = match(FieldDescriptor(
            fieldId: "wa", label: "Are you legally authorized to work in the United States?",
            fieldType: "select", options: ["Yes", "No"]))
        XCTAssertEqual(auth?.value, "Yes")
        XCTAssertEqual(auth?.action, "select")

        let sponsor = match(FieldDescriptor(
            fieldId: "sp", label: "Will you now or in the future require sponsorship?",
            fieldType: "radio", options: ["Yes", "No"]))
        XCTAssertEqual(sponsor?.value, "No")
    }

    func testEEOFallsBackToDeclineOption() {
        // The shared fixture leaves the EEO fields empty — every one must fall
        // back to the decline option (or "Prefer not to answer" for free text).
        let gender = match(FieldDescriptor(fieldId: "g", label: "Gender", fieldType: "select",
                                           options: ["Male", "Female", "Prefer not to say"]))
        XCTAssertEqual(gender?.value, "Prefer not to say")

        let race = match(FieldDescriptor(fieldId: "r", label: "Race/Ethnicity", fieldType: "text"))
        XCTAssertEqual(race?.value, "Prefer not to answer")
    }

    func testEEOFromPopulatedProfile() {
        let p = Profile(gender: "Female", raceEthnicity: "Two or more races",
                        veteranStatus: "I am not a veteran", disabilityStatus: "No")
        func m(_ f: FieldDescriptor) -> FieldValue? {
            ProfileFieldMatcher.matchProfileFields(profile: p, fields: [f])[f.fieldId]
        }
        // Gender select resolves the profile value to the matching option.
        XCTAssertEqual(m(FieldDescriptor(fieldId: "g", label: "Gender", fieldType: "select",
                                         options: ["Male", "Female", "Prefer not to say"]))?.value,
                       "Female")
        // A veteran sentence maps to the closest real option.
        XCTAssertEqual(m(FieldDescriptor(fieldId: "v", label: "Veteran status", fieldType: "select",
                                         options: ["I am a protected veteran",
                                                   "I am not a protected veteran"]))?.value,
                       "I am not a protected veteran")
        // Race as free text fills the profile value verbatim (no decline fallback).
        XCTAssertEqual(m(FieldDescriptor(fieldId: "r", label: "Race/Ethnicity",
                                         fieldType: "text"))?.value, "Two or more races")
        // Disability yes/no select.
        XCTAssertEqual(m(FieldDescriptor(fieldId: "d", label: "Do you have a disability?",
                                         fieldType: "select",
                                         options: ["Yes", "No", "Prefer not to answer"]))?.value, "No")
    }

    func testMiddleNameAndAddressLine2FromProfile() {
        let p = Profile(middleName: "Quinn", streetAddress2: "Apt 5B")
        func m(_ f: FieldDescriptor) -> FieldValue? {
            ProfileFieldMatcher.matchProfileFields(profile: p, fields: [f])[f.fieldId]
        }
        XCTAssertEqual(m(FieldDescriptor(fieldId: "mn", label: "Middle Name"))?.value, "Quinn")
        XCTAssertEqual(m(FieldDescriptor(fieldId: "a2", label: "Apartment / Suite"))?.value, "Apt 5B")
        // Empty middle name / line 2 must leave the field for the LLM, not fill blank.
        let empty = ApplyFixtures.profile()
        XCTAssertNil(ProfileFieldMatcher.matchProfileFields(
            profile: empty,
            fields: [FieldDescriptor(fieldId: "mn", label: "Middle Name")])["mn"])
    }

    func testEducationStartDateGuardBlocksAvailabilityMisfill() {
        // A Workday-style education start date must NOT be answered with
        // available_start ("Immediately") — the empty guard sends it to the LLM.
        XCTAssertNil(match(FieldDescriptor(fieldId: "education-1--startDate",
                                           label: "Start Date", extraContext: "Education")))
    }

    func testAgreeTermsCheckbox() {
        let fv = match(FieldDescriptor(fieldId: "t",
                                       label: "I agree to the terms and conditions",
                                       fieldType: "checkbox"))
        XCTAssertEqual(fv?.value, "Yes")
        XCTAssertEqual(fv?.action, "check")
        XCTAssertEqual(fv?.confidence, 0.55)
    }

    func testSkillsTextarea() {
        XCTAssertEqual(match(FieldDescriptor(fieldId: "sk", label: "Skills",
                                             fieldType: "textarea"))?.value, "Swift, Python")
    }

    func testDegreeResolvesToEducationLevelBucket() {
        let fv = match(FieldDescriptor(fieldId: "d", label: "Highest education level",
                                       fieldType: "select",
                                       options: ["High School", "Bachelor's Degree", "Master's Degree"]))
        XCTAssertEqual(fv?.value, "Bachelor's Degree")
    }

    func testYearsOfExperience() {
        let fv = match(FieldDescriptor(fieldId: "y",
                                       label: "How many years of experience do you have?"))
        XCTAssertNotNil(fv)
        XCTAssertGreaterThanOrEqual(Int(fv?.value ?? "") ?? -1, 5)
        // Skill-specific years must be left to the LLM.
        XCTAssertNil(match(FieldDescriptor(fieldId: "yp",
                                           label: "How many years of experience with Python?")))
    }

    func testCurrentCompanyAndTitle() {
        XCTAssertEqual(match(FieldDescriptor(fieldId: "cc", label: "Current Company"))?.value,
                       "Acme")
        XCTAssertEqual(match(FieldDescriptor(fieldId: "ct", label: "Current Job Title"))?.value,
                       "Senior Engineer")
    }
}

// MARK: - FieldMapper pipeline

final class FieldMapperTests: XCTestCase {
    private var db: AppDatabase!
    private var store: AnswerBankStore!
    private var engine: MockAIEngine!
    private var mapper: FieldMapper!
    private let config = ApplyFixtures.config()
    private let job = ApplyJobContext(jobId: "j1", title: "iOS Engineer", company: "Acme",
                                      url: "https://acme.dev/jobs/1",
                                      description: "Build the app.")

    override func setUpWithError() throws {
        db = try AppDatabase.inMemory()
        store = AnswerBankStore(db)
        engine = MockAIEngine()
        mapper = FieldMapper(engine: engine, bank: AnswerBankMatcher(store: store))
    }

    func testFileInputPhase() async throws {
        let fields = [
            FieldDescriptor(fieldId: "cl", label: "Attach Cover Letter", fieldType: "file"),
            FieldDescriptor(fieldId: "resume", fieldType: "file"),
            FieldDescriptor(fieldId: "", fieldType: "file"),  // bare → resume
        ]
        let out = await mapper.map(fields: fields, profile: ApplyFixtures.profile(),
                                   job: job, config: config)
        XCTAssertEqual(out.map(\.value), ["cover_letter", "resume", "resume"])
        XCTAssertEqual(Set(out.map(\.action)), ["upload"])
        XCTAssertEqual(Set(out.map(\.source)), ["profile"])
        XCTAssertTrue(engine.requests.isEmpty)
    }

    func testFileInputsNeverReachTheLLM() async throws {
        let fields = [
            // Workday shape: generated field_id, no label/name — the old
            // keyword pass missed it, fell to the LLM, and came back "skip".
            FieldDescriptor(fieldId: "field_23", label: "Select files", fieldType: "file"),
            // Drop-zone text only reaches us via extra_context.
            FieldDescriptor(fieldId: "field_9", fieldType: "file",
                            extraContext: "Upload your Cover Letter here"),
            // Clearly not a resume slot — deterministic skip, still no LLM.
            FieldDescriptor(fieldId: "field_4", label: "Profile photo", fieldType: "file"),
        ]
        let out = await mapper.map(fields: fields, profile: ApplyFixtures.profile(),
                                   job: job, config: config)
        XCTAssertEqual(out.map(\.value), ["resume", "cover_letter", ""])
        XCTAssertEqual(out.map(\.action), ["upload", "upload", "skip"])
        XCTAssertTrue(engine.requests.isEmpty, "file inputs must never reach the LLM")
    }

    func testPasswordFieldsNeverReachTheLLM() async throws {
        let fields = [
            FieldDescriptor(fieldId: "pw", label: "Create Password", fieldType: "password"),
            FieldDescriptor(fieldId: "pw2", label: "Confirm", fieldType: "password",
                            autocomplete: "new-password"),
        ]
        let out = await mapper.map(fields: fields, profile: ApplyFixtures.profile(),
                                   job: job, config: config)
        XCTAssertEqual(out.map(\.action), ["skip", "skip"])
        XCTAssertEqual(out.map(\.source), ["skip", "skip"])
        XCTAssertTrue(engine.requests.isEmpty, "password fields must never reach the LLM")
    }

    func testAnswerBankShortCircuitSkipsLLM() async throws {
        try store.upsert(AnswerBankEntry(key: "tell_us_about_yourself", label: "",
                                         keywords: [], value: "I am Jane."))
        try store.upsert(AnswerBankEntry(key: "why_this_role", label: "",
                                         keywords: [], value: "I love the mission."))
        let fields = [
            FieldDescriptor(fieldId: "q1", label: "Tell us about yourself",
                            fieldType: "textarea"),
            FieldDescriptor(fieldId: "q2", label: "Why do you want to work here?",
                            fieldType: "textarea"),
        ]
        let out = await mapper.map(fields: fields, profile: ApplyFixtures.profile(),
                                   job: job, config: config)
        XCTAssertEqual(out.map(\.value), ["I am Jane.", "I love the mission."])
        XCTAssertEqual(Set(out.map(\.source)), ["answer_bank"])
        XCTAssertEqual(Set(out.map(\.confidence)), [1.0])
        XCTAssertEqual(engine.requests.count, 0,
                       "no LLM call when the bank resolves everything")
    }

    func testPlaceholderBankValueFallsThroughToLLM() async throws {
        try AnswerBankMatcher(store: store).seedIfEmpty()  // placeholders only
        engine.register("Return the JSON array now.", .text(
            #"[{"field_id": "q1", "value": "Hi there", "action": "fill", "confidence": 0.8, "source": "llm_generated"}]"#))
        let fields = [FieldDescriptor(fieldId: "q1", label: "Tell us about yourself",
                                      fieldType: "textarea")]
        let out = await mapper.map(fields: fields, profile: ApplyFixtures.profile(),
                                   job: job, config: config)
        XCTAssertEqual(out.map(\.source), ["llm_generated"])
        XCTAssertEqual(engine.requests.count, 1)
        // The prompt uses the auto_apply_field_map system template.
        XCTAssertTrue(engine.requests[0].system?.contains("map each field") == true)
        XCTAssertTrue(engine.requests[0].user.contains("CANDIDATE PROFILE:"))
        XCTAssertTrue(engine.requests[0].user.contains("Name: Jane Q Doe"))
        XCTAssertTrue(engine.requests[0].user.hasSuffix("Return the JSON array now."))
    }

    func testFullPipelineMalformedItemsGapFillAndOrder() async throws {
        try store.upsert(AnswerBankEntry(key: "why_this_role", label: "",
                                         keywords: [], value: "I love the mission."))
        // The LLM answers q1, sends a malformed q2 (non-string value), emits
        // junk, and omits q3 entirely — wrapped in a fenced block with prose.
        engine.register("Return the JSON array now.", .text("""
        Here you go:
        ```json
        [
          {"field_id": "q1", "value": "Reading", "action": "fill", "confidence": 0.7, "source": "llm_generated"},
          {"field_id": "q2", "value": 42},
          "garbage"
        ]
        ```
        Hope that helps!
        """))
        let fields = [
            FieldDescriptor(fieldId: "e1", label: "Email", fieldType: "email"),
            FieldDescriptor(fieldId: "q1", label: "Describe your favorite hobby",
                            fieldType: "textarea"),
            FieldDescriptor(fieldId: "b1", label: "Why do you want to work here?",
                            fieldType: "textarea"),
            FieldDescriptor(fieldId: "q2", label: "What is your spirit animal"),
            FieldDescriptor(fieldId: "q3", label: "What is your favorite color"),
        ]
        let out = await mapper.map(fields: fields, profile: ApplyFixtures.profile(),
                                   job: job, config: config)
        // Phase 3 must preserve the original field order.
        XCTAssertEqual(out.map(\.fieldId), ["e1", "q1", "b1", "q2", "q3"])
        XCTAssertEqual(out.map(\.source),
                       ["profile", "llm_generated", "answer_bank", "skip", "skip"])
        XCTAssertEqual(out[0].value, "jane@example.com")
        XCTAssertEqual(out[1].value, "Reading")
        XCTAssertEqual(out[2].value, "I love the mission.")
        XCTAssertEqual(out[3].action, "skip")  // malformed → gap-filled
        XCTAssertEqual(out[4].action, "skip")  // omitted → gap-filled
        XCTAssertEqual(engine.requests.count, 1)
    }

    func testLLMFailureGapFillsAsSkip() async throws {
        engine.register("Return the JSON array now.",
                        .failure("boom"), .failure("boom"), .failure("boom"))
        let fields = [FieldDescriptor(fieldId: "q1", label: "What is your spirit animal")]
        let out = await mapper.map(fields: fields, profile: ApplyFixtures.profile(),
                                   job: job, config: config)
        XCTAssertEqual(out.map(\.action), ["skip"])
        XCTAssertEqual(out.map(\.source), ["skip"])
    }

    func testExtractJSONVariants() {
        // Plain array.
        XCTAssertNotNil(FieldMapper.extractJSON(#"[{"a": 1}]"#) as? [Any])
        // Fenced with prose around it.
        let fenced = "Sure!\n```json\n[{\"a\": 1}]\n```\nExplanation."
        XCTAssertEqual((FieldMapper.extractJSON(fenced) as? [Any])?.count, 1)
        // Leading + trailing prose without fences.
        let prose = "The answer is: [{\"a\": \"x]y\"}, {\"b\": 2}] as requested."
        XCTAssertEqual((FieldMapper.extractJSON(prose) as? [Any])?.count, 2)
        // Python-style single quotes.
        XCTAssertEqual((FieldMapper.extractJSON("[{'a': True}]") as? [Any])?.count, 1)
        // Garbage.
        XCTAssertNil(FieldMapper.extractJSON("no json here"))
    }

    func testWireFormatSnakeCaseCodingKeys() throws {
        let descriptorJSON = """
        {"field_id": "f1", "label": "Email", "field_type": "email",
         "extra_context": "Contact", "autocomplete": "email", "required": true}
        """
        let f = try JSONDecoder().decode(FieldDescriptor.self,
                                         from: Data(descriptorJSON.utf8))
        XCTAssertEqual(f.fieldId, "f1")
        XCTAssertEqual(f.fieldType, "email")
        XCTAssertEqual(f.extraContext, "Contact")
        XCTAssertTrue(f.required)
        XCTAssertNil(f.options)

        let value = FieldValue(fieldId: "f1", value: "x", action: "fill",
                               confidence: 0.5, source: "profile")
        let encoded = try JSONSerialization.jsonObject(
            with: JSONEncoder().encode(value)) as? [String: Any]
        XCTAssertEqual(encoded?["field_id"] as? String, "f1")
        XCTAssertEqual(Set(encoded?.keys.map { $0 } ?? []),
                       ["field_id", "value", "action", "confidence", "source"])

        // FieldValue decode fills Python defaults for omitted keys.
        let minimal = try JSONDecoder().decode(
            FieldValue.self, from: Data(#"{"field_id": "f2", "value": "y"}"#.utf8))
        XCTAssertEqual(minimal.action, "fill")
        XCTAssertEqual(minimal.confidence, 1.0)
        XCTAssertEqual(minimal.source, "profile")
    }
}
