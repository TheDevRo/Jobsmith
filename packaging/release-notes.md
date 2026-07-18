<!-- notes-updated-for: 0.2.6 -->
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

**A redesigned desktop experience.** The desktop app and Docker dashboard get a
standalone UI of their own — inspired by the iOS app, built for a keyboard and
a big screen. Functionally everything is unchanged: same backend, same data,
same sync.

- **Inbox is now a triage stage.** New jobs arrive as one big card — fit ring,
  salary, "why it fits", description — with an Up Next rail beside it. Pass /
  Open / Shortlist with the buttons or entirely from the keyboard (`←` pass,
  `→` shortlist, `Enter` open, `T` shortlist + tailor now, `U` undo). Prefer
  the old list? Press `L` or use the toggle.
- **Pipeline is a kanban board.** Shortlisted → Tailoring → Ready to review →
  Applied → Needs attention, with drag-and-drop between stages (each drop maps
  to the exact action the buttons already performed) and a drop-to-pass zone.
- **Click any posting to peek.** Anywhere outside the classic layout, clicking
  a job pops its full detail out in place — score, fit analysis, description,
  and every action — without navigating away. `Esc` closes.
- **⌘K command palette** — jump anywhere, kick off fetch/score/tailor runs, or
  search your jobs from one keystroke.
- **A calmer shell** — collapsible icon-rail sidebar, a run console with live
  logs in place of the old action-card grid, a dashboard digest column, and a
  global "Now" rail showing what's running.
- **Prefer the old UI?** Settings → Layout → Classic brings the previous
  dashboard back, pixel-identical.
- **Fixed along the way** — HTML5 drag-and-drop now works inside the macOS
  shell, and the pipeline funnel no longer over-asks the API and errors on
  large pipelines.

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
