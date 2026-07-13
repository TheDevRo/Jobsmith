import Foundation

/// The 4-phase form-field mapping pipeline, ported from
/// `backend/auto_apply/llm_client.py` map_fields_to_values:
///
///   0.   File inputs resolved deterministically (never sent to the LLM):
///        keyword match → action "upload" with a kind token
///        ("resume"/"cover_letter") the extension swaps for file bytes;
///        unmatched inputs default to resume unless clearly another document
///        kind (photo, portfolio, transcript, …), which is skipped.
///   0.5  Deterministic profile matching (ProfileFieldMatcher); password
///        fields never reach the LLM.
///   1.   Answer bank via findBestMatch on label+extra_context.
///   2.   LLM chunks of 25 through the "auto_apply_field_map" template;
///        malformed items skipped, omitted fields gap-filled as skip.
///   3.   Merge preserving the original field order.
public struct FieldMapper: Sendable {
    let engine: any AIEngine
    let bank: AnswerBankMatcher
    let chunkSize: Int

    public init(engine: any AIEngine, bank: AnswerBankMatcher, chunkSize: Int = 25) {
        self.engine = engine
        self.bank = bank
        self.chunkSize = chunkSize
    }

    /// Upload fields that clearly want something other than a resume/cover
    /// letter — defaulting the resume into these would be wrong.
    /// Mirror of _NON_RESUME_UPLOAD_TOKENS in backend/auto_apply/llm_client.py.
    static let nonResumeUploadTokens = [
        "photo", "picture", "image", "avatar", "headshot",
        "portfolio", "transcript", "certific", "license",
        "passport", "visa", "sample",
    ]

    // ------------------------------------------------------------------
    // Pipeline
    // ------------------------------------------------------------------

    public func map(fields: [FieldDescriptor], profile: Profile,
                    job: ApplyJobContext, config: AppConfig) async -> [FieldValue] {
        guard !fields.isEmpty else { return [] }

        // --- Phase 0: deterministic mapping for file inputs ---
        // File inputs never reach the LLM — it can't attach files, so a
        // fall-through would come back "skip" and the upload silently
        // degrades to manual.
        var fileResolved: [String: FieldValue] = [:]
        var remaining: [FieldDescriptor] = []
        for f in fields {
            if f.fieldType.lowercased() == "file" {
                // Greenhouse-style file inputs label themselves "Attach" and
                // carry no `name` attribute — the only meaningful signal is
                // `id="resume"` / `id="cover_letter"`, sent as field_id.
                // Workday-style inputs have no signal at all (a generated
                // field_id and a "Select files" button); the drop zone's
                // group text, when present, arrives as extraContext.
                let hay = "\(f.label) \(f.name) \(f.placeholder) \(f.fieldId) \(f.extraContext)"
                    .lowercased()
                let kind: String
                if hay.contains("cover") {
                    kind = "cover_letter"
                } else if ["resume", "cv", "curriculum"].contains(where: { hay.contains($0) }) {
                    kind = "resume"
                } else if Self.nonResumeUploadTokens.contains(where: { hay.contains($0) }) {
                    // A different kind of document — nothing sensible to
                    // attach, and the LLM can't help; leave it manual.
                    fileResolved[f.fieldId] = FieldValue(
                        fieldId: f.fieldId, value: "", action: "skip",
                        confidence: 0.0, source: "skip")
                    continue
                } else {
                    kind = "resume"  // unlabeled uploader — default to resume
                }
                fileResolved[f.fieldId] = FieldValue(
                    fieldId: f.fieldId, value: kind, action: "upload",
                    confidence: 0.95, source: "profile")
                continue
            }
            remaining.append(f)
        }

        // --- Phase 0.5: deterministic profile matching ---
        var detResolved = ProfileFieldMatcher.matchProfileFields(profile: profile,
                                                                 fields: remaining)
        var afterDet: [FieldDescriptor] = []
        for f in remaining {
            if detResolved[f.fieldId] != nil { continue }
            // Password fields must never reach the LLM — if the matcher had a
            // credential it already used it; otherwise skip outright.
            if f.fieldType.lowercased() == "password" {
                detResolved[f.fieldId] = FieldValue(fieldId: f.fieldId, value: "",
                                                    action: "skip", confidence: 0.0,
                                                    source: "skip")
                continue
            }
            afterDet.append(f)
        }
        remaining = afterDet

        // --- Phase 1: resolve fields from the answer bank ---
        var bankResolved: [String: FieldValue] = [:]
        var llmFields: [FieldDescriptor] = []
        for f in remaining {
            // Combine label + group context (fieldset legend etc.) so bank
            // keyword matching sees the actual question, not just "Yes".
            let combined = [f.label, f.extraContext].filter { !$0.isEmpty }
                .joined(separator: " ")
            let questionText = combined.isEmpty ? f.name : combined
            if !questionText.isEmpty,
               let match = bank.findBestMatch(question: questionText) {
                bankResolved[f.fieldId] = FieldValue(fieldId: f.fieldId,
                                                     value: match.value,
                                                     action: "fill",
                                                     confidence: 1.0,
                                                     source: "answer_bank")
            } else {
                llmFields.append(f)
            }
        }

        // --- Phase 2: LLM call(s) for remaining fields (chunked, skipped if none) ---
        var llmResults: [FieldValue] = []
        if !llmFields.isEmpty {
            let system = PromptRegistry.template("auto_apply_field_map", config: config)
            let answerBank = bank.allSnippets()
            var mappedIds = Set<String>()

            var chunks: [[FieldDescriptor]] = []
            var i = 0
            while i < llmFields.count {
                chunks.append(Array(llmFields[i..<min(i + chunkSize, llmFields.count)]))
                i += chunkSize
            }

            for chunk in chunks {
                let user = Self.buildFieldMapUser(profile: profile, job: job,
                                                  fields: chunk, answerBank: answerBank)
                let raw: Any
                do {
                    raw = try await completeJSON(system: system, user: user, config: config)
                } catch {
                    raw = [Any]()  // failed chunk → its fields get gap-filled as skip
                }
                for item in (raw as? [Any]) ?? [] {
                    guard let fv = Self.parseItem(item) else { continue }  // malformed → skipped
                    llmResults.append(fv)
                    mappedIds.insert(fv.fieldId)
                }
            }

            // Fill gaps for any llm_fields the LLM omitted.
            for f in llmFields where !mappedIds.contains(f.fieldId) {
                llmResults.append(FieldValue(fieldId: f.fieldId, value: "",
                                             action: "skip", confidence: 0.0,
                                             source: "skip"))
            }
        }

        // --- Phase 3: merge in original field order ---
        var llmById: [String: FieldValue] = [:]
        for fv in llmResults { llmById[fv.fieldId] = fv }
        var out: [FieldValue] = []
        for f in fields {
            if let fv = fileResolved[f.fieldId] {
                out.append(fv)
            } else if let fv = detResolved[f.fieldId] {
                out.append(fv)
            } else if let fv = bankResolved[f.fieldId] {
                out.append(fv)
            } else if let fv = llmById[f.fieldId] {
                out.append(fv)
            }
        }
        return out
    }

    // ------------------------------------------------------------------
    // LLM helpers
    // ------------------------------------------------------------------

    struct InvalidJSONError: Error {
        let lastResponse: String
    }

    /// Port of LLMClient.complete_json: up to 3 attempts, appending a
    /// strict-JSON reminder to the user prompt after each parse failure.
    /// Engine (network) errors propagate to the caller like Python's.
    func completeJSON(system: String, user: String, config: AppConfig,
                      maxRetries: Int = 3) async throws -> Any {
        var prompt = user
        var lastText = ""
        for _ in 1...maxRetries {
            let text = try await engine.complete(
                CompletionRequest(system: system, user: prompt, tier: .fast,
                                  temperature: config.ai.temperature,
                                  maxTokens: config.ai.maxTokens),
                config: config.ai)
            lastText = text
            if let parsed = Self.extractJSON(text) { return parsed }
            prompt += "\n\n[IMPORTANT: Return ONLY valid JSON. No markdown, no extra text.]"
        }
        throw InvalidJSONError(lastResponse: String(lastText.prefix(300)))
    }

    /// Validate one raw LLM array element into a FieldValue; nil = malformed.
    /// Mirrors Pydantic: field_id and value are required strings; action and
    /// source default; confidence coerces from number or numeric string.
    static func parseItem(_ item: Any) -> FieldValue? {
        guard let dict = item as? [String: Any],
              let fieldId = dict["field_id"] as? String,
              let value = dict["value"] as? String else { return nil }
        var action = "fill"
        if let raw = dict["action"] {
            guard let s = raw as? String else { return nil }
            action = s
        }
        var source = "profile"
        if let raw = dict["source"] {
            guard let s = raw as? String else { return nil }
            source = s
        }
        var confidence = 1.0
        if let raw = dict["confidence"], !(raw is NSNull) {
            guard let d = LenientJSON.doubleValue(raw) else { return nil }
            confidence = d
        }
        return FieldValue(fieldId: fieldId, value: value, action: action,
                          confidence: confidence, source: source)
    }

    // ------------------------------------------------------------------
    // Prompt building — port of _build_field_map_user
    // ------------------------------------------------------------------

    static func buildFieldMapUser(profile: Profile, job: ApplyJobContext,
                                  fields: [FieldDescriptor],
                                  answerBank: [String: String]) -> String {
        // Omit placeholder snippets from the prompt.
        let filtered = answerBank.filter {
            !($0.value.hasPrefix("<") && $0.value.hasSuffix(">"))
        }
        let bankStr = prettyJSON(filtered)
        let fieldsStr = prettyJSON(fields)
        return "CANDIDATE PROFILE:\n\(profileText(profile))\n\n"
            + "JOB:\nTitle: \(job.title)\nCompany: \(job.company)\n"
            + "Description (first 300 chars):\n\(String(job.description.prefix(300)))\n\n"
            + "ANSWER BANK:\n\(bankStr)\n\n"
            + "FORM FIELDS TO MAP:\n\(fieldsStr)\n\n"
            + "Return the JSON array now."
    }

    static func prettyJSON(_ dict: [String: String]) -> String {
        guard let data = try? JSONSerialization.data(
            withJSONObject: dict, options: [.prettyPrinted, .sortedKeys]) else { return "{}" }
        return String(data: data, encoding: .utf8) ?? "{}"
    }

    static func prettyJSON(_ fields: [FieldDescriptor]) -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        guard let data = try? encoder.encode(fields) else { return "[]" }
        return String(data: data, encoding: .utf8) ?? "[]"
    }

    /// Flat text representation of the profile for LLM prompts — port of
    /// UserProfile.to_text(). Desktop-only fields the iOS profile lacks keep
    /// their Python defaults (Country/Age 18) or are omitted (middle name,
    /// address line 2, EEO answers).
    public static func profileText(_ p: Profile) -> String {
        var lines = [
            "Name: \(p.fullName)",
            "Email: \(p.email)",
            "Phone: \(p.phone)",
            "Location: \(p.location)",
        ]
        if !p.streetAddress.isEmpty {
            lines.append("Street Address (Address Line 1): \(p.streetAddress)")
        }
        if !p.city.isEmpty { lines.append("City: \(p.city)") }
        if !p.state.isEmpty { lines.append("State / Province / Region: \(p.state)") }
        if !p.zipCode.isEmpty { lines.append("Zip Code (Postal Code): \(p.zipCode)") }
        lines.append("Country: United States")
        if !p.linkedin.isEmpty { lines.append("LinkedIn: \(p.linkedin)") }
        if !p.github.isEmpty { lines.append("GitHub: \(p.github)") }
        if !p.portfolio.isEmpty { lines.append("Portfolio: \(p.portfolio)") }
        lines.append("Work Authorization: \(p.workAuthorization)")
        lines.append("Sponsorship Required: \(p.sponsorshipRequired)")
        lines.append("Age 18 or Older: Yes")
        if !p.noticePeriod.isEmpty { lines.append("Notice Period: \(p.noticePeriod)") }
        if !p.availableStart.isEmpty { lines.append("Earliest Start Date: \(p.availableStart)") }
        if !p.desiredSalary.isEmpty { lines.append("Desired Salary: \(p.desiredSalary)") }
        if !p.skills.isEmpty { lines.append("Skills: \(p.skills.joined(separator: ", "))") }
        if !p.summary.isEmpty { lines.append("Summary: \(p.summary)") }
        for exp in p.experience {
            lines.append("Experience: \(exp.title) at \(exp.company) "
                         + "(\(exp.startDate) – \(exp.endDate))")
            for b in exp.bullets.prefix(3) {
                lines.append("  • \(b)")
            }
        }
        for edu in p.education {
            lines.append("Education: \(edu.degree), \(edu.school) (\(edu.year))")
        }
        if !p.certifications.isEmpty {
            lines.append("Certifications: \(p.certifications.joined(separator: ", "))")
        }
        return lines.joined(separator: "\n")
    }

    // ------------------------------------------------------------------
    // JSON extraction — port of _extract_json / _trim_trailing_prose
    // ------------------------------------------------------------------

    /// Strip markdown fences and parse JSON from LLM output. Leading prose
    /// before the first [ or { is trimmed; trailing prose after the matching
    /// close bracket is trimmed with a string-aware depth counter. Falls back
    /// to a Python-literal fixup (single quotes, True/False/None) mirroring
    /// the desktop ast.literal_eval rescue. Returns nil when unparseable.
    public static func extractJSON(_ raw: String) -> Any? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)

        // Strip markdown code fences.
        if let r = text.range(of: "```json") {
            text = String(text[r.upperBound...])
            if let end = text.range(of: "```") { text = String(text[..<end.lowerBound]) }
            text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        } else if let r = text.range(of: "```") {
            text = String(text[r.upperBound...])
            if let end = text.range(of: "```") { text = String(text[..<end.lowerBound]) }
            text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        }

        // Trim any leading non-JSON prose before the first [ or {. Only when
        // the text does not already start with an opener — searching for {
        // inside [{"k":…}] would truncate incorrectly.
        if let first = text.first, first != "[", first != "{" {
            if let idx = text.firstIndex(of: "[") {
                text = String(text[idx...])
            } else if let idx = text.firstIndex(of: "{") {
                text = String(text[idx...])
            }
        }

        text = trimTrailingProse(text)

        if let data = text.data(using: .utf8),
           let parsed = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) {
            return parsed
        }

        // Python-style literal rescue (single quotes / True / False / None).
        var fixed = text.replacingOccurrences(of: "'", with: "\"")
        fixed = Rx.replaceAll(#"\bTrue\b"#, in: fixed, with: "true")
        fixed = Rx.replaceAll(#"\bFalse\b"#, in: fixed, with: "false")
        fixed = Rx.replaceAll(#"\bNone\b"#, in: fixed, with: "null")
        if let data = fixed.data(using: .utf8),
           let parsed = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]),
           parsed is [Any] || parsed is [String: Any] {
            return parsed
        }
        return nil
    }

    /// Return the shortest prefix of `text` that contains a complete
    /// top-level JSON array or object, discarding characters after the
    /// matching close bracket. Skips string literals so brackets inside
    /// quoted values don't affect the depth count.
    public static func trimTrailingProse(_ text: String) -> String {
        guard let opener = text.first else { return text }
        let closer: Character
        if opener == "[" {
            closer = "]"
        } else if opener == "{" {
            closer = "}"
        } else {
            return text  // No JSON opener found — return as-is.
        }

        var depth = 0
        var inString = false
        var escapeNext = false
        for (i, ch) in text.enumerated() {
            if escapeNext {
                escapeNext = false
                continue
            }
            if ch == "\\" && inString {
                escapeNext = true
                continue
            }
            if ch == "\"" {
                inString.toggle()
                continue
            }
            if inString { continue }
            if ch == opener {
                depth += 1
            } else if ch == closer {
                depth -= 1
                if depth == 0 {
                    let end = text.index(text.startIndex, offsetBy: i + 1)
                    return String(text[..<end])
                }
            }
        }
        // Unbalanced — return original so the caller's parser fails clearly.
        return text
    }
}
