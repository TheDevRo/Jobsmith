# Jobsmith Standalone (iOS)

A **fully standalone** native iOS version of Jobsmith: the entire pipeline —
fetch, score, tailor, review, apply — runs on the phone. No FastAPI server,
no Playwright, no Docker. (The thin server-connector app lives in `ios/`;
this one is `ios-standalone/` and both can be installed side by side.)

The only thing that still lives off-device is the LLM itself: point the app
at any OpenAI-compatible endpoint (LM Studio over LAN/Tailscale, Ollama,
OpenRouter, OpenAI), or — on iOS 26 devices with Apple Intelligence — use
Apple's on-device foundation model for the lighter tasks.

## What made the cut

| Desktop capability | Standalone iOS |
|---|---|
| Job sources | All pure-HTTP sources ported natively: RemoteOK, WeWorkRemotely, Arbeitnow, Greenhouse, Lever, Ashby, Workable, Recruitee, Adzuna, USAJobs, plus the LinkedIn guest scraper (throttled) and paste-a-URL/JSON-LD generic parsing. **Indeed is out** — it needs a real browser + Cloudflare solver. |
| Scoring / tailoring / AI Edit / honesty levels | Full port — same 17 prompt templates, same honesty/tone directives, same lenient JSON salvage. |
| DOCX resume + cover letter | Native OOXML writer, same three style presets (standard/minimal/modern), same layout. |
| Apply Assist extension | Bundled as a Safari Web Extension that talks to the app via **native messaging** — profile, tailored DOCX files, answer bank, and LLM field-mapping all come from the app. No token pairing, no backend URL. |
| Review Queue / AI Edit | Native SwiftUI editor with per-edit honesty + model overrides and QuickLook DOCX preview. |
| n8n scheduled fetch | `BGAppRefreshTask` (cheap JSON sources) + `BGProcessingTask` (LinkedIn + scoring), with summary notifications. |
| Add job by URL | Inbox toolbar button **and** a share extension — share any posting from Safari or the LinkedIn app straight into Jobsmith. |
| Dashboard | Rethought mobile-first: a swipe-to-triage **Inbox** (right = shortlist, left = pass), a staged **Pipeline**, **Activity** stats, and Settings with the same knobs. Fit scores render as "heat" — steel blue (cold) to ember (strong fit). |

## Layout

```
ios-standalone/
├── project.yml                  # xcodegen spec (app + Safari ext + share ext + tests)
├── JobsmithKit/                 # Swift package — the whole engine
│   └── Sources/JobsmithKit/
│       ├── Core/                # AppConfig, ConfigStore, Profile models, App Group
│       ├── Data/                # GRDB database, Job/Application/AnswerBank stores, FileVault
│       ├── Fetching/            # JobSource protocol, per-board sources, filters, dedup, FetchPipeline
│       ├── AI/                  # AIEngine protocol, OpenAI-compatible client, prompt registry,
│       │                        #   scoring/tailoring services, resume text parser
│       ├── Documents/           # OOXML DOCX writer + style presets, cover letter generator
│       ├── Apply/               # FieldDescriptor/FieldValue, answer bank matcher,
│       │                        #   deterministic profile matcher, LLM field mapper,
│       │                        #   ActiveJobStore, NativeMessageRouter
│       ├── Salary/              # SOC classification + Adzuna histogram + BLS estimates
│       └── Importing/           # Resume text extraction (PDF/DOCX/TXT)
├── App/                         # SwiftUI app: Inbox/Pipeline/Activity/Settings, onboarding,
│                                #   background tasks, apply handoff
├── SafariExt/                   # Safari Web Extension target (JS synced from extension/src)
├── ShareExt/                    # Share-sheet ingestion
├── KitTests/                    # Unit tests (parsers, filters, prompts, OOXML, mapping)
└── UITests/                     # XCUITest smoke suite (-UseMockAI)
```

The extension JS stays single-source in `extension/src/`: the iOS-standalone
build installs `manifest.ios-standalone.json` as the manifest and swaps
`common/api.js` for `common/api.native.js`, which reimplements the same
`Jobsmith.*` API over `browser.runtime.sendNativeMessage`. Everything else
(snapshot/fill/overlay/dropcatch) is untouched.

## Build

```bash
brew install xcodegen            # once
cd ios-standalone
xcodegen generate
open JobsmithStandalone.xcodeproj
```

Simulator builds need no signing. For a device, select your team on all
three targets (app, Assist extension, Share extension) — App Group
entitlements require the targets to share a team.

Tests:

```bash
xcodebuild -project JobsmithStandalone.xcodeproj -scheme JobsmithKit \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test    # unit
xcodebuild -project JobsmithStandalone.xcodeproj -scheme JobsmithStandalone \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test    # UI smoke
```

## First run

1. Onboarding imports your resume (PDF/DOCX/paste) and the AI extracts a
   profile for review — or skip and fill it manually in Settings.
2. Connect the AI: LM Studio at `http://<machine-ip>:1234/v1`, or a hosted
   provider with a key.
3. Pick sources and keywords, then **Fetch** from the Inbox and start
   swiping: right to shortlist, left to pass.
4. On a shortlisted job: **Score** → **Tailor** → **Review** (AI Edit,
   preview, approve) → **Apply**, which opens the posting in Safari where
   the **Jobsmith Assist** extension scans the form, autofills from your
   profile + answer bank + LLM mapping, and attaches the tailored DOCX.
   Back in the app, confirm whether you submitted.

Enable the extension once: Settings → Apps → Safari → Extensions →
Jobsmith Assist → allow on the sites you apply on.

## The apply flow, exactly

The app writes the active job to the shared App Group
(`active_job.json`) and opens the posting in the real Safari app — Safari
Web Extensions do not run inside in-app browser views. The extension asks
the app for the active job over native messaging, scans the DOM, and
requests field mappings; the handler runs the same 4-phase mapper as the
desktop (file inputs → deterministic profile rules → answer bank → LLM),
reading everything from the App Group. Tailored DOCX bytes cross the same
bridge base64-encoded.

## Known gaps

- **Indeed** requires Playwright + a Cloudflare solver — use the desktop or
  Docker deployment if you need it.
- **Auto-apply (autonomous form submission)** is deliberately absent; Apply
  Assist is the flow, same as the desktop's recommended path.
- **LinkedIn fetching on cellular/mobile IPs** rate-limits aggressively;
  the throttler backs off hard, but Wi-Fi and background (overnight)
  processing runs are the reliable path.
- **Background refresh is opportunistic** — iOS decides when. Manual fetch
  is always primary.
- **App Store**: guest scraping of job boards is ToS-gray; this app is
  built for personal sideloading, not review.
