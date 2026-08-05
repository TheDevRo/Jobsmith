---
title: Getting started (desktop)
---

# Getting started with Jobsmith on the desktop

This page walks through a first run end to end: install, setup wizard, first
fetch, scoring and shortlisting, tailoring, the browser extension, Apply Assist,
and marking a job applied. It assumes nothing is configured yet.

If you would rather run Jobsmith from source or in Docker, see
[README.md](https://github.com/TheDevRo/Jobsmith#setup). For how the desktop
build is put together, see
[README-DESKTOP.md](https://github.com/TheDevRo/Jobsmith/blob/main/README-DESKTOP.md).

## 1. Install

Grab the [latest release](https://github.com/TheDevRo/Jobsmith/releases/latest).

- **macOS (Apple Silicon)** — download `Jobsmith_<version>_aarch64.dmg`, open it,
  and drag Jobsmith to Applications.
- **Windows, Linux, Intel macOS** — there is no installer yet; use Docker
  (`docker compose up -d`) and open <http://localhost:8888>. The rest of this
  page applies unchanged.

**Gatekeeper.** The macOS app is unsigned, so the first launch is blocked. Open
**System Settings → Privacy & Security** and click **Open Anyway**, or run:

```bash
xattr -dr com.apple.quarantine /Applications/Jobsmith.app
```

## 2. First launch

The first launch downloads Playwright's Chromium (~150 MB) into the app's data
directory, so it takes a few minutes. This is normal and happens once. Jobsmith
needs that browser to scrape job boards.

Then the **setup wizard** opens. Five steps:

1. **Connect AI** — point Jobsmith at your AI server, test the connection, and
   pick your fast and strong models from the list the server reports. Local
   options are [LM Studio](https://lmstudio.ai) (recommended, fully private) and
   Ollama; hosted providers such as OpenRouter or OpenAI work with an API key.
   Start your AI server *before* this step so the model list is populated.
2. **Résumé** — upload or paste your existing resume. The AI parses it into
   structured fields.
3. **Profile** — check what was parsed and fill in the rest (contact details,
   work authorization, salary target). This is the only data the AI is allowed
   to use; it never invents facts about you.
4. **Job Search** — keywords, locations, and a salary floor.
5. **Finish** — save and go.

There is no config file to edit. `config.yaml` is created on first boot and the
wizard writes every answer into it.

**Skipping and re-running.** Every step can be skipped, and you can close the
wizard entirely — the app works, it just has less to go on. Re-run it any time
from **Settings → Re-run setup wizard**. A re-run adds a **Review** step that
shows a diff of what would change before anything is saved, so it is safe to
run again on a configured install.

If your AI server is not reachable, the dashboard shows a warning banner with
an **Open AI Settings** and a **Retry** button. Scoring, tailoring, and resume
parsing do not work until that clears; everything else does.

## 3. First fetch

Job sources are all **on by default**, so there is nothing to configure. Click
**Fetch Jobs** in the Inbox toolbar (or **Fetch** in the Activity run console).

These sources work with no API key at all:

- LinkedIn, Indeed, RemoteOK, WeWorkRemotely, Arbeitnow
- Per-company ATS boards: Greenhouse, Lever, Ashby, Workable, Recruitee

Adzuna and USAJobs need free API keys, added later in **Settings → Integrations**
if you want them. Skip them for now.

The run chip at the top shows live progress. A first fetch typically returns a
few dozen jobs.

## 4. Score and shortlist

**Score** asks your AI server to rate each job against your profile and gives it
a **fit score** — 0-100, how well the job matches you. Hover any score chip in
the app for the same one-line explanation.

Then triage in the **Inbox**. The stage view shows one job at a time:

- **→** or **S** — shortlist it
- **←** or **X** — pass
- **Enter** — open the full posting
- **U** — undo
- **T** — shortlist and tailor in one step
- **L** — switch to the list view

Shortlisting is the signal Jobsmith acts on: shortlisted jobs are the ones that
get tailored documents.

If a run ends with "Scored 0 jobs (N failed)", the AI server is almost certainly
offline — check the banner.

## 5. Tailor

**Tailor** generates a resume and cover letter customized to a specific job.

- **Per job** — open a job and click **Tailor Resume**.
- **In batch** — the **Tailor** verb in the Activity run console tailors your
  shortlist. It only processes jobs with a **fit score of 50 or higher**. If it
  reports 0 tailored, either nothing is scored yet (run Score first) or nothing
  cleared 50 — use the per-job button to override.

**Honesty levels** control how far the AI may stray from your facts:
`honest` (reword only) → `tailored` (use the job's keywords, no fabrication) →
`embellished` (upgrade scope and impact) → `fabricated` (invent achievements).
The default is `honest`. Set the global default in **Settings → Honesty**, and
override it per edit in the AI Edit panel. Anything above `tailored` can produce
false statements — you are responsible for what you submit.

Generated documents land in the **Pipeline**, under **Ready to Review**. Review
them there; **AI Edit** lets you revise with natural-language instructions.

## 6. Install the browser extension

Applying uses the Jobsmith browser extension, which runs inside your normal
Chrome or Firefox.

Go to **Settings → Apply Assist** and use **Get for Chrome** / **Get for
Firefox**. Chrome installs are unpacked (`chrome://extensions` → Developer mode
→ Load unpacked); Firefox has a signed build served straight from the app.

**Pairing is automatic.** The extension pairs itself with your local Jobsmith
the first time you launch Apply Assist. The extension token shown on that
settings page exists only as a fallback — paste it into the extension popup's
"Extension token" field only if automatic pairing fails.

## 7. Apply Assist

From any job with tailored documents, click **Apply Assist**. Jobsmith opens the
job's real ATS page in your browser and the extension injects a sidebar
containing your tailored resume, your cover letter, and your saved answers. It
autofills the standard fields for you.

You stay in the loop: check what was filled, fix anything the form did oddly,
and click **Submit** yourself. Jobsmith never submits on your behalf here.

Answers you type into a form are remembered in the **answer bank** and replayed
on the next application that asks the same question.

If Apply Assist fails to start, the usual cause is that the extension is not
installed yet — see step 6.

## 8. Mark Applied

After you submit, click **Mark Applied** — from the extension sidebar, the job
detail pane, or the Pipeline row. The job moves to **Applied** everywhere and
stops showing up in your review queue.

That is the full loop: **Fetch → Score → Shortlist → Tailor → Review → Apply
Assist → Mark Applied**. From here, the Pipeline board (Shortlisted → Tailoring
→ Ready to Review → Applied) is where you spend most of your time.

## Where to get help

- **Replay the product tour** — **Settings → Replay product tour** restarts the
  in-app walkthrough at any time.
- **Re-run the setup wizard** — **Settings → Re-run setup wizard**, safe on a
  configured install (it shows a diff first).
- **Bugs and questions** —
  [GitHub Issues](https://github.com/TheDevRo/Jobsmith/issues). Including what
  you were doing, what you expected, your OS, and which AI endpoint you have
  configured makes a fix much faster.
