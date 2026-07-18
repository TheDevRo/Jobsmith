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

public enum SettingsSync {

    /// A user-facing sync toggle, mirroring `settings_registry.CATEGORIES`.
    /// Each maps to a `jobsmith.sync.settings.<key>` UserDefaults bool (device-
    /// local). A category syncs on this device only when its toggle is on here;
    /// two devices share a category only if both enable it.
    public struct Category {
        public let key: String
        public let label: String
        public let defaultOn: Bool
        public init(key: String, label: String, defaultOn: Bool) {
            self.key = key; self.label = label; self.defaultOn = defaultOn
        }
    }

    /// Keep in lockstep with settings_registry.CATEGORIES (order + keys + defaults).
    /// `profile` defaults ON to preserve today's behavior (profile already syncs);
    /// it gates the EXISTING profile bridge, not a `registry` row below.
    public static let categories: [Category] = [
        Category(key: "profile", label: "Profile", defaultOn: true),
        Category(key: "inbox", label: "Inbox", defaultOn: true),
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
        // inbox (the swipe deck): the standing pay gate + the deck sort order.
        // Both live on `SearchConfig` on iOS but gate on the `inbox` category, so
        // the Postings toggle and the Inbox toggle stay independent.
        .init("inbox.require_stated_pay", category: "inbox", ios: "search.requireStatedPay"),
        .init("inbox.sort", category: "inbox", ios: "search.inboxSort",
              note: "ENUM: best_bets/best_match/newest/salary/company. Unknown values are ignored on apply."),
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

    // MARK: - lookups + parity

    /// The `ios` config keypath that routes a tier to Apple's on-device model.
    /// A tier holding this sentinel is NEVER exported (it's meaningless off-device).
    static let onDeviceModelSentinel = "apple-on-device"

    /// Sorted canonical ids of every registry row (the `prompts.*` wildcard
    /// verbatim). The registry-parity test asserts this equals the Python
    /// `settings_registry.syncable_canonical_ids()`.
    static func canonicalIDs() -> [String] { registry.map(\.canonical).sorted() }

    private static let byCanonical: [String: Entry] = {
        var out: [String: Entry] = [:]
        for e in registry { out[e.canonical] = e }
        return out
    }()

    /// The gating category for a concrete canonical path, or nil when it is not a
    /// registry row (nil ⇒ never applied — the allowlist is the only switch).
    static func category(for path: String) -> String? {
        if let e = byCanonical[path] { return e.category }
        if path.hasPrefix("prompts.") { return "prompts" }   // prompts.* expansion
        return nil
    }

    /// Allowed values for `inbox.sort` (canonical JobSort). An import carrying
    /// anything else is ignored, exactly as a desktop ENUM setting normalizes an
    /// unrecognized value rather than writing it.
    static let inboxSortValues: Set<String> = ["best_bets", "best_match", "newest", "salary", "company"]

    private static let legacyResumeStyles = ["standard": "ledger", "modern": "ledger", "minimal": "swiss"]

    private static func normalizeEnum(_ path: String, _ value: JSONValue) -> JSONValue {
        guard path == "application_honesty.resume_style", case .string(let s) = value else { return value }
        let low = s.lowercased()
        return .string(legacyResumeStyles[low] ?? low)
    }

    // MARK: - nested [String: JSONValue] access (iOS-native camelCase config)

    private static func getIOS(_ config: [String: JSONValue], _ dotted: String) -> JSONValue? {
        var node = JSONValue.object(config)
        for part in dotted.split(separator: ".") {
            guard case .object(let o) = node, let next = o[String(part)] else { return nil }
            node = next
        }
        return node
    }

    private static func setIOS(_ config: inout [String: JSONValue], _ dotted: String, _ value: JSONValue) {
        let parts = dotted.split(separator: ".").map(String.init)
        func recurse(_ node: inout [String: JSONValue], _ i: Int) {
            if i == parts.count - 1 { node[parts[i]] = value; return }
            var child: [String: JSONValue] = {
                if case .object(let o)? = node[parts[i]] { return o }
                return [:]
            }()
            recurse(&child, i + 1)
            node[parts[i]] = .object(child)
        }
        recurse(&config, 0)
    }

    // MARK: - enabled_sources fold (canonicalization rule #3)

    /// iOS `search.enabledSources` (a Set encoded as an array) plus the separate
    /// `search.linkedInEnabled` flag -> canonical SORTED list of enabled ids
    /// (linkedin folded in). Sorted so the snapshot hash is stable.
    static func foldEnabledSources(_ config: [String: JSONValue]) -> [String] {
        var ids = Set<String>()
        if case .array(let arr)? = getIOS(config, "search.enabledSources") {
            for v in arr { if case .string(let s) = v { ids.insert(s) } }
        }
        if case .bool(let on)? = getIOS(config, "search.linkedInEnabled") {
            if on { ids.insert("linkedin") } else { ids.remove("linkedin") }
        }
        return ids.sorted()
    }

    /// Inverse: canonical list -> iOS `enabledSources` (minus linkedin) plus
    /// `linkedInEnabled`.
    static func unfoldEnabledSources(_ config: inout [String: JSONValue], _ value: JSONValue) {
        guard case .array(let arr) = value else { return }
        var ids = Set<String>()
        for v in arr { if case .string(let s) = v { ids.insert(s) } }
        let linkedIn = ids.contains("linkedin")
        ids.remove("linkedin")
        setIOS(&config, "search.enabledSources", .array(ids.sorted().map { .string($0) }))
        setIOS(&config, "search.linkedInEnabled", .bool(linkedIn))
    }

    // MARK: - export / apply  (iOS-native config dict <-> canonical settings)

    /// Read the modeled settings out of the iOS-native config dict (camelCase,
    /// nested: honesty/search/ai/promptOverrides), keyed by canonical path, as
    /// `{path: {"value": JSONValue}}`. Only categories in `enabled` are emitted;
    /// folds enabledSources, expands promptOverrides, and never exports a model
    /// tier holding the on-device sentinel (or an empty slot).
    static func export(_ config: [String: JSONValue], enabled: Set<String>) -> [String: [String: JSONValue]] {
        var out: [String: [String: JSONValue]] = [:]
        for e in registry where enabled.contains(e.category) {
            if e.canonical == "prompts.*" {
                if case .object(let prompts)? = getIOS(config, "promptOverrides") {
                    for (pid, val) in prompts { out["prompts.\(pid)"] = ["value": val] }
                }
                continue
            }
            if e.canonical == "search.enabled_sources" {
                out[e.canonical] = ["value": .array(foldEnabledSources(config).map { .string($0) })]
                continue
            }
            guard let iosPath = e.ios, let raw = getIOS(config, iosPath) else { continue }
            // Never let a tier routed to Apple's on-device model (or an empty
            // fallback slot) land on another device.
            if e.canonical.hasPrefix("ai.models.") {
                if case .string(let s) = raw, s == onDeviceModelSentinel || s.isEmpty { continue }
            }
            out[e.canonical] = ["value": normalizeEnum(e.canonical, raw)]
        }
        return out
    }

    /// Apply one imported setting into the iOS-native config dict in place. A
    /// canonical path this device doesn't model (ios == nil, e.g. pipeline.*) is a
    /// no-op here — the engine simply never tombstones it, so it survives a
    /// round-trip through the phone untouched (base-overlay). Rejects any path not
    /// in `registry`; normalizes legacy resume styles.
    static func apply(_ config: inout [String: JSONValue], path: String, value: JSONValue) {
        guard category(for: path) != nil else { return }
        if path.hasPrefix("prompts.") {
            let pid = String(path.dropFirst("prompts.".count))
            setIOS(&config, "promptOverrides.\(pid)", value)
            return
        }
        if path == "search.enabled_sources" {
            unfoldEnabledSources(&config, value)
            return
        }
        guard let e = byCanonical[path], let iosPath = e.ios else { return }  // unmodeled: base-overlay
        // ENUM guard: an out-of-vocabulary inbox sort is ignored, never written.
        if path == "inbox.sort" {
            guard case .string(let s) = value, inboxSortValues.contains(s) else { return }
        }
        // A tier the user routed on-device is pinned device-local. The
        // sentinel is never exported, so the folder only ever holds another
        // device's endpoint model for this path — applying it would silently
        // undo the on-device choice on every import (the reported bug: models
        // set to on-device kept reverting to the desktop's cloud model).
        if isDeviceLocal(path, config: config) { return }
        setIOS(&config, iosPath, normalizeEnum(path, value))
    }

    /// True when `path` is pinned device-local right now: a model tier whose
    /// local value is the on-device sentinel. Such a path is skipped on export
    /// (already), must not be overwritten by imports, and must not be
    /// tombstoned (the other device's endpoint model remains its own business).
    static func isDeviceLocal(_ path: String, config: [String: JSONValue]) -> Bool {
        guard path.hasPrefix("ai.models."),
              let e = byCanonical[path], let iosPath = e.ios,
              case .string(let current)? = getIOS(config, iosPath) else { return false }
        return current == onDeviceModelSentinel
    }

    /// True when this device models `path` in its config (so the engine tracks it
    /// in the snapshot and may tombstone it). Unmodeled paths are never emitted.
    static func isModeled(_ path: String) -> Bool {
        if path.hasPrefix("prompts.") { return true }
        return byCanonical[path]?.ios != nil
    }

    static func removePrompt(_ config: inout [String: JSONValue], _ path: String) {
        guard path.hasPrefix("prompts.") else { return }
        let pid = String(path.dropFirst("prompts.".count))
        if case .object(var prompts)? = getIOS(config, "promptOverrides") {
            prompts.removeValue(forKey: pid)
            setIOS(&config, "promptOverrides", .object(prompts))
        }
    }
}
