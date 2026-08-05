<!-- notes-updated-for: 0.2.8 -->
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

**Zero-setup AI on Apple Silicon.** On macOS 26+ with Apple Intelligence
enabled, Jobsmith can now run scoring and other short AI tasks on Apple's
built-in on-device model — free, private, offline, and no server to install.
The setup wizard offers it automatically when no AI server answers, and
Settings → AI lets you choose it per model tier. Resume and cover-letter
generation stays on your configured endpoint (LM Studio, Ollama, or a hosted
provider) — the on-device model is too small to write good documents, and
Jobsmith won't pretend otherwise.

**Easier to start.** A package of first-run improvements for new users:

- The app now tells you when the **AI server is unreachable** (or the
  configured model isn't loaded) with a banner and a fix-it button — no more
  silent "Scored 0 jobs (40 failed)".
- A **getting-started checklist** on the home screen tracks AI, profile, first
  fetch, first shortlist, and extension pairing until all are done.
- Empty states name the actual blocker, the Inbox **Fetch button actually
  fetches**, the product tour waits until you have jobs to look at, and a
  dozen bits of jargon got plain-language tooltips.

**One mental model per screen.** The Deck/Classic split is gone in favor of
per-view toggles — Inbox flips between cards ⇄ list, Pipeline between board ⇄
table, and your old preference migrates automatically. The pipeline funnel is
now a clickable stage filter, Settings went from nine tabs to five (with a
Basic mode that shows only the essentials), and Activity became a proper Home
with a single **Fetch & Score** action for the everyday loop. Every rendering
and capability survives — only the parallel structures went away.

**Recycle bin.** Passed and deleted jobs land in a recycle bin with undo,
restore, and permanent-erase, instead of vanishing.

**Fixed.**

- **Firefox extension "NetworkError" on fresh installs** — the panel now
  routes backend calls through the extension's background process, so it works
  immediately without manually granting host permissions. The Mozilla-signed
  XPI ships inside the app.
- **Duplicate tailoring runs** — dragging a card to Tailoring after the board
  had been open a while could fire the run many times over and pile up
  duplicate "Ready to Review" drafts. Fixed at every layer, and the app cleans
  up existing duplicates on first launch.

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
