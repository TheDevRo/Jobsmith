# Jobsmith iOS (server-connector app)

> **Maintenance only.** This app is the thin server-connector: it needs a
> Jobsmith backend running somewhere to talk to. Active development happens in
> **[`ios-standalone/`](README-IOS-STANDALONE.md)**, which runs the entire
> pipeline on the phone with no server at all — that's the one to install.
> This one is kept building and gets security/compatibility fixes, but no new
> features. The two share no code and can be installed side by side.

The iOS app is the mobile counterpart of the Tauri desktop shell: a native
SwiftUI shell around the same web frontend. The desktop app bundles the
FastAPI backend as a sidecar; on iOS that's impossible (Python + Playwright +
Chromium don't run there), so the app **connects to the Jobsmith server you
already run** — the desktop app on your Mac, Docker, or `start_server.sh` on
a homelab box.

Everything the web dashboard does works unchanged, because it *is* the web
dashboard: fetching, scoring, tailoring, AI Edit, Review Queue, Settings,
notifications. The frontend's existing responsive CSS handles phone-sized
screens.

**Apply Assist** ships as a bundled **Safari Web Extension** (`Jobsmith
Assist`), built from the same `extension/src` as the Chrome/Firefox variants.
Job links and assist launch pages open in an in-app Safari view where the
extension injects the sidebar, exactly like on desktop browsers.

## Layout

```
ios/
├── project.yml                  # xcodegen spec — the .xcodeproj is generated, not committed
├── Jobsmith/                    # SwiftUI app target
│   ├── JobsmithApp.swift        # Entry: setup screen vs dashboard
│   ├── ServerConfig.swift       # Server URL persistence + /api/stats probe
│   ├── WebViewStore.swift       # WKWebView + delegates (links, downloads, JS dialogs)
│   └── Views/                   # ServerSetupView, DashboardContainerView, Safari/Share wrappers
├── JobsmithAssist/              # Safari Web Extension target
│   ├── SafariWebExtensionHandler.swift
│   └── Resources/               # Generated from extension/src (gitignored)
├── JobsmithUITests/             # XCUITest smoke tests (live backend required)
└── scripts/
    ├── sync-extension-resources.sh  # extension/src + manifest.safari.json → Resources/
    └── run-ui-tests.sh              # starts backend if needed, runs the test suite
```

`extension/src/manifest.safari.json` follows the existing
`manifest.<browser>.json` convention. Its only real difference: the
assist-launch handshake content script matches `*://*/assist/launch/*`
instead of just localhost, because on iOS the backend is a remote machine.

## Build

One-time: `brew install xcodegen` (and Xcode 15+).

```bash
cd ios
xcodegen generate          # produces Jobsmith.xcodeproj
open Jobsmith.xcodeproj    # or: xcodebuild -scheme Jobsmith -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build
```

A pre-build phase re-syncs the extension resources from `extension/src` on
every build, so extension edits flow into the iOS bundle automatically.

### Running on a real device

1. Open the project in Xcode, select the Jobsmith target → Signing &
   Capabilities → pick your personal team (free Apple ID works).
2. Do the same for the JobsmithAssist target.
3. Build & run to your phone.

Free-account profiles expire after 7 days; re-deploy from Xcode when that
happens. Proper distribution (TestFlight/App Store) needs a paid Apple
Developer account and is deliberately not wired up yet — same posture as the
unsigned desktop build.

## First run

1. Make sure your Jobsmith server is reachable from the phone
   (same Wi-Fi/LAN, or Tailscale).
2. Enter its address on the welcome screen — `192.168.1.100:8888` style.
   Port 8888 and `http://` are assumed if omitted. The app probes
   `/api/stats` before accepting it.
3. **Shake the device** (or use the error screen's button) to change the
   server later.

### Enabling Apply Assist

1. iOS Settings → Apps → Safari → Extensions → **Jobsmith Assist** → enable,
   and grant access to the sites you apply on ("All Websites" is simplest).
2. Open the extension's popup (puzzle icon in Safari) and set the backend
   URL to your server address plus the extension token from the desktop
   Settings → Extension page — same pairing flow as Chrome/Firefox.
3. From the dashboard, **Apply Assist** opens the posting in the in-app
   Safari view; the extension takes it from there.

## Behavior notes

- The webview ships the UA token `JobsmithiOS/<version>` (deliberately *not*
  `JobsmithDesktop`: that token makes the frontend route external links
  through the backend's `/api/system/open-url`, which would open the browser
  on the server machine).
- External links (job postings, `target="_blank"`) open in an in-app
  `SFSafariViewController`; the dashboard itself never leaves the webview.
- Resume/cover-letter DOCX downloads surface the iOS share sheet — save to
  Files, AirDrop, or attach in another app.
- Plain-`http` LAN servers require the ATS exception already set in the
  app's Info.plist (`NSAllowsArbitraryLoads`) plus the local-network
  permission prompt on first connect.

## Tests

```bash
ios/scripts/run-ui-tests.sh    # XCUITest smoke suite on the simulator
```

Covers: first-run connect flow (typed address → dashboard), dashboard render
inside the WKWebView, and in-page SPA navigation (sidebar → Job Feed). The
suite talks to a real backend on `127.0.0.1:8888`; the script starts one from
`.venv` if nothing is listening.

## Known gaps (out of scope for now)

- **No signing/distribution setup.** Simulator builds work out of the box;
  device installs need your own team selected in Xcode.
- **Safari extension is untested on-device.** It builds, bundles, and uses
  only APIs iOS Safari supports (`storage`, `scripting`, `tabs`,
  `webNavigation`, `cookies`, `runtime`), but the sidebar/drag interactions
  were designed for desktop pointers — expect rough edges on touch until
  it's exercised on hardware.
- **Version bumps are manual.** `MARKETING_VERSION` in `ios/project.yml`
  mirrors `package.json` (0.2.2) but isn't wired into `scripts/release.sh`.
- **No push notifications.** The dashboard's in-page notification bell works
  (it polls); native push would need server work.
