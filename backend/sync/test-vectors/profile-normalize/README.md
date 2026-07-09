# Vector: profile-normalize

Not a merge vector (no `expected.json`, so the merge runner skips it) — these
are **mapper fixtures** for the profile normalization in [`../../spec/PROFILE.md`](../../spec/PROFILE.md),
checked by `reference/profile_conformance.py` and by each app's mapper test.

The same profile in three shapes that must inter-convert losslessly:

- `canonical.json` — the snake_case payload that travels in the change record.
- `ios-profile.json` — the iOS `Profile` Codable JSON (camelCase; omits the
  fields iOS doesn't model: `middle_name`, `street_address_2`, the EEO block).
- `desktop-config.json` — the desktop `config.yaml` `profile:` dict (snake_case;
  includes the ATS-login credentials that must NOT sync).

What the fixtures prove:

- `desktop_to_canonical` drops `workday_email` / `workday_password` /
  `ats_login_password` and otherwise equals `canonical.json`.
- `canonical_to_ios` equals `ios-profile.json` (rename map + subset).
- Base-overlay round-trips both directions with **zero field loss**: an iOS edit
  keeps the demographics it can't see; a desktop import keeps its local secrets.
