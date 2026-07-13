# Pipeline Intelligence — Implementation Plan

> Executes FUTURE_IDEAS.md Theme A across backend, frontend, and iOS-standalone.
> Written 2026-07-12 against `fix/search-filter-consistency`.

## Premise

A1 (outcome tracking + funnel) already shipped on desktop: `applications.outcome`
(`database.py:68` migration 18), `PATCH /api/applications/{id}/outcome`
(`routers/applications.py:433`), `GET /api/analytics/outcomes`
(`routers/settings.py:74` → `database.py:1171`), a dashboard funnel
(`frontend/js/dashboard.js:82`) and a per-card dropdown (`frontend/js/review.js:423`).
FUTURE_IDEAS A1 is stale and should be marked done.

**But nothing produces the data it charts.** `outcome` defaults to `awaiting` and only
ever changes when a human picks from a dropdown — on the desktop, days after the fact.
With no data entry, `response_rate` reports 0% and every by-source / by-fit / by-honesty
breakdown is uniformly zero. The funnel looks authoritative and is empty.

So the next phase is **not more analytics**. It is making outcome data cheap or automatic
to capture. Three mechanisms do that, and they are the spine of this plan:

1. **Capture where the user is** — you learn about a screener on your phone. iOS has no
   outcome UI at all. (Phase 2)
2. **Ask at the right moment** — A2's reminders *are* the capture flow, not a nicety.
   "Applied to Acme 7 days ago — heard anything?" + three tap targets. (Phase 3)
3. **Infer the rest** — `awaiting` past N days is `no_response`. A rule, not a chart.
   (Phase 1)

Everything downstream (B2 variant learning, E3 insights, A4's conversion-weighted
ranking) is blocked on there being real data. Ordered accordingly.

---

## Phase 1 — Event log + auto-ghosting (backend)

The one schema decision worth making before A2/B2 pile on.

`outcome` is a single mutable column with one `outcome_updated_at`. Consequences, both
real today:

- You cannot answer "how long from applied to screener" — there is no per-stage history.
- A rejection *after* an interview loses its stage history entirely. The funnel code
  admits this at `database.py:1196`: "rejected/withdrawn don't carry stage history, so
  they only count toward applied." The funnel therefore *undercounts* every stage a
  rejected candidate actually reached.

**Add an append-only event table.** Keep the `outcome` column as a denormalized
"current state" so nothing existing breaks.

```sql
-- SCHEMA_MIGRATIONS entry (21,)
CREATE TABLE IF NOT EXISTS application_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  TEXT NOT NULL REFERENCES applications(id),
    from_outcome    TEXT,             -- NULL for the initial 'applied' event
    to_outcome      TEXT NOT NULL,    -- a VALID_OUTCOMES member
    occurred_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    note            TEXT,
    source          TEXT NOT NULL DEFAULT 'user'  -- user | rule | email
);
-- (22,) CREATE INDEX idx_app_events_app ON application_events(application_id);
```

Work:

- `database.py`: append migrations 21/22 to `SCHEMA_MIGRATIONS` (`database.py:50`).
  Make `update_application_outcome` (`database.py:1044`) write an event **and** update
  the column, in one transaction. Backfill: one-time event synthesized from each existing
  applied row's `(applied_at, outcome, outcome_updated_at)`.
- Rewrite the funnel in `get_outcome_analytics` (`database.py:1171`) to compute
  "reached stage X" as `EXISTS(event WHERE to_outcome >= X)` rather than summing current
  outcomes. This is the bug fix: a rejected-after-interview application now correctly
  counts toward `interview`.
- **Auto-ghosting rule.** `awaiting` + `applied_at` older than `ghost_after_days`
  (config, default 21) → write a `no_response` event with `source='rule'`. Runs on the
  backend's only existing periodic loop — `sync/service.py:186 run_periodic` — or on
  startup + an hourly tick added to `main.py`'s lifespan. There is no scheduler in the
  backend today (confirmed: the sync poller is the sole loop), so this is the cheapest
  correct home.
- `GET /api/analytics/outcomes` gains `stage_durations` (median days applied→screening,
  screening→interview, …) — pure aggregation over the new table.
- Tests: extend `tests/test_outcome_tracking.py`.

**Ships:** an honest funnel, stage durations, and a response rate that stops lying when
the user hasn't done data entry.

---

## Phase 2 — iOS outcome parity + sync (the biggest hole)

iOS models no outcome at all (`ios-standalone/.../Data/Models.swift:113`). This is where
the user *is* when they learn the outcome.

- **GRDB migration v3** in `AppDatabase.swift` (migrator at `:27`, last is `v2_deleted_jobs`
  at `:122`): add `outcome`, `outcomeUpdatedAt` to `applications`; add an
  `application_events` table mirroring the backend's.
- `Models.swift`: add the fields to `Application`; add an `ApplicationEvent` model.
  Mirror `VALID_OUTCOMES` as a Swift enum — one source of truth, matched to
  `database.py:1033`.
- **UI:** an outcome control on applied rows in `PipelineView.swift` (which today groups
  only by job `status` and offers no status/outcome actions), and on `JobDetailView`.
  Three-tap ergonomics: the common answers are *no response*, *screener*, *rejected*.
- `ActivityView.swift` gains the funnel + response-rate tiles (today it's four stat
  tiles, no charts). Swift Charts, following the `dataviz` conventions.

**Sync — read this before writing code.** Two facts from `Sync/SyncEntities.swift`:

- Desktop's `outcome` *already round-trips through iOS untouched* — `appIOSToCanonical`
  (`:176`) preserves unknown desktop keys verbatim via base-overlay. So adding the field
  to `appCanonToIOS` (`:82`) is additive and back-compatible; it does not need a
  `syncFormatVersion` bump (currently 3, `SyncEngine.swift:27`).
- **LWW is whole-entity, not per-field** (`SyncMerge.swift:28`). If the phone sets an
  outcome while the desktop touches the same application, one side's write is lost
  wholesale. This is a real regression risk the moment iOS gets *write* access to a column
  the desktop also writes.

  → Mitigation: sync `application_events` as its own **append-only entity**. Append-only
  logs merge without conflict by construction — union, dedupe by `(application_id,
  occurred_at, to_outcome)`. Then derive the `outcome` column locally on each side from
  its event log rather than syncing it as mutable state. This is why the event log is
  Phase 1 and not an afterthought.

- Tests: extend `SyncEngineTests` / `SyncConformanceTests` with a concurrent
  outcome-edit case.

**Ships:** outcome capture on the device where outcomes are learned, and a sync design
that doesn't silently drop it.

---

## Phase 3 — A2, reminders (iOS is the surface; backend computes)

**Do not put the notification on the desktop.** Backend notifications are an in-memory
`deque` (`app_state.py:110`) that the frontend polls every 3s (`frontend/js/core.js:425`)
— they only fire if the app happens to be open, which for "you applied 7 days ago" is
useless. iOS already has real `UNUserNotification` + `BGProcessingTask`
(`App/Background/`). Split accordingly:

- **Backend** — migration: `follow_up_at`, `interview_at` on `applications`.
  `PATCH /api/applications/{id}/schedule`. A `GET /api/applications/due` returning
  everything needing attention (follow-up due, interview upcoming, ghost-threshold
  approaching). The desktop renders this as a **"Needs attention" dashboard queue** —
  honest about being pull, not push.
- **iOS** — schedule real `UNCalendarNotificationTrigger`s when a date is set.
  `NotificationManager.swift` today has three fire-and-forget functions and *no*
  `UNUserNotificationCenterDelegate` — note the existing `userInfo["deepLink"]` at `:31`
  is written but **never consumed**, so notification taps currently go nowhere. Fix that
  as part of this: add the delegate + `onOpenURL`, so a reminder tap lands on the
  application.
  Add **notification actions** ("Heard back" / "Still waiting" / "Rejected") so the
  outcome is captured from the lock screen without opening the app. This is the single
  highest-leverage piece of the whole plan — it turns a reminder into a data-capture flow.
- `BackgroundScheduler.swift` re-checks due items on wake alongside the existing fetch.

**Ships:** the mechanism that actually populates the funnel.

---

## Phase 4 — A3, duplicate-application guard (small)

The identity key already exists on both sides: `_identity_key` (`job_sources/__init__.py:487`,
normalized title/company/location tuple) and iOS `Fetching/Deduplicator.swift`.

Today `get_jobs()` excludes only *the same job row* you applied to (`database.py:531`).
Reposts and cross-source duplicates of a company+title you already applied to sail
straight through — exactly the case the guard is for.

- Backend: after dedup in `fetch_all_jobs` (`job_sources/__init__.py:499`), cross-check
  normalized `(company, title)` against applied history; flag the job.
- Surface as a badge on the job card (`review.js`, iOS `InboxView`), plus an optional
  auto-skip filter in settings.
- Reuse `_identity_key` — do not write a second normalizer.

---

## Phase 5 — A4, digest (the payoff)

`GET /api/digest`: weighted rank over scored, un-applied jobs — fit, freshness, salary,
apply-effort (easy_apply first). Weights editable in Settings. Dashboard "Today" card;
iOS Inbox default sort + optional morning notification.

The reason it's last: once Phases 1–3 have produced real outcome data, the digest can
weight by **your own measured conversion rates** ("Greenhouse roles respond in 5 days;
LinkedIn Easy Apply averages 22"). That is what makes it a strategy tool rather than
another sort order. Building it before the data exists gets you the sort order only.

---

## Beyond the doc — two things worth adding

**Email ingestion is misfiled.** FUTURE_IDEAS C2 lists IMAP parsing under "more sources"
(job alerts → pipeline). Its far higher value is the *opposite* direction: parsing
"unfortunately we've decided", "thanks for applying", and "let's find a time" emails to
**auto-advance the funnel** with `source='email'` events. Same IMAP plumbing, and it is
the zero-effort version of the entire data-capture problem. If C2 gets built at all, build
it for outcomes first.

**Stage-duration intelligence** (falls out of Phase 1 for free): "median time-to-screener
is 9 days, so these 4 applications are effectively dead" and per-source response latency.
This is the insight a job-seeker acts on.

---

## Sequence & sizing

| Phase | What | Effort | Blocks |
|---|---|---|---|
| 1 | Event log + auto-ghost + funnel fix | M | everything |
| 2 | iOS outcome parity + event sync | M | 3 |
| 3 | Reminders w/ notification actions | M | — |
| 4 | Dupe guard | S | — |
| 5 | Conversion-weighted digest | M | 1–3 for the data |

Phase 4 is independent and can be slotted anywhere. Phases 1→2→3 are the critical path
and are the ones that make the existing dashboard tell the truth.
