# Vector: work-request

The `work_request` cross-device hand-off entity (see [`../../spec/FORMAT.md`](../../spec/FORMAT.md)).
A `work_request` is a *command*, not state: one device asks another to do work it
couldn't finish itself (today `kind: score_all`). Its lifecycle is plain
per-record LWW — no special-casing.

Two devices: `A1B2` is the requester (stands in for iOS), `C3D4` is the
fulfiller (the desktop). Feed both logs to a merge engine and assert the result
equals `expected.json` (ignoring the `_comment` / `_won_from` annotation keys).

| id      | versions                                                      | winner                         |
|---------|---------------------------------------------------------------|--------------------------------|
| `req-1` | A1B2 **pending** @12:00, C3D4 **done** @12:05                  | done — a newer re-emit wins back on the requester |
| `req-2` | A1B2 **pending** @12:00, A1B2 **delete** @12:10               | tombstone — the requester prunes its own retired request |

What this guards:

- **Done wins back.** The fulfiller re-emits the *same id* as `done` with a later
  `updated_at`; ordinary LWW carries that state back to the requester without any
  entity-specific merge rule.
- **Retirement is an ordinary tombstone.** A completed request is pruned with a
  plain `deleted: true` record; the delete propagates like any other.

Importing a `pending` request must never by itself trigger work — fulfillment is
an explicit local opt-in on the serving device — but that is engine behaviour,
not a merge-result property, so it isn't asserted by this vector.
