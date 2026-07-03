# Jobsmith Browser Extension

Replaces the Playwright-based applicant-assist sidebar. Runs inside the
user's real Chrome or Firefox, uses their existing cookies/session, and talks
to the Jobsmith backend on `localhost:8888`.

## Status

**v0.3 — scan + autofill + highlight + resume drag-source.** The side panel
maps fields via the backend (profile / answer bank / local LLM), fills forms
using the native-setter trick (works on React/Vue/Angular), outlines each
field by outcome, and exposes the tailored resume + cover-letter DOCX as
draggable tiles that drop into native `<input type=file>` controls.

## Build

```bash
./extension/scripts/build.sh
```

Produces:
- `extension/dist/chrome/`  — load unpacked at `chrome://extensions` (Developer Mode on → Load unpacked). Persists across restarts.
- `extension/dist/firefox/` — unsigned dir for quick iteration via `about:debugging#/runtime/this-firefox` → Load Temporary Add-on (cleared on restart).
- `extension/dist/*.zip`    — packaged artifacts the backend serves at `/api/extension/download/{chrome,firefox}`.

A signed `.xpi` in `extension/dist/firefox/web-ext-artifacts/` survives rebuilds
(build.sh stashes it) and is excluded from the zips.

In the app, Settings → Applicant Assist → **Get for Chrome / Get for Firefox**
calls `POST /api/extension/save/{browser}` (loopback only), which copies the
extension into `~/Downloads` and reveals it — the desktop shell's webview can't
download files, so the backend writes to disk directly. Chrome gets the
unpacked folder; Firefox gets the signed `.xpi` when one exists, otherwise the
unpacked folder for a temporary install.

## Install (permanent)

**Chrome / Edge / Brave:** `chrome://extensions` → enable Developer Mode → **Load unpacked** → pick `extension/dist/chrome/`. Persists across restarts.

**Firefox (Mozilla-signed):** Firefox removes temporary add-ons on restart, so a permanent install must be signed. Build the signed `.xpi` once:

```bash
cd extension/dist/firefox
web-ext sign --channel=unlisted --api-key="$AMO_JWT_ISSUER" --api-secret="$AMO_JWT_SECRET"
```

(Generate `AMO_JWT_ISSUER`/`AMO_JWT_SECRET` at <https://addons.mozilla.org/developers/addon/api/key/>.) This drops a signed `.xpi` into `extension/dist/firefox/web-ext-artifacts/`. Then either:

- Run the backend and open Settings → Applicant Assist → **Install for Firefox (signed)** (served from `/api/extension/firefox-xpi`); Firefox prompts to add it, **or**
- `about:addons` → gear ⚙️ → **Install Add-on From File…** → pick the `.xpi`.

> Re-signing requires a bumped `version` in `src/manifest.firefox.json` each time — AMO rejects duplicate versions.

## Configure

1. Start the backend (`./start_server.sh`). The token will be logged on startup
   and persisted at `data/extension_token.txt`.
2. Click the extension's toolbar icon → popup opens.
3. Paste the token into the **Extension token** field, click **Save**.
4. Click **Test connection** — should report "Connected. Token OK."

## Use

- Click the toolbar icon → **Open panel** (Chrome) or open the sidebar (Firefox).
- Navigate to a job application page.
- Enter the **Job ID** (from the Jobsmith UI) and click **Load**. This
  pulls the tailored resume + cover-letter DOCX as draggable tiles, and lets
  the LLM see the job description when mapping fields.
- Click **Scan** → backend maps every visible field via profile / answer bank
  / local LLM.
- Click **Autofill** → fields are filled in your real browser. Outlines show
  the outcome (green=filled, yellow=low confidence, gray=skipped,
  red=failed/missing).
- **Click any field card** in the panel to copy its value to the clipboard
  (handy for fields the autofill can't reach) — a toast confirms the copy.
- Toggle **Auto-scan** off to stop the panel scanning/polling in the
  background; it then acts only when you click Scan/Autofill.
- Drag the **Resume** or **Cover Letter** tile onto the ATS's file input.

## Layout

```
extension/
  src/
    manifest.chrome.json    MV3, sidePanel
    manifest.firefox.json   MV3, sidebar_action
    background.js           SW / background script
    popup.html / popup.js   First-run config
    sidepanel.html / .js    Main UI
    common/
      api.js                Shared backend client (token, fetch wrapper)
      snapshot.js           Injected into the active tab to grab field list
      fill.js               Injected into the active tab to fill + highlight fields
  scripts/build.sh          Produces dist/chrome and dist/firefox
```
