<!-- notes-updated-for: 0.2.9 -->
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

A security and reliability release. No new features; everyone on 0.2.8
should update.

**Security**

- The apply-assist launch page no longer runs script from a malicious job
  posting's apply URL (only http/https URLs are accepted, and the embedded
  data is escaped).
- Pages served from other localhost ports (another dev server, a local tool)
  can no longer make state-changing requests to Jobsmith.
- The on-device AI bridge rejects DNS-rebinding requests and oversized
  bodies.
- The access token and the daily database backups are now readable only by
  your user account.

**Sync**

- A newly-added device can no longer overwrite your real profile and
  settings with its blank defaults on its first sync.
- iOS won't sync if its database failed to open.

**macOS app**

- Cmd+Q asks before quitting while a search or scoring run is active.
- Clicking the Dock icon brings the window back when Jobsmith is hidden in
  the menu bar.
- Force-quitting or crashing no longer leaves the backend running and
  holding port 8888.
- Old Playwright browser downloads are cleaned up (saves ~150 MB per
  upgrade).

**Docker**

- LAN, Tailscale and reverse-proxy hostnames work again (they were rejected
  with a 400). The token hint now points at `data/extension_token.txt`.

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
