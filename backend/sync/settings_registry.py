#!/usr/bin/env python3
"""Canonical settings registry — the single source of truth for config sync.

This is the heart of the "sync settings across devices" feature. It is an
EXPLICIT ALLOWLIST: only keys listed here with class SYNC ever leave a device.
Everything else (secrets, machine-local values) is excluded by omission, not by
a filter that can be forgotten.

The canonical vocabulary is the desktop `config.yaml` shape (snake_case, nested)
because that is already the sync wire format (see profile_map.py — the desktop's
native shape IS canonical). iOS/extension mappers translate to/from this.

STATUS: SCAFFOLD. The REGISTRY table below is the finished design artifact and
should be treated as authoritative. The read/write plumbing at the bottom
(`export_settings` / `apply_setting`) is stubbed where it needs engine wiring —
see SETTINGS_SYNC_PLAN.md, Phase 1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class Cls(str, Enum):
    SYNC = "sync"       # travels between devices (per-key LWW) — IF its category toggle is on
    SECRET = "secret"   # NEVER leaves a device (API keys, creds, cookies, tokens)
    LOCAL = "local"     # NEVER syncs — meaningful only on the device it lives on


@dataclass(frozen=True)
class Category:
    """A user-facing sync toggle. Mirrors how the settings screens are grouped
    today, so the toggle list reads exactly like the Settings UI. Each maps to a
    `sync.settings.<key>` bool. A category syncs on a device only when its toggle
    is on THERE — the rule is symmetric, so two devices share a category only if
    both enable it (documented in SETTINGS_SYNC_PLAN.md)."""
    key: str            # config key under `sync.settings.*` and UserDefaults suffix on iOS
    label: str          # UI label — match the existing Settings screen wording
    default: bool       # default state on a fresh install
    note: str = ""


# The toggle list. `profile` defaults ON to preserve today's behavior (profile
# already syncs unconditionally); every NEW category is opt-in / OFF, as asked.
# `ai_connection` syncs the endpoint + model choices + the API KEY so a user who
# runs the app off the inference box doesn't re-enter any of it. The key travels
# in the user-owned folder but is still masked in the HTTP /api/config response
# (api_masked) so an off-machine extension caller can't read it.
CATEGORIES: tuple[Category, ...] = (
    Category("profile", "Profile", True,
             note="Handled by the existing profile bridge (profile/me), now GATED by this "
                  "toggle. ON by default = no regression; turning it OFF is a new capability."),
    Category("inbox", "Inbox", True,
             note="Job postings, triage decisions, and the Inbox filters (stated-pay gate + "
                  "sort). ON by default so the job/triage entities keep syncing exactly as they "
                  "did before this toggle existed; turning it OFF keeps the phone's and desktop's "
                  "feeds independent."),
    Category("postings", "Postings (Search & sources)", False),
    Category("documents", "Documents (resume & honesty)", False),
    Category("ai_connection", "AI Connection", False,
             note="Endpoint + model tiers + generation params. The api_key is NOT included "
                  "(stays SECRET). Assumes the synced devices reach the SAME inference server."),
    Category("pipeline", "Pipeline & ranking", False),
    Category("auto_apply", "Auto-apply rules", False),
    Category("salary", "Salary estimator", False),
    Category("prompts", "AI prompt overrides", False),
    Category("general", "General", False, note="Misc UX prefs, e.g. notification sound."),
)
CATEGORY_KEYS = frozenset(c.key for c in CATEGORIES)


class Kind(str, Enum):
    """How the value is (de)serialized / merged. Nulls are always significant:
    an explicit `null` ('no limit') must survive a round-trip distinct from an
    absent key ('older client that doesn't model this')."""
    STRING = "string"
    ENUM = "enum"                 # constrained string; see `enum_values`
    INT = "int"
    INT_NULLABLE = "int?"         # explicit null carries meaning
    BOOL = "bool"
    LIST_STR = "list[str]"        # order-insensitive; canonicalize sorted before hashing
    DICT = "dict"                 # small nested object (e.g. digest_weights)


@dataclass(frozen=True)
class Setting:
    canonical: str                 # dotted path in canonical (config.yaml) vocab — the sync id
    cls: Cls
    kind: Kind
    category: str                  # UI grouping + future per-category toggles
    ios: Optional[str] = None      # AppConfig keypath (camelCase dotted), None if iOS doesn't model it
    ext: Optional[str] = None      # extension storage.local key, None if N/A
    enum_values: tuple[str, ...] = ()
    api_masked: bool = False       # mask in the HTTP /api/config response to off-machine callers,
                                   # INDEPENDENT of whether it syncs through the (user-owned) folder.
                                   # A SYNC key can still be api_masked (e.g. ai.api_key).
    note: str = ""                 # gotchas / normalization rules the implementer MUST honor


# ---------------------------------------------------------------------------
# THE REGISTRY.  Add a row to make a key syncable; there is no other switch.
# `cls=SECRET` / `cls=LOCAL` rows are documented here ON PURPOSE so the exclusion
# is auditable and a reviewer can see nothing was forgotten. Only SYNC rows are
# ever exported (see `syncable()`).
# ---------------------------------------------------------------------------
REGISTRY: tuple[Setting, ...] = (
    # -- Documents / honesty (application_honesty.*) — all SYNC, high value ---
    Setting("application_honesty.honesty_level", Cls.SYNC, Kind.ENUM, "documents",
            ios="honesty.level",
            enum_values=("honest", "tailored", "embellished", "fabricated")),
    Setting("application_honesty.cover_letter_tone", Cls.SYNC, Kind.ENUM, "documents",
            ios="honesty.coverLetterTone",
            enum_values=("professional", "conversational", "enthusiastic")),
    Setting("application_honesty.resume_style", Cls.SYNC, Kind.ENUM, "documents",
            ios="honesty.resumeStyle",
            enum_values=("executive", "ledger", "banner", "compact", "swiss"),
            note="Normalize legacy aliases on READ (standard/modern->ledger, minimal->swiss) "
                 "on both sides. See app_state.LEGACY_RESUME_STYLES and AppConfig.swift:266."),
    Setting("application_honesty.resume_accent", Cls.SYNC, Kind.ENUM, "documents",
            ios="honesty.resumeAccent",
            enum_values=("default", "navy", "burgundy", "forest", "plum", "charcoal")),
    Setting("application_honesty.document_format", Cls.SYNC, Kind.ENUM, "documents",
            ios="honesty.documentFormat", enum_values=("docx", "pdf")),
    Setting("application_honesty.max_resume_experience_entries", Cls.SYNC, Kind.INT_NULLABLE,
            "documents", ios="honesty.maxResumeExperienceEntries",
            note="null = 'include all'. Preserve explicit null vs absent."),
    Setting("application_honesty.ai_edit_model_tier", Cls.SYNC, Kind.ENUM, "documents",
            ios="honesty.aiEditTier", enum_values=("fast", "strong"),
            note="Desktop frontend ALSO keeps localStorage 'aiEditModelTier' as a session "
                 "override layered over this global. Sync the global; leave the override local."),

    # -- Search criteria (search.*) — SYNC -----------------------------------
    Setting("search.keywords", Cls.SYNC, Kind.LIST_STR, "postings", ios="search.keywords"),
    Setting("search.locations", Cls.SYNC, Kind.LIST_STR, "postings", ios="search.locations"),
    Setting("search.exclude_keywords", Cls.SYNC, Kind.LIST_STR, "postings", ios="search.excludeKeywords"),
    Setting("search.min_salary", Cls.SYNC, Kind.INT_NULLABLE, "postings", ios="search.minSalary"),
    Setting("search.max_age_days", Cls.SYNC, Kind.INT_NULLABLE, "postings", ios="search.maxAgeDays",
            note="iOS distinguishes explicit null ('no limit') from absent (AppConfig.swift:98)."),
    Setting("search.greenhouse_boards", Cls.SYNC, Kind.LIST_STR, "postings", ios="search.greenhouseBoards"),
    Setting("search.lever_companies", Cls.SYNC, Kind.LIST_STR, "postings", ios="search.leverCompanies"),
    Setting("search.ashby_boards", Cls.SYNC, Kind.LIST_STR, "postings", ios="search.ashbyBoards"),
    Setting("search.workable_accounts", Cls.SYNC, Kind.LIST_STR, "postings", ios="search.workableAccounts"),
    Setting("search.recruitee_companies", Cls.SYNC, Kind.LIST_STR, "postings", ios="search.recruiteeCompanies"),
    # Source enablement: desktop nests per-source `enabled` bools; iOS has a flat
    # Set<String> `enabledSources` + a separate `linkedInEnabled`. The canonical
    # form is a SORTED list of enabled source ids. Requires a real bidirectional
    # transform on BOTH sides (not a rename) — see note.
    Setting("search.enabled_sources", Cls.SYNC, Kind.LIST_STR, "postings", ios="search.enabledSources",
            note="CANONICAL = sorted list of enabled source ids. Desktop: fold/unfold the "
                 "per-source `enabled` bools (indeed.enabled, etc.). iOS: it's already a Set — "
                 "sort to a stable array. iOS `linkedInEnabled` folds in as the 'linkedin' member. "
                 "Sort before hashing or the snapshot re-emits every cycle."),

    # -- Inbox filters (inbox.*) — SYNC. The job/triage ENTITIES are gated by the
    #    same `inbox` category in the engine; these two rows carry the standing
    #    Inbox display prefs. iOS reference: JobListFilter.applyPayFilter +
    #    JobFilters.statedAnnualPay + JobSort. ------------------------------------
    Setting("inbox.require_stated_pay", Cls.SYNC, Kind.BOOL, "inbox", ios="search.requireStatedPay",
            note="Companion to search.min_salary: hide jobs with no stated pay / unknown pay "
                 "period. Ignored without a floor (see statedAnnualPay). Default false. "
                 "iOS carries it on SearchConfig but gates on the `inbox` category."),
    Setting("inbox.sort", Cls.SYNC, Kind.ENUM, "inbox", ios="search.inboxSort",
            enum_values=("best_bets", "best_match", "newest", "salary", "company"),
            note="Inbox deck ordering. Default best_match. Desktop maps best_bets->fit_score desc "
                 "(documented approximation — iOS best-bets consults device-local reply data)."),

    # -- Pipeline ranking (pipeline.*) — SYNC (desktop-only concept today) ----
    Setting("pipeline.ghost_after_days", Cls.SYNC, Kind.INT, "pipeline",
            note="iOS does not model pipeline; base-overlay preserves it on the phone untouched."),
    Setting("pipeline.skip_already_applied", Cls.SYNC, Kind.BOOL, "pipeline"),
    Setting("pipeline.digest_weights", Cls.SYNC, Kind.DICT, "pipeline",
            note="Small fixed-key float dict {fit,freshness,salary,effort,conversion}. LWW whole dict."),

    # -- Auto-apply POLICY (auto_apply.*) — SYNC the policy, never the toggles
    #    that depend on a local browser install (headless / use_browser_use). ---
    Setting("auto_apply.enabled", Cls.SYNC, Kind.BOOL, "auto_apply"),
    Setting("auto_apply.mode", Cls.SYNC, Kind.ENUM, "auto_apply", enum_values=("autofill", "submit")),
    Setting("auto_apply.max_daily_applications", Cls.SYNC, Kind.INT, "auto_apply"),
    Setting("auto_apply.per_domain_rate_limit", Cls.SYNC, Kind.INT, "auto_apply"),
    Setting("auto_apply.submit_whitelist", Cls.SYNC, Kind.LIST_STR, "auto_apply"),
    Setting("auto_apply.review_required_rules", Cls.SYNC, Kind.DICT, "auto_apply",
            note="{unknown_ats: bool, min_confidence: float}."),

    # -- Salary estimator (salary_estimator.*) — SYNC minus the BLS api key ---
    Setting("salary_estimator.enabled", Cls.SYNC, Kind.BOOL, "salary"),
    Setting("salary_estimator.auto_on_ingest", Cls.SYNC, Kind.BOOL, "salary"),
    Setting("salary_estimator.market_compare_on_score", Cls.SYNC, Kind.BOOL, "salary"),
    Setting("salary_estimator.model_tier", Cls.SYNC, Kind.ENUM, "salary", enum_values=("fast", "strong", "utility")),

    # -- AI Connection (ai.*) — SYNC endpoint + models + gen params, NOT the key.
    #    Assumes synced devices reach the SAME inference server. ----------------
    Setting("ai.base_url", Cls.SYNC, Kind.STRING, "ai_connection", ios="ai.baseURL",
            note="Endpoint URL. A LAN address (e.g. 192.168.x) only resolves where reachable — "
                 "that's the user's call, hence its own toggle."),
    Setting("ai.api_key", Cls.SYNC, Kind.STRING, "ai_connection", ios="ai.apiKey", api_masked=True,
            note="SYNCED by user decision — travels in the user-owned folder with the rest of the "
                 "AI Connection group. STILL api_masked=True: never leak it in the HTTP /api/config "
                 "response to an off-machine extension caller. Two different surfaces; don't conflate."),
    Setting("ai.models.strong", Cls.SYNC, Kind.STRING, "ai_connection", ios="ai.strongModel",
            note="Canonical value = the model-id STRING. Desktop stores it nested at "
                 "ai.models.strong.model (object may hold future per-tier params) — apply must "
                 "write .model via base-overlay so sibling keys survive. iOS: flat ai.strongModel."),
    Setting("ai.models.fast", Cls.SYNC, Kind.STRING, "ai_connection", ios="ai.fastModel",
            note="See ai.models.strong. iOS may hold the 'apple-on-device' sentinel here — iOS "
                 "MUST NOT export that value (skip the row) so it never lands on desktop."),
    Setting("ai.models.utility", Cls.SYNC, Kind.STRING, "ai_connection", ios="ai.utilityModel",
            note="See ai.models.fast (same 'apple-on-device' skip rule)."),
    Setting("ai.temperature", Cls.SYNC, Kind.STRING, "ai_connection", ios="ai.temperature"),
    Setting("ai.max_tokens", Cls.SYNC, Kind.INT, "ai_connection", ios="ai.maxTokens",
            note="Depends on the target model's limits — coherent only when devices share a server."),
    Setting("ai.context_window", Cls.SYNC, Kind.INT, "ai_connection",
            note="Desktop-only (iOS doesn't model it); rides via base-overlay untouched on the phone."),

    # -- General UX prefs ----------------------------------------------------
    Setting("assist.notification_sound", Cls.SYNC, Kind.BOOL, "general"),

    # -- Prompt overrides (prompts.*) — SYNC. Free-form dict of template strings.
    #    Both platforms model this (desktop cfg['prompts'], iOS promptOverrides).
    #    Sync as ONE record per prompt id so two devices editing different prompts
    #    don't clobber (id = "prompts.<promptId>"). See Phase 1 note.
    Setting("prompts.*", Cls.SYNC, Kind.STRING, "prompts", ios="promptOverrides",
            note="WILDCARD: expand at runtime to one setting record per prompt id present in "
                 "cfg['prompts'] / promptOverrides. Do NOT sync as one blob (per-prompt LWW)."),

    # =======================================================================
    #  EXCLUDED — listed so the exclusion is auditable. NEVER exported.
    # =======================================================================
    # -- Secrets (unify the two disagreeing lists — settings.py _SECRET_FIELDS
    #    and profile_map.SECRET_KEYS — into THIS registry as the SSOT) --------
    # ai.api_key is NOT here — it is SYNC under ai_connection (user decision), but
    # api_masked=True keeps it out of the HTTP /api/config response. See above.
    Setting("api_keys.adzuna_app_id", Cls.SECRET, Kind.STRING, "_excluded", ios="apiKeys.adzunaAppID"),
    Setting("api_keys.adzuna_app_key", Cls.SECRET, Kind.STRING, "_excluded", ios="apiKeys.adzunaAppKey"),
    Setting("api_keys.usajobs_email", Cls.SECRET, Kind.STRING, "_excluded", ios="apiKeys.usajobsEmail"),
    Setting("api_keys.usajobs_api_key", Cls.SECRET, Kind.STRING, "_excluded", ios="apiKeys.usajobsAPIKey"),
    Setting("salary_estimator.bls.api_key", Cls.SECRET, Kind.STRING, "_excluded", ios="apiKeys.blsRegistrationKey"),
    Setting("profile.workday_email", Cls.SECRET, Kind.STRING, "_excluded"),
    Setting("profile.workday_password", Cls.SECRET, Kind.STRING, "_excluded"),
    Setting("profile.ats_login_password", Cls.SECRET, Kind.STRING, "_excluded"),
    Setting("linkedin.li_at", Cls.SECRET, Kind.STRING, "_excluded", ios="apiKeys.linkedInCookie",
            note="iOS keeps this in the Keychain and the UI promises it never leaves the device."),
    Setting("extension.token", Cls.SECRET, Kind.STRING, "_excluded", ext="token"),

    # -- Machine-local (would break or mislead another device) ---------------
    # NOTE: ai.base_url / ai.models.* / ai.temperature / ai.max_tokens /
    # ai.context_window are now SYNC under category 'ai_connection' (see above).
    # Only the api_key stays SECRET.
    Setting("server.host", Cls.LOCAL, Kind.STRING, "_excluded"),
    Setting("server.port", Cls.LOCAL, Kind.INT, "_excluded"),
    Setting("flaresolverr.url", Cls.LOCAL, Kind.STRING, "_excluded"),
    Setting("linkedin.browser", Cls.LOCAL, Kind.ENUM, "_excluded", enum_values=("firefox", "chrome")),
    Setting("auto_apply.headless", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("auto_apply.use_browser_use", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.folder", Cls.LOCAL, Kind.STRING, "_excluded"),
    Setting("sync.device_id", Cls.LOCAL, Kind.STRING, "_excluded"),
    Setting("sync.device_label", Cls.LOCAL, Kind.STRING, "_excluded"),
    Setting("sync.enabled", Cls.LOCAL, Kind.BOOL, "_excluded"),
    # The per-category toggles for THIS feature (sync.settings.<key>). Local by
    # definition — each device opts itself into each category. See CATEGORIES.
    Setting("sync.settings.profile", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.settings.inbox", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.settings.postings", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.settings.documents", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.settings.pipeline", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.settings.auto_apply", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.settings.salary", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.settings.prompts", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.settings.general", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("sync.interval_seconds", Cls.LOCAL, Kind.INT, "_excluded"),
    Setting("onboarding_complete", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("tour_complete", Cls.LOCAL, Kind.BOOL, "_excluded"),
    Setting("extension.backend_url", Cls.LOCAL, Kind.STRING, "_excluded", ext="backendUrl"),
    # iOS UserDefaults device-local: jobsmith.sync.*, jobsmith.bgsearch.*,
    # scoring/continuation transients, jobSort, hasSeenSearchTip — all LOCAL.
    # Frontend localStorage: jobsmith_theme (no iOS counterpart), live_refresh,
    # settings_mode, port_banner — all LOCAL.
)


# ---- derived views ---------------------------------------------------------

def syncable() -> tuple[Setting, ...]:
    """The rows that actually travel. Everything else is excluded by class."""
    return tuple(s for s in REGISTRY if s.cls is Cls.SYNC)


def secret_canonical_keys() -> frozenset[str]:
    """FOLDER-STRIP list: keys that must never be written to the sync folder.
    This is what the sync export/profile_map filters on. Note ai.api_key is NOT
    here — the user chose to sync it — but it IS in api_masked_keys()."""
    return frozenset(s.canonical for s in REGISTRY if s.cls is Cls.SECRET)


def api_masked_keys() -> frozenset[str]:
    """HTTP-MASK list: keys the /api/config response masks for off-machine
    callers (routers/settings.py). Superset of the folder-strip secrets PLUS any
    SYNC key flagged api_masked (ai.api_key). Distinct surface from the folder —
    a key can sync through the user's folder yet still be masked over HTTP."""
    return secret_canonical_keys() | frozenset(
        s.canonical for s in REGISTRY if s.api_masked
    )


def by_category() -> dict[str, list[Setting]]:
    out: dict[str, list[Setting]] = {}
    for s in syncable():
        out.setdefault(s.category, []).append(s)
    return out


def category_defaults() -> dict[str, bool]:
    """Fresh-install state of every toggle — seed `sync.settings.*` from this."""
    return {c.key: c.default for c in CATEGORIES}


def enabled_categories(cfg: dict) -> frozenset[str]:
    """Which categories this device currently syncs, reading `sync.settings.*`
    and falling back to each category's default when the key is absent. NOTE:
    `profile` here gates the EXISTING profile bridge, not a REGISTRY row."""
    section = (cfg.get("sync") or {}).get("settings") or {}
    return frozenset(
        c.key for c in CATEGORIES
        if bool(section.get(c.key, c.default))
    )


def syncable_for(cfg: dict) -> tuple[Setting, ...]:
    """The SYNC rows whose category toggle is on for this device. This is what
    `export_settings` iterates; import must likewise skip disabled categories.
    (`profile` is not a REGISTRY row, so it never appears here — the engine gates
    the profile bridge separately on `'profile' in enabled_categories(cfg)`.)"""
    on = enabled_categories(cfg)
    return tuple(s for s in syncable() if s.category in on)


# ---- canonical-id parity + lookups (also dumped by the parity test) --------

def syncable_canonical_ids() -> list[str]:
    """Sorted canonical ids of every SYNC row (the `prompts.*` wildcard included
    verbatim — it expands per prompt id at runtime). The registry-parity test
    asserts the Swift `registry` produces the identical list."""
    return sorted(s.canonical for s in syncable())


_BY_CANONICAL: dict[str, Setting] = {s.canonical: s for s in syncable()}


def category_for_path(path: str) -> Optional[str]:
    """The gating category for a concrete canonical path, or None when the path
    is not a SYNC row (a secret, a machine-local key, or an unknown key an older/
    newer peer emitted). None ⇒ never written on import — the allowlist is the
    only switch."""
    s = _BY_CANONICAL.get(path)
    if s is not None:
        return s.category
    if path.startswith("prompts."):  # prompts.* wildcard expansion
        return "prompts"
    return None


# ---- enabled_sources fold (canonicalization rule #3) -----------------------
# Canonical form is a SORTED list of enabled source ids. The desktop stores each
# source's on/off as `search.<id>.enabled`; only `indeed` persists this today
# (opt-in, OFF by default), the rest default ON. iOS carries a Set<String> plus a
# separate `linkedInEnabled` — the mappers fold both into/out of this same list.
SOURCE_IDS: tuple[str, ...] = (
    "adzuna", "arbeitnow", "ashby", "greenhouse", "indeed", "linkedin",
    "recruitee", "remoteok", "usajobs", "weworkremotely", "workable",
)
# indeed is the one source that is opt-in; every other source is on unless the
# user turned it off. Keep in sync with job_sources.SOURCES defaults.
_SOURCE_DEFAULT_ON = frozenset(s for s in SOURCE_IDS if s != "indeed")


def fold_enabled_sources(cfg: dict) -> list[str]:
    search = cfg.get("search") or {}
    out = []
    for sid in SOURCE_IDS:
        sub = search.get(sid)
        default = sid in _SOURCE_DEFAULT_ON
        enabled = bool(sub.get("enabled", default)) if isinstance(sub, dict) else default
        if enabled:
            out.append(sid)
    return sorted(out)


def unfold_enabled_sources(cfg: dict, ids) -> None:
    enabled = {str(s) for s in (ids or [])}
    search = cfg.setdefault("search", {})
    for sid in SOURCE_IDS:
        sub = search.get(sid)
        if not isinstance(sub, dict):
            sub = {}
            search[sid] = sub
        sub["enabled"] = sid in enabled


# ---- config <-> {canonical_path: value} bridge -----------------------------
# `export_settings` reads each enabled SYNC path out of the nested config dict
# (expanding the prompts.* wildcard and folding enabled_sources); `apply_setting`
# writes one path back with base-overlay so a sibling key this device doesn't
# model is preserved. Mirrors the profile bridge in service.py:_make_engine.

_MISSING = object()

# Canonical path -> the nested cfg path when they differ. The desktop nests the
# model id under a per-tier object (`ai.models.<tier>.model`) whose other keys
# must survive; canonical carries only the id string.
_CFG_PATH_OVERRIDES: dict[str, str] = {
    "ai.models.strong": "ai.models.strong.model",
    "ai.models.fast": "ai.models.fast.model",
    "ai.models.utility": "ai.models.utility.model",
}


def _cfg_path(canonical: str) -> str:
    return _CFG_PATH_OVERRIDES.get(canonical, canonical)


def _read_path(cfg: dict, dotted: str):
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _write_path(cfg: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _delete_path(cfg: dict, dotted: str) -> None:
    parts = dotted.split(".")
    node: Any = cfg
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _normalize_enum(setting: Optional[Setting], value: Any) -> Any:
    """Normalize enum values on both read and write. Resume styles carry retired
    aliases (standard/modern->ledger, minimal->swiss); other enums are lowercased
    so casing never causes spurious churn."""
    if setting is None or setting.kind is not Kind.ENUM or not isinstance(value, str):
        return value
    v = value.lower()
    if setting.canonical == "application_honesty.resume_style":
        v = _LEGACY_RESUME_STYLES.get(v, v)
    return v


_LEGACY_RESUME_STYLES = {"standard": "ledger", "modern": "ledger", "minimal": "swiss"}


def export_settings(cfg: dict) -> dict[str, dict]:
    """{canonical_path: {"value": <json>}} for every enabled SYNC path present in
    `cfg`. Category-gated (iterates `syncable_for`), folds enabled_sources, and
    expands `prompts.*` to one record per prompt id. An explicit null is emitted
    (it carries meaning); an absent key is simply omitted (older-client shape)."""
    out: dict[str, dict] = {}
    for s in syncable_for(cfg):
        if s.canonical == "prompts.*":
            for pid, template in (cfg.get("prompts") or {}).items():
                out[f"prompts.{pid}"] = {"value": template}
            continue
        if s.canonical == "search.enabled_sources":
            out[s.canonical] = {"value": fold_enabled_sources(cfg)}
            continue
        val = _read_path(cfg, _cfg_path(s.canonical))
        if val is _MISSING:
            continue
        out[s.canonical] = {"value": _normalize_enum(s, val)}
    return out


def apply_setting(cfg: dict, path: str, value: Any) -> None:
    """Write one imported setting into `cfg` in place, base-overlay style. Rejects
    any path not classed SYNC in the registry (secrets, machine-local keys, and
    keys a newer peer knows but we don't are silently ignored). Normalizes enums,
    unfolds enabled_sources, and routes prompts.<id> into cfg['prompts']."""
    if category_for_path(path) is None:
        return  # not a SYNC row — the allowlist is the only switch
    if path.startswith("prompts."):
        cfg.setdefault("prompts", {})[path[len("prompts."):]] = value
        return
    if path == "search.enabled_sources":
        unfold_enabled_sources(cfg, value)
        return
    value = _normalize_enum(_BY_CANONICAL.get(path), value)
    _write_path(cfg, _cfg_path(path), value)


def remove_setting(cfg: dict, path: str) -> None:
    """Apply a setting tombstone (a removed key — chiefly a deleted prompt
    override). Ignores non-SYNC paths, like `apply_setting`."""
    if category_for_path(path) is None:
        return
    if path.startswith("prompts."):
        prompts = cfg.get("prompts")
        if isinstance(prompts, dict):
            prompts.pop(path[len("prompts."):], None)
        return
    _delete_path(cfg, _cfg_path(path))
