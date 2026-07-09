# Jobsmith — repository layout & the one-line rule

Jobsmith ships **three artifacts, all built from `main`**:

- **iOS app** — `ios-standalone/` (xcodegen + xcodebuild)
- **Desktop DMG** — `frontend/` + `src-tauri/` + the PyInstaller backend sidecar (`scripts/build_desktop.sh`)
- **Docker container** — `Dockerfile` (the self-hosted server), published to GHCR by `scripts/release.sh`

There is exactly **one** of each. If you're looking at two of anything, one is a straggler — consolidate it.

## The one-line rule

- **`main` is the single source of truth.** The iOS app, the DMG, and the Docker image are *always* built from `main`.
- **Feature branches are short-lived.** Branch → do the work → merge back to `main` → delete the branch (local **and** `origin/*`). Don't let a feature branch outlive its merge.
- **One working directory.** Avoid long-lived `git worktree`s. If you must use one for a parallel build, remove it (`git worktree remove`) as soon as you're done — never leave `~/jobsmith-*` worktrees lying around. A worktree-per-feature sprawl is exactly how the July 2026 forge/sync/apply-browser mix-up happened.
- **No uncommitted features.** If a feature exists only as uncommitted changes in a worktree, it's invisible to every branch and every build. Commit it or lose it.

## Before you build or edit — preflight

```sh
git worktree list        # confirm you're in the ONE working dir on main
git status               # no straggler uncommitted features
git branch               # main only (plus any short-lived branch you're actively on)
```

Then build the target you need from `main`:

```sh
scripts/build_desktop.sh          # DMG  -> src-tauri/target/release/bundle/dmg/
# iOS:    see README-IOS-STANDALONE.md
# Docker: docker build .          # or scripts/release.sh to publish
```

## Versioning

`package.json` is the version SSOT; `src-tauri/tauri.conf.json` reads it, and `backend/version.py` + `src-tauri/Cargo.toml` must match (`scripts/release.sh` guards on this). Bump all three together.

## Open follow-up

`wip/uncommitted-features` holds two in-progress features not yet in `main` (an `email_tracking` backend and an iOS AI-provider / "Studio" settings redesign). Merge it into `main` and delete the branch when ready — the normal short-lived-branch flow above.
