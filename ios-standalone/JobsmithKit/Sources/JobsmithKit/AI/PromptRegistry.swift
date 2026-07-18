import Foundation

/// Central registry of every internal LLM prompt, ported verbatim from the
/// desktop `prompt_registry.py`. Overrides come from AppConfig.promptOverrides
/// and win over the defaults; rendering substitutes only known lowercase
/// `{placeholder}` names so JSON braces in prompt bodies never need escaping.
public enum PromptRegistry {
    public static var templateIds: [String] { orderedIds }

    /// The built-in default template for an id, or nil for unknown ids.
    public static func defaultTemplate(_ id: String) -> String? {
        defaults[id]
    }

    /// The user's override when set and non-blank, else the default.
    public static func template(_ id: String, config: AppConfig) -> String {
        if let override = config.promptOverrides[id],
           !override.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return override
        }
        return defaults[id] ?? ""
    }

    public static func render(_ id: String, _ vars: [String: String], config: AppConfig) -> String {
        render(template: template(id, config: config), vars)
    }

    // MARK: - UI-facing metadata

    /// A documented placeholder: the `{name}` token plus a one-line note on
    /// what the code supplies for it at render time.
    public struct PromptVariable: Sendable, Hashable {
        public let name: String
        public let doc: String
        public init(_ name: String, _ doc: String) {
            self.name = name
            self.doc = doc
        }
    }

    /// UI metadata for a prompt, ported from the desktop `prompt_registry.py`
    /// PROMPTS dict. `variables` preserves the Python ordered-dict order so
    /// placeholder chips read in a stable, meaningful sequence.
    public struct PromptInfo: Sendable, Identifiable {
        public let id: String
        public let label: String
        public let group: String
        public let description: String
        public let variables: [PromptVariable]
        public init(id: String, label: String, group: String,
                    description: String, variables: [PromptVariable]) {
            self.id = id
            self.label = label
            self.group = group
            self.description = description
            self.variables = variables
        }
    }

    /// Metadata for an id, or nil for an unknown id.
    public static func info(_ id: String) -> PromptInfo? {
        infoById[id]
    }

    private static let infoById: [String: PromptInfo] =
        Dictionary(uniqueKeysWithValues: orderedInfos.map { ($0.id, $0) })

    /// Every prompt's metadata, in the same order as `orderedIds`.
    public static let orderedInfos: [PromptInfo] = [
        // ── Scoring & suggestions ────────────────────────────────────────
        PromptInfo(
            id: "score_job_fit",
            label: "Job Fit Scoring",
            group: "Scoring & Suggestions",
            description: "Scores how well a job matches your profile (0-100) and builds the matched/missing skill breakdown shown on job cards.",
            variables: [
                PromptVariable("job_title", "Job posting title"),
                PromptVariable("job_company", "Company name"),
                PromptVariable("job_description", "Job description (truncated to 3000 chars)"),
                PromptVariable("profile_summary", "Your structured profile (name, summary, skills, experience, education)"),
            ]),
        PromptInfo(
            id: "select_resume_experiences",
            label: "Resume Experience Selection",
            group: "Scoring & Suggestions",
            description: "Ranks your past roles by relevance to a job when the profile has more roles than the resume entry cap allows.",
            variables: [
                PromptVariable("job_title", "Job posting title"),
                PromptVariable("job_company", "Company name"),
                PromptVariable("job_description", "Job description (truncated to 3000 chars)"),
                PromptVariable("role_lines", "Numbered list of your unpinned roles with highlights"),
            ]),
        PromptInfo(
            id: "suggest_job_titles",
            label: "Job Title Suggestions",
            group: "Scoring & Suggestions",
            description: "Recommends job titles to search for, based on your profile and the direction questions in the setup wizard.",
            variables: [
                PromptVariable("answer_lines", "Your answers to the direction questions"),
                PromptVariable("profile_summary", "Your structured profile"),
            ]),
        PromptInfo(
            id: "suggest_companies",
            label: "Company Suggestions",
            group: "Scoring & Suggestions",
            description: "Recommends companies to add to your ATS watchlists, based on your profile, keywords, and liked companies.",
            variables: [
                // `directions` is present in the iOS default template (a
                // CANDIDATE PREFERENCES block) though the desktop registry
                // omits it — declared here so it reads as a known placeholder.
                PromptVariable("directions", "Your answers to the direction questions (stated preferences)"),
                PromptVariable("profile_summary", "Your structured profile"),
                PromptVariable("keywords", "Your saved search keywords"),
                PromptVariable("liked", "Companies that scored well for you recently"),
                PromptVariable("excluded", "Companies already watched or already suggested"),
            ]),
        PromptInfo(
            id: "classify_job_role",
            label: "Salary Role Classification",
            group: "Scoring & Suggestions",
            description: "Canonicalizes a job title and picks the closest SOC occupation code for the salary estimator.",
            variables: [
                PromptVariable("soc_hints", "List of common SOC code examples"),
                PromptVariable("job_title", "Job posting title"),
                PromptVariable("job_description", "Job description (truncated to 1500 chars)"),
            ]),

        // ── Documents ────────────────────────────────────────────────────
        PromptInfo(
            id: "tailor_resume",
            label: "Tailored Resume",
            group: "Documents",
            description: "Generates the tailored resume for a job. Careful with the OUTPUT FORMAT rules — the document parser depends on them.",
            variables: [
                PromptVariable("honesty_instruction", "The tailoring directive for your current honesty level"),
                PromptVariable("keyword_targets", "ATS keyword-targeting block from the fit score (may be empty)"),
                PromptVariable("job_title", "Job posting title"),
                PromptVariable("job_company", "Company name"),
                PromptVariable("job_description", "Job description (truncated to 5000 chars)"),
                PromptVariable("profile_summary", "Your structured profile (selected roles only)"),
            ]),
        PromptInfo(
            id: "cover_letter",
            label: "Cover Letter",
            group: "Documents",
            description: "Generates the tailored cover letter for a job.",
            variables: [
                PromptVariable("honesty_instruction", "The tailoring directive for your current honesty level"),
                PromptVariable("tone_instruction", "The tone directive for your chosen cover letter tone"),
                PromptVariable("job_title", "Job posting title"),
                PromptVariable("job_company", "Company name"),
                PromptVariable("job_description", "Job description (truncated to 5000 chars)"),
                PromptVariable("profile_summary", "Your structured profile (selected roles only)"),
            ]),
        PromptInfo(
            id: "revise_resume",
            label: "Resume Revision (AI Edit)",
            group: "Documents",
            description: "Applies your revision instructions to an already-tailored resume as a scoped edit.",
            variables: [
                PromptVariable("honesty_instruction", "The tailoring directive for your current honesty level"),
                PromptVariable("fabrication_guard", "Honesty-aware guard limiting what edits may invent"),
                PromptVariable("profile_summary", "Your structured profile (selected roles only)"),
                PromptVariable("job_title", "Job posting title"),
                PromptVariable("job_company", "Company name"),
                PromptVariable("job_description", "Job description (truncated to 5000 chars)"),
                PromptVariable("user_instructions", "The revision instructions you typed"),
                PromptVariable("current_resume", "The current tailored resume text"),
            ]),
        PromptInfo(
            id: "revise_cover_letter",
            label: "Cover Letter Revision (AI Edit)",
            group: "Documents",
            description: "Applies your revision instructions to an already-generated cover letter as a scoped edit.",
            variables: [
                PromptVariable("honesty_instruction", "The tailoring directive for your current honesty level"),
                PromptVariable("tone_instruction", "The tone directive for your chosen cover letter tone"),
                PromptVariable("fabrication_guard", "Honesty-aware guard limiting what edits may invent"),
                PromptVariable("profile_summary", "Your structured profile (selected roles only)"),
                PromptVariable("job_title", "Job posting title"),
                PromptVariable("job_company", "Company name"),
                PromptVariable("job_description", "Job description (truncated to 5000 chars)"),
                PromptVariable("user_instructions", "The revision instructions you typed"),
                PromptVariable("current_letter", "The current cover letter text"),
            ]),
        PromptInfo(
            id: "embellishment_log",
            label: "Embellishment Log",
            group: "Documents",
            description: "Diffs the generated resume and cover letter against your real profile to log every embellishment.",
            variables: [
                PromptVariable("profile_summary", "Your structured profile"),
                PromptVariable("resume_text", "The generated resume (truncated to 3000 chars)"),
                PromptVariable("cover_letter_text", "The generated cover letter (truncated to 2000 chars)"),
            ]),

        // ── Profile import ───────────────────────────────────────────────
        PromptInfo(
            id: "resume_parse",
            label: "Résumé Parser",
            group: "Profile Import",
            description: "Extracts your profile fields from an uploaded or pasted résumé during onboarding.",
            variables: [
                PromptVariable("resume", "The raw résumé text"),
            ]),
        PromptInfo(
            id: "linkedin_import",
            label: "LinkedIn Profile Import",
            group: "Profile Import",
            description: "Extracts your profile fields from the scraped text of your own LinkedIn profile pages.",
            variables: [
                PromptVariable("resume", "The scraped LinkedIn profile text"),
            ]),

        // ── Applying ─────────────────────────────────────────────────────
        PromptInfo(
            id: "custom_answers",
            label: "Application Question Answers",
            group: "Applying",
            description: "Answers custom application-form questions (Greenhouse/Lever) from your profile.",
            variables: [
                PromptVariable("job_title", "Job posting title"),
                PromptVariable("job_company", "Company name"),
                PromptVariable("profile_summary", "Your structured profile"),
                PromptVariable("questions", "The list of questions from the application form"),
            ]),
        PromptInfo(
            id: "auto_apply_field_map",
            label: "Auto-Apply Field Mapping",
            group: "Applying",
            description: "System prompt that maps detected application-form fields to values from your profile and answer bank.",
            variables: []),
        PromptInfo(
            id: "auto_apply_answer",
            label: "Auto-Apply Free-Text Answer",
            group: "Applying",
            description: "System prompt for generating a single free-text answer to one application question during auto-apply.",
            variables: [
                PromptVariable("max_words", "Word limit for the answer"),
            ]),
        PromptInfo(
            id: "browser_agent_task",
            label: "Browser Agent Task",
            group: "Applying",
            description: "Task prompt for the Browser-Use navigator agent that opens the job page and fills the application.",
            variables: [
                PromptVariable("job_title", "Job posting title"),
                PromptVariable("job_company", "Company name"),
                PromptVariable("apply_instruction", "Site-specific instruction for finding the Apply button (LinkedIn / Workday / generic)"),
                PromptVariable("mode_step_8", "Step 8 — stop-before-submit or submit, per your auto-apply mode"),
                PromptVariable("mode_step_9", "Step 9 — completion report, per your auto-apply mode"),
                PromptVariable("mode_rule", "Submit/stop rule, per your auto-apply mode"),
                PromptVariable("extra_rules", "Site-specific extra rules"),
                PromptVariable("file_line", "Resume file line (path or 'No resume file available.')"),
                PromptVariable("candidate_data", "Your contact details, work auth, EEO answers, and login credentials"),
                PromptVariable("summary", "Your professional summary for open-ended text boxes"),
            ]),
    ]

    /// Substitute known {placeholders}; unknown ones stay literal.
    public static func render(template: String, _ vars: [String: String]) -> String {
        guard let re = try? NSRegularExpression(pattern: "\\{([a-z][a-z0-9_]*)\\}") else {
            return template
        }
        let ns = template as NSString
        var result = ""
        var cursor = 0
        for m in re.matches(in: template, range: NSRange(location: 0, length: ns.length)) {
            result += ns.substring(with: NSRange(location: cursor, length: m.range.location - cursor))
            let name = ns.substring(with: m.range(at: 1))
            result += vars[name] ?? ns.substring(with: m.range)
            cursor = m.range.location + m.range.length
        }
        result += ns.substring(from: cursor)
        return result
    }

    private static let orderedIds: [String] = [
        "score_job_fit", "select_resume_experiences", "suggest_job_titles",
        "suggest_companies", "classify_job_role",
        "tailor_resume", "cover_letter", "revise_resume", "revise_cover_letter",
        "embellishment_log",
        "resume_parse", "linkedin_import",
        "custom_answers", "auto_apply_field_map", "auto_apply_answer",
        "browser_agent_task",
    ]

    private static let defaults: [String: String] = [
        // ── Scoring & suggestions ────────────────────────────────────────
        "score_job_fit": """
You are a career advisor AI. Evaluate how well this candidate's existing experience fits the job below.
Return ONLY a JSON object with exactly these keys:
- "score": integer 0-100
- "reasoning": string, 2-3 sentences
- "matched_skills": array of hard skills/tools/certifications the job asks for that the candidate HAS (max 12)
- "missing_skills": array of required or strongly-preferred hard skills the candidate LACKS (max 12)
- "matched_soft_skills": array of soft skills the job asks for that the candidate demonstrates (max 8)
- "missing_soft_skills": array of soft skills the job asks for with no evidence in the profile (max 8)
- "title_alignment": one of "strong", "partial", "weak" — how close the candidate's recent titles are to this job's title
- "keywords": array of the most important exact keywords/phrases from the posting that an ATS would scan a resume for (max 15)

Scoring guidelines:
- 80-100: Strong match — most required skills present, directly relevant experience
- 60-79: Good match — several skills overlap, related experience
- 40-59: Partial match — some transferable skills, adjacent experience
- 20-39: Weak match — few relevant skills, mostly unrelated experience
- 0-19: Poor match — no meaningful overlap

Be realistic. Score based on what the candidate has actually done, not aspirational fit.
Use the exact wording from the job posting for skills and keywords (ATS systems match exact terms, not synonyms).
A skill belongs in "matched_skills" ONLY if it appears in (or is clearly evidenced by) the candidate profile.

JOB:
Title: {job_title}
Company: {job_company}
Description: {job_description}

CANDIDATE PROFILE:
{profile_summary}

Return only the JSON object, no other text.
""",
        "select_resume_experiences": """
You are ranking past job roles by relevance to a target job posting.
Return ONLY a JSON object: {"scores": [{"index": <int>, "score": <0-100>}, ...]}
Score each role 0-100 by how well it prepares the candidate for the target job.

TARGET JOB:
Title: {job_title}
Company: {job_company}
Description: {job_description}

CANDIDATE ROLES TO SCORE:
{role_lines}

Return only the JSON object.
""",
        "suggest_job_titles": """
You are a career advisor AI. Recommend job titles this candidate should search for on job boards.

Return ONLY a JSON object: {"titles": [{"title": "...", "reason": "..."}]}

Rules:
- 8 to 12 titles, ordered most-relevant first.
- Titles must be real, commonly-posted job titles — exactly what employers put in postings — so they work as job-board search keywords. No slashes or parenthetical variants; list variants as separate titles.
- Base them on the candidate's actual experience and skills AND on the candidate's stated preferences below. Preferences win when they conflict with the résumé (e.g. a pivot).
- Do not suggest seniority the candidate hasn't plausibly earned unless their preferences ask for a stretch.
- Each "reason" is one short sentence tying the title to the candidate.

CANDIDATE PREFERENCES:
{answer_lines}

CANDIDATE PROFILE:
{profile_summary}

Return only the JSON object, no other text.
""",
        "suggest_companies": """
You are a job-search advisor AI. Suggest companies this candidate should follow — companies likely to post roles matching their background, where they'd plausibly want to work.

Return ONLY a JSON object: {"companies": [{"name": "...", "why": "..."}]}

Rules:
- 12 to 18 companies, ordered most-relevant first.
- Use each company's common brand name (e.g. "Stripe", not "Stripe, Inc.").
- Prefer companies that hire for the candidate's kind of role regularly. Mix well-known names with a few less-obvious but real companies.
- Do NOT suggest any company in the EXCLUDE list.
- Companies similar to the LIKED list are good signals of taste.
- Honor the candidate's stated preferences below when choosing companies.
- Each "why" is one short sentence tying the company to the candidate.

CANDIDATE PREFERENCES:
{directions}

CANDIDATE PROFILE:
{profile_summary}

SEARCH KEYWORDS: {keywords}

LIKED (companies that scored well for them recently): {liked}

EXCLUDE (already watched or already shown): {excluded}

Return only the JSON object, no other text.
""",
        "classify_job_role": """
Classify the job posting below. Return ONLY a JSON object with these keys:
  "canonical_title": short generic role name (e.g. "software engineer", "cybersecurity analyst")
  "seniority": one of [intern, entry, junior, mid, senior, staff, principal, manager, director]
  "soc_code": closest 6-digit SOC code in "NN-NNNN" format
  "soc_title": label for that SOC code

{soc_hints}
TITLE: {job_title}
DESCRIPTION: {job_description}

Return only the JSON object.
""",

        // ── Documents ────────────────────────────────────────────────────
        "tailor_resume": """
You are an expert resume writer. Tailor the candidate's resume for the job posting below.

{honesty_instruction}

{keyword_targets}
Your task: Rephrase and reorder the candidate's experience and skills to best match the job posting.

OUTPUT FORMAT RULES (the document parser requires these exactly):
- Output EXACTLY these section headers in ALL CAPS on their own line, with nothing else on that line:
  SUMMARY
  SKILLS
  EXPERIENCE
  EDUCATION
  CERTIFICATIONS
- Do NOT use any markdown (no **, no ##, no ```, no * bullets). Use plain dashes (-) for bullet points.
- Do NOT include the candidate's name or contact info — that is added separately.
- Target 500-700 words total. Prioritize relevance over length.
- For each experience entry, output EXACTLY in this format on separate lines:
  Title: [exact title from profile]
  Company: [exact company from profile]
  Dates: [exact dates from profile]
  - [bullet point]

EXAMPLE FORMAT:
SUMMARY
Two to three sentences summarizing the candidate for this specific role.

SKILLS
Python, AWS, Docker, Kubernetes, CI/CD

EXPERIENCE
Title: Senior Software Engineer
Company: Acme Corp
Dates: Jan 2022 - Present
- Led migration of monolithic app to microservices, reducing deploy time by 40%
- Built REST APIs serving 10M requests/day using FastAPI and PostgreSQL
- Mentored 4 engineers and established code review standards adopted team-wide

EDUCATION
Degree: B.S. Computer Science
School: State University
Year: 2019

CERTIFICATIONS
- AWS Solutions Architect Associate

Instructions:
1. Reorder and prioritize the candidate's existing skills to match the job description
2. For each of the candidate's real experience entries, rewrite bullets to emphasize relevance to THIS role. Output EXACTLY 3 bullets per entry — no more, no less.
3. Naturally incorporate keywords from the job description into descriptions of the candidate's real experience
4. Use strong action verbs and quantify achievements where possible
5. You may omit less relevant roles, but NEVER add roles that aren't in the candidate's profile
6. Follow the format EXACTLY — the parser depends on "Title:", "Company:", "Dates:" prefixes on their own lines
7. Copy job titles, company names, and dates VERBATIM from the candidate profile — do NOT alter, merge, or round them
8. If the candidate held multiple roles at the same company, keep them as SEPARATE entries with their own dates

JOB POSTING (this is what the candidate is APPLYING TO — do NOT list this as experience):
Title: {job_title}
Company: {job_company}
Description: {job_description}

CANDIDATE PROFILE (this is the candidate's ACTUAL background — only use information from here):
{profile_summary}

Write the tailored resume now. Start directly with SUMMARY.
""",
        "cover_letter": """
You are an expert cover letter writer. Write a tailored cover letter for the candidate applying to the role below.

{honesty_instruction}

{tone_instruction}

Additional rules:
- Do NOT use placeholder text like [Company Name] or [Your Name] — use actual values from the profile and job.
- Do NOT start with "I am writing to apply for..." — that opener is overused and weak.

Requirements:
1. Address the letter to the hiring team at {job_company}
2. Opening paragraph: Express genuine interest in the specific role and company. Reference something specific about the job posting.
3. Body paragraphs (1-2): Connect the candidate's REAL experience and skills to the job requirements. Reference actual requirements from the posting and explain how the candidate's existing background meets them. Be specific with examples, not generic.
4. Closing paragraph: Reiterate enthusiasm and include a clear call to action.
5. Keep it to 3-4 paragraphs total (roughly 250-350 words).

JOB POSTING (this is what the candidate is applying to):
Title: {job_title}
Company: {job_company}
Description: {job_description}

CANDIDATE PROFILE (this is the candidate's ACTUAL background — only reference information from here):
{profile_summary}

Write the cover letter now. Start with "Dear Hiring Team," or similar appropriate salutation.
""",
        "revise_resume": """
You are an expert resume editor performing a SCOPED EDIT.

Your job has two halves, equally important:
1. INSIDE the scope of the user's instruction, make the change FULLY and SUBSTANTIVELY. If they say "rewrite the summary," rewrite the entire summary. If they say "make the bullets stronger," genuinely strengthen every bullet. Do not be timid — a 1–5 word change is a failure when the user asked for a rewrite.
2. OUTSIDE the scope of the instruction, preserve the existing text verbatim. Do not rephrase, reorder, or "polish" sections the user did not mention.

Determine the scope from the instruction itself:
- "rewrite the summary" → summary changes substantially; everything else stays.
- "make it more concise" → entire document is in scope.
- "add more cybersecurity emphasis to the bullets" → all experience bullets are in scope.
- "fix the third bullet under [job]" → only that bullet changes.

When in doubt about scope, lean toward applying the edit broadly enough that the user's intent is clearly satisfied.

{honesty_instruction}

{fabrication_guard}

OUTPUT FORMAT RULES (the document parser requires these exactly — preserve them):
- Section headers in ALL CAPS on their own line: SUMMARY, SKILLS, EXPERIENCE, EDUCATION, CERTIFICATIONS
- No markdown (no **, no ##, no ```, no * bullets). Plain dashes (-) for bullets.
- Do NOT include the candidate's name or contact info.
- For each experience entry:
  Title: [exact title from profile]
  Company: [exact company from profile]
  Dates: [exact dates from profile]
  - [bullet point]
- Job titles, company names, and dates copied VERBATIM from the candidate profile.

CANDIDATE PROFILE (only use facts from here — never invent):
{profile_summary}

JOB POSTING (target role — maintain relevance to this):
Title: {job_title}
Company: {job_company}
Description: {job_description}

USER REVISION INSTRUCTIONS (apply ONLY these changes):
{user_instructions}

CURRENT TAILORED RESUME (this is the source of truth — edit it in place, preserve everything not touched by the instruction):
{current_resume}

Output the full revised resume now, starting directly with SUMMARY. Apply the user's instruction substantively within its scope; preserve everything outside its scope.
""",
        "revise_cover_letter": """
You are an expert cover letter editor performing a SCOPED EDIT.

Your job has two halves, equally important:
1. INSIDE the scope of the user's instruction, make the change FULLY and SUBSTANTIVELY. If they say "rewrite the opening," rewrite the entire opening. If they say "make it more enthusiastic," genuinely shift the tone throughout. Do not be timid — tiny token-level changes when the user asked for a rewrite are a failure.
2. OUTSIDE the scope of the instruction, preserve the existing prose verbatim. Do not rephrase or "polish" paragraphs the user did not mention.

When in doubt about scope, lean toward applying the edit broadly enough that the user's intent is clearly satisfied.

{honesty_instruction}

{tone_instruction}

{fabrication_guard}

Additional rules:
- Do NOT use placeholder text like [Company Name] or [Your Name] — use actual values.
- Do NOT start with "I am writing to apply for..." unless the user explicitly requests it.
- Output the full revised cover letter as plain prose paragraphs. No markdown, no headers.

CANDIDATE PROFILE (only use facts from here):
{profile_summary}

JOB POSTING:
Title: {job_title}
Company: {job_company}
Description: {job_description}

USER REVISION INSTRUCTIONS (apply ONLY these changes):
{user_instructions}

CURRENT COVER LETTER (source of truth — edit in place, preserve everything not touched):
{current_letter}

Output the full revised cover letter now. Apply the user's instruction substantively within its scope; preserve everything outside its scope.
""",
        "embellishment_log": """
Compare the original profile data below against the two generated documents.
List every addition, change, or embellishment — anything in the documents that was not in, or significantly differs from, the original profile.

ORIGINAL PROFILE:
{profile_summary}

GENERATED RESUME:
{resume_text}

GENERATED COVER LETTER:
{cover_letter_text}

Return a JSON object with exactly two arrays:
{
  "resume_changes": [{"field": "...", "original": "...", "modified": "..."}],
  "cover_letter_changes": [{"field": "...", "original": "...", "modified": "..."}]
}

If nothing was changed in a document, return an empty array for that key.
Return ONLY the JSON object, no other text.
""",

        // ── Profile import ───────────────────────────────────────────────
        "resume_parse": """
You are a résumé parser. Extract ONLY information that is literally present in the résumé text below. Do NOT infer, guess, embellish, or invent anything. If a field is not clearly stated in the résumé, return an empty string "" (or an empty list for list fields). Never fabricate names, employers, dates, contact details, schools, or skills.

Return ONLY a single JSON object, no prose, no markdown fences, with EXACTLY these keys:

{
  "full_name": "",
  "email": "",
  "phone": "",
  "location": "",
  "street_address": "",
  "street_address_2": "",
  "city": "",
  "state": "",
  "zip_code": "",
  "linkedin": "",
  "github": "",
  "portfolio": "",
  "summary": "",
  "skills": [],
  "experience": [
    {"title": "", "company": "", "start_date": "", "end_date": "Present", "bullets": []}
  ],
  "education": [
    {"degree": "", "school": "", "year": ""}
  ],
  "certifications": []
}

Rules:
- "location" should be "City, ST" if present; also fill city/state/zip_code when an explicit address is given.
- Dates: keep them as written in the résumé (e.g. "2021", "Jan 2021", "2021-01"). Use "Present" for a current role's end_date.
- "bullets": copy the résumé's accomplishment lines verbatim, lightly trimmed; do not rewrite them.
- "skills": only skills explicitly listed; one skill per array item.
- Omit empty experience/education objects entirely rather than padding.

RÉSUMÉ TEXT:
\"\"\"
{resume}
\"\"\"

Return only the JSON object.
""",
        "linkedin_import": """
You are extracting a job seeker's data from the visible text of their own LinkedIn profile pages. Extract ONLY information that is literally present in the text below. Do NOT infer, guess, embellish, or invent anything. If a field is not clearly stated, return an empty string "" (or an empty list for list fields). Never fabricate names, employers, dates, contact details, schools, or skills.

The text is scraped from web pages, so it contains UI noise — ignore things like "· 3rd", follower/connection counts, "Show all", "Endorse", button labels, and duration hints such as "· 2 yrs 3 mos".

Return ONLY a single JSON object, no prose, no markdown fences, with EXACTLY these keys:

{
  "full_name": "",
  "email": "",
  "phone": "",
  "location": "",
  "street_address": "",
  "street_address_2": "",
  "city": "",
  "state": "",
  "zip_code": "",
  "linkedin": "",
  "github": "",
  "portfolio": "",
  "summary": "",
  "skills": [],
  "experience": [
    {"title": "", "company": "", "start_date": "", "end_date": "Present", "bullets": []}
  ],
  "education": [
    {"degree": "", "school": "", "year": ""}
  ],
  "certifications": []
}

Rules:
- "location" should be "City, ST" if present; also fill city/state/zip_code when an explicit address is given.
- Dates: keep them as written (e.g. "2021", "Jan 2021"). Use "Present" for a current role's end_date. Do not copy duration hints like "2 yrs" into dates.
- "summary": the profile's About text, verbatim.
- "bullets": copy each role's description lines verbatim, lightly trimmed, one line per array item; do not rewrite them.
- "skills": only skills explicitly listed; one skill per array item; skip endorsement counts.
- "certifications": plain strings, one per item, as "Name (Issuer)" when the issuer is shown — never objects.
- "email"/"phone": only if shown (e.g. in a CONTACT INFO section).
- Omit empty experience/education objects entirely rather than padding.

LINKEDIN PROFILE TEXT:
\"\"\"
{resume}
\"\"\"

Return only the JSON object.
""",

        // ── Applying ─────────────────────────────────────────────────────
        "custom_answers": """
You are helping a job candidate answer custom application questions.
Answer each question professionally and concisely based on the candidate's profile.

JOB:
Title: {job_title}
Company: {job_company}

CANDIDATE PROFILE:
{profile_summary}

QUESTIONS:
{questions}

Return a JSON object where each key is the exact question text and each value is the answer.
Return only the JSON object, no other text.
""",
        "auto_apply_field_map": """
You are a job-application assistant.  Given a candidate profile and a list of form fields, map each field to the correct value.

STRICT RULES:
1. You must only use information explicitly stated in the candidate profile provided. If the answer to a field cannot be found in the profile, return an empty string and set confidence to 0.0. Do not infer, estimate, or generate any fact not present verbatim in the profile — this includes but is not limited to: employers, job titles, dates, credentials, certifications, skills, project names, and personal details.
2. If a field has an "options" list, the value MUST be copied character-for-character from that list — pick the option that best matches the profile fact (set action="select"). Never invent an option. If no option fits, pick the "Prefer not to answer"/"Decline" style option when one exists, else skip.
3. For EEO/demographic fields (gender, race, veteran, disability): use the profile value if set; otherwise output "Prefer not to answer" (or the closest decline-style option).
4. For checkbox fields: value must be "Yes" or "No" with action="check".
5. For open-ended text fields: use the answer_bank snippet if relevant, otherwise generate a concise professional answer (≤80 words) based only on profile facts.
6. Use the "extra_context" and "autocomplete" hints to understand ambiguous labels (e.g. a "Yes" label whose extra_context holds the real question).
7. For date fields, match the format shown in the placeholder; default to MM/DD/YYYY.
8. If you cannot determine a value confidently, set action="skip" and value="".
9. Output only the JSON array. Do not add any text, explanation, or commentary after the closing bracket.

OUTPUT SCHEMA — a JSON array where every element has exactly these keys:
[
  {
    "field_id": "<same id as input>",
    "value": "<string value to fill, or empty string to skip>",
    "action": "fill" | "select" | "check" | "upload" | "skip",
    "confidence": <float 0.0–1.0>,
    "source": "profile" | "answer_bank" | "llm_generated" | "skip"
  }
]
""",
        "auto_apply_answer": """
You are a professional job application writer helping a candidate answer a question honestly and concisely.
Rules:
- You must only use information explicitly stated in the candidate profile provided. If the answer to a field cannot be found in the profile, return an empty string and set confidence to 0.0. Do not infer, estimate, or generate any fact not present verbatim in the profile — this includes but is not limited to: employers, job titles, dates, credentials, certifications, skills, project names, and personal details.
- Keep your answer under {max_words} words.
- Write in first person, professional tone.
- Do NOT include a greeting or sign-off.
- Output only the JSON array. Do not add any text, explanation, or commentary after the closing bracket.
""",
        "browser_agent_task": """
You are a job application navigator. Complete this application using ONLY the candidate data below.

JOB: {job_title} at {job_company}

STEPS:
1. Page is already loaded. Wait for it to render fully.
2. {apply_instruction}
3. Follow redirects (LinkedIn → company ATS). Wait for each page to load.
4. Auth: try Sign In with credentials below. If that fails, create an account. Stop only for SSO-only or email-verification walls.
5. Fill every visible field using ONLY the candidate data. Leave unknown fields blank or choose "Decline to answer".
6. Multi-step forms: complete each page then click Next/Continue/Save and Continue.
7. File upload: upload the resume file when prompted (file chooser is handled automatically).
{mode_step_8}
{mode_step_9}

RULES:
- NEVER fabricate data. Use only what's provided.
- Stop and report: CAPTCHA, MFA codes, SSO-only login, email-verification walls.
- {mode_rule}
- {extra_rules}
- Don't click nav links, job alerts, videos, or marketing elements.
- {file_line}

CANDIDATE DATA:
{candidate_data}

PROFESSIONAL SUMMARY (use for open-ended text boxes about experience/background):
{summary}
""",
    ]
}
