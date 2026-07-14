# Jobsmith

A self-hosted job application copilot that uses any OpenAI-compatible AI server (LM Studio by default) for job scoring, resume tailoring, and assisted submission via a browser extension (**Apply Assist**). Features a modern web dashboard for managing your entire job search pipeline — from discovery through application.

> **Note:** The fully autonomous `auto_apply` form-filler is on the back burner; the primary workflow is **Apply Assist** — a browser-extension sidebar that injects your tailored resume, cover letter, and pre-filled answers into the live ATS form so you click Submit yourself. The auto-apply code remains in the repo but is no longer the recommended path.

## Download

Grab the [latest release](https://github.com/TheDevRo/Jobsmith/releases/latest):

- **macOS (Apple Silicon)** — `Jobsmith_<version>_aarch64.dmg`. The app is
  unsigned: after the first blocked launch, use System Settings → Privacy &
  Security → **Open Anyway** (or `xattr -dr com.apple.quarantine
  /Applications/Jobsmith.app`). First launch downloads Chromium (~150 MB).
- **Browser extension** — `jobsmith-extension-{chrome,firefox}-v*.zip`,
  sideloaded (Chrome: Load unpacked; Firefox: Load Temporary Add-on).
- **Windows / Linux / Intel macOS** — use Docker: `docker compose up -d`
  (see [Docker](#docker)).
- **iOS** — a fully standalone native app (no server needed); build it
  yourself or install via TestFlight. See
  [README-IOS-STANDALONE.md](README-IOS-STANDALONE.md).

AI features need an OpenAI-compatible server — [LM Studio](https://lmstudio.ai)
running locally (recommended, fully private), Ollama, or a hosted provider like
OpenRouter or OpenAI with an API key. Everything else works without one.

## Features

- **Multi-source job aggregation** — LinkedIn, Adzuna, RemoteOK, WeWorkRemotely, USAJobs, Arbeitnow, Indeed (Playwright-based, no API key), plus per-company ATS watchlists (Greenhouse, Lever, Ashby, Workable, Recruitee) with a board finder and AI company suggestions
- **Single-URL ingestion** — Paste any job URL to add it directly; per-source parsers for known boards plus a generic fallback
- **AI-powered tailoring** — Your AI server scores job fit and generates tailored resumes and cover letters
- **Bring your own AI** — Any OpenAI-compatible endpoint works: LM Studio or Ollama for fully local/private inference, or hosted providers (OpenRouter, OpenAI, Groq…) with an API key
- **Honesty levels** — Choose how much latitude the AI takes per job: `honest` / `tailored` / `embellished` / `fabricated`
- **AI Edit** — Iteratively revise generated resumes and cover letters with natural-language instructions; per-edit honesty + model tier overrides
- **Resume style presets** — `executive`, `ledger`, `banner`, `compact`, `swiss` (all ATS-friendly), each with a selectable accent color
- **Smart role selection** — Cap resume length and let the local LLM pick the most relevant past roles for each job; pin roles to force-include
- **References section** — Optional references appended verbatim to generated resumes; never sent to the AI
- **Apply Assist (primary submit flow)** — Browser extension opens the job's ATS in a real browser, injects a sidebar with your tailored resume, cover letter, and pre-filled answers, and autofills standard fields. You stay in the loop and click Submit.
- **Answer bank** — Custom application answers (work auth, sponsorship, etc.) are remembered and replayed on future forms
- **Auto-apply (on the back burner)** — Heuristic form filler with anti-fabrication guardrails still ships, but is no longer the recommended workflow
- **Per-domain rate limits** — Cap daily applies per ATS host
- **Fit-score breakdown** — Click any score on the dashboard for a per-criterion explanation
- **Salary estimates** — Jobs without disclosed comp get an AI-aided market estimate (Adzuna + BLS data), clearly labeled
- **Editable AI prompts** — Every internal LLM prompt (scoring, tailoring, cover letters, parsing, auto-apply…) can be viewed, edited, and reset from Settings → Prompts; overrides persist in `config.yaml`, defaults keep improving with updates
- **Basic / Advanced settings** — Settings opens in Basic mode with just the essentials; flip the toggle to Advanced to expose every knob (auto-apply tuning, prompt editor, scoring tier, context window, logs, and more)
- **Session persistence** — Per-domain browser session management so you stay logged in across runs
- **FlareSolverr integration** — Bypasses Cloudflare challenges on protected job boards
- **Modern dashboard** — Sidebar navigation (**Inbox** → **Pipeline** → **Activity**), split-pane job feed, real-time notifications, batch controls with stop/pause
- **Folder sync (desktop ↔ iOS)** — Optional serverless two-way sync of jobs, applications, and your profile through a shared folder (iCloud Drive, Dropbox, …). No account and no server — each device reads and writes the same folder (last-writer-wins merge); ATS login passwords are never written to the folder. Configure in **Settings → Sync**.
- **Native iOS app** — A fully standalone SwiftUI app (`ios-standalone/`) runs the whole pipeline on-device (fetch, score, tailor, review, apply) and syncs with the desktop; see [README-IOS-STANDALONE.md](README-IOS-STANDALONE.md)

## Architecture

```
                        +--------------------+
                        |     AI Server      |
                        | (OpenAI-compatible |
                        |  e.g. LM Studio)   |
                        +--------+-----------+
                                 |
+-------------+          +-------v--------+          +----------+
| Job Sources |--------->|   FastAPI       |<-------->|  SQLite  |
| - LinkedIn  |          |   Backend       |          |    DB    |
| - Adzuna    |          |  (port 8888)    |          +----------+
| - RemoteOK  |          +---+--------+----+
| - WWR       |              |        |
| - Greenhouse|          +---v--+ +---v--------+
| - Lever     |          | REST | | Static     |
+-------------+          | API  | | Frontend   |
                         +------+ +------------+

+-------------+              ^
|    n8n      |              |
| (optional)  |--------------+
| Webhooks &  |
| Scheduling  |
+-------------+
```

## Prerequisites

- **Python 3.11+**
- **An OpenAI-compatible AI server** — [LM Studio](https://lmstudio.ai) (recommended, local), Ollama, or a hosted provider (OpenRouter, OpenAI, …) with an API key
- **n8n** (optional) — for scheduled automation workflows
- **FlareSolverr** (optional) — for bypassing Cloudflare-protected job boards

## Setup

### 1. Clone and run setup

```bash
git clone https://github.com/TheDevRo/Jobsmith.git ~/jobsmith
cd ~/jobsmith
chmod +x setup.sh
./setup.sh
```

This creates a virtual environment, installs Python dependencies, and installs the Playwright Chromium browser.

### 2. Configure

Copy the example config and fill in your details:

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` — the key sections:

#### Profile

Your personal info used for form filling and resume generation. The AI will **only** use data you provide here — it never fabricates information.

```yaml
profile:
  full_name: Jane Doe
  email: jane@example.com
  phone: 555-555-5555
  location: Denver, CO
  street_address: 123 Main St
  city: Denver
  state: CO
  zip_code: '80202'
  linkedin: https://linkedin.com/in/janedoe
  desired_salary: '90000'
  work_authorization: 'Yes'
  sponsorship_required: 'No'
  workday_email: jane@example.com   # Used for Workday account login/creation
  workday_password: your-password
  summary: "Professional summary..."
  skills:
    - Python
    - Docker
  experience:
    - title: Software Engineer
      company: Acme Corp
      start_date: '2023-01-01'
      end_date: Present
      bullets:
        - Built and maintained microservices handling 10k requests/sec
  education:
    - degree: B.S. Computer Science
      school: University Name
      year: '2021'
  certifications:
    - AWS Solutions Architect Associate
  references:                     # Optional — appended verbatim to resumes, never sent to AI
    - name: Pat Reference
      position: Engineering Manager, Acme Corp
      email: pat@example.com
      phone: 555-555-5556
```

> Pin roles in the **Profile** tab to force-include them on every tailored resume regardless of relevance ranking.

#### AI connection

Any OpenAI-compatible chat-completions endpoint works. Both settings are also
editable in the app under **Settings → Integrations → AI Connection**.

```yaml
ai:
  base_url: http://localhost:1234/v1   # LM Studio URL (use machine IP if on homelab)
  api_key: lm-studio                   # Bearer token — local servers ignore it;
                                       # set a real key for hosted providers
  temperature: 0.7
  max_tokens: 16384
  models:
    fast:
      model: qwen/qwen3.5-9b           # Used for Browser-Use agent and AI Navigator
    strong:
      model: mistralai/mistral-7b-instruct-v0.3   # Used for resume tailoring
```

Example endpoints:

| Provider | `base_url` | `api_key` |
|---|---|---|
| LM Studio (default) | `http://localhost:1234/v1` | not needed |
| Ollama | `http://localhost:11434/v1` | not needed |
| OpenRouter | `https://openrouter.ai/api/v1` | your OpenRouter key |
| OpenAI | `https://api.openai.com/v1` | your OpenAI key |

The key can also be supplied via the `JOBSMITH_AI_API_KEY` environment
variable, which takes precedence over `config.yaml`.

#### Search preferences

```yaml
search:
  keywords:
    - software engineer
    - backend developer
  locations:
    - Remote
    - Denver
  exclude_keywords:
    - director
    - principal
  min_salary: 80000
  max_age_days: 7
  greenhouse_boards:      # Company slugs from boards.greenhouse.io/<slug>
    - stripe
  lever_companies:        # Company slugs from jobs.lever.co/<slug>
    - openai
  indeed:
    enabled: true         # Playwright scraper, no API key needed
    max_pages: 5
```

#### Auto-apply (deprioritized — see Apply Assist)

The autonomous auto-apply pipeline is no longer the recommended workflow. Prefer **Apply Assist** (browser extension) for day-to-day applications. Settings below remain functional if you want to experiment.

```yaml
auto_apply:
  enabled: false                 # Set true to enable autonomous auto-apply (off by default)
  auto_approve: false            # Set true to skip manual review
  headless: true                 # true = hidden browser, false = visible (also toggleable in Settings UI)
  max_daily_applications: 20
  use_browser_use: false         # Set true to use Browser-Use agent fallback
  mode: autofill                 # autofill = fill but don't submit (safe default); submit = click Submit on whitelisted domains
  submit_whitelist:
    - greenhouse.io
    - lever.co
  per_domain_rate_limit: 5       # Max applies to the same domain per day (0 = unlimited)
  step_ceiling: 0                # Max Playwright actions per page before aborting (0 = unlimited)
  review_required_rules:
    unknown_ats: true            # Flag unknown ATS sites for human review before submit
    min_confidence: 0.70         # If LLM field-mapping confidence is below this, require review
```

#### Application honesty & AI edits

```yaml
application_honesty:
  honesty_level: honest          # honest | tailored | embellished | fabricated
  cover_letter_tone: professional  # professional | conversational | enthusiastic
  resume_style: ledger           # executive | ledger | banner | compact | swiss
  resume_accent: default         # default | navy | burgundy | forest | plum | charcoal
  max_resume_experience_entries: null  # null = include all roles; or 1-20 to cap and let the LLM pick the most relevant
  ai_edit_model_tier: strong     # fast | strong — default model for the AI Edit feature
```

All of these can also be changed live in the **Settings** tab.

> Settings has a **Basic / Advanced** toggle (top-right of the tab bar). Basic covers everything needed to run the app; Advanced additionally reveals the Auto-Apply, Prompts, and Logs tabs plus tuning knobs like scoring tier, context window, cookie import, ATS/Workday credentials, max resume entries, and the AI Edit model tier. Values set in Advanced stay in effect when you switch back to Basic.

#### AI prompts (optional)

Every prompt sent to the local LLM can be customized from **Settings → Prompts** (Advanced mode). Templates use `{placeholder}` variables that are filled in at run time — literal braces (e.g. JSON examples) need no escaping. Only customized prompts are stored, under a top-level `prompts:` key:

```yaml
prompts:
  score_job_fit: |
    Your custom scoring prompt here...
    JOB: {job_title} at {job_company}
    ...
```

Prompts left at default automatically pick up improvements in app updates. Use the per-prompt **Reset to Default** button (or delete the key) to revert.

#### API keys (optional)

```yaml
api_keys:
  adzuna_app_id: your-app-id       # Free at developer.adzuna.com
  adzuna_app_key: your-api-key
  usajobs_email: your@email.com
  usajobs_api_key: your-api-key
```

#### FlareSolverr (optional)

```yaml
flaresolverr:
  url: http://localhost:8191/v1    # Point to your FlareSolverr instance
```

### 3. Environment variables

`.env` controls feature flags and runtime paths. The defaults work for most setups:

```bash
USE_BROWSER_USE=true              # Enable Browser-Use autonomous agent
BROWSER_PROFILE_DIR=.browser-profile
SESSIONS_DIR=sessions
FAILED_SCREENSHOTS_DIR=failed_screenshots
BROWSER_USE_MAX_STEPS=50
```

> `config.yaml` is **gitignored** — your credentials never leave your machine.

### 4. Start your AI server

For the default LM Studio setup:

1. Open LM Studio and download a model (Mistral 7B or Qwen 3.5 recommended)
2. Go to the **Local Server** tab, load your model, click **Start Server**
3. Server runs at `http://localhost:1234` by default

If LM Studio is on a separate machine (e.g., a homelab server), use its IP: `http://192.168.x.x:1234/v1`

Using a hosted provider instead? Skip this step — just set the server URL and
API key in **Settings → Integrations** (or `ai.base_url` / `ai.api_key` in
`config.yaml`) and pick your models from the dropdowns.

### 5. Start the server

```bash
source .venv/bin/activate
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8888
```

Open **http://localhost:8888** in your browser.

### Docker

Works on Windows, Linux, and Intel macOS.

```bash
git clone https://github.com/TheDevRo/Jobsmith.git && cd Jobsmith

# Create the bind-mount dirs first. On Linux, Docker would otherwise create
# them as root and the container (uid 1000) crash-loops on its first write.
mkdir -p config data resumes sessions failed_screenshots .browser-profile sync-folder

cp .env.example .env   # optional — every value has a working default

docker compose up -d   # builds the image locally, then seeds
                       # config/config.yaml from config.example.yaml
```

Open **http://localhost:8888**, then go to **Settings → Integrations** and set
your AI endpoint. If LM Studio (or Ollama) is running on the same machine as
Docker, use `http://host.docker.internal:1234/v1` — from inside the container,
plain `localhost` means the container itself, not your host.

The app boots fine with no LLM configured (the healthcheck doesn't touch AI);
only the AI features — scoring, tailoring, AI Edit — stay dormant until you
point it at a server. Settings you save in the UI live in `./config/config.yaml`
and survive `docker compose restart` / `up -d --build`.

Everything is loopback-only by default: the dashboard is unauthenticated for
local callers and `/api/config` can hand out your profile and API keys, so
publishing it on `0.0.0.0` publishes those too. To reach it from another
machine, set a Jobsmith API token in Settings first, then set
`JOBSMITH_BIND=0.0.0.0` in `.env` — or, better, front it with a reverse proxy
or Tailscale.

> Setting `JOBSMITH_AI_BASE_URL` / `JOBSMITH_AI_API_KEY` in `.env` makes the
> environment the source of truth: it is re-applied on every config load and
> overrides whatever you type into Settings. Leave them commented out unless
> that's what you want.

## Daily Workflow

1. **Fetch jobs** — On the **Inbox** tab, select sources and click "Fetch New Jobs". You can also paste a single job URL into the **Add Job by URL** box for one-off ingestion.
2. **Score** — Click "Score Unscored" to rank jobs by fit. Click any score for a per-criterion breakdown.
3. **Tailor** — Click "Tailor All Unprocessed" to generate tailored resumes and cover letters.
4. **Review & edit** — Browse the **Inbox** feed (split-pane view with detail panel), filter by score/source/status. Use **AI Edit** in the application detail to revise resumes or cover letters with natural-language instructions (per-edit honesty + model tier overrides available).
5. **Apply (recommended: Apply Assist)** — From the job detail pane, click **Apply Assist** to open the posting in your browser with a sidebar holding your tailored resume, cover letter, and pre-filled answers. Standard fields autofill; you fill any custom questions and click Submit. The **Pipeline** tab (Shortlisted → Ready to Review → Applied) lets you "Approve" tailored apps for your own audit trail — note that Approve **does not** submit anything by itself.

Batch operations (fetch, score, tailor) have **Stop** buttons so you can cancel mid-run. Real-time **notifications** appear in the bell icon when background tasks complete.

## Auto-Apply (legacy, deprioritized)

> Apply Assist (browser extension) is the recommended way to submit applications. The autonomous auto-apply below remains in the repo but is no longer actively developed against new ATS quirks.

Two modes are available:

### Browser-Use Agent (recommended)

Set `use_browser_use: true` in `config.yaml` or the Settings tab. The Browser-Use agent uses a local AI model to navigate job sites autonomously — it reads the page, fills forms, handles multi-step flows, and detects CAPTCHAs. Capabilities:

- Account creation / sign-in using your `workday_email` / `workday_password`
- Multi-page Workday, Greenhouse, Lever, and generic ATS forms
- Resume file upload
- TOS/agreement checkboxes (auto-checked)
- Cloudflare challenges (via FlareSolverr)
- Automatic retry on CDP connection failures (clears stale browser state)

**Headless mode** is toggled in the Settings tab (writes to `config.yaml`). The browser is hidden by default.

### Built-in Playwright Handlers

Platform-specific handlers for common ATS systems. Falls back to "Manual Apply" status (with direct URL) for unsupported sites.

| Platform | Behavior |
|----------|----------|
| Greenhouse | Auto-fills and submits |
| Lever | Auto-fills and submits |
| LinkedIn Easy Apply | Fills multi-step modal (requires LinkedIn session) |
| Workday | Auto-fills form fields |
| Other | Returns URL for manual application |

## Session Management

For sites that require login, save a session so auto-apply stays authenticated:

1. Go to **Settings → Browser Sessions**
2. Click **Sign in to LinkedIn** (or the relevant domain)
3. A visible browser window opens — log in normally
4. The session is saved to `sessions/` and reused automatically on future runs

## Troubleshooting

**CDP connection timeout**
The Browser-Use agent clears stale browser lock files and kills orphaned Chromium processes automatically before each run. If timeouts persist, delete `.browser-profile/` and retry.

**Browser opens even with headless mode enabled**
Toggle headless in the **Settings tab** — this writes to `config.yaml`, which takes precedence. The `BROWSER_HEADLESS` env var in `.env` is a lower-priority fallback.

**AI connection failed**
Ensure your AI server is running with a model loaded and the URL in Settings matches (for LM Studio: Local Server tab → Start Server). For hosted providers, check the API key. Click **Test Connection** to diagnose.

**No jobs found**
Broaden search keywords, add Adzuna API keys for more results, or check that Greenhouse/Lever company slugs are correct.

**Auto-apply fails or gets stuck**
Check `failed_screenshots/` for screenshots of the failure state. For Cloudflare-protected sites, configure FlareSolverr. For sites requiring login, set up a session in Settings → Browser Sessions. CAPTCHAs will stop the agent and return a "manual apply required" message.

**LinkedIn login issues**
Try a different browser type in Settings (Firefox/Chromium/WebKit). If issues persist, delete `data/linkedin_session/` and retry.

**Database reset**
Delete `data/jobsmith.db` and restart the server — tables are recreated automatically.

**Debugging a specific apply URL**
Run `.venv/bin/python scripts/dev/debug_apply.py "<url>"` to drive the auto-apply flow end-to-end against a single posting. The script fills the form but never clicks Submit, regardless of `mode`.

**A prompt edit broke generation output**
Custom prompts from Settings → Prompts are used verbatim — if generated resumes stop parsing (missing SUMMARY/EXPERIENCE headers, markdown creeping in), hit **Reset to Default** on the prompt you changed. Placeholders the app doesn't recognize are left as literal `{text}` and flagged when you save.

**Tests**
`.venv/bin/python -m pytest tests/ -v` — covers field mapping, honesty prompts, prompt registry/overrides, parsers, salary estimation, and the API layer.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, test commands, and the
versioning convention.

## License

Jobsmith is licensed under the [GNU AGPL-3.0](LICENSE). You are free to use,
modify, and self-host it. If you run a modified version as a network service,
the AGPL requires you to publish your changes under the same license.

A hosted tier — where the AI inference endpoint and job-board API keys are set
up and managed for you as a subscription — is planned. Self-hosting is, and
will remain, fully functional and free.

## n8n Automation (Optional)

Automate the pipeline on a schedule:

1. Install n8n: `npm install -g n8n` or use Docker
2. Import `n8n/workflows.json` via Settings → Import Workflow
3. Activate the workflows you want

Included workflows:
- **Scheduled Job Fetch** — fetches new jobs every 6 hours and triggers batch tailoring
- **Daily Digest** — sends a summary with pending review count and stats
- **Auto-Apply Executor** — webhook-triggered, processes approved applications

## Project Structure

```
jobsmith/
├── backend/
│   ├── main.py                    # FastAPI app assembly; serves the frontend statically
│   ├── app_state.py               # config.yaml load/save (single source of truth)
│   ├── database.py                # SQLite ORM (aiosqlite)
│   ├── ai_engine.py               # LM Studio: job scoring, tailoring, cover letters, AI Edit
│   ├── prompt_registry.py         # All internal LLM prompt templates + override rendering
│   ├── resume_parser.py           # Résumé → profile extraction (onboarding)
│   ├── linkedin_profile_import.py # LinkedIn profile → profile extraction
│   ├── salary_estimator.py        # Adzuna/BLS market salary estimates
│   ├── resume_generator.py        # DOCX/PDF resume and cover letter generation
│   ├── applicant_assist.py        # Apply Assist sidebar backend
│   ├── extension_api.py           # Browser-extension API (token auth, autofill data)
│   ├── browser_use_agent.py       # Browser-Use autonomous agent wrapper (legacy auto-apply)
│   ├── session_manager.py         # Per-domain browser session persistence
│   ├── sync/                      # Serverless folder-sync engine (last-writer-wins merge,
│   │                              #   entity adapters, content-addressed documents) — desktop↔iOS
│   ├── auto_apply/                # Legacy auto-apply: field mapping, LLM client, answer bank
│   ├── routers/                   # One APIRouter per area (jobs, pipeline, settings,
│   │                              #   prompts, applications, assist, extension, …)
│   └── job_sources/               # adzuna, remoteok, weworkremotely, usajobs, arbeitnow,
│                                  #   indeed, linkedin, greenhouse, lever, ashby, workable,
│                                  #   recruitee, manual (paste-a-URL), _generic fallback
├── frontend/
│   ├── index.html                 # Single-page web UI (sidebar nav, split-pane feed)
│   ├── js/                        # Plain-JS modules: core, dashboard, jobs, review,
│   │                              #   settings, prompts, sessions, onboarding, …
│   └── style.css                  # Dark/light theme styles
├── src-tauri/                     # Tauri desktop shell (macOS .app / .dmg — see README-DESKTOP.md)
├── ios-standalone/                # Fully standalone native iOS app (SwiftUI + GRDB; runs the
│                                  #   whole pipeline on-device, syncs with desktop) —
│                                  #   see README-IOS-STANDALONE.md
├── extension/                     # Apply Assist browser extension (Chrome/Firefox/Safari)
├── packaging/                     # PyInstaller spec + splash page for the desktop build
├── scripts/                       # build_desktop.sh (DMG) and other build helpers
├── tests/                         # pytest suite (.venv/bin/python -m pytest tests/)
├── n8n/
│   └── workflows.json             # Optional n8n automation workflows
├── data/                          # Runtime data (auto-created, gitignored)
│   ├── jobsmith.db                # SQLite database
│   └── linkedin_session/          # Persistent LinkedIn browser session
├── sessions/                      # Per-domain Browser-Use sessions (gitignored)
├── .browser-profile/              # Browser-Use Chromium profile (gitignored)
├── failed_screenshots/            # Browser-Use failure captures (gitignored)
├── config.example.yaml            # Config template — copy to config.yaml
├── config.yaml                    # Your config (gitignored — never committed)
├── .env                           # Feature flags and runtime paths (gitignored)
├── requirements.txt               # Python dependencies
├── Dockerfile                     # Container image (Playwright + Chromium base)
├── docker-compose.yml             # Compose service (ports, volume mounts, env overrides)
└── setup.sh                       # One-command setup script
```
