# Jobsmith — Improvement Plan (Status)

> **Status as of 2026-07-12.** Worked through on branch `fix/search-filter-consistency`
> (commits `ae21641`…`22f9f3f`). Original plan generated 2026-07-12.
>
> **Legend:** ✅ done · 🟡 partial (scope deliberately reduced — reason given) ·
> ❌ not done · 🚫 **rejected** (the plan was wrong; doing it would have caused a bug)
>
> Anything marked 🚫 or 🟡 has its reasoning inline. Those are the interesting rows —
> read them before re-opening an item.

---

## Verification actually run

| Surface | Result |
|---|---|
| Backend | **742 passed**, 3 skipped (`pytest tests/`) |
| Extension + frontend | `npm test` **pass** — 66 checks (19 new) |
| iOS | **297 KitTests, 0 failures**; `JobsmithKit` + app target **BUILD SUCCEEDED** |
| Tauri shell | `cargo check` **pass** |
| Live server | `Host: attacker.com` → **400** · SSRF `169.254.169.254` → **refused** · `/api/health/live` → 200 unauth · log now `0600` · token no longer logged |
| **Docker** | ⚠️ **NEVER BUILT** — no daemon on the build machine. SEC-07/SEC-12/REL-13/PKG-01 are code-complete but **unexecuted**. |

---

## ⚠️ Open actions for a human

1. **The extension token is in `data/logs/jobsmith.log` in cleartext** — 13 historical
   lines (Jul 3–6), from before SEC-06. The fix stops *future* logging; it cannot
   scrub history. **Rotate the token** (Settings → Integrations) or delete the log.
2. **Build the Docker image before trusting it.** `USER pwuser`, the HEALTHCHECK and
   `requirements.lock` have never been executed.
3. **App Store Connect key rotation — NOT urgent** (see SEC-09). Your call.

---

## Behaviour changes shipped (expect these)

- **First use on a new ATS domain now costs one permission click** (SEC-11).
- **A 403 from one job board now aborts that source's whole run** (REL-02).
- **Off-machine dashboard users (i.e. every Docker user) must paste a token once** (SEC-01).
- **iOS is iPhone-only** now (UX-06).

---

## 1. Security

| ID | Status | Notes |
|----|--------|-------|
| **SEC-01** | ✅ | `routers/_auth.py`: loopback passes, everyone else needs the token (header **or cookie**). **The plan's version breaks Docker** — the container sees host traffic from the bridge gateway (172.17.0.1), not 127.0.0.1, so every Docker user is "off-machine" and would have been 401'd out with no way in. Added `POST /api/auth/login` (token→cookie) **and a token gate in the SPA**. `/api/config` masks secrets off-machine; `POST` strips the mask so an untouched field can't overwrite the real value. Escape hatch: `JOBSMITH_ALLOW_INSECURE=1`. |
| **SEC-02** | ✅ | Host is resolved and **every** returned IP must be public (a public *name* can point at 127.0.0.1). |
| **SEC-03** | ✅ | `safeHref()` on all rendered hrefs. **The plan missed 2 sinks** (`settings.js` board URLs; `openExternal()` took `job.url` directly). **Step 3 rejected** 🚫 — `core.js`'s `IS_DESKTOP_SHELL` early-return is the desktop *link router*, not a scheme guard; removing it would funnel every external link through the backend and fix nothing. Real guard added instead. |
| **SEC-04** | ✅ | Scanned ATS labels escaped before `innerHTML`. Plan's `:298`/`:144` were already safe (`textContent`). |
| **SEC-05** | ✅ | `TrustedHostMiddleware` **when bound to loopback**. Not pinned when bound wide (Docker/LAN reach us by an IP we can't enumerate) — there, the token requirement is the control, which rebinding cannot satisfy. |
| **SEC-06** | ✅ | Token no longer logged; log + rotations `0600`. **Residual: see Open Actions #1.** |
| **SEC-07** | ✅⚠️ | `chown` + `USER pwuser`. **Unverified — image never built.** |
| **SEC-08** | 🟡 | LinkedIn `li_at` → Keychain (`AfterFirstUnlockThisDeviceOnly`), migrating off plaintext; file protection on config/db. **Step 3 rejected** 🚫 — `config.json` sits at the container **root**, so excluding "the dir holding it" would exclude the job DB and résumés too, **silently destroying user data on device restore**. The cookie is `ThisDeviceOnly` now, so it never enters a backup regardless. |
| **SEC-09** | ✅ | **Reclassified: NOT critical.** The repo is **private** and the `.p8` was **never committed** — the leaked values are *identifiers* (key id, issuer id, team id), and team ids ship in every signed binary. Did the hygiene: untracked, gitignored, env-var reads, sanitized `.example`. **No rotation, no history rewrite.** Note the same team id is also load-bearing in `project.yml`. |
| **SEC-10** | 🟡 | Sender validation done (runtime id + extension-page URL + `fnName` allowlist + rebuilt `InjectionTarget`). **`web_accessible_resources` narrowing skipped**: the docked panel must be iframable on arbitrary ATS domains, so narrowing `matches` breaks the primary flow. `use_dynamic_url` skipped — can't verify blind, and getting it wrong silently kills the panel. Residual risk low (RPC now refuses non-extension senders). |
| **SEC-11** | ✅ | `<all_urls>` → `optional_host_permissions`. Neither browser allows `permissions.request()` without a user gesture, so **the popup does the asking**, not the background. ⚠️ costs one click on a new domain. |
| **SEC-12** | ✅⚠️ | Auto-generates + prints a VNC password instead of `-nopw`. Needed plumbing `NOVNC_BIND` into the container env — it was host-side only, so the entrypoint literally could not see it was exposed. **Unverified — image never built.** |
| **SEC-13** | ✅ | CORS pinned to the served origin; `allow_credentials` **off** (cookie is same-origin + `SameSite=strict`; extension uses a header). |
| **SEC-14** | ✅ | `config.yaml` → `0600` (it holds the passwords). Assist **setup token is now ephemeral**, traded for the persistent token in the checkin body instead of being embedded in the launch-page DOM. `postMessage` **receiver** now pins `e.source` — *the plan pointed at the wrong file*; the unvalidated listener was in `applicant_assist.py`, injected into the third-party job page. Diag files off `/tmp`. Tauri CSP set. **`ast.literal_eval` reassessed** 🚫 — it is **not** an RCE vector (literals only: no calls, no attribute access) and the `isinstance` guard it asked for **already existed**. Bounded the input instead, which is the actual failure mode (parser stack exhaustion). |

## 2. Reliability

| ID | Status | Notes |
|----|--------|-------|
| **REL-01** | ✅ | `score` throws instead of persisting a fake `0`. One offline "Score all" was permanently poisoning every unscored job. |
| **REL-02** | ✅ | `SourceAuthError` on 401/403. ⚠️ A 403 on one board now aborts that source's run. |
| **REL-03** | ✅ | Sync tests moved into `KitTests` so they actually run — **and two were failing**: a GRDB reentrancy crash and a stale assertion. Both were *test* bugs; fixed. This is exactly why the item mattered. |
| **REL-04** | ✅ | Chromium install on a daemon thread + `/api/system/browser-status` + retry + SPA banner. Deadline 600s → 60s. |
| **REL-05** | ✅ | Glob guard gone (a Playwright bump left a stale `chromium-*` matching forever); always run the idempotent install; prune stale revisions. |
| **REL-06** | ✅ | `schema_version` + 20 numbered migrations. Legacy DBs baseline cleanly; real errors (locked/corrupt) are now loud instead of `except: pass`. Duplicate `idx_jobs_status` removed. |
| **REL-07** | ✅ | 409 on double-POST for fetch/score/tailor/detect/estimate (+ the n8n tailor webhook, which shares the slot). |
| **REL-08** | ✅ | `watch_parent` also polls `JOBSMITH_SHELL_PID` — force-quit never fires `RunEvent::Exit`, so uvicorn was surviving and squatting on 8888. |
| **REL-09** | ✅ | `setTaskCompleted` on expiry (one-shot guarded); refresh tier narrowed + timeout budget. |
| **REL-10** | ✅ | Tolerant decoders; a bad config is **kept** and copied to `config.corrupt.json` rather than silently reset to defaults. |
| **REL-11** | ✅ | SW state → `storage.session`; upload retry is a ~1.5s poll falling back to dropcatch. |
| **REL-12** | ✅ | Manual job selection is sticky against the 3s auto-bind poll. |
| **REL-13** | ✅⚠️ | `/api/health/live` (no AI probe) + HEALTHCHECK. **Healthcheck unverified — image never built.** |
| **REL-14** | 🟡 | Done: sidecar → `shell.log`, `Terminated` → failure screen, HTTP readiness probe, frontend poll backoff + "Reconnecting…". **Not done:** `_MEI` temp-dir leak (depends on PKG-05); **headed-mode Docker process supervision** (X-socket poll / x11vnc restart loop) — still `sleep 1`. |

## 3. Performance

| ID | Status | Notes |
|----|--------|-------|
| **PERF-01** | 🚫 | **Rejected as specified.** The plan says "aiosqlite serializes on its own thread, so a shared connection is safe." That is **half-true**: it serializes *statement execution*, not *transaction boundaries*. On one shared connection, coroutine A's `commit()` commits coroutine B's half-finished write — a correctness regression traded for a micro-optimisation, in an app whose background workers write while requests are served. Took **the plan's own stated fallback**: WAL is persistent, so it's now set once in `init_db` instead of on all 42 opens. |
| **PERF-02** | ✅ | mtime-cached config; falls back to last-known-good on a YAML error instead of silently becoming `{}` (i.e. running on defaults). |
| **PERF-03** | 🟡 | Backoff done (REL-14). **SSE not done** — the plan itself calls it "longer-term". |

## 4. Design / UX

| ID | Status | Notes |
|----|--------|-------|
| **UX-01** | ✅ | VoiceOver labels/actions on the card deck (the swipe gesture is unusable under VoiceOver), Pipeline, Apply, Activity. Plan step 4 was **already done** (the fit score was always drawn as text). |
| **UX-02** | ✅ | Dynamic Type; also fixed fixed-size frames that clip at AX5, which the plan only hinted at. |
| **UX-03** | ✅ | **Plan's premise was wrong** — `ink`/`slate`/`steel` are *dead code*, and there is no Light Mode leak. But there **was** a real bug underneath: `HeatChip` drew **white text on the amber heat ramp at ~2.2:1 contrast**, far below AA. Now picks fg by measured WCAG luminance. |
| **UX-04** | ✅ | Breakpoints, `prefers-color-scheme` (fixing a latent bug: "dark" was encoded by *removing* the attribute, so explicit-dark and no-preference were indistinguishable), keyboard-operable cards, shared `renderError`. |
| **UX-05** | 🟡 | `SettingsView` 561 → 345 lines; `AIConnectionSettingsView` + `SourceKeysSettingsView` extracted. **`@Bindable` rewrite rejected** 🚫 — `AppModel` persists *only* via `saveConfig { }`, so binding straight into `model.config.*` would mutate memory and **never write to disk**, silently breaking settings persistence. |
| **UX-06** | ✅ | iOS iPhone-only; desktop window-state + app menu. Spoofed user-agent left alone (out of scope, safe one-liner later). |

## 5. Packaging & Release

| ID | Status | Notes |
|----|--------|-------|
| **PKG-01** | ✅ | `requirements.lock` from the real venv — as a **dependency closure**, not `pip freeze`, which would have baked browser-use + pytest into the runtime image (contradicting PKG-07). |
| **PKG-02** | ✅ | Cargo drift 0.2.1 → 0.2.4; `scripts/bump_version.sh` is the single source. |
| **PKG-03** | ✅ | main-branch + `gh auth` guards; pushes only this tag; `--publish-only` resume; fails if release notes weren't updated. |
| **PKG-04** | 🟡 | "Check for Updates" menu item done (works unsigned). **Signing + notarization not done — needs an Apple Developer account ($99/yr).** |
| **PKG-05** | ❌ | onefile → onedir. **Not attempted:** cannot honestly verify a packaging change without a full PyInstaller + `tauri build` + launch of the bundled app. Blocks the `_MEI` bullet of REL-14. |
| **PKG-06** | ❌ | Cross-platform builds (Intel mac / Windows / Linux). Not attempted. |
| **PKG-07** | ✅⚠️ | Runtime image slimmed; Playwright pinned via `ARG` with a **build-time assert** that it matches the lock; browser-use → `requirements-optional.txt` + lazy import. **Unverified — image never built.** |

## 6. Code Quality

| ID | Status | Notes |
|----|--------|-------|
| **CQ-01** | ✅ | **The plan's "byte-identical today" claim was already false.** `scripts/check_apply_js_sync.py` (CI-gated) compares code with comments stripped — and **immediately caught a live drift**: the REL-11 upload-retry fix had landed in the extension but never reached the iOS copy. Synced. Backend `_SNAPSHOT_JS` left as-is (swapping it to a runtime read is a behaviour change I couldn't verify). |
| **CQ-02** | ✅ | `common/{handshake,storage,permissions}.js`. |
| **CQ-03** | ✅ | `.github/workflows/ci.yml` — pytest + `npm test` + iOS `xcodebuild test` + the JS drift check. Nothing gated a push before this. |
| **CQ-04** | ✅ | `ios/` marked maintenance-only; generated Info.plists untracked; stale Safari-extension docs corrected. |
| **CQ-05** | 🟡 | Done: stagehand refs removed, ARCHITECTURE.md desktop section added. **Not done:** ES-module conversion (the SPA depends on cross-script globals *and* inline `onclick=`, which can't see module scope — every handler needs rewiring); `server_config.py` host/port consolidation. |

## 7. Ease of Use

| ID | Status | Notes |
|----|--------|-------|
| **EOU-01** | ❌ | Store publishing (AMO signing / Chrome Web Store). **External** — requires store accounts + submissions. SEC-11 (the stated blocker) is now cleared. |
| **EOU-02** | ✅ | "Auto-detect from Jobsmith" in the popup; Retry on the panel's unreachable state. Plan suggested opening the launch page — there *is* no launch page without an assist session, so it fetches the token directly. |
| **EOU-03** | ✅ | Non-8888 port banner. |

---

## Appendix — the cross-cutting theme, resolved

The plan's closing call was right: the root risk was that **the dashboard API trusted its
network position instead of authenticating requests.** SEC-01/05/06/13 all stemmed from
"loopback == trusted". That is now fixed at the design level — a shared auth dependency, a
token/cookie exchange so a browser can actually satisfy it, Host-header validation where
loopback trust still applies, and secret redaction for anyone off-machine.

The one thing the plan didn't see: **"loopback" is not what it looks like inside Docker.**
The container sees the bridge gateway, so a literal implementation would have locked every
Docker user out of their own dashboard. That's why the cookie exchange and the SPA token
gate exist — they are load-bearing, not extras.
