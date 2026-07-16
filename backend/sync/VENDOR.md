# Vendored sync contract

The files below are **copied** from the
[`jobsmith-sync`](https://github.com/TheDevRo/jobsmith-sync) contract repo — the
single source of truth for the sync format, merge rules, profile mapping, JSON
schemas, and conformance vectors. They are vendored here so the backend has no
runtime dependency on a sibling checkout.

Vendored from `jobsmith-sync` @ `24983f4`:

- `merge.py`        ← `reference/merge.py`
- `profile_map.py`  ← `reference/profile_map.py`
- `schema/`         ← `schema/`
- `test-vectors/`   ← `test-vectors/`

**Do not edit vendored files here.** Change them in `jobsmith-sync`, then refresh:

```sh
SRC=~/jobsmith-sync
DST=backend/sync
cp "$SRC"/reference/merge.py "$SRC"/reference/profile_map.py "$DST"/
cp "$SRC"/schema/*.json "$DST"/schema/
rm -rf "$DST"/test-vectors && cp -R "$SRC"/test-vectors "$DST"/test-vectors
```

After refreshing: re-add the `# VENDORED from jobsmith-sync@<hash> …` header
line to `merge.py` and `profile_map.py` (the upstream files don't carry it) and
update the pin above. `tests/test_sync_vendored_contract.py` fails if the
vendored files drift from a sibling `~/jobsmith-sync` checkout (headers
excepted) or if emitted records stop validating against the vendored schema.

The conformance test (`tests/test_sync_conformance.py`) runs the vendored
vectors through this package's `merge`, proving the desktop engine agrees with
the oracle. `engine.py` and `entities.py` are the real desktop implementation
and are **not** vendored — they live and evolve here.
