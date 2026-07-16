# Vector: settings-merge

Per-key LWW over the config-backed `setting` entity. Each syncable config key is
its own change record (`id` = the canonical dotted path, e.g.
`application_honesty.resume_style`), so two devices editing unrelated settings
never collide, and a single changed key overrides without dragging its
neighbours along.

Two devices, `A1B2` and `C3D4`, each with a log under `changes/`. Feed both to a
merge engine and assert the result equals `expected.json` (ignoring the
`_comment` / `_won_from` annotation keys, which are documentation only).

| id                                  | rule under test                                            |
|-------------------------------------|------------------------------------------------------------|
| `application_honesty.resume_style`  | cross-device override — C3D4 @ 12:00 beats A1B2 @ 10:00     |
| `search.enabled_sources`            | single version passes through; value is a canonical list   |
| `prompts.score`                     | untouched neighbour — per-key records don't clobber siblings|
| `ai.api_key`                        | a user-owned secret that *does* ride the folder            |
| `prompts.junk`                      | tombstone — a later delete removes an override             |

`ai.api_key` is deliberately present: unlike the ATS-login credentials excluded
in `spec/PROFILE.md`, the AI key is a user-owned setting that syncs through the
user's own folder so both devices reach the same inference server.
