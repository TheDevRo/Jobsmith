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
| Apply Assist | An **in-app Apply browser** (WKWebView) opens the posting inside Jobsmith, injects the same `snapshot.js`/`fill.js` scripts, and runs the LLM field-mapper **in-process** — no Safari extension, no native messaging, no per-site permission. A fallback panel offers tap-to-copy answers and one-tap document export for anything the injector can't set. |
| Review Queue / AI Edit | Native SwiftUI editor with per-edit honesty + model overrides and QuickLook DOCX preview. |
| n8n scheduled fetch | `BGAppRefreshTask` (cheap JSON sources) + `BGProcessingTask` (LinkedIn + scoring), with summary notifications. |
| Add job by URL | Inbox toolbar button **and** a share extension — share any posting from Safari or the LinkedIn app straight into Jobsmith. |
| Dashboard | Rethought mobile-first: a swipe-to-triage **Inbox** (right = shortlist, left = pass), a staged **Pipeline**, **Activity** stats, and Settings with the same knobs. Fit scores render as "heat" — steel blue (cold) to ember (strong fit). |

## Layout

```
ios-standalone/
├── project.yml                  # xcodegen spec (app + share ext + tests); also the
│                                #   source of truth for App/Info.plist (generated)
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
│                                #   background tasks, in-app Apply browser
│   └── Apply/JS/                # snapshot.js/fill.js (copied from extension/src/common)
│                                #   injected into the Apply browser's WKWebView
├── ShareExt/                    # Share-sheet ingestion
├── KitTests/                    # Unit tests (parsers, filters, prompts, OOXML,
│                                #   mapping, sync merge/conflict) — the whole suite
└── UITests/                     # XCUITest smoke suite (-UseMockAI)
```

**There is no Safari Web Extension.** Apply Assist runs entirely in-process: the
Apply browser injects `snapshot.js`/`fill.js` (copied from `extension/src/common`)
into its own WKWebView and calls `NativeMessageRouter` directly. That type keeps
its name — and the `{name, body}` message shapes of `backend/extension_api.py` —
because it is the same contract the desktop browser extension speaks, but nothing
crosses a process boundary.

`JobsmithKit` has no SPM test target: the Kit links UIKit-dependent code, so
`swift test` on macOS can't build it. Every test lives in `KitTests/` and runs
on the simulator via the `JobsmithKit` scheme.

## Build

```bash
brew install xcodegen            # once
cd ios-standalone
xcodegen generate
open JobsmithStandalone.xcodeproj
```

Simulator builds need no signing. For a device, select your team on both
targets (app + Share extension) — App Group entitlements require the targets
to share a team. The app is iPhone-only (`TARGETED_DEVICE_FAMILY: "1"`).

`App/Info.plist` and `ShareExt/Info.plist` are **generated** from the
`info.properties` blocks in `project.yml` and are git-ignored; edit `project.yml`.

Tests:

```bash
xcodebuild -project JobsmithStandalone.xcodeproj -scheme JobsmithKit \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test    # unit
xcodebuild -project JobsmithStandalone.xcodeproj -scheme JobsmithStandalone \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test    # UI smoke
```

## TestFlight

`scripts/testflight-upload.sh` archives and uploads a build. It is **git-ignored**
and carries no credentials — copy the committed template and supply the App Store
Connect identifiers through the environment:

```bash
cp ios-standalone/scripts/testflight-upload.sh.example \
   ios-standalone/scripts/testflight-upload.sh
chmod +x ios-standalone/scripts/testflight-upload.sh
```

| Variable | Required | Meaning |
|---|---|---|
| `ASC_KEY_ID` | yes | App Store Connect API key id (Users and Access → Integrations) |
| `ASC_ISSUER_ID` | yes | Issuer id from the same page |
| `TEAM_ID` | yes | Apple Developer team id |
| `ASC_KEY_PATH` | no | Defaults to `~/.appstoreconnect/private_keys/AuthKey_$ASC_KEY_ID.p8` |

The key must have the **Admin** role — App Manager cannot mint the distribution
certificate ("Cloud signing permission error"). The private `.p8` lives outside
the repo and is never committed.

```bash
export ASC_KEY_ID=... ASC_ISSUER_ID=... TEAM_ID=...
cd ios-standalone && ./scripts/testflight-upload.sh
```

## Where secrets live

| Secret | Storage | Why |
|---|---|---|
| LinkedIn `li_at` session cookie | **Keychain**, `AfterFirstUnlockThisDeviceOnly` | A live session token — account takeover if it leaks. `ThisDeviceOnly` keeps it out of backups; first-unlock (not `complete`) so background fetches can still read it. |
| LLM API key, Adzuna/USAJobs/BLS keys, profile | `config.json` in the App Group container, file protection `completeUntilFirstUserAuthentication` | Encrypted at rest inside an app-sandboxed container, and readable by the background tasks that need them. |
| Job database, generated résumés | App Group container, same protection class | Same trade-off. |

The trade-off worth knowing: the container is **not** excluded from iCloud/iTunes
backups, so the job DB and résumés survive a device restore — and so does
`config.json` with the LLM key in it. The one credential that would be genuinely
dangerous in a backup (the LinkedIn cookie) is the one held in the Keychain as
`ThisDeviceOnly`, which never leaves the device.

`ConfigStore` falls back to plaintext JSON for the cookie if the Keychain is
unavailable (an unsigned sideload), so the feature still works — it just loses
that protection.

## First run

1. Onboarding imports your resume (PDF/DOCX/paste) and the AI extracts a
   profile for review — or skip and fill it manually in Settings.
2. Connect the AI: LM Studio at `http://<machine-ip>:1234/v1`, or a hosted
   provider with a key.
3. Pick sources and keywords, then **Fetch** from the Inbox and start
   swiping: right to shortlist, left to pass.
4. On a shortlisted job: **Score** → **Tailor** → **Review** (AI Edit,
   preview, approve) → **Apply**, which opens the posting in the **in-app
   Apply browser**. Tap **Autofill** to scan the form and fill it from your
   profile + answer bank + LLM mapping. Attach the tailored résumé/cover
   letter from the answers panel, submit, then confirm whether you applied.

No setup — the Apply browser is built in. Nothing to enable in Safari.

## The apply flow, exactly

**Apply** presents `ApplyBrowserView`, a WKWebView loading the posting
inside the app. Tapping **Autofill** injects the bundled `snapshot.js`
(returns the form's fields), runs the same 4-phase mapper as the desktop
in-process (`FieldMapper`: file inputs → deterministic profile rules →
answer bank → LLM), then injects `fill.js` to set the values and outline
each field by outcome. Because this all runs inside the app, there is no
extension, no native messaging, and no App-Group handoff for apply.

The one thing a WKWebView can't do is set `<input type=file>` — so the
**answers panel** offers tap-to-copy values for anything the injector
missed and a one-tap "Save to Files" export of the tailored DOCX, which
then surfaces in the OS file picker's Recents when you tap the upload
control. An **Open in Safari** button is there as an escape hatch for
pages that block embedded web views or require a login.

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
