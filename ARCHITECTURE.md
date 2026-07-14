# Jobsmith — Architecture Reference

> **Purpose:** Comprehensive map of every file, module, and data flow in the app.
> Written for Claude Code instances (and humans) who need to understand, debug, or improve this codebase without starting from scratch.
> Last reviewed: 2026-04-20

---

## 1. What This App Is

Jobsmith is a **self-hosted, fully automated job application pipeline** running on a single Python/FastAPI backend with a vanilla-JS frontend. It:

1. Scrapes job listings from multiple sources (LinkedIn, Indeed, Greenhouse, Adzuna, etc.)
2. Scores each job against your resume using a **local LLM** (LM Studio, never cloud AI)
3. Generates tailored resumes and cover letters at a configurable honesty level
4. Attempts to auto-apply to jobs using Playwright browser automation
5. Falls back to a sidebar "Applicant Assist" overlay when automation fails

All AI calls stay on your local network — the LM Studio instance at `config.yaml → ai.base_url`.

---

## 2. Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend server | FastAPI (Python 3.11+), uvicorn, `--reload` in dev |
| Database | SQLite via `aiosqlite` (WAL mode) |
| Browser automation | Playwright (async, Chromium only) |
| Local AI | LM Studio → OpenAI-compatible API (`openai` Python SDK + raw `aiohttp`) |
| Resume generation | `python-docx` |
| Frontend | Vanilla HTML/CSS/JS (no build step, single-page hash routing) |
| Configuration | `config.yaml` (YAML, read on every request) |

---

## 3. Repository Layout

```
jobsmith/
├── backend/                    # All server-side Python
│   ├── main.py                 # FastAPI app assembly (lifespan, middleware, routers)
│   ├── app_state.py            # Shared runtime state: config, cancel events, status dicts
│   ├── background_tasks.py     # Long-running workers (_bg_fetch_jobs, _bg_apply, ...)
│   ├── routers/                # One APIRouter per area: jobs, applications, pipeline,
│   │                           #   settings, sessions, system, assist, extension, answer_bank
│   ├── database.py             # SQLite layer (aiosqlite)
│   ├── ai_engine.py            # LM Studio: scoring, resume gen, cover letters
│   ├── resume_generator.py     # DOCX file generation (python-docx)
│   ├── applicant_assist.py     # Sidebar overlay for manual-assist mode
│   ├── session_manager.py      # Playwright session helpers (login flows)
│   ├── page_extractor.py       # Extracts readable text from job listing pages
│   ├── resume_parser.py        # Résumé text extraction + strictly-extractive LLM profile mapping
│   ├── linkedin_profile_import.py  # Scrapes own LinkedIn profile (saved session) → same LLM mapping
│   ├── browser_use_agent.py    # Feature-flagged Browser-Use 0.12 integration
│   │
│   ├── job_sources/            # One module per job board scraper
│   │   ├── __init__.py         # fetch_all_jobs() orchestrator + global filters
│   │   ├── linkedin.py         # LinkedIn scraper (Playwright, slow, detailed)
│   │   ├── indeed.py           # Indeed scraper (Playwright + session cookies)
│   │   ├── greenhouse.py       # Greenhouse & Lever API fetchers
│   │   ├── adzuna.py           # Adzuna REST API
│   │   ├── remoteok.py         # RemoteOK RSS feed
│   │   ├── weworkremotely.py   # WeWorkRemotely RSS feed
│   │   ├── arbeitnow.py        # Arbeitnow API
│   │   └── usajobs.py          # USAJobs REST API (requires API key)
│   │
│   ├── auto_apply/             # Auto-apply pipeline (Python-native, replaced Node stagehand)
│   │   ├── __init__.py         # Public facade: auto_apply_job(), has_linkedin_session()
│   │   ├── orchestrator.py     # Central coordinator: rate limits, mode, adapter dispatch
│   │   ├── browser_controller.py # Playwright wrapper: launch, navigate, fill, snapshot
│   │   ├── llm_client.py       # LM Studio client: map_fields_to_values(), generate_answer()
│   │   ├── answer_bank.py      # Persistent Q&A snippets (data/answer_bank.json)
│   │   ├── models.py           # Pydantic models: UserProfile, FieldDescriptor, ApplyResult, etc.
│   │   ├── logger.py           # Structured JSONL logger for apply attempts
│   │   └── adapters/           # ATS-specific form-filling strategies
│   │       ├── __init__.py     # ALL_ADAPTERS list (priority-ordered)
│   │       ├── base.py         # ATSAdapter protocol (interface definition)
│   │       ├── greenhouse.py   # Greenhouse ATS adapter
│   │       ├── lever.py        # Lever ATS adapter
│   │       ├── linkedin.py     # LinkedIn Easy Apply adapter (1253 lines, most complex)
│   │       ├── workday.py      # Workday adapter
│   │       ├── indeed.py       # Indeed Quick Apply adapter
│   │       ├── adzuna.py       # Adzuna adapter
│   │       └── generic.py      # Fallback: DOM-snapshot + LLM mapping for unknown ATS
│   │
│   └── services/
│       └── apply_type_detector.py  # Bulk classify apply_type for all 'unknown' jobs
│
├── frontend/
│   ├── index.html              # Single-page app shell (699 lines)
│   └── app.js                  # All UI logic: tabs, API calls, rendering (3047 lines)
│
├── data/                       # Runtime data (gitignored sensitive parts)
│   ├── jobsmith.db        # SQLite database
│   ├── answer_bank.json        # Persistent Q&A snippets
│   ├── screenshots/            # Per-job apply logs + screenshots
│   ├── linkedin_chrome_profile/  # Persistent Chromium profile for LinkedIn session
│   ├── indeed_chrome_profile/    # Persistent Chromium profile for Indeed session
│   └── workday_session/          # Firefox session data for Workday
│
├── resumes/                    # Generated DOCX files (job_id_resume.docx, etc.)
├── tests/                      # pytest test suite
│   └── auto_apply/             # Tests for adapters, LLM mapping, orchestrator, etc.
├── config.yaml                 # All user settings (profile, search, AI, auto_apply)
├── config.example.yaml         # Template config for new installs
├── start_server.sh             # One-command startup: venv + uvicorn :8888 --reload
└── scripts/dev/                # Developer one-offs (not shipped)
    ├── debug_apply.py          # CLI tool: test apply to a URL (never actually submits)
    ├── linkedin_login.py       # Create the persistent LinkedIn browser profile
    └── test_navigator.py       # Smoke test for the AI navigator loop
```

---

## 4. Data Flow — Full Pipeline

### 4.1 Job Fetch Phase

```
User clicks "Fetch Jobs"
    → POST /api/jobs/fetch
    → asyncio.create_task(_bg_fetch_jobs())
    → job_sources/__init__.py::fetch_all_jobs()
        → runs all source modules CONCURRENTLY, each under its own timeout
          (linkedin/indeed=600s, greenhouse=300s, others=60-120s); progress
          reported as each source completes, blocked/timed-out/failed source
          names surfaced via on_progress
        → sources that accept known_ids (linkedin, indeed, greenhouse) skip
          detail/enrichment fetches for jobs already in the DB
        → each module returns list[dict] with normalized job shape
        → global filter: exclude_keywords (word-boundary) + location +
          max_age_days (when date parseable) + min_salary (when stated)
        → deduplicate by URL, then by normalized (title, company, location)
    → db.upsert_job() for each result
        → INSERT new jobs, or backfill empty fields on duplicates
        → status='discovered', apply_type='unknown'
    → _push_notification() → frontend polls /api/notifications
```

**Normalized job dict shape:**
```python
{
  "id": uuid,
  "source": "linkedin" | "indeed" | "greenhouse" | ...,
  "external_id": source-specific ID,
  "title": str,
  "company": str,
  "location": str,
  "url": str,
  "description": str,
  "salary_min": int | None,
  "salary_max": int | None,
  "tags": list[str],
  "date_posted": str,
  "is_remote": bool,
  "is_easy_apply": bool,
  "apply_type": "easy_apply" | "quick_apply" | "external" | "unknown",
}
```

### 4.2 Scoring & Tailoring Phase

```
User clicks "Tailor" on a job (or "Batch Tailor")
    → POST /api/jobs/{job_id}/tailor
    → _bg_tailor_job(job_id)
        1. ai_engine.score_job_fit()    → (score: float 0-100, reasoning: str)
        2. ai_engine.generate_tailored_resume()  → tailored resume markdown
        3. ai_engine.generate_cover_letter()     → cover letter text
        4. resume_generator.generate_resume_docx() → resumes/{job_id}_resume.docx
        5. resume_generator.generate_cover_letter_docx() → resumes/{job_id}_cl.docx
        6. db.create_application()      → applications row, status='pending_review' (or 'approved')
        7. ai_engine.generate_embellishment_log() → diff of what AI changed vs profile
        8. db.set_embellishment_log()   → stored as JSON on the job row
```

**Honesty levels** (`config.yaml → application_honesty.honesty_level`):
- `honest` — reorder/reword only, no fabrication
- `tailored` — rephrase for relevance, stay truthful
- `embellished` — stretch scope/impact, stay plausible
- `fabricated` — invent experience or skills (use with caution)

### 4.3 Review Phase

The frontend's Review Queue tab shows applications with `status='pending_review'`. The user can:
- Edit resume/cover letter text
- Approve (status → `approved`)
- Skip (status → `manual`)

### 4.4 Auto-Apply Phase

```
User clicks "Auto Apply" on an approved application
    → POST /api/applications/{app_id}/auto-apply
    → asyncio.create_task(_bg_apply(app_id))
        → determines: use browser_use agent OR orchestrator
        → orchestrator.run_apply(job, application, profile, config)
            1. Build UserProfile + JobApplicationRequest from dicts
            2. Check rate limits (max_daily_applications, per_domain_rate_limit)
            3. Choose ApplyMode: AUTOFILL (default) or SUBMIT (whitelist-gated)
            4. Pick adapter: iterate ALL_ADAPTERS, first .matches(url) wins
            5. Resolve session paths (LinkedIn/Indeed need persistent Chromium profiles)
            6. BrowserController.__aenter__() → launch Playwright Chromium
            7. ctrl.navigate(job.url)
            8. adapter.apply(ctrl, profile, job, llm, mode, log)
            9. On failure: inject Applicant Assist sidebar, wait for browser close
        → update DB: status='applied' or 'manual' or 'needs_review'
```

---

## 5. Key Components Deep-Dive

### 5.1 BrowserController (`auto_apply/browser_controller.py`)

Single Playwright context for one application attempt. Two launch modes:

- **Persistent profile** (LinkedIn, Indeed): `launch_persistent_context(profile_dir)` — preserves cookies AND localStorage so session survives. Removes `SingletonLock` on startup.
- **Fresh context** (Greenhouse, Lever, generic): `browser.new_context()` — injects `storage_state` from file if available.

**Key methods:**
```python
await ctrl.navigate(url)               # goto + wait domcontentloaded
await ctrl.get_dom_snapshot()          # → list[FieldDescriptor] via injected JS
await ctrl.fill_field(field_id, val)   # click×3 + type (select-all before typing)
await ctrl.select_field(field_id, val) # <select> with value/label/partial fallback
await ctrl.check_field(field_id)       # checkbox
await ctrl.click_radio(field_id, val)  # radio button by label match
await ctrl.upload_file(field_id, path) # file input with FileChooser fallback
await ctrl.switch_to_new_page()        # handle new tab (LinkedIn external apply)
await ctrl.dismiss_popups()            # ranked list of cookie/modal dismiss patterns
await ctrl.screenshot(path)            # full-page PNG
```

The `_SNAPSHOT_JS` injected script walks the DOM and returns field metadata (label, placeholder, type, options, extra_context). This is what gets sent to the LLM for field mapping.

### 5.2 Adapter System (`auto_apply/adapters/`)

Each adapter implements `ATSAdapter` protocol:
```python
name: str
def matches(url: str, page_text: str) -> bool
async def apply(ctrl, profile, job, llm, mode, log) -> ApplyResult
```

Priority order in `ALL_ADAPTERS`:
1. `GreenhouseAdapter` — matches `greenhouse.io` or `boards.greenhouse.io`
2. `LeverAdapter` — matches `lever.co` or `jobs.lever.co`
3. `LinkedInEasyApplyAdapter` — matches `linkedin.com` (most complex: 1253 lines)
4. `WorkdayAdapter` — matches `myworkdayjobs.com`
5. `IndeedEasyApplyAdapter` — matches `indeed.com`
6. `AdzunaAdapter` — matches `adzuna.com`
7. `GenericAdapter` — matches everything (fallback, uses pure LLM field mapping)

**GenericAdapter strategy:** snapshot DOM → send fields to LLMClient → fill each field → click Next/Continue buttons → loop up to `step_ceiling` pages.

### 5.3 LLMClient (`auto_apply/llm_client.py`)

All LLM calls go through this class. Never uses cloud AI.

**`map_fields_to_values(profile, job, fields, answer_bank)`:**
1. First pass: check `AnswerBank` for keyword matches (skips LLM entirely if all fields resolve)
2. Second pass: send remaining fields to LM Studio in a single batch
3. Returns `list[FieldValue]` with `confidence` scores

The LLM response is JSON-extracted with multiple fallback strategies (`json.loads` → `ast.literal_eval`) to handle local model quirks.

**Confidence threshold:** Fields with `confidence < 0.60` in SUBMIT mode flip the result to `NEEDS_REVIEW`.

### 5.4 AI Engine (`backend/ai_engine.py`)

Handles the higher-level AI tasks (resume tailoring, scoring):

- `score_job_fit(job, profile, cfg)` → `(float, str)` score 0-100 + reasoning
- `generate_tailored_resume(job, profile, cfg, honesty_level)` → markdown text
- `generate_cover_letter(job, profile, cfg, honesty_level)` → text
- `generate_embellishment_log(profile, resume, cl, level, cfg)` → `EmbellishmentLog`
- `test_connection(cfg)` → checks LM Studio is reachable on startup

Uses `openai.AsyncOpenAI` with `base_url` pointing at local LM Studio. Supports tiered models (`ai.models.fast` / `ai.models.strong`).

### 5.5 Applicant Assist (`backend/applicant_assist.py`)

When auto-apply fails, the orchestrator calls `_build_sidebar_script(backend_url)` and injects it into the current page via `page.evaluate()`. This inserts an `<iframe>` pointing to `/assist-sidebar` that survives page navigations (registered via `add_init_script`).

The sidebar shows:
- Tailored resume text (Copy All button)
- Cover letter text (Copy All button)
- Download DOCX buttons (fetches from `/api/assist/file`)

**Module-level state** (set by `_bg_apply` before the apply attempt):
```python
_active_session = {"job_id": ..., "resume_path": ..., "cover_letter_path": ..., "resume_text": ..., "cover_letter_text": ...}
```

### 5.6 Database (`backend/database.py`)

SQLite at `data/jobsmith.db`. All operations are async via `aiosqlite`.

**Tables:**
```sql
jobs           -- job listings (status: discovered → tailoring → review → applied/manual)
applications   -- generated materials + apply status (pending_review → approved → applying → applied/manual)
activity_log   -- append-only human-readable event log
qa_cache       -- (legacy) Q&A cache from old stagehand-service
```

**Application status flow:**
```
pending_review → (user approves) → approved
approved       → (auto-apply starts) → applying
applying       → applied | manual | needs_review | autofill_complete
needs_review   → (user must manually change) → applied | manual
```

On startup, any `applying` records are reset to `manual` (crash recovery).

**`upsert_job()`** is smart: on duplicate (source, external_id), it backfills empty fields rather than overwriting.

### 5.7 Job Sources (`backend/job_sources/`)

Each source module exposes `async fetch_jobs(config) -> list[dict]`.

| Source | Method | Notes |
|--------|--------|-------|
| `linkedin` | Guest search API (aiohttp) | OR-batched keyword queries, then concurrent detail-page fetches. Internal budget keeps both phases under the 600s timeout. |
| `indeed` | Playwright + Byparr | OR-batched keyword queries; Cloudflare solved once via Byparr, /viewjob enrichment via direct aiohttp. Internal budget spans primer + search + enrichment. |
| `greenhouse` | HTTP API | One request per board (`?content=true` returns descriptions inline); boards run concurrently. Also covers Lever boards. |
| `adzuna` | REST API | Requires `api_keys.adzuna_app_id` + `adzuna_app_key`. Keyword × location combos run concurrently. |
| `remoteok` | JSON API | No auth required |
| `weworkremotely` | RSS feeds | No auth required; feeds fetched concurrently |
| `arbeitnow` | REST API | No auth required |
| `usajobs` | REST API | Requires `api_keys.usajobs_email` + `usajobs_api_key`. Keyword × location combos run concurrently. |

`fetch_all_jobs()` runs all sources **concurrently**, each wrapped in `asyncio.wait_for` with a per-source timeout, and reports progress as each completes. Plain-HTTP sources go through `fetch_with_retries()` (jittered backoff on timeouts/connection errors/5xx). Per-source job counts are tracked in `data/source_stats.json`; a source that previously returned jobs but hits 0 for 3+ consecutive runs is flagged via `sources_suspect` (silent parser breakage detection).

### 5.8 Apply Type Detector (`backend/services/apply_type_detector.py`)

After fetching, jobs have `apply_type='unknown'`. This service classifies them:
- `easy_apply` — LinkedIn Easy Apply
- `quick_apply` — Indeed Quick Apply
- `external` — redirects to ATS site
- `unknown` — can't determine

Each job source provides its own `detect_*` function (pure logic, no network). Called via `POST /api/detect-apply-types`.

---

## 6. REST API Surface

Endpoints live in `backend/routers/` (one module per group); `backend/main.py` assembles them. Key groups:

```
# Job management
GET  /api/jobs                    # Paginated, filterable job list
GET  /api/jobs/{id}               # Single job with application
POST /api/jobs/fetch              # Start background job fetch (202)
POST /api/jobs/fetch/cancel       # Cancel running fetch
GET  /api/jobs/fetch/status       # Fetch progress
POST /api/jobs/delete             # Bulk delete (by IDs, filters, or all)
DELETE /api/jobs/{id}             # Delete single job
PATCH /api/jobs/{id}/status       # Manually update job status
GET  /api/jobs/{id}/embellishment-log  # What the AI changed
GET  /api/jobs/{id}/apply-log     # Step-by-step apply log (legacy JSON)
GET  /api/jobs/{id}/apply-log-v2  # JSONL apply log (current)
GET  /api/jobs/{id}/screenshots   # List screenshots for a job

# Scoring & tailoring
POST /api/jobs/{id}/score         # Score a single job (202)
POST /api/jobs/score-batch        # Score all unscored jobs (202)
POST /api/jobs/score-batch/cancel
POST /api/jobs/{id}/tailor        # Tailor single job (202)
POST /api/jobs/tailor-batch       # Tailor all discovered (202)
POST /api/jobs/tailor-batch/cancel

# Apply type detection
POST /api/detect-apply-types      # Classify all unknown (202)
GET  /api/detect-apply-types/status
POST /api/detect-apply-types/cancel

# Applications
GET  /api/applications/pending    # Pending review queue
GET  /api/applications/submitted  # Applied applications
GET  /api/applications/failed     # Manual/failed applications
POST /api/applications/{id}/auto-apply        # Start auto-apply (202)
POST /api/applications/{id}/auto-apply/pause  # Pause (keeps browser open)
POST /api/applications/{id}/auto-apply/resume
POST /api/applications/{id}/auto-apply/force-stop  # Close browser + cancel
GET  /api/apply-status            # Is auto-apply running?
PATCH /api/applications/{id}/content   # Edit resume/CL text
PATCH /api/applications/{id}/status    # Change status (applied/manual)

# Applicant Assist (sidebar)
POST /api/assist/start            # Launch assist browser for a job
GET  /api/assist/content          # Serve resume/CL text to sidebar
GET  /api/assist/file             # Download DOCX file
POST /api/assist/autofill         # AI-autofill current page in assist browser
GET  /assist-sidebar              # The sidebar HTML page itself

# Config & onboarding
GET  /api/config                  # Read config.yaml
POST /api/config                  # Update config.yaml (partial)
POST /api/onboarding/parse-resume    # Résumé file/text → partial profile (review only, no persist)
POST /api/onboarding/import-linkedin # Scrape own LinkedIn profile → partial profile (review only)
POST /api/config/honesty-level    # Update honesty level
GET  /api/ai/status               # Check LM Studio connection

# Answer bank
GET  /api/answer-bank             # All snippets
POST /api/answer-bank             # Upsert snippet
DELETE /api/answer-bank/{key}     # Delete snippet

# Dashboard
GET  /api/stats                   # Counts by status, applied today, avg score
GET  /api/fit-breakdown           # Score distribution
GET  /api/activity                # Recent activity log
GET  /api/notifications           # Notification events (poll from ?since_id=N)
GET  /api/sources                 # List available source names
GET  /api/operations/status       # Which background ops are running
```

---

## 7. Frontend (`frontend/`)

Single-page app with hash routing. No build step.

**Tabs:**
- `#dashboard` — stats widgets, activity log, quick-action buttons
- `#jobs` — filterable job list with search, sort, score badges
- `#review` — pending review queue with resume/CL editor
- `#settings` — edits config.yaml via the API
- `#fit-breakdown` — score distribution charts

**Key patterns:**
- Polls `/api/notifications?since_id=N` for live updates, with exponential
  backoff (3→6→12→24→30s) and a "Reconnecting…" banner after repeated failures
- Background ops (fetch, score, tailor, apply) show progress bars
- Job cards have inline action buttons (Score, Tailor, Apply, Assist, Delete)
- `window._currentJobs` caches the current page of job dicts
- All rendered `href`s go through `safeHref()` — scraped job URLs are
  attacker-controlled, so a `javascript:` URL must never reach the DOM

---

## 7b. Desktop Shell (`src-tauri/`, `packaging/`)

The macOS DMG is a **Tauri (Rust) shell wrapping the same FastAPI backend and the
same `frontend/` SPA** — there is no second UI. The shell's only jobs are to boot
the backend, wait for it, and point a WebView at it.

**The three files that matter:**

| File | Role |
|------|------|
| `src-tauri/src/lib.rs` | Rust shell. Picks a free port, spawns the backend sidecar with `JOBSMITH_PORT`/`JOBSMITH_SHELL_PID`, polls `GET /api/health/live` until ready, then loads the SPA. Owns the app menu, window state, and the failure screen. |
| `packaging/desktop_entry.py` | The sidecar's entrypoint (PyInstaller-bundled). Resolves `JOBSMITH_HOME`, kicks off the bundled-Chromium install **on a background thread**, and runs uvicorn. |
| `backend/paths.py` | The `JOBSMITH_HOME` indirection: in the packaged app, user state lives in `~/Library/Application Support/Jobsmith/`, not next to the code. |

**Boot sequence:**

```
Tauri shell starts
  → pick_port()                      free port (8888 if available)
  → spawn sidecar (desktop_entry)    env: JOBSMITH_PORT, JOBSMITH_SHELL_PID
      → ensure_chromium() on a daemon thread   ← does NOT block startup
      │     publishes state.browser_install_status: installing|ready|failed
      │     surfaced by GET /api/system/browser-status, retried via
      │     POST /api/system/browser-install; the SPA shows a banner
      └─→ uvicorn starts immediately
  → poll GET /api/health/live until {"status":"ok"}   (60s deadline)
  → WebView loads http://127.0.0.1:<port>
```

**Two lifetime gotchas this design exists to solve:**
- *Chromium download blocking boot.* It is ~150 MB on first launch. Running it
  inline meant a slow link looked like "backend failed to start" behind a static
  splash. Hence the thread + status endpoint + banner.
- *Orphaned backends.* `watch_parent` polls **both** the PyInstaller bootloader
  and `JOBSMITH_SHELL_PID`, because force-quitting the app never fires Tauri's
  `RunEvent::Exit` — without the shell-PID check, uvicorn survives and squats on
  the port, and the next launch silently moves to a random one.

Sidecar stdout/stderr is appended to `data/logs/shell.log` (the packaged app has
no visible stdout).

---

## 8. Configuration (`config.yaml`)

Top-level sections:

```yaml
profile:          # Candidate info: name, address, experience, skills, etc.
search:           # Job search params: keywords, locations, exclude_keywords, min_salary
ai:               # LM Studio URL, model names (fast/strong), temperature, max_tokens
api_keys:         # Adzuna, USAJobs API keys
auto_apply:       # enabled, auto_approve, headless, max_daily, mode, submit_whitelist
server:           # host, port (default 8888)
flaresolverr:     # Optional FlareSolverr proxy for Cloudflare-protected sites
linkedin:         # browser: firefox (legacy) or chromium
application_honesty:  # honesty_level: honest|tailored|embellished|fabricated
assist:           # notification_sound: bool
```

The config is **read fresh on every background task** — you can change `config.yaml` mid-run and the next job will pick up the new values (hot reload).

---

## 9. Session Management

| Platform | Session Path | Notes |
|----------|-------------|-------|
| LinkedIn | `data/linkedin_chrome_profile/` | Persistent Chromium profile. Run `scripts/dev/linkedin_login.py` to create. Sentinel: `login_success.json` |
| Indeed | `data/indeed_chrome_profile/` | Persistent Chromium profile. Also injects `data/indeed_session/storage_state.json` cookies. |
| Workday | `data/workday_session/` | Firefox profile. Uses `profile.workday_email` / `profile.workday_password` from config. |

Sessions are detected by checking for the `login_success.json` sentinel file. Missing sessions log a warning and proceed without auth (expect login walls).

---

## 10. Apply Modes & Safety Gates

### Apply Mode
- `AUTOFILL` (default): fills every field, stops before the final Submit button. Human must click Submit.
- `SUBMIT`: fills AND submits. Only activates if `auto_apply.mode=submit` AND the job's domain is in `submit_whitelist`.

### Pause/Resume/Force-Stop
- **Pause**: sets `_paused=True` in orchestrator. The coroutine loops `asyncio.sleep(1)` keeping the browser open. No fields are filled while paused.
- **Resume**: clears `_paused`, automation continues from where it left off.
- **Force Stop**: calls `orchestrator.force_stop()` which closes the browser and sets `_force_stop_event` so any blocking waits unblock immediately.

### needs_review Override
If SUBMIT mode is used and any field had `confidence < 0.60`, the result is overridden to `NEEDS_REVIEW` — the application record is flagged and auto-transitions to `manual`/`applied` are blocked until a human reviews.

---

## 11. Logging

**Structured apply log:** `data/auto_apply_log.jsonl` — one JSON object per line, keyed by `job_id`. Contains every field fill attempt, adapter decision, confidence score, and any errors. Accessible via `/api/jobs/{id}/apply-log-v2`.

**Activity log:** SQLite `activity_log` table — human-readable events (fetched, tailored, applied, etc.).

**Diagnostic files** (non-test mode): `/tmp/UI_ljc_diag_*.json` — orchestrator writes these at each major lifecycle stage for debugging apply hangs.

---

## 12. Test Suite (`tests/`)

```
tests/auto_apply/
  conftest.py                  # Shared fixtures (mock config, mock browser)
  test_llm_mapping.py          # LLMClient field mapping logic
  test_profile_mapping.py      # AnswerBank persistence
  test_greenhouse_fetcher.py   # Greenhouse API fetcher
  test_linkedin_easy_apply.py  # LinkedIn adapter
  test_indeed_*.py             # Indeed adapter + session
  test_generic_*.py            # Generic adapter: multi-page, SPA wait, pre-apply
  test_orchestrator_*.py       # Rate limits, pause, confidence override
  test_browser_use_agent.py    # Browser-Use integration
  ...
```

Run with: `venv/bin/python -m pytest tests/auto_apply/ -v`
Debug a specific URL without submitting: `venv/bin/python scripts/dev/debug_apply.py "https://..."`

---

## 13. Dead Code / Legacy

- `stagehand-service/` — Old Node.js microservice using Playwright + Stagehand + LM Studio. Fully replaced by `backend/auto_apply/`. **Deleted 2026-07** (was already not run; also removed the setup.sh install steps).
- `backend/auto_apply_legacy.py` and `backend/ai_navigator.py` — **deleted 2026-07**. The LinkedIn session helpers they carried now live in `backend/auto_apply/linkedin_auth.py`.
- `backend/browser_use_agent.py` — Feature-flagged Browser-Use 0.12 integration. Active when `auto_apply.use_browser_use: true` in config (except for Indeed, which always uses the orchestrator). Under active development.

---

## 14. Known Sharp Edges

1. **LinkedIn adapter is 1253 lines** — it handles Easy Apply (multi-step modal), external apply (new tab detection), and session-preservation tricks. Most fragile part of the codebase; LinkedIn frequently changes their DOM.

2. **Config is read on every background task** — hot-reload works but means concurrent tasks can see different config states if you edit mid-run.

3. **`_bg_apply` reaches into `db._get_db()` directly** — bypasses the public `database.py` helpers. This is intentional (needs a join), but is worth knowing when tracing database calls.

4. **Answer bank is loaded as a module singleton** — `get_answer_bank()` returns the same instance process-wide. If you add/update snippets via the API, changes persist immediately to disk and the in-memory copy is updated.

5. **`_active_session` in applicant_assist is module-level mutable state** — only one assist session can be active at a time. Fine for single-user self-hosted use.

6. **No job queue** — background tasks are plain `asyncio.create_task()` objects. Only one of each type (fetch, score, tailor, apply) can run at a time; new requests while one is running would create a second task. The UI's disabled-button logic prevents this from the frontend, but there's no server-side mutex.
