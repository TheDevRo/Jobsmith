import Foundation

public struct EmbellishmentChange: Codable, Equatable, Sendable {
    public var field: String
    public var original: String
    public var modified: String

    public init(field: String, original: String, modified: String) {
        self.field = field; self.original = original; self.modified = modified
    }
}

/// Mirrors the desktop embellishment-log dict (snake_case keys, WARNING flag).
public struct EmbellishmentLog: Codable, Equatable, Sendable {
    public var honestyLevel: String
    public var resumeChanges: [EmbellishmentChange]
    public var coverLetterChanges: [EmbellishmentChange]
    public var generatedAt: String
    public var warning: String?

    enum CodingKeys: String, CodingKey {
        case honestyLevel = "honesty_level"
        case resumeChanges = "resume_changes"
        case coverLetterChanges = "cover_letter_changes"
        case generatedAt = "generated_at"
        case warning = "WARNING"
    }
}

/// Ports of the document-generation entry points in `ai_engine.py`:
/// tailored resume, cover letter, AI-Edit revisions, embellishment log,
/// custom answers, plus the resume experience selection.
public enum TailoringService {
    // MARK: - Resume

    public static func tailorResume(job: Job, profile: Profile, config: AppConfig,
                                    engine: AIEngine,
                                    honestyLevel: HonestyConfig.Level? = nil,
                                    matchReportJSON: String? = nil) async throws -> String {
        let level = honestyLevel ?? config.honesty.level
        let selected = await selectResumeExperiences(
            profile.experience, job: job,
            maxEntries: config.honesty.maxResumeExperienceEntries,
            config: config, engine: engine)

        let prompt = PromptRegistry.render("tailor_resume", [
            "honesty_instruction": Directives.honestyInstruction(level),
            "keyword_targets": keywordTargetsBlock(matchReportJSON),
            "job_title": job.title,
            "job_company": job.company,
            "job_description": String(job.description.prefix(5000)),
            "profile_summary": Directives.profileSummary(profile, experiences: selected),
        ], config: config)

        let request = CompletionRequest(user: prompt, tier: .strong,
                                        temperature: config.ai.temperature,
                                        maxTokens: config.ai.maxTokens)
        return try await completeWithRetry(request, retryTemperature: 0.5,
                                           config: config, engine: engine)
    }

    // MARK: - Cover letter

    public static func coverLetter(job: Job, profile: Profile, config: AppConfig,
                                   engine: AIEngine,
                                   honestyLevel: HonestyConfig.Level? = nil) async throws -> String {
        let level = honestyLevel ?? config.honesty.level
        let selected = await selectResumeExperiences(
            profile.experience, job: job,
            maxEntries: config.honesty.maxResumeExperienceEntries,
            config: config, engine: engine)

        let prompt = PromptRegistry.render("cover_letter", [
            "honesty_instruction": Directives.honestyInstruction(level),
            "tone_instruction": Directives.toneInstruction(config.honesty.coverLetterTone),
            "job_title": job.title,
            "job_company": job.company.isEmpty ? "the company" : job.company,
            "job_description": String(job.description.prefix(5000)),
            "profile_summary": Directives.profileSummary(profile, experiences: selected),
        ], config: config)

        let request = CompletionRequest(user: prompt, tier: .strong,
                                        temperature: config.ai.temperature,
                                        maxTokens: config.ai.maxTokens)
        return try await completeWithRetry(request, retryTemperature: 0.5,
                                           config: config, engine: engine)
    }

    // MARK: - AI Edit (scoped revisions)

    public static func reviseResume(currentResume: String, instructions: String,
                                    job: Job, profile: Profile, config: AppConfig,
                                    engine: AIEngine,
                                    tier: ModelTier? = nil,
                                    honestyLevel: HonestyConfig.Level? = nil) async throws -> String {
        let level = honestyLevel ?? config.honesty.level
        let selected = await selectResumeExperiences(
            profile.experience, job: job,
            maxEntries: config.honesty.maxResumeExperienceEntries,
            config: config, engine: engine)

        let prompt = PromptRegistry.render("revise_resume", [
            "honesty_instruction": Directives.honestyInstruction(level),
            "fabrication_guard": Directives.reviseFabricationGuard(level),
            "profile_summary": Directives.profileSummary(profile, experiences: selected),
            "job_title": job.title,
            "job_company": job.company,
            "job_description": String(job.description.prefix(5000)),
            "user_instructions": instructions,
            "current_resume": currentResume,
        ], config: config)

        let request = CompletionRequest(user: prompt, tier: tier ?? config.honesty.aiEditTier,
                                        temperature: 0.5,
                                        maxTokens: max(config.ai.maxTokens, 3000))
        return try await engine.complete(request, config: config.ai)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public static func reviseCoverLetter(currentLetter: String, instructions: String,
                                         job: Job, profile: Profile, config: AppConfig,
                                         engine: AIEngine,
                                         tier: ModelTier? = nil,
                                         honestyLevel: HonestyConfig.Level? = nil) async throws -> String {
        let level = honestyLevel ?? config.honesty.level
        let selected = await selectResumeExperiences(
            profile.experience, job: job,
            maxEntries: config.honesty.maxResumeExperienceEntries,
            config: config, engine: engine)

        let prompt = PromptRegistry.render("revise_cover_letter", [
            "honesty_instruction": Directives.honestyInstruction(level),
            "tone_instruction": Directives.toneInstruction(config.honesty.coverLetterTone),
            "fabrication_guard": Directives.reviseFabricationGuard(level),
            "profile_summary": Directives.profileSummary(profile, experiences: selected),
            "job_title": job.title,
            "job_company": job.company,
            "job_description": String(job.description.prefix(5000)),
            "user_instructions": instructions,
            "current_letter": currentLetter,
        ], config: config)

        let request = CompletionRequest(user: prompt, tier: tier ?? config.honesty.aiEditTier,
                                        temperature: 0.5,
                                        maxTokens: max(config.ai.maxTokens, 2500))
        return try await engine.complete(request, config: config.ai)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Embellishment log

    /// Diff the generated documents against the original profile. Never
    /// throws — on any failure the log is returned with empty change lists.
    public static func embellishmentLog(profile: Profile, resumeText: String,
                                        coverLetterText: String,
                                        honestyLevel: HonestyConfig.Level,
                                        config: AppConfig,
                                        engine: AIEngine) async -> EmbellishmentLog {
        let prompt = PromptRegistry.render("embellishment_log", [
            "profile_summary": Directives.profileSummary(profile),
            "resume_text": String(resumeText.prefix(3000)),
            "cover_letter_text": String(coverLetterText.prefix(2000)),
        ], config: config)

        var resumeChanges: [EmbellishmentChange] = []
        var coverLetterChanges: [EmbellishmentChange] = []

        let request = CompletionRequest(user: prompt, tier: .strong,
                                        temperature: 0.2, maxTokens: config.ai.maxTokens)
        if let text = try? await engine.complete(request, config: config.ai)
            .trimmingCharacters(in: .whitespacesAndNewlines),
           let data = LenientJSON.parseObject(text) {
            resumeChanges = cleanChanges(data["resume_changes"])
            coverLetterChanges = cleanChanges(data["cover_letter_changes"])
        }

        var log = EmbellishmentLog(
            honestyLevel: honestyLevel.rawValue,
            resumeChanges: resumeChanges,
            coverLetterChanges: coverLetterChanges,
            generatedAt: ISO8601DateFormatter().string(from: Date()),
            warning: nil)
        if honestyLevel == .fabricated {
            log.warning = "This application contains fabricated content. Review before interviews."
        }
        return log
    }

    /// Keep only entries with the expected keys; coerce values to strings.
    static func cleanChanges(_ raw: Any?) -> [EmbellishmentChange] {
        guard let entries = raw as? [Any] else { return [] }
        return entries.compactMap { entry in
            guard let dict = entry as? [String: Any],
                  let field = dict["field"], let original = dict["original"],
                  let modified = dict["modified"] else { return nil }
            return EmbellishmentChange(field: LenientJSON.stringValue(field),
                                       original: LenientJSON.stringValue(original),
                                       modified: LenientJSON.stringValue(modified))
        }
    }

    // MARK: - Custom application answers

    /// Never throws — unparseable output maps every question to "".
    public static func customAnswers(job: Job, profile: Profile, questions: [String],
                                     config: AppConfig, engine: AIEngine) async -> [String: String] {
        let prompt = PromptRegistry.render("custom_answers", [
            "job_title": job.title,
            "job_company": job.company,
            "profile_summary": Directives.profileSummary(profile),
            "questions": questions.map { "- \($0)" }.joined(separator: "\n"),
        ], config: config)

        let request = CompletionRequest(user: prompt, tier: .strong,
                                        temperature: 0.5, maxTokens: config.ai.maxTokens)
        let empty = Dictionary(uniqueKeysWithValues: questions.map { ($0, "") })
        guard let text = try? await engine.complete(request, config: config.ai)
            .trimmingCharacters(in: .whitespacesAndNewlines) else { return empty }

        var data = LenientJSON.decodeObject(text)
        if data == nil,
           let groups = Rx.first("\\{[^{}]*\\}", in: text, options: [.dotMatchesLineSeparators]),
           let raw = groups[0] {
            data = LenientJSON.decodeObject(raw)
        }
        guard let data else { return empty }
        return data.mapValues { LenientJSON.stringValue($0) }
    }

    // MARK: - Resume experience selection

    /// Pick which experience entries belong on the tailored resume.
    /// Pinned roles are always included (even beyond the cap); remaining
    /// slots go to the LLM's top-scored unpinned roles, falling back to the
    /// original order on any failure. Result is sorted end-date descending
    /// (Present roles first).
    public static func selectResumeExperiences(_ experiences: [WorkExperience], job: Job,
                                               maxEntries: Int?, config: AppConfig,
                                               engine: AIEngine) async -> [WorkExperience] {
        guard !experiences.isEmpty else { return experiences }
        let cap = maxEntries ?? 0
        guard cap > 0, experiences.count > cap else { return experiences }

        let pinned = experiences.filter(\.pinned)
        let unpinned = experiences.filter { !$0.pinned }

        if pinned.count >= cap { return sortedByEndDateDescending(pinned) }
        let slotsRemaining = cap - pinned.count
        if unpinned.isEmpty { return sortedByEndDateDescending(pinned) }

        // Numbered description of unpinned roles for the LLM to score
        let roleLines = unpinned.enumerated().map { i, exp -> String in
            let bullets = exp.bullets.filter { !$0.isEmpty }.joined(separator: "; ")
            return "Role \(i): \(exp.title) at \(exp.company) "
                + "(\(exp.startDate) - \(exp.endDate)). "
                + "Highlights: \(String(bullets.prefix(500)))"
        }

        let prompt = PromptRegistry.render("select_resume_experiences", [
            "job_title": job.title,
            "job_company": job.company,
            "job_description": String(job.description.prefix(3000)),
            "role_lines": roleLines.joined(separator: "\n"),
        ], config: config)

        var selectedUnpinned: [WorkExperience] = []
        let request = CompletionRequest(user: prompt, tier: .utility,
                                        temperature: 0.2, maxTokens: 400)
        if let text = try? await engine.complete(request, config: config.ai)
            .trimmingCharacters(in: .whitespacesAndNewlines),
           let scored = parseRoleScores(text) {
            var seen: Set<Int> = []
            for row in stableSortedDescending(scored, by: { $0.score }) {
                if row.index >= 0, row.index < unpinned.count, !seen.contains(row.index) {
                    seen.insert(row.index)
                    selectedUnpinned.append(unpinned[row.index])
                    if selectedUnpinned.count >= slotsRemaining { break }
                }
            }
        } else {
            selectedUnpinned = Array(unpinned.prefix(slotsRemaining))
        }

        // If the LLM returned fewer scores than slots, top up in original order
        if selectedUnpinned.count < slotsRemaining {
            for exp in unpinned where !selectedUnpinned.contains(exp) {
                selectedUnpinned.append(exp)
                if selectedUnpinned.count >= slotsRemaining { break }
            }
        }

        return sortedByEndDateDescending(pinned + selectedUnpinned)
    }

    /// JSON {"scores": [{index, score}]} with a regex salvage; nil on failure.
    static func parseRoleScores(_ text: String) -> [(index: Int, score: Double)]? {
        if let data = LenientJSON.decodeObject(text), let rows = data["scores"] as? [Any] {
            return rows.compactMap { row -> (index: Int, score: Double)? in
                guard let dict = row as? [String: Any] else { return nil }
                let index = Int(LenientJSON.doubleValue(dict["index"]) ?? -1)
                let score = LenientJSON.doubleValue(dict["score"]) ?? 0
                return (index: index, score: score)
            }
        }
        let salvaged = Rx.all("\"index\"\\s*:\\s*(\\d+)\\s*,\\s*\"score\"\\s*:\\s*(\\d+)", in: text)
            .compactMap { groups -> (index: Int, score: Double)? in
                guard let i = groups[1].flatMap({ Int($0) }),
                      let s = groups[2].flatMap({ Double($0) }) else { return nil }
                return (index: i, score: s)
            }
        return salvaged.isEmpty ? nil : salvaged
    }

    /// Sort key for descending chronological order — Present roles float to top.
    static func experienceSortKey(_ exp: WorkExperience) -> String {
        let end = exp.endDate.trimmingCharacters(in: .whitespacesAndNewlines)
        if ["present", "current", ""].contains(end.lowercased()) { return "9999-99-99" }
        return end
    }

    static func sortedByEndDateDescending(_ exps: [WorkExperience]) -> [WorkExperience] {
        exps.enumerated()
            .sorted { a, b in
                let ka = experienceSortKey(a.element)
                let kb = experienceSortKey(b.element)
                return ka != kb ? ka > kb : a.offset < b.offset
            }
            .map { $0.element }
    }

    private static func stableSortedDescending<T>(
        _ rows: [T], by key: (T) -> Double
    ) -> [T] {
        rows.enumerated()
            .sorted { a, b in
                let ka = key(a.element), kb = key(b.element)
                return ka != kb ? ka > kb : a.offset < b.offset
            }
            .map { $0.element }
    }

    // MARK: - Shared prompt blocks

    /// ATS keyword-targeting block derived from the scoring match report.
    static func keywordTargetsBlock(_ matchReportJSON: String?) -> String {
        guard let json = matchReportJSON, let report = LenientJSON.parseObject(json) else {
            return ""
        }
        func strings(_ key: String) -> [String] {
            (report[key] as? [Any])?.compactMap { $0 as? String } ?? []
        }
        var lines = ["KEYWORD TARGETS (from ATS gap analysis of this posting):"]
        let matched = strings("matched_skills")
        let keywords = strings("keywords")
        let missing = strings("missing_skills")
        if !matched.isEmpty {
            lines.append("- Candidate HAS these skills the job requires — feature them prominently, "
                + "using this exact wording: \(matched.joined(separator: ", "))")
        }
        if !keywords.isEmpty {
            lines.append("- ATS scan keywords — weave these exact phrases into the summary and bullets "
                + "wherever the candidate's real experience supports them: \(keywords.joined(separator: ", "))")
        }
        if !missing.isEmpty {
            lines.append("- Candidate LACKS these required skills: \(missing.joined(separator: ", ")). "
                + "Follow the TAILORING DIRECTIVE above for how much latitude you have; "
                + "do not claim them beyond what it permits.")
        }
        return lines.joined(separator: "\n") + "\n"
    }

    private static func completeWithRetry(_ request: CompletionRequest,
                                          retryTemperature: Double,
                                          config: AppConfig,
                                          engine: AIEngine) async throws -> String {
        do {
            return try await engine.complete(request, config: config.ai)
                .trimmingCharacters(in: .whitespacesAndNewlines)
        } catch {
            var retry = request
            retry.temperature = retryTemperature
            return try await engine.complete(retry, config: config.ai)
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }
}
