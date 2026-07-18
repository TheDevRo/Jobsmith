# ats-account

The `ats_account` entity: a per-tenant ATS account registry (Workday needs a
separate account per `{company}.wd{N}.myworkdayjobs.com` tenant). It lets every
surface — the browser-extension Apply Assist, the iOS Apply browser, and the
desktop automated adapter — remember which tenants already have an account and
go straight to sign-in instead of re-deriving "sign in vs create account" from
the DOM each time.

Identity is `{provider}:{tenant_host}` (e.g.
`workday:acme.wd5.myworkdayjobs.com`). It is a flat, mutable, per-record
last-writer-wins entity (the `triage`/`work_request` shape). A password is
**never** part of any record — only the email, a `status`
(`active` | `pending_verification`), and timestamps.

This vector exercises:

- **LWW promotion** — A1B2 (iOS) creates `acme.wd5` as `pending_verification`
  @12:00; C3D4 (desktop) signs in and re-emits it `active` @12:05, which wins.
- **Retirement** — A1B2 registers `globex.wd1` @12:00, then deletes it @12:20,
  tombstoning it.
