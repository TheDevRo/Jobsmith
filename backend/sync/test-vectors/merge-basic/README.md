# Vector: merge-basic

The minimum scenario that exercises every merge rule. Two devices,
`A1B2` and `C3D4`, each with its own log under `changes/`. Feed both logs to a
merge engine and assert the result equals `expected.json` (ignoring the
`_comment` / `_won_from` annotation keys, which are documentation only).

What each record proves:

| id                      | rule under test                                          |
|-------------------------|----------------------------------------------------------|
| `job greenhouse:100`    | cross-device override — B's later edit beats A's create  |
| `job lever:200`         | single version passes through untouched                  |
| `application uuid-app-1`| tombstone — later delete beats earlier create            |
| `answer uuid-ans-1`     | second device's entity merges in cleanly                 |
| `profile me`            | identical-timestamp tie broken deterministically by `device` |

A correct engine produces `expected.json` regardless of the order in which
logs or lines are read — read-order independence is part of the contract.
