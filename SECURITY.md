# Security Policy

Jobsmith is a self-hosted, single-maintainer project. There is no hosted
service — the app runs entirely on your own machine (desktop, Docker, or the
standalone iOS build), and your LinkedIn cookie and ATS/Workday credentials
never leave it. That shapes what a "vulnerability" looks like here: the most
useful reports are ones that could let a malicious job posting, ATS page, or
sync folder read or exfiltrate data from a user's own machine, or that weaken
the guardrails around stored credentials.

## Supported versions

Only the **latest release** gets security fixes. If you're running an older
build, upgrade to the [latest release](https://github.com/TheDevRo/Jobsmith/releases/latest)
before reporting — the issue may already be fixed.

## Reporting a vulnerability

Please report privately through GitHub's security advisories rather than opening
a public issue:

1. Go to the repo: <https://github.com/TheDevRo/Jobsmith>
2. Open the **Security** tab.
3. Click **Report a vulnerability** (under **Advisories**).

Include the platform (desktop / Docker / iOS), the version, and enough detail to
reproduce. If it helps, attach the relevant lines from `data/logs/shell.log`
(desktop) — scrub any personal data or credentials first.

## What to expect

This is a best-effort, spare-time project maintained by one person, so there's no
guaranteed response time. I'll acknowledge valid reports as soon as I reasonably
can, work on a fix for anything that holds up, and credit you in the release
notes if you'd like. Please give me a chance to ship a fix before disclosing
publicly.

## Scope notes

- **No hosted service.** There is no Jobsmith server to attack — every install is
  the user's own. Reports about infrastructure we don't run are out of scope.
- **Credentials stay local.** LinkedIn/ATS/Workday credentials and API keys live
  on the user's machine and are deliberately kept out of the sync folder (except
  the AI endpoint key, which the user opts into syncing — see Settings → Sync).
  Findings that break these boundaries are in scope.
- Bugs with no security impact belong in a regular
  [issue](https://github.com/TheDevRo/Jobsmith/issues), not an advisory.
