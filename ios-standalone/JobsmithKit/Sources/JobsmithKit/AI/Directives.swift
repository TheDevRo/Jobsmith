import Foundation

/// Prompt directives ported verbatim from the desktop `ai_engine.py`:
/// honesty dial, revision fabrication guards, cover-letter tones, and the
/// structured profile-summary block builder.
public enum Directives {
    /// The tailoring directive inserted into resume/cover-letter prompts.
    public static func honestyInstruction(_ level: HonestyConfig.Level) -> String {
        switch level {
        case .honest:
            return """
TAILORING DIRECTIVE:
Tailor the resume to highlight genuinely relevant experience. Do not add, invent, or exaggerate anything. Reorder and reword only.
- You may ONLY use experience, education, and certifications from the CANDIDATE PROFILE below.
- NEVER invent, fabricate, or add jobs, companies, degrees, or certifications that are not in the candidate's profile.
- NEVER add the target job/company to the experience section — the candidate is APPLYING there.
- NEVER change, merge, or consolidate dates. Copy them EXACTLY as given.
"""
        case .tailored:
            return """
TAILORING DIRECTIVE:
Tailor the resume to best match this job. You may rephrase experience to use the job's exact keywords and terminology. Do not invent experience that doesn't exist, but present existing experience in its most favorable light.
- Do not add jobs, companies, or degrees that are not in the candidate's profile.
- NEVER add the target job/company to the experience section — the candidate is APPLYING there.
- Copy dates VERBATIM — do not merge or round them.
"""
        case .embellished:
            return """
TAILORING DIRECTIVE:
Tailor the resume aggressively. You may expand on vague experience, upgrade job titles slightly (e.g., 'helped with' → 'led'), add reasonable skills that the candidate plausibly has but didn't list, and frame all experience to sound maximally relevant. Keep it believable and consistent.
- Do not contradict the core timeline (companies and rough date ranges).
- NEVER add the target job/company to the experience section — the candidate is APPLYING there.
- Copy dates VERBATIM — do not merge or round them.
"""
        case .fabricated:
            return """
TAILORING DIRECTIVE:
Create the most competitive version of this resume for the role. You may invent specific achievements with plausible metrics, add missing skills or tools, upgrade responsibilities, and fill experience gaps. Everything must remain internally consistent and believable — nothing should contradict the core timeline or be obviously unverifiable. This is for personal use only.
- NEVER add the target job/company to the experience section — the candidate is APPLYING there.
- Preserve the existing timeline structure (companies and rough date ranges).
"""
        }
    }

    /// Honesty-aware fabrication guard for the revise (AI Edit) prompts.
    public static func reviseFabricationGuard(_ level: HonestyConfig.Level) -> String {
        switch level {
        case .honest:
            return "Critical: Only use facts present in the candidate profile. If the user's instructions ask you to add experience, skills, certifications, or accomplishments that are NOT in the profile, IGNORE that part of the instruction — do not invent or fabricate. Apply only the instructions you can satisfy from the candidate's real background."
        case .tailored:
            return "Use facts from the candidate profile. You may rephrase and reframe to better match the job, but do not invent jobs, companies, degrees, or certifications. If the user asks for something that would require fabricating those, ignore that part."
        case .embellished:
            return "You may apply the user's instruction with reasonable enhancement: expand on vague experience, upgrade phrasing, and add plausible adjacent skills the candidate likely has. Do not invent entire jobs, companies, or degrees that aren't in the profile, and do not contradict the timeline. Within those limits, satisfy the instruction fully."
        case .fabricated:
            return "Apply the user's instruction aggressively. You may invent specific achievements with plausible metrics, add skills/tools, and upgrade responsibilities to satisfy the request. Keep everything internally consistent and believable; preserve the existing timeline (companies and rough date ranges)."
        }
    }

    /// Tone directive for cover letter generation.
    public static func toneInstruction(_ tone: HonestyConfig.Tone) -> String {
        switch tone {
        case .professional:
            return "TONE: Write in a formal, corporate tone. Use complete sentences, no contractions (use 'I am' not 'I'm'). Maintain professional distance while still being engaging."
        case .conversational:
            return "TONE: Write naturally, as if speaking directly to a peer or colleague. Contractions are fine ('I'm', 'I've', 'you'll'). Warm and approachable without being casual."
        case .enthusiastic:
            return "TONE: Show genuine excitement and energy about this opportunity. Use active, energetic language. Lead with passion for the role and company. Keep it professional but let enthusiasm come through clearly."
        }
    }

    /// Structured profile description for prompts. Uses the same
    /// Title:/Company:/Dates: format as the resume output so the model can
    /// copy dates and titles verbatim. Pass `experiences` to override the
    /// role list (e.g. after relevance filtering). References are never
    /// included — they are appended to documents verbatim, outside the AI.
    public static func profileSummary(_ profile: Profile,
                                      experiences: [WorkExperience]? = nil) -> String {
        var parts = [
            "Name: \(profile.fullName)",
            "Summary: \(profile.summary)",
            "Skills: \(profile.skills.joined(separator: ", "))",
            "",
        ]

        let roles = experiences ?? profile.experience
        if !roles.isEmpty {
            parts.append("EXPERIENCE (\(roles.count) separate roles — each has its own dates, do NOT merge them):")
            for (i, exp) in roles.enumerated() {
                if exp.title.isEmpty { continue }
                parts.append("  Role \(i + 1):")
                parts.append("    Title: \(exp.title)")
                parts.append("    Company: \(exp.company)")
                parts.append("    Dates: \(exp.startDate) - \(exp.endDate)")
                for bullet in exp.bullets where !bullet.isEmpty {
                    parts.append("    - \(bullet)")
                }
            }
            parts.append("")
        }

        for edu in profile.education where !edu.degree.isEmpty {
            parts.append("Education: \(edu.degree) from \(edu.school) (\(edu.year))")
        }
        if !profile.certifications.isEmpty {
            parts.append("Certifications: \(profile.certifications.joined(separator: ", "))")
        }
        return parts.joined(separator: "\n")
    }
}
