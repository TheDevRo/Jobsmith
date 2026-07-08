# Jobsmith sync — implementation status

Serverless, folder-based, two-way last-writer-wins sync between the desktop
backend and the iOS standalone app. No account, no server: an OS file provider
(iCloud Drive / Dropbox / a Docker bind mount) moves the bytes; each side merges
per record. Contract lives in the `jobsmith-sync` repo and is vendored under
`backend/sync/` (see VENDOR.md).

## What runs where

| Piece | Location | Status |
|---|---|---|
| Merge oracle + vectors + schemas + profile map | `backend/sync/` (vendored) | ✅ tested |
| Desktop engine (export/import over SQLite) | `backend/sync/engine.py`, `entities.py` | ✅ tested |
| Desktop transport (manifest, compaction) | `backend/sync/transport.py` | ✅ tested |
| Content-addressed documents | `backend/sync/documents.py` | ✅ tested |
| Desktop wiring (service + `/api/sync/*` + poller) | `backend/sync/service.py`, `routers/sync.py`, `main.py` | ✅ tested |
| Swift merge + `JSONValue` | `JobsmithKit/.../Sync/SyncMerge.swift`, `JSONValue.swift` | ✅ host-verified |
| Swift field mappers (camel↔snake, profile) | `JobsmithKit/.../Sync/SyncEntities.swift` | ✅ host-verified |
| Swift documents + folder/compaction | `JobsmithKit/.../Sync/DocumentStore.swift`, `SyncFolder.swift` | ✅ host-verified |
| Cross-language agreement (Swift ↔ Python) | `tests/test_sync_crosslang.py` | ✅ tested |
| Swift GRDB engine (rows ↔ change log) | `JobsmithKit/.../Sync/SyncEngine.swift` | ✅ simulator-verified |
| iOS iCloud/Files provider + `NSFileCoordinator` | `JobsmithKit/.../Sync/SyncCoordinator.swift` | 🔶 compiles; device-pending |
| App glue: Profile⇄dict + device id + cycle | `JobsmithKit/.../Sync/SyncManager.swift` | ✅ simulator-verified |
| Settings → Sync screen (SwiftUI) | *pending* | ⛔ app UI |

## Verification

Python (24 tests): `python3 -m pytest tests/test_sync_*.py`
  - conformance (vectors + invariants), engine round-trip / LWW / tombstone /
    cross-schema key preservation, documents, transport, service (incl. a full
    document round-trip A→folder→B), and cross-language.

Swift core is **pure Foundation/CryptoKit** (no GRDB, no app deps), so it is
verified on the host with `swiftc`:
  - merge conformance against the same vendored vectors — 3/3, matching Python;
  - documents + folder/compaction logic — all pass.

`tests/test_sync_crosslang.py` compiles the real Swift sources
(`JSONValue`+`SyncMerge`+`SyncEntities`) into a host tool and checks both
directions: a Swift-emitted change log imports correctly into the desktop engine
(job/application/profile land; an ATS secret is stripped by the Swift mapper),
and the Swift merge of a Python-produced folder equals the Python oracle.

The GRDB `SyncEngine` + conformance were run for real on an iOS Simulator from a
throwaway integration tree (branch `sync-ios-integration`: the iOS app branch +
the sync sources + the `Data/` layer). **`JobsmithKitTests`: 7/7 pass** —
`SyncEngineTests` (export→folder→import round-trip, last-writer-wins, tombstone
cascade, profile base-overlay) and `SyncConformanceTests` (vectors + invariants).

> ⚠️ **`.gitignore` bug (repo-wide).** Line `data/` in the root `.gitignore` is
> unanchored, so on macOS's case-insensitive filesystem it matches the iOS
> source dir `ios-standalone/JobsmithKit/Sources/JobsmithKit/Data/`
> (`AppDatabase.swift`, `Models.swift`, `JobStore.swift`, …). Those files have
> **never been committed on any branch** — they exist only in the working tree.
> That's why the package won't build from a fresh checkout. Fix: anchor the
> pattern (e.g. `/data/` plus `/backend/data/`) so only runtime data dirs are
> ignored, then commit the `Data/` source. The GRDB `SyncEngine` depends on it.

## Remaining integration

Both the `.gitignore` fix and the iOS integration are done on this branch
(`sync`, off the iOS app branch): the sync sources, backend wiring, and
`SyncManager` are present; `JobsmithKitTests` (11) and the Python suite (24)
pass. What's left:

1. **Settings → Sync screen (SwiftUI)** — a toggle + folder picker (iCloud vs.
   Files) that calls `SyncManager.shared.syncOnce(...)` and a periodic/background
   trigger. Deferred here to avoid clobbering in-progress `SettingsView` work.
   The one call it needs:
   ```swift
   try await SyncManager.shared.syncOnce(
       folder: try SyncCoordinator.resolveFolder(.iCloud(containerId: nil)),
       db: AppDatabase.shared(),
       docsLocalDir: AppGroup.containerURL.appendingPathComponent("sync-docs"))
   ```
2. **Merge onto the app branch** — this branch's `backend/main.py`,
   `routers/__init__.py`, and `config.example.yaml` edits will conflict with the
   uncommitted email-tracking WIP on the app branch (both touch the lifespan /
   router list / example config); resolve by keeping both.
3. **Device verification** — real iCloud needs the app's iCloud entitlement and
   a signed-in device; `SyncCoordinator` compiles but isn't device-tested.

## Manual device end-to-end (Step 8)

Real iCloud sync between a Mac build and an iPhone can't be automated headless:

1. Desktop: Settings → Sync → enable, pick the shared iCloud Drive folder.
2. iOS: enable sync, pick the same iCloud folder.
3. Create/edit a job on iOS → it appears on the desktop after the next poll,
   and vice-versa. Delete on one → tombstone removes it on the other.
4. Docker: bind-mount a synced folder into the container and point
   `sync.folder` at the mount.

ATS-login credentials are never written into the sync folder (enforced by
`SECRET_KEYS` in `profile_map.py` and `SyncEntities.secretKeys` in Swift).
