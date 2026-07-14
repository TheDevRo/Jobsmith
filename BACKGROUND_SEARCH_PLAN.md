# Background Search & Scoring Plan (iOS standalone)

Goal: a user who starts a job search (or Score-all) and then leaves the app / locks the
screen should still end up with a complete search and scored jobs — without LinkedIn
"erroring out". This plan is written to be executed as-is; all paths are relative to
`ios-standalone/` unless noted.

---

## 0. Ground rules for the executor

- **Branch:** this worktree is currently on `feature/resume-styles-v2` (unrelated,
  in-flight). Create a new branch off `main` (e.g. `feature/background-search-resume`)
  before touching anything. Leave the untracked `beta-release/` directory alone.
- **Project is xcodegen-managed.** After adding or removing any file under
  `ios-standalone/`, run `xcodegen generate` in `ios-standalone/` or the file won't
  join the target. ("No such module JobsmithKit" from SourceKit is index noise.)
- **Build:** `xcodebuild -project JobsmithStandalone.xcodeproj -scheme JobsmithStandalone
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build CODE_SIGNING_ALLOWED=NO`
- **Kit unit tests** (the main test surface for this plan): same command with
  `-scheme JobsmithKit … test`. 210+ tests, fast.
- **UI smoke tests:** `-scheme JobsmithStandalone
  -only-testing:JobsmithStandaloneUITests/SmokeTests … test`.
- **Known-broken test:** `EndToEndWalkthroughTests.testFullPipelineWalkthrough` fails on
  a stale matcher on a clean baseline. Its failure is NOT a regression — don't chase it.
- **Swift 6 concurrency:** new cross-actor callbacks (the delivery hook in 1.2) must be
  `@Sendable`; `FetchPipeline` is an actor, `AppModel` is `@MainActor`.
- No progress notes in this file are required; commit per phase with the repo's
  existing commit style.

---

## 1. Diagnosis — why LinkedIn errors out today

### The core mismatch: ~30 s of background runtime vs a ~10-minute source

`AppModel.fetchJobs()` (`App/AppModel.swift:512`) wraps the fetch in a
`UIApplication.beginBackgroundTask` assertion. That buys **~30 seconds** of continued
execution after the user backgrounds the app — then iOS suspends the process. LinkedIn
(`JobsmithKit/Sources/JobsmithKit/Fetching/LinkedIn/LinkedInSource.swift`) budgets itself
**up to 560 s** (search phase 240 s + detail phase 420 s, total cap 560, outer pipeline
timeout 600 s) with throttled sequential page fetches. The other 9 sources finish in
seconds and survive the window; LinkedIn is always the one still mid-flight when the
process suspends. That is exactly the reported symptom.

### What suspension does to the run (four distinct failure mechanisms)

1. **In-flight sockets die, errors are swallowed as "page done".**
   `LinkedInSource.fetchOnce` (`LinkedInSource.swift:259-271`) turns *any* network error
   into `nil`; `fetchWithLinkedInRetries` returning `nil` makes the caller **stop
   paginating** (`LinkedInSource.swift:91-92`). A suspension-killed request is
   indistinguishable from "LinkedIn is down" — the search silently truncates.

2. **On resume, the outer timeout fires instantly and discards everything.**
   `FetchPipeline.withTimeout` (`Fetching/FetchPipeline.swift:222-235`) races the source
   against `Task.sleep(for: 600s)`. Neither `ContinuousClock` nor the `TimeBudget`
   uptime math (`LinkedInThrottler.swift:52-64`, `ProcessInfo.systemUptime`) pauses
   during **app** suspension. If the user reopens the app minutes later, the deadline
   fires immediately → `SourceTimeoutError` → `group.cancelAll()` → **all jobs LinkedIn
   collected are discarded** (the source's own header comment warns: "cancellation
   discards EVERYTHING collected"). The user sees "Some sources had trouble: linkedin"
   / "timed out" with zero LinkedIn results.

3. **Nothing is persisted until the very end.**
   `FetchPipeline.run` upserts **once, after all sources finish**
   (`FetchPipeline.swift:164-169`). If iOS jetsams the suspended app mid-search, even
   the fast sources' results are lost — there is no partial persistence and no record
   that a search was in progress.

4. **The expiration handler doesn't stop or checkpoint anything.**
   The handler in `fetchJobs()` only ends the assertion (`AppModel.swift:517-520`). The
   pipeline task is left running as the process freezes mid-write, mid-request, with no
   checkpoint and no continuation scheduled.

### Scoring has it worse

`scoreAll` (`App/AppModel.swift:350-378`) has **no background assertion at all**, runs on
the MainActor, and calls the LLM via plain `URLSession.shared` with a 90 s timeout
(`AI/OpenAICompatibleEngine.swift:67,80`). Two compounding policies:

- **First failure aborts the whole batch** (`AppModel.swift:363-365`). That's the right
  call for a dead endpoint, but a suspension-killed request or a phone that left the
  LM-Studio LAN (192.168.1.7 is only reachable at home) hits the same branch — the run
  dies and never resumes.
- There is no persistent notion of "a scoring run is in progress", so nothing resumes
  on foreground return.

### What already exists (build on it, don't duplicate it)

- `App/Background/BackgroundScheduler.swift`: registered `BGAppRefreshTask` +
  `BGProcessingTask` tiers, `UIBackgroundModes` [fetch, processing] and permitted
  identifiers already in `App/Info.plist` / `project.yml:66-71`. The processing tier
  already runs LinkedIn with no budget cap. A careful race-safe `Completion` wrapper
  exists. But these are *scheduled, opt-in* runs — they never continue a user-initiated
  search, and they never score.
- `FetchPipeline.progressUpdates()` stream; `NotificationManager.notifySearchComplete`.
- Scoring results are persisted per job (`scoreOne` → `jobStore.setScore`), and
  `ScoreBatch.isUnscored` makes "what's left" derivable from the DB — scoring is
  *naturally* checkpointed; only the run loop isn't.

### What iOS permits (constraints the plan must respect)

- `beginBackgroundTask`: ~30 s, non-negotiable.
- `BGProcessingTask`: minutes of runtime, but launched **at the system's discretion**
  (usually device idle; often within minutes when submitted with no
  `earliestBeginDate`, but never guaranteed).
- Background `URLSession` is the only sanctioned way to keep transfers going while
  suspended — but it supports only download/upload tasks with a delegate (no
  `data(for:)`, no async/await), and a scrape that parses page N to build page N+1
  means one app-wake per page. That's a large refactor; treat it as a last resort
  (Phase 4, deferred).

**Therefore the architecture is: persist incrementally, checkpoint on expiration,
resume opportunistically (foreground return + immediate BGProcessingTask), and notify
on completion.** A search that outlives the 30 s window pauses cleanly and finishes
later instead of erroring out.

---

## 2. Phase 1 — Stop losing work (incremental persistence)

### 1.1 Upsert per source, not per run
In `FetchPipeline.run`, move the dedupe/filter/upsert into the `for await (name, result)`
loop: when a source completes, filter its jobs, dedupe against (a) what's already been
upserted this run and (b) the DB, and upsert immediately. Keep a running
`insertedTotal`/`updatedTotal` for the summary. Cross-source dedup currently happens in
one `Deduplicator.dedupe(collected)` pass — preserve its semantics by keeping the
in-memory `collected` array purely for dedup keys of already-upserted jobs (fast sources
land first; LinkedIn dedups against them when it lands).

### 1.2 Incremental delivery from LinkedIn
Add an optional checkpoint hook to `JobSource`, with a protocol-extension default that
forwards to the existing method — so the other 9 sources need **zero changes**:

```swift
// JobSource.swift — new overload; default impl calls the old fetchJobs and
// delivers everything as one final checkpoint with a nil cursor.
func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>,
               onCheckpoint: @Sendable ([NormalizedJob], _ cursorJSON: String?) async -> Void
) async throws -> [NormalizedJob]
```

The cursor is an **opaque JSON string** at the protocol level — only LinkedIn produces
one (Phase 2 defines its contents; in Phase 1 LinkedIn may pass `nil`). `LinkedInSource`
calls `onCheckpoint` after **each search page** (phase 1) and after each small group of
detail merges (phase 2). `FetchPipeline` upserts each batch as it arrives (through the
same dedupe path as 1.1) and, once Phase 2 lands, persists the cursor in the same
transaction. Result: cancellation/timeout/jetsam can no longer discard collected
LinkedIn jobs — at worst the tail is missing, and detail-less jobs get descriptions
filled in when the detail merge for them lands (re-upsert updates the row).

### 1.3 Make the expiration handler checkpoint, not shrug
In `fetchJobs()`: keep a reference to the pipeline `Task`. In the expiration handler
(and only there): cancel the task, then end the assertion. With 1.1/1.2 in place,
cancellation is now safe — everything delivered so far is already in SQLite. Also set a
persisted "interrupted" marker (Phase 2's run record) *before* ending the assertion —
the handler has ~a few seconds; a single SQLite write is fine.

### 1.4 Classify suspension-vs-failure
Thread enough error information out of the sources to distinguish:
- **interrupted** — `URLError.cancelled` / `.networkConnectionLost` / `.timedOut` while
  `UIApplication.shared.applicationState != .active` (or explicit task cancellation).
- **failed** — everything else, as today.
Surface interrupted sources separately in `FetchSummary` (`interrupted: [String]`) so the
UI can say "Search paused — it will finish in the background or when you return" instead
of "linkedin had trouble". `LinkedInSource.fetchOnce` must stop swallowing errors into
`nil` — return/throw enough to tell a 429 retry from a dead socket from a cancellation.

**Tests (KitTests):** pipeline upserts per source (stub two sources, one slow — kill it,
assert the fast one's rows exist); LinkedIn `onBatch` fires per page (URLProtocol stub —
`HTTPClient.session` is already a `var` for exactly this); interrupted-vs-failed
classification.

---

## 3. Phase 2 — Resumable search runs (cursor + continuation)

### 2.1 Run record + cursor
New table (GRDB migration in `Data/`): `search_runs` —
`id, startedAt, state (running|interrupted|complete), requestedSources, completedSources,
linkedInCursor (JSON), insertedSoFar`. The LinkedIn cursor captures: query index,
location index, page `start`, phase (search|detail), and the external IDs still awaiting
detail fetch. Keep it small and versioned (`v: 1`) so a stale cursor can be discarded.

### 2.2 Cursor-aware LinkedIn source
`LinkedInSource.fetchJobs` accepts an optional cursor (decode the opaque JSON from 1.2;
a cursor that fails to decode or has the wrong `v` is treated as nil) and (a) skips
ahead to it, (b) reports an updated cursor with each `onCheckpoint` delivery, which the
pipeline persists to the run record. This requires the query/location iteration order to
be deterministic — it already is (`LinkedInGuestAPI.batchKeywords` and config order);
add a test locking that in. Fresh `TimeBudget`s are created per *attempt*, which
sidesteps the "budget expired while suspended" problem entirely — a resumed run gets
full budgets for the remaining work.

### 2.3 Resume triggers (three, in priority order)
1. **Foreground return:** in `JobsmithStandaloneApp` `scenePhase → .active`, if a run is
   `interrupted`, auto-resume it (call a new `AppModel.resumeSearch()` that re-enters the
   pipeline with `remainingSources` + cursor). Show the existing fetch banner.
2. **Immediate BGProcessingTask:** when the bg assertion expires mid-run, submit a
   `BGProcessingTaskRequest(identifier: processingID)` with `earliestBeginDate = nil`
   and `requiresNetworkConnectivity = true`. In `BackgroundScheduler.handleProcessing`,
   **before** the scheduled-tier work, check for an interrupted run and continue it from
   its cursor. If that window also expires, checkpoint again and resubmit — the run
   converges across windows. (Note: `handleProcessing` currently gates on
   `isEnabled()` — the continuation path must bypass the recurring-search opt-in gate;
   continuing a user-initiated search is not "background search" in the opt-in sense.
   Restructure `shouldRun` so a pending interrupted run always runs, and only the
   *scheduled* tier work honors the toggle.)
3. **Completion notification:** when a resumed run finishes (either trigger), post the
   existing `notifySearchComplete`. When a run is interrupted, optionally post a quiet
   "Search paused — will finish shortly" notification so the user isn't confused.

### 2.4 Run-state hygiene
- Only one active run: starting a new search while one is `interrupted` supersedes it
  (mark old run `complete`, keep its inserted rows).
- Stale runs (>24 h old) are discarded at launch.
- `fetchProgress`/Inbox banner reflects a resumed run ("Resuming search — linkedin…").

**Tests:** cursor round-trip encode/decode; LinkedIn resumes from mid-pagination cursor
(stub returns pages 3-4 only, assert pages 1-2 not refetched); `handleProcessing`
prefers an interrupted run over tier work and bypasses the opt-in gate for it; stale-run
discard.

---

## 4. Phase 3 — Scoring survives backgrounding

### 3.1 Same assertion pattern as fetchJobs
Wrap the `scoreAll` loop body in a `beginBackgroundTask` assertion (extract the
assertion pattern from `fetchJobs` into a small helper, e.g.
`withBackgroundAssertion(name:) { ... }` on `AppModel`, and use it in both places).
Do **not** move the loop off `@MainActor` — `AppModel` is `@MainActor` and the `await`
on each LLM call suspends rather than blocks, so the main thread is already free; a
relocation would only add cross-actor churn for `scoreAllDone`/`jobStore` access.

### 3.2 Error classification instead of first-failure-aborts
Split `ScoringError.engineUnavailable` handling in `scoreAll`:
- **Endpoint dead** (connection refused, host unreachable, DNS failure, 401): abort the
  run as today — every subsequent job would fail too.
- **Transient/suspension** (`.cancelled`, `.networkConnectionLost`, `.timedOut`,
  `.notConnectedToInternet`): mark the run *paused*, don't abort-with-error. The
  LM-Studio-on-LAN case (phone left home) lands here: pause quietly.
Classification lives in `JobsmithKit` (e.g. `ScoringService.isTransient(_:)`) so it's
unit-testable.

### 3.3 Resume scoring
No new table needed — `ScoreBatch.unscored` already derives the remaining work from the
DB. Persist just a tiny "scoring run intent" in **UserDefaults** (keys
`jobsmith.scoring.pending: Bool`, `jobsmith.scoring.cap: Int`,
`jobsmith.scoring.candidates: String` = `inbox`|`pipeline`; clear all three when the
run completes or the user cancels). Resume triggers mirror
Phase 2: scenePhase → active, and the tail of the BGProcessingTask continuation (after
the fetch finishes, if `scoringPending` and time remains, score jobs until the window
expires — each scored job is already persisted individually, so expiry costs at most
one in-flight call).

### 3.4 Optional: scoring in the scheduled processing tier
Settings toggle "Score new jobs in background". When on, `handleProcessing` scores
newly inserted jobs after its fetch, bounded by a job-count cap and a reachability
pre-check (one cheap `GET {baseURL}/models` with a 5 s timeout — if the endpoint is
unreachable, e.g. LM Studio at home while the phone is out, skip silently, never brand
jobs). This is what makes "search *and* scoring happen without the app open" true
end-to-end for the scheduled path.

**Tests:** transient-vs-fatal classification table; scoreAll pauses (not errors) on
transient failure; resume picks up only unscored jobs; processing-tier scoring respects
cap and skips when endpoint unreachable (URLProtocol stub).

---

## 5. Phase 4 — Deferred: true background transfers

Only if Phases 1–3 prove insufficient in practice. Background `URLSession` (download
tasks per LinkedIn page, delegate-driven, `handleEventsForBackgroundURLSession` via an
`UIApplicationDelegateAdaptor`) can keep transfers running while suspended, waking the
app briefly per completed page. It requires: no async `data(for:)` (delegate + file
based), a persisted state machine (which Phase 2's cursor already provides), and app
relaunch handling. Scoring POSTs would use background upload tasks. Substantial
complexity for marginal gain over checkpoint+resume — do not build this until the
simpler architecture has been evaluated on-device.

---

## 6. Execution order & file map

| Step | Files |
|---|---|
| 1.1 incremental upsert | `JobsmithKit/Sources/JobsmithKit/Fetching/FetchPipeline.swift` |
| 1.2 onBatch delivery | `Fetching/JobSource.swift`, `Fetching/LinkedIn/LinkedInSource.swift`, `FetchPipeline.swift` |
| 1.3 expiration checkpoint | `App/AppModel.swift` (`fetchJobs`) |
| 1.4 interrupted classification | `LinkedInSource.swift` (`fetchOnce`), `FetchPipeline.swift` (`FetchSummary`), `AppModel.swift` (error copy) |
| 2.1 run record | new `Data/` migration + small store; `AppDatabase` |
| 2.2 cursor-aware LinkedIn | `LinkedInSource.swift`, `LinkedInGuestAPI.swift` (if cursor needs query/location ordering made deterministic) |
| 2.3 resume triggers | `App/JobsmithStandaloneApp.swift`, `App/Background/BackgroundScheduler.swift`, `AppModel.swift` |
| 3.1–3.3 scoring | `AppModel.swift` (`scoreAll`), `JobsmithKit/.../AI/ScoringService.swift` |
| 3.4 bg scoring toggle | `BackgroundScheduler.swift`, `App/Screens/SettingsView.swift` / `SearchScheduleView.swift` |
| tests | `KitTests/FetchingTests.swift`, `KitTests/LinkedInTests.swift`, new `KitTests/SearchRunTests.swift`, `KitTests/AITests.swift` |

Each phase is independently shippable and independently valuable: Phase 1 alone fixes
"LinkedIn errors out and loses everything"; Phase 2 makes long searches *finish*;
Phase 3 does the same for scoring.

## 7. Acceptance criteria

**Automated — the executor verifies all of these before finishing:**

1. All existing KitTests pass (minus the known-broken `EndToEndWalkthroughTests` case);
   simulator smoke tests (`UITests/SmokeTests.swift`) pass.
2. New KitTests cover, at minimum: per-source incremental upsert survives a cancelled
   sibling source; LinkedIn `onCheckpoint` fires per search page (URLProtocol stub —
   `HTTPClient.session` is a `var` for exactly this); interrupted-vs-failed error
   classification; cursor round-trip + resume-from-cursor skips completed pages;
   deterministic query/location ordering; transient-vs-fatal scoring classification;
   scoring resume selects only unscored jobs.
3. Dead-endpoint Score-all behavior unchanged: connection-refused still aborts the run
   immediately with the existing "endpoint" error (covered by a test).
4. The app target builds with `CODE_SIGNING_ALLOWED=NO` after `xcodegen generate`.

**Manual, on-device (hand back to the user for TestFlight verification — the executor
cannot run these, but must state how each maps to the code changes):**

5. Start a search with LinkedIn enabled, background the app after ~10 s: no "linkedin
   had trouble/timed out" error; jobs collected before suspension are in the Inbox.
6. Reopen the app: the search resumes automatically from where it stopped (no refetch of
   completed pages) and completes; banner reflects the resumption.
7. Leave the app closed: the search completes in a BGProcessingTask window (simulatable
   via LLDB `e -l objc -- (void)[[BGTaskScheduler sharedScheduler]
   _simulateLaunchForTaskWithIdentifier:@"com.thedevro.jobsmith.standalone.processing"]`)
   and posts the completion notification.
8. Start Score-all on 20+ jobs, background the app: scoring pauses without an error
   banner and resumes on return; already-scored jobs are not re-scored.
9. iOS build number bump for TestFlight is a **release step, not part of this work** —
   leave build numbers untouched.

## 8. Non-goals / expectation setting

iOS does not allow indefinite arbitrary background execution. This plan makes long
searches **pause cleanly and finish opportunistically** rather than run uninterrupted.
The one path iOS *would* allow for uninterrupted transfers (background URLSession) is
deliberately deferred (Phase 4). The scheduled background tiers remain opt-in and
discretionary; the continuation of a *user-initiated* run is exempt from that opt-in.
