# Jobsmith __VERSION__

A standalone macOS app (Apple Silicon), the browser extension zips, and the
Docker image — all built from the same tag.

## What's new in 0.2.2

- **Editable AI prompts** — every prompt Jobsmith sends to your local model
  (job scoring, resume tailoring, cover letters, revisions, parsing,
  auto-apply…) is now viewable and editable from **Settings → Prompts**.
  Placeholders like `{profile_summary}` are filled in at run time, literal
  braces need no escaping, and each prompt has its own Save / Reset to
  Default. Customized prompts persist in `config.yaml`; prompts left at
  default keep picking up built-in improvements.
- **Basic / Advanced settings** — Settings now opens in a leaner Basic mode;
  an Advanced toggle reveals the Auto-Apply, Prompts, and Logs tabs plus
  deeper knobs (scoring tier, context window, cookie import, ATS/Workday
  credentials, BLS, FlareSolverr, max resume entries, AI Edit tier). The
  Auto-Apply settings tab is reachable from the UI again.
- **Save Settings moved to the top** — no more scrolling past every panel to
  save.
- **Tour & setup wizard refreshed** — the product tour has new stops for the
  Advanced toggle and the prompt editor, and its Settings walkthrough matches
  the current tabs; the wizard now points power users at Advanced mode.

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

**First launch** downloads a private copy of Chromium (~150 MB) before the
dashboard appears — expect a few minutes on the splash screen. Later launches
are fast. App data (config, database, browsers) lives in
`~/Library/Application Support/Jobsmith`.

**Prerequisite:** AI features need [LM Studio](https://lmstudio.ai) running a
local server on `http://localhost:1234` (configurable in Settings). The app
starts and browses jobs fine without it.

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

See the repo README for `docker compose` usage. (The repo/registry is private;
`docker login ghcr.io` with a token first.)

## Checksums

`SHA256SUMS` covers every asset — verify with `shasum -a 256 -c SHA256SUMS`.
