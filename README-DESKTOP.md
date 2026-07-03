# Jobsmith Desktop (Tauri)

The desktop app is a thin [Tauri](https://tauri.app) shell around the existing
FastAPI backend. Tauri opens a native window; the backend runs as a bundled
**sidecar** (a PyInstaller one-file build of `backend/main.py`) that the shell
spawns on launch and kills on exit. The UI is the same web frontend, served by
the sidecar at `http://127.0.0.1:8888`.

**The dev loop does not change.** Day-to-day you still run `./start_server.sh`
and edit Python with uvicorn `--reload`. Tauri only matters when you want to run
*inside the desktop window* or ship an installer.

## Where user data lives

The desktop build sets `JOBSMITH_HOME` to an app-data directory so nothing is written
into the (read-only, update-replaced) app bundle:

- macOS: `~/Library/Application Support/Jobsmith/`

Config, the SQLite DB, resumes, and sessions all live there. Playwright's
Chromium is downloaded into `JOBSMITH_HOME/browsers/` on first launch (it is too
large to ship in the installer), so the first run takes a few minutes. Assets
shipped *with* the app (the frontend, `config.example.yaml`) stay in the bundle —
see `backend/paths.py` for the split.

## One-time setup

```bash
# 1. Rust toolchain (Tauri needs it)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
. "$HOME/.cargo/env"

# 2. Tauri CLI + PyInstaller
npm install                       # brings @tauri-apps/cli (devDependency)
.venv/bin/pip install pyinstaller
```

## Run in the desktop window (dev)

`tauri dev` points the webview at your live dev server via `devUrl`, so it does
**not** manage Python for you — start the backend yourself first:

```bash
./start_server.sh          # terminal 1 — the real backend on :8888
npx tauri dev              # terminal 2 — opens the native window at :8888
```

Edit Python → uvicorn reloads → refresh the window. Same loop as the browser.

## Build an installer (unsigned)

```bash
scripts/build_desktop.sh
```

This PyInstaller-builds the sidecar, stages it at
`src-tauri/binaries/jobsmith-backend-<target-triple>` (the name Tauri
requires), then runs `tauri build`. Output lands in
`src-tauri/target/release/bundle/` — both `macos/Jobsmith.app` and
`dmg/Jobsmith_<version>_aarch64.dmg`.

Use `scripts/build_desktop.sh --sidecar-only` to rebuild just the Python binary
(fast) without recompiling the Rust shell.

## Port selection

Release builds prefer `127.0.0.1:8888`; if it's taken (e.g. a Docker Jobsmith
is running), the shell picks a free port and passes it to the sidecar via
`JOBSMITH_PORT`. Dev mode (`tauri dev`) always uses 8888 to match `devUrl`.

## Versioning

`package.json` is the single source of truth. `src-tauri/tauri.conf.json`
points its `version` field at it; `src-tauri/Cargo.toml` and
`backend/version.py` mirror it (release.sh checks the Python one matches
before tagging).

## Cutting a release

Releases are built locally and published with the `gh` CLI (private repo —
macOS CI minutes bill at 10x and the audience is us):

```bash
scripts/release.sh --dry-run   # build everything, stage assets, no publish
scripts/release.sh             # tag v<version>, push, create draft release
```

The script guards on a clean tree and matching versions, builds the desktop
app and the extension zips, stages the dmg / app.tar.gz / extension zips /
SHA256SUMS under `build/release-assets/`, tags (which also fires the GHCR
Docker publish workflow), and creates a **draft** GitHub release with notes
rendered from `packaging/release-notes.md`. Review the draft and publish it.

## Known gaps (out of scope for now)

- **Unsigned build.** macOS Gatekeeper will refuse to open it on first
  double-click — System Settings → Privacy & Security → "Open Anyway", or
  `xattr -dr com.apple.quarantine "Jobsmith.app"`. Signing/notarization needs
  an Apple Developer account and is deliberately not wired up yet.
- **No auto-update.** No update server is configured.
- **macOS Apple Silicon only so far.** The scaffold is cross-platform in
  principle, but only the aarch64 macOS build has been exercised. Windows,
  Linux, and Intel macOS notes live in the release plan; Docker covers them
  meanwhile.
