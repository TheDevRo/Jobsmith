<!-- notes-updated-for: 0.3.0 -->
<!--
  Template for scripts/release.sh. __VERSION__ / __EXT_VERSION__ are substituted
  at render time. Before every release: rewrite the "What's new" section, then
  bump the notes-updated-for marker above to the version being shipped —
  release.sh refuses to tag while it lags package.json, so last release's notes
  can't ship again by accident.
-->

# Jobsmith __VERSION__

A standalone macOS app (Apple Silicon), the browser extension zips, and the
Docker image — all built from the same tag.

## What's new in __VERSION__

A feature release: AI writing you can steer and trust, requirement coverage,
more job sources, and an easier setup.

**AI writing**

- **Facts about me**: short facts (certifications, clearances, numbers you're
  proud of) that every resume, cover letter and AI edit can draw on. You can
  also add facts to a single application.
- **AI Edit by section**: rewrite one section of a resume or cover letter, and
  tick "Keep this for future edits" to have an instruction applied to every
  later edit.
- **Revision history with Undo** for resumes and cover letters.
- **Check this**: Jobsmith flags names, numbers and credentials in AI-written
  text that your profile, the posting and your facts don't back up.
- Job posting text is now treated as untrusted in every prompt, so a posting
  can't steer the AI with hidden instructions.

**Finding jobs**

- **Flush inbox**: after time away, clear out Inbox jobs you never decided on
  (all of them, or only ones found more than N days ago). Shortlisted, passed,
  applied and started jobs are kept, a flushed posting comes back if a later
  search still finds it, and the flush syncs to your iPhone.
- **Requirement coverage**: each scored job shows how many of its
  requirements you meet (N/M), with the full list on the job and a "Best
  coverage" sort.
- **Job portals**: Remotive, Himalayas and Jobicy, off by default (Settings →
  Search & sources).
- A **Setup Assistant** (Local / Cloud / Advanced) and sources that work
  without any setup.
- **Quick match**: an optional on-device model (BGE-small-en-v1.5) that scores
  new postings in a fraction of a second, with its own switch in Settings → AI.

**Documents and Apply Assist**

- Resumes and cover letters default to **PDF** (Settings → AI). Existing
  installs keep their current setting.
- The extension has a "Prefer PDF documents" switch, and "Clear" is now
  "Clear Highlights".
- Apply Assist works with a remote HTTPS backend (thanks @salehihassen).
- The Apply Assist pages match the app's look.

**Fixes**

- Manually applied jobs show up in the Applied pipeline again (thanks
  @salehihassen).
- Docker Compose passes `JOBSMITH_EXTERNAL_URL` through and explains a bad
  value.
- Dependency security updates throughout.

The iOS app now lives in its own repo and ships through TestFlight.

## macOS app (Apple Silicon)

Download `Jobsmith___VERSION___aarch64.dmg`, open it, and drag **Jobsmith** to
Applications.

**The app is unsigned**, so macOS will refuse to open it the first time:

1. Double-click Jobsmith.app — macOS shows "Jobsmith is damaged" or "cannot be
   opened because it is from an unidentified developer". Click **Done/Cancel**.
2. Open **System Settings → Privacy & Security**, scroll down, and click
   **Open Anyway** next to the Jobsmith message, then confirm.

Or from a terminal, clear the quarantine flag directly:

```sh
xattr -dr com.apple.quarantine /Applications/Jobsmith.app
```

If the dmg itself won't open, use the `Jobsmith___VERSION___aarch64.app.tar.gz`
asset instead: `tar -xzf` it, move Jobsmith.app to Applications, then apply the
same steps above.

**First launch** downloads a private copy of Chromium (~150 MB) for auto-apply,
but it now downloads **in the background** — the dashboard opens immediately and
Jobsmith shows the install status (with a retry) until it's ready. App data
(config, database, browsers) lives in `~/Library/Application Support/Jobsmith`.

**AI prerequisite:** on macOS 26+ with Apple Intelligence enabled, scoring
works out of the box on the built-in on-device model. For document generation
(and for older Macs), point Jobsmith at an OpenAI-compatible server —
[LM Studio](https://lmstudio.ai) on `http://localhost:1234` by default, or
Ollama / a hosted provider with an API key (configurable in Settings). The app
starts and browses jobs fine without any of it.

If port 8888 is busy (e.g. a Docker Jobsmith is running), the app picks
another port automatically.

## Browser extension

Download `jobsmith-extension-chrome-v__EXT_VERSION__.zip` or
`jobsmith-extension-firefox-v__EXT_VERSION__.zip`.

- **Chrome**: unzip, open `chrome://extensions`, enable Developer mode, click
  **Load unpacked**, select the unzipped folder.
- **Firefox**: the easy path is the Mozilla-signed XPI served by the app
  itself — Settings → Apply Assist → install the extension. (The zip here is
  for development: `about:debugging` → This Firefox → **Load Temporary
  Add-on**, re-loaded after browser restarts.)

The token pairs automatically the first time you launch Apply Assist; paste it
from Jobsmith's Settings into the extension popup only if pairing fails.

## Docker (macOS Intel / Windows / Linux)

The same tag publishes a multi-arch image to GHCR:

```sh
docker pull ghcr.io/thedevro/jobsmith:__VERSION__
```

See the repo README for `docker compose` usage — no login required.

## Checksums

`SHA256SUMS` covers every asset — verify with `shasum -a 256 -c SHA256SUMS`.
