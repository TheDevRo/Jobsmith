# Contributing to Jobsmith

Thanks for your interest in Jobsmith. This document covers how the repository is
laid out, how to run the tests, and the conventions to follow when you send a
change.

## What this repo builds

Jobsmith ships three artifacts, all built from `main`:

- **iOS app** — `ios-standalone/` (xcodegen + xcodebuild; see `README-IOS-STANDALONE.md`)
- **Desktop app** — `frontend/` + `src-tauri/` plus the PyInstaller backend sidecar (`scripts/build_desktop.sh`)
- **Docker image** — `Dockerfile`, the self-hosted server

There is exactly one of each. `main` is the source of truth for all three.

See `ARCHITECTURE.md` for a tour of the backend, the browser extension, and the
sync engine.

## Development setup

```sh
./setup.sh                 # creates .venv and installs Python + Node deps
./start_server.sh          # backend + UI on http://localhost:8888
```

Developer one-offs (a single-URL apply debugger, the LinkedIn profile bootstrap,
an AI-navigator smoke test) live in `scripts/dev/` and are not part of any
shipped artifact.

## Running the tests

```sh
.venv/bin/python -m pytest tests/ -q -m "not integration"   # backend
npm test                                                    # extension + frontend JS
```

Tests marked `integration` need live external services (a running LLM endpoint, a
real browser) and are deselected by default. The iOS test suites run through
Xcode — see `README-IOS-STANDALONE.md`.

CI runs the backend suite, the Node suites, an extension↔iOS JavaScript drift
check, and the iOS simulator tests. Please make sure the two commands above pass
locally before opening a pull request.

## Branches and pull requests

- Branch off `main`, keep the branch focused, and open a pull request against
  `main`.
- Keep feature branches short-lived: branch → do the work → merge → delete the
  branch. A long-running branch drifts from the three build targets above.
- Write tests for behavior you add or fix. If you change shared logic, check
  whether it has a counterpart on another platform (the sync engine and the form-fill
  JavaScript both have Python and Swift twins that are checked for drift in CI).

## Versioning

`package.json` is the version single source of truth.

- `src-tauri/tauri.conf.json` reads the version from it.
- `backend/version.py` and `src-tauri/Cargo.toml` must be kept in step.

Bump them together with `scripts/bump_version.sh <version>`; the release process
guards on all of them agreeing (`scripts/bump_version.sh <version> --check`).

## License

Jobsmith is licensed under the GNU Affero General Public License v3.0
(`LICENSE`). By contributing, you agree that your contributions are licensed
under the same terms.
