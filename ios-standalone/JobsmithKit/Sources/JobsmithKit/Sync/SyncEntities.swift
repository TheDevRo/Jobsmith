import Foundation

/// Maps between the canonical (snake_case) sync `data` payloads and iOS's
/// native (camelCase) column/model shapes. The canonical form is the desktop's
/// native shape, so this file is the iOS twin of backend/sync/entities.py's
/// column lists plus backend/sync/profile_map.py.
///
/// Pure dictionary transforms over `[String: JSONValue]` so they are unit-
/// testable without GRDB. The GRDB engine (SyncEngine) reads/writes rows as
/// these dictionaries and defers all field naming to here.
///
/// Cross-schema rule: map only the keys this side models; carry everything else
/// verbatim (the write-back invariant). iOS-only keys (triage, salary_estimate,
/// style_preset) travel in canonical snake_case so desktop preserves them.
public enum SyncEntities {

    // MARK: job

    /// canonical(snake) -> iOS(camel). Excludes volatile local columns
    /// (lastSeen/timesSeen), matching the Python job column exclusions.
    public static let jobCanonToIOS: [String: String] = [
        "source": "source", "external_id": "externalId", "title": "title",
        "company": "company", "location": "location", "url": "url",
        "description": "description", "salary_min": "salaryMin",
        "salary_max": "salaryMax", "salary_period": "salaryPeriod",
        "date_posted": "datePosted", "date_discovered": "dateDiscovered",
        "status": "status", "fit_score": "fitScore",
        "fit_reasoning": "fitReasoning", "apply_type": "applyType",
        "is_remote": "isRemote", "is_easy_apply": "isEasyApply",
        "tags": "tags", "match_report": "matchReport",
        "embellishment_log": "embellishmentLog",
        // iOS-only, but synced so it round-trips iOS<->iOS and survives desktop:
        "triage": "triage", "salary_estimate": "salaryEstimate",
    ]

    // MARK: application

    /// canonical(snake) -> iOS(camel). `job_ref`, `resume_doc`, `cover_doc` are
    /// handled by the engine (parent link + content-addressed documents), not
    /// here. Excludes desktop-only outcome/auto_approved/error_message — those
    /// arrive as unknown keys and are preserved verbatim.
    public static let appCanonToIOS: [String: String] = [
        "resume_content": "resumeContent",
        "cover_letter_content": "coverLetterContent",
        "custom_answers": "customAnswers", "status": "status",
        "honesty_level": "honestyLevel", "applied_at": "appliedAt",
        "created_at": "createdAt",
        // iOS-only, synced:
        "style_preset": "stylePreset", "updated_at": "updatedAt",
    ]

    // MARK: profile (mirrors backend/sync/profile_map.py)

    static let profileScalar: [String: String] = [
        "full_name": "fullName", "email": "email", "phone": "phone",
        "location": "location", "street_address": "streetAddress",
        "city": "city", "state": "state", "zip_code": "zipCode",
        "linkedin": "linkedin", "github": "github", "portfolio": "portfolio",
        "desired_salary": "desiredSalary", "work_authorization": "workAuthorization",
        "sponsorship_required": "sponsorshipRequired",
        "available_start": "availableStart", "notice_period": "noticePeriod",
        "summary": "summary",
    ]
    static let profileList: [String: String] = ["skills": "skills", "certifications": "certifications"]
    static let expCanonToIOS: [String: String] = [
        "id": "id", "title": "title", "company": "company",
        "start_date": "startDate", "end_date": "endDate",
        "bullets": "bullets", "pinned": "pinned",
    ]
    static let eduCanonToIOS: [String: String] = ["id": "id", "degree": "degree", "school": "school", "year": "year"]
    static let refCanonToIOS: [String: String] = [
        "id": "id", "name": "name", "position": "position", "email": "email", "phone": "phone",
    ]

    /// ATS-login credentials that must NEVER enter a change record.
    public static let secretKeys: Set<String> = ["workday_email", "workday_password", "ats_login_password"]

    // MARK: generic remap helpers

    private static func remap(_ src: [String: JSONValue], _ map: [String: String]) -> [String: JSONValue] {
        var out: [String: JSONValue] = [:]
        for (from, to) in map where src[from] != nil { out[to] = src[from] }
        return out
    }

    private static func invert(_ map: [String: String]) -> [String: String] {
        var out: [String: String] = [:]
        for (k, v) in map { out[v] = k }
        return out
    }

    private static func remapItems(_ items: [JSONValue], _ map: [String: String]) -> [JSONValue] {
        items.map { item in
            guard let obj = item.objectValue else { return item }
            return .object(remap(obj, map))
        }
    }

    // MARK: job / application dict mapping

    public static func jobCanonicalToIOS(_ canon: [String: JSONValue]) -> [String: JSONValue] {
        remap(canon, jobCanonToIOS)
    }

    /// iOS row -> canonical, overlaid on `base` so keys iOS doesn't model
    /// (desktop-only columns) survive.
    public static func jobIOSToCanonical(_ ios: [String: JSONValue], base: [String: JSONValue] = [:]) -> [String: JSONValue] {
        var out = base
        for (from, to) in invert(jobCanonToIOS) where ios[from] != nil { out[to] = ios[from] }
        return out
    }

    public static func appCanonicalToIOS(_ canon: [String: JSONValue]) -> [String: JSONValue] {
        remap(canon, appCanonToIOS)
    }

    public static func appIOSToCanonical(_ ios: [String: JSONValue], base: [String: JSONValue] = [:]) -> [String: JSONValue] {
        var out = base
        for (from, to) in invert(appCanonToIOS) where ios[from] != nil { out[to] = ios[from] }
        return out
    }

    // MARK: profile mapping (base-overlay, secret exclusion)

    public static func profileCanonicalToIOS(_ canon: [String: JSONValue]) -> [String: JSONValue] {
        var out = remap(canon, profileScalar)
        for (from, to) in profileList where canon[from] != nil { out[to] = canon[from] }
        if case .array(let a)? = canon["experience"] { out["experience"] = .array(remapItems(a, expCanonToIOS)) }
        if case .array(let a)? = canon["education"] { out["education"] = .array(remapItems(a, eduCanonToIOS)) }
        if case .array(let a)? = canon["references"] { out["references"] = .array(remapItems(a, refCanonToIOS)) }
        return out
    }

    /// iOS profile -> canonical, overlaid on `base` to preserve fields iOS
    /// doesn't model (middle_name, EEO block, ...). Never emits secrets.
    public static func profileIOSToCanonical(_ ios: [String: JSONValue], base: [String: JSONValue] = [:]) -> [String: JSONValue] {
        var out = base
        for (canon, iosKey) in profileScalar where ios[iosKey] != nil { out[canon] = ios[iosKey] }
        for (canon, iosKey) in profileList where ios[iosKey] != nil { out[canon] = ios[iosKey] }
        if case .array(let a)? = ios["experience"] { out["experience"] = .array(remapItems(a, invert(expCanonToIOS))) }
        if case .array(let a)? = ios["education"] { out["education"] = .array(remapItems(a, invert(eduCanonToIOS))) }
        if case .array(let a)? = ios["references"] { out["references"] = .array(remapItems(a, invert(refCanonToIOS))) }
        for k in secretKeys { out.removeValue(forKey: k) }  // never sync secrets
        return out
    }
}
