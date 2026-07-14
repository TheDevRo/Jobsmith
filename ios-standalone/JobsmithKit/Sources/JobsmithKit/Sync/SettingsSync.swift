import Foundation

// Canonical settings-sync bridge (iOS side).
//
// MIRROR of backend/sync/settings_registry.py. The canonical vocabulary is the
// desktop config.yaml shape (snake_case). This file maps that to/from the iOS
// `AppConfig` (camelCase) for the keys iOS models, and preserves everything it
// does not model via base-overlay — exactly like SyncEntities.profile*.
//
// STATUS: SCAFFOLD. The `Registry` table is the authoritative design artifact
// and must stay byte-for-byte aligned with settings_registry.py (a cross-
// language conformance test enforces this — see SETTINGS_SYNC_PLAN.md Phase 3).
// The read/write plumbing is stubbed where it needs SyncEngine wiring (Phase 2).

enum SettingsSync {

    /// A user-facing sync toggle, mirroring `settings_registry.CATEGORIES`.
    /// Each maps to a `jobsmith.sync.settings.<key>` UserDefaults bool (device-
    /// local). A category syncs on this device only when its toggle is on here;
    /// two devices share a category only if both enable it.
    struct Category {
        let key: String
        let label: String
        let defaultOn: Bool
    }

    /// Keep in lockstep with settings_registry.CATEGORIES (order + keys + defaults).
    /// `profile` defaults ON to preserve today's behavior (profile already syncs);
    /// it gates the EXISTING profile bridge, not a `registry` row below. There is
    /// deliberately no "AI Connection" category — that whole screen is device-local.
    static let categories: [Category] = [
        Category(key: "profile", label: "Profile", defaultOn: true),
        Category(key: "postings", label: "Postings (Search & sources)", defaultOn: false),
        Category(key: "documents", label: "Documents (resume & honesty)", defaultOn: false),
        Category(key: "ai_connection", label: "AI Connection", defaultOn: false),
        Category(key: "pipeline", label: "Pipeline & ranking", defaultOn: false),
        Category(key: "auto_apply", label: "Auto-apply rules", defaultOn: false),
        Category(key: "salary", label: "Salary estimator", defaultOn: false),
        Category(key: "prompts", label: "AI prompt overrides", defaultOn: false),
        Category(key: "general", label: "General", defaultOn: false),
    ]

    /// One syncable setting. `ios` is nil for keys the phone doesn't model
    /// (e.g. pipeline.*, auto_apply.*) — those still round-trip via base-overlay.
    /// `category` selects the toggle that gates it (must be a `categories` key).
    struct Entry {
        let canonical: String        // dotted path in canonical (config.yaml) vocab = the sync id
        let category: String         // gating toggle key
        let ios: String?             // AppConfig keypath (dotted camelCase), nil if not modeled
        let note: String
        init(_ canonical: String, category: String, ios: String? = nil, note: String = "") {
            self.canonical = canonical; self.category = category; self.ios = ios; self.note = note
        }
    }

    /// SYNC rows only. Keep in lockstep with settings_registry.syncable().
    /// Secrets/local keys are NOT listed here — they are excluded by omission
    /// (never emitted) exactly as on the desktop.
    static let registry: [Entry] = [
        // documents / honesty
        .init("application_honesty.honesty_level", category: "documents", ios: "honesty.level"),
        .init("application_honesty.cover_letter_tone", category: "documents", ios: "honesty.coverLetterTone"),
        .init("application_honesty.resume_style", category: "documents", ios: "honesty.resumeStyle",
              note: "Normalize legacy aliases on read: standard/modern->ledger, minimal->swiss (AppConfig.swift:266)."),
        .init("application_honesty.resume_accent", category: "documents", ios: "honesty.resumeAccent"),
        .init("application_honesty.document_format", category: "documents", ios: "honesty.documentFormat"),
        .init("application_honesty.max_resume_experience_entries", category: "documents", ios: "honesty.maxResumeExperienceEntries",
              note: "Preserve explicit null vs absent."),
        .init("application_honesty.ai_edit_model_tier", category: "documents", ios: "honesty.aiEditTier"),
        // postings (search & sources)
        .init("search.keywords", category: "postings", ios: "search.keywords"),
        .init("search.locations", category: "postings", ios: "search.locations"),
        .init("search.exclude_keywords", category: "postings", ios: "search.excludeKeywords"),
        .init("search.min_salary", category: "postings", ios: "search.minSalary"),
        .init("search.max_age_days", category: "postings", ios: "search.maxAgeDays"),
        .init("search.greenhouse_boards", category: "postings", ios: "search.greenhouseBoards"),
        .init("search.lever_companies", category: "postings", ios: "search.leverCompanies"),
        .init("search.ashby_boards", category: "postings", ios: "search.ashbyBoards"),
        .init("search.workable_accounts", category: "postings", ios: "search.workableAccounts"),
        .init("search.recruitee_companies", category: "postings", ios: "search.recruiteeCompanies"),
        .init("search.enabled_sources", category: "postings", ios: "search.enabledSources",
              note: "Canonical = SORTED [String]. Fold iOS Set<String> + linkedInEnabled ('linkedin' member) in/out. Sort before hashing."),
        // AI Connection — endpoint + api key + model tiers + gen params.
        .init("ai.base_url", category: "ai_connection", ios: "ai.baseURL"),
        .init("ai.api_key", category: "ai_connection", ios: "ai.apiKey",
              note: "Synced by user decision (travels in the user-owned folder). iOS keeps it in the config JSON already; unlike li_at it is NOT keychain-only."),
        .init("ai.models.strong", category: "ai_connection", ios: "ai.strongModel",
              note: "Value = model-id string. Do NOT export when it equals the 'apple-on-device' sentinel — skip the row so it never lands on desktop."),
        .init("ai.models.fast", category: "ai_connection", ios: "ai.fastModel",
              note: "Same 'apple-on-device' skip rule as ai.models.strong."),
        .init("ai.models.utility", category: "ai_connection", ios: "ai.utilityModel",
              note: "Same 'apple-on-device' skip rule."),
        .init("ai.temperature", category: "ai_connection", ios: "ai.temperature"),
        .init("ai.max_tokens", category: "ai_connection", ios: "ai.maxTokens"),
        .init("ai.context_window", category: "ai_connection",
              note: "Desktop-only; rides through the phone untouched via base-overlay."),
        // Keys iOS does NOT model — imported into config only via base-overlay,
        // never emitted by iOS (it has nothing to write). Listed so the conformance
        // test confirms iOS tolerates receiving them.
        .init("pipeline.ghost_after_days", category: "pipeline"),
        .init("pipeline.skip_already_applied", category: "pipeline"),
        .init("pipeline.digest_weights", category: "pipeline"),
        .init("auto_apply.enabled", category: "auto_apply"),
        .init("auto_apply.auto_approve", category: "auto_apply"),
        .init("auto_apply.mode", category: "auto_apply"),
        .init("auto_apply.max_daily_applications", category: "auto_apply"),
        .init("auto_apply.per_domain_rate_limit", category: "auto_apply"),
        .init("auto_apply.submit_whitelist", category: "auto_apply"),
        .init("auto_apply.review_required_rules", category: "auto_apply"),
        .init("salary_estimator.enabled", category: "salary"),
        .init("salary_estimator.auto_on_ingest", category: "salary"),
        .init("salary_estimator.market_compare_on_score", category: "salary"),
        .init("salary_estimator.model_tier", category: "salary"),
        .init("assist.notification_sound", category: "general"),
        .init("prompts.*", category: "prompts", ios: "promptOverrides",
              note: "Wildcard: one record per prompt id (id = prompts.<id>) for per-prompt LWW."),
    ]

    // MARK: - AppConfig <-> {canonical_path: JSONValue} bridge  (STUB, Phase 2)

    /// Read the settings this device owns out of `config`, keyed by canonical
    /// path, as `{path: {"value": JSONValue}}`. Fold enabledSources -> sorted
    /// list; expand promptOverrides -> one record per id.
    static func export(_ config: AppConfig) -> [String: [String: JSONValue]] {
        fatalError("Phase 2: read each ios!=nil registry row from AppConfig; fold enabledSources; expand prompts.*")
    }

    /// Apply one imported setting back into `config` (in place), honoring
    /// base-overlay: a canonical path this device doesn't model is written to
    /// nothing here but MUST be preserved in the sync snapshot for re-export.
    /// Reject any path not in `registry`. Normalize enums (legacy resume styles).
    static func apply(_ config: inout AppConfig, path: String, value: JSONValue) {
        fatalError("Phase 2: map canonical path -> AppConfig keypath; unfold enabled_sources; normalize.")
    }
}
