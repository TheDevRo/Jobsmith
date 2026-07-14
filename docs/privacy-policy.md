---
title: Privacy Policy
---

# Jobsmith Privacy Policy

**Last updated: 2026-07-14**

Jobsmith is a job search assistant that runs on your iPhone. It is built to be
local-first: there is no Jobsmith account, no Jobsmith login, and no Jobsmith
server. This policy explains, plainly, what stays on your device, what leaves
it, and who receives it.

The short version: **the developer of Jobsmith collects nothing about you.**
There is no analytics, no telemetry, no crash reporting, no advertising, and no
tracking of any kind. Nothing about you is sold or shared with anyone for
marketing. Jobsmith does not have servers that could receive your data even if
it wanted to.

The longer version matters, because Jobsmith does make network requests on your
behalf — to job boards, and to an AI service **that you choose and configure**.
Those requests are described below.

## What the developer collects

Nothing.

Jobsmith contains no analytics SDK, no crash reporting SDK, and no advertising
SDK. It does not use the advertising identifier (IDFA), does not ask for
tracking permission, and does not build a profile of you. It does not phone home
on launch, on install, or ever. The developer has no way to know that you use
the app, what you searched for, where you applied, or what is in your resume.

This is stated in the app's Apple privacy manifest as well: tracking is
disabled and no data types are collected.

## What is stored on your device

Everything you put into Jobsmith stays on your device, in the app's private
storage (a shared app container that the app and its Share extension both read):

- **Your profile** — name, contact details, work experience, education, skills,
  and anything else you enter or import from a resume.
- **Your resumes and cover letters** — both the ones you import and the ones
  Jobsmith generates for you (PDF and Word files).
- **Jobs and applications** — the postings Jobsmith has found, your scores and
  notes, and the status of each application. These live in a local database
  file on the device.
- **Your settings** — including any job-board API keys you have supplied.

These files are stored with iOS file protection enabled, so they are encrypted
at rest. (Specifically: protection is set so that background job searches can
still run while your phone is locked.)

**Resume import happens on your device.** When you import a PDF or Word resume,
the text is extracted locally on the phone. The file is not uploaded anywhere to
be parsed.

### The LinkedIn sign-in cookie

If you use the optional LinkedIn sign-in, you sign in to LinkedIn inside a web
view — the same as signing in with Safari. Your LinkedIn username and password
are typed into LinkedIn's own page; Jobsmith never sees them and never stores
them.

Jobsmith does capture your LinkedIn session cookie (`li_at`) and stores it in
the **iOS Keychain**, marked as device-only. This means:

- It never leaves your device.
- It is **not** included in iCloud or iTunes backups.
- It is **not** written to the sync folder.
- It is not transmitted to the developer, or to anyone else, by Jobsmith.

Jobsmith's LinkedIn job search uses LinkedIn's public guest interface, so your
LinkedIn account is not used to search for jobs.

You can remove this cookie by signing out in the app or by deleting the app.

## What leaves your device, and who receives it

### 1. Job boards and job listing sites

To find jobs, Jobsmith requests listings directly from job boards. These
requests go from your phone to the job board — not through any server of the
developer's. The job board therefore sees a request from your device (which, as
with any website you visit, means it can see your IP address).

Depending on which sources you enable and which companies you follow, Jobsmith
may contact:

- RemoteOK
- WeWorkRemotely
- Arbeitnow
- Greenhouse
- Lever
- Ashby
- Workable
- Recruitee
- LinkedIn (public guest job search)
- Adzuna (only if you supply your own Adzuna API key)
- USAJobs (only if you supply your own USAJobs API key)
- The U.S. Bureau of Labor Statistics (used for salary estimates)

These requests contain your **search terms** — things like job title keywords
and location. They do not contain your resume or your profile.

If you paste a job link into Jobsmith, it fetches that page from whatever site
you pasted, so that site receives a request from your device too.

Any API keys you enter for these services (for example Adzuna or USAJobs) are
**your own** keys, under your own accounts with those providers, and are stored
on your device.

### 2. The AI service that you configure — please read this

Jobsmith uses a large language model to score jobs against your background and
to write tailored resumes and cover letters. **You choose which AI service does
that work**, and this choice determines whether your resume and profile text
leave your device.

There are two kinds of choice:

**On-device (Apple Intelligence).** If you select Apple's on-device model, the
text is processed by the model running on your iPhone. It does not go over the
network.

**A model endpoint that you point Jobsmith at.** Jobsmith can talk to any
OpenAI-compatible endpoint. What that means for your privacy depends entirely on
what you point it at:

- If you point it at **an AI server you run yourself** — for example LM Studio
  or Ollama on your own computer on your home network — then your text goes to
  your own machine and no further. This is the default configuration Jobsmith
  ships with (a local address).
- If you point it at **a third-party cloud AI provider** — for example OpenAI,
  OpenRouter, or any other hosted service — then **your resume content, your
  profile details, and the job descriptions are sent to that provider**, over
  the internet, to be processed. That is how the feature works; there is no way
  for the app to use a cloud model without sending it the text.

**Be deliberate about this.** If you configure a cloud provider, the text
Jobsmith sends is subject to **that provider's** privacy policy and data
retention practices, not this one. The developer of Jobsmith has no control
over, no visibility into, and no relationship with whatever endpoint you choose.
Jobsmith does not select a provider for you and does not send your data to any
AI provider you have not configured.

If you want nothing to leave your device, use the on-device Apple model, or run
your own AI server.

### 3. Applying to jobs

When you apply, Jobsmith opens the employer's application page in an in-app
browser. If you use the autofill button, Jobsmith fills that employer's form
with your profile details and can attach your generated resume — the same
information you would have typed in yourself. That information goes to the
employer (and to whichever applicant tracking system they use), because that is
what applying to a job is. This only happens for jobs you choose to apply to,
when you choose to do it.

### 4. Optional folder sync

Jobsmith can sync with your other devices through a **shared folder that you
pick** — for example a folder in iCloud Drive or Dropbox. This is off by
default.

There is no Jobsmith sync server. Jobsmith simply writes files into the folder
you chose. **Your data therefore goes wherever that folder goes.** If you pick
an iCloud Drive folder, your data is in iCloud, subject to Apple's terms. If you
pick a Dropbox folder, it is in Dropbox, subject to Dropbox's terms. That is
your choice to make.

The folder receives your jobs, applications, profile, and generated documents.
Saved passwords and the LinkedIn session cookie are deliberately **excluded** and
are never written to the sync folder.

## Notifications

Jobsmith can notify you about new matching jobs and reminders. These are local
notifications, scheduled on the device by the app itself. Jobsmith does not use
push notifications, so no notification content is sent through Apple's or
anyone else's servers.

## Children

Jobsmith is a tool for job seekers and is not directed at children. It is not
intended for use by anyone under 13, and the developer does not knowingly
collect information from children — or, for that matter, from anyone.

## Deleting your data

Because your data is on your device, you are in control of it:

- **Delete individual items** — remove jobs, applications, or documents in the
  app.
- **Delete all tracked postings** — in Settings, this clears every job and its
  tailored documents, while keeping your profile and settings.
- **Delete all data** — in Settings, this erases all postings, documents, saved
  answers, your profile, and your settings, resetting the app to a clean
  install.
- **Delete the app** — removing Jobsmith from your iPhone removes the app's
  local database, your profile, your settings, your generated documents, and the
  LinkedIn cookie in the Keychain.
- **Sync folder** — if you enabled folder sync, that folder is yours and is not
  removed when you delete the app. Delete it yourself from iCloud Drive,
  Dropbox, or wherever you put it.

There is no account to close and nothing to request from the developer, because
the developer holds nothing.

## Data sale and sharing

Jobsmith does not sell your data. Jobsmith does not share your data with third
parties for advertising or marketing. The only data that goes to third parties
is described above: it goes to job boards, to employers you apply to, to the AI
endpoint you configured, and to the file-sync provider you chose — in each case
because you asked Jobsmith to do that thing.

## Changes to this policy

If this policy changes, the "Last updated" date at the top will change, and the
updated policy will be published on this page.

## Contact

Questions about privacy, or something in this policy that looks wrong?

- Open an issue: [github.com/TheDevRo/Jobsmith/issues](https://github.com/TheDevRo/Jobsmith/issues)
- Email: [contact@thedevro.com](mailto:contact@thedevro.com)
