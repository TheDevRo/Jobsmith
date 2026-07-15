<!-- notes-updated-for: 0.2.4 -->
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

**Jobsmith is now open source, under the AGPL-3.0.** Self-hosting is free and
stays that way.

- **Docker: your settings survive a restart.** This is the big one. Anything you
  saved in the Settings UI — profile, keywords, AI endpoint — was being silently
  discarded when the container restarted, because the config write replaced the
  symlink to your mounted config instead of writing through it. Fixed. Docker
  users should re-enter any settings that went missing.
- **Settings sync across devices (opt-in).** Sync your app configuration —
  search criteria, resume and honesty preferences, ranking weights, AI
  connection, prompt overrides — across your devices and the iOS app, on top of
  the same serverless folder sync your jobs and profile already use. It's
  per-category and off by default (profile aside); machine-local values stay on
  the device.
- **Five resume styles** — `executive`, `ledger`, `banner`, `compact`, `swiss`,
  each with a selectable accent color and embedded PDF fonts, all still
  ATS-friendly. The style picker now shows you the resume each one produces.
- **Pipeline intelligence** — application outcome tracking, follow-up reminders,
  auto-ghosting of stale applications, a duplicate-application guard, and a
  digest weighted by what actually converts for you.
- **Security pass** — the dashboard API is authenticated, SSRF and XSS holes are
  closed, `javascript:` URLs are blocked in the frontend, the extension no
  longer requests `<all_urls>` and validates its RPC senders, and the Docker
  container runs as a non-root user bound to loopback by default.
- **Apply fixes** — Workday file uploads were being skipped (file inputs now
  resolve deterministically), and tailored documents attach during in-app apply.
- **iOS** — a long search now survives leaving the app; importing a PDF résumé
  no longer drops every employment date; LinkedIn sign-in leads the setup wizard
  and, when connected, your own session is used for LinkedIn searches instead of
  anonymous guest access.

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

**Prerequisite:** AI features need an OpenAI-compatible server —
[LM Studio](https://lmstudio.ai) on `http://localhost:1234` by default, or
Ollama / a hosted provider with an API key (configurable in Settings). The app
starts and browses jobs fine without one.

If port 8888 is busy (e.g. a Docker Jobsmith is running), the app picks
another port automatically.

## Browser extension

Download `jobsmith-extension-chrome-v__EXT_VERSION__.zip` or
`jobsmith-extension-firefox-v__EXT_VERSION__.zip`.

- **Chrome**: unzip, open `chrome://extensions`, enable Developer mode, click
  **Load unpacked**, select the unzipped folder.
- **Firefox**: open `about:debugging` → This Firefox → **Load Temporary
  Add-on** and pick the zip (re-load after browser restarts).

Then paste the extension token shown in Jobsmith's Settings page into the
extension popup.

## Docker (macOS Intel / Windows / Linux)

The same tag publishes a multi-arch image to GHCR:

```sh
docker pull ghcr.io/thedevro/jobsmith:__VERSION__
```

See the repo README for `docker compose` usage — no login required.

## Checksums

`SHA256SUMS` covers every asset — verify with `shasum -a 256 -c SHA256SUMS`.
