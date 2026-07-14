---
title: Support
---

# Jobsmith Support

## What Jobsmith is

Jobsmith is a local-first job search assistant for iPhone. It searches job
boards for you, scores the results against your background, writes tailored
resumes and cover letters, and helps you fill in application forms.

It has no account and no server. Your profile, resumes, and saved jobs live on
your device. Jobsmith uses an AI model that **you** choose — either Apple's
on-device model, an AI server you run yourself (such as LM Studio on your own
computer), or a cloud AI provider you configure. See the
[Privacy Policy](privacy-policy.html) for what that means for your data.

## Getting help

The best way to report a bug or ask a question is to open a GitHub issue:

**[github.com/TheDevRo/Jobsmith/issues](https://github.com/TheDevRo/Jobsmith/issues)**

When reporting a bug, it helps a lot to include:

- What you were doing when it happened
- What you expected, and what happened instead
- Your iOS version, and the Jobsmith version (visible in the App Store listing
  under your purchase history, or in TestFlight)
- Which AI endpoint you have configured (on-device, local server, or a cloud
  provider) — many issues are endpoint-specific

If you would rather not use GitHub, you can email:
<!-- TODO(owner): replace with a real contact email before App Store submission -->
`TODO-ADD-CONTACT-EMAIL`

## Troubleshooting

### "Could not reach the server" — the AI endpoint is not reachable

This is the most common issue. Jobsmith needs an AI model to score jobs and
write documents. Check, in order:

1. **Which engine is selected?** Settings → AI connection. If you picked Apple's
   on-device model, it requires a device with Apple Intelligence and a recent
   iOS version. If it is unavailable there, switch to an endpoint.
2. **Is the endpoint URL right?** For a local server such as LM Studio, the URL
   is typically `http://<your-computer's-LAN-IP>:1234/v1` — **not**
   `localhost`. `localhost` on your iPhone means the iPhone itself, not your
   computer.
3. **Is your phone on the same Wi-Fi as the computer?** A local AI server is
   only reachable while you are on the same network. On cellular, it will not
   be.
4. **Is the server actually listening on the network?** LM Studio (and similar)
   often defaults to listening only on `127.0.0.1`. Enable the option to serve
   on the local network.
5. **Local network permission.** The first time Jobsmith contacts a server on
   your LAN, iOS asks for local network access. If you declined, re-enable it in
   iOS Settings → Privacy & Security → Local Network → Jobsmith.
6. **Use "Test connection"** in Settings → AI connection. It reports the exact
   error, which is usually enough to identify the cause.
7. **Firewall.** Your computer's firewall may be blocking inbound connections to
   the AI server's port.

If you are using a cloud provider (OpenAI, OpenRouter, etc.), check that the
base URL and the API key are both correct, and that the model name you selected
exists on that provider.

### LinkedIn sign-in

LinkedIn sign-in is **optional**. Job search on LinkedIn uses LinkedIn's public
guest interface and works without signing in.

- **The sign-in screen won't complete.** You sign in inside a web view, exactly
  as you would in Safari. If LinkedIn asks for a verification code or shows a
  checkpoint, complete it in that view. Signing in to linkedin.com in Safari
  first, then returning to Jobsmith, sometimes clears a stuck checkpoint.
- **Where do my credentials go?** They are typed into LinkedIn's own page.
  Jobsmith never sees or stores your LinkedIn password. It stores only the
  session cookie, in the iOS Keychain, on the device.
- **To remove it**, sign out in the app, or delete the app.
- **Sign-in stops working after a while.** LinkedIn sessions expire. Sign in
  again.

### Sync setup

Sync is optional and off by default. It works through a shared folder — there is
no sync server and no account.

1. Go to Settings → Sync and choose a folder. Pick one that is itself synced
   between your devices, e.g. a folder in **iCloud Drive** or **Dropbox**.
2. On your other device (or the Jobsmith desktop app), point it at **the same
   folder**.
3. Both devices then read and write that folder, and changes merge.

Common problems:

- **Nothing syncs.** Make sure both devices really point at the *same* folder,
  and that your cloud provider has finished uploading/downloading it. In the
  Files app, an iCloud folder may still be pending download.
- **A new device shows no data.** Give the cloud provider time to pull the
  folder's contents down before running a sync.
- **Passwords didn't sync.** That is intentional. Saved ATS passwords and the
  LinkedIn cookie are deliberately never written to the sync folder.

### Background job searches

Jobsmith can search in the background and notify you about new matches. iOS
decides when to run background tasks, so they are not exactly on a schedule. If
background searches never seem to run:

- Make sure Background App Refresh is enabled (iOS Settings → General →
  Background App Refresh), and that notifications are allowed for Jobsmith.
- Background searches that need the AI will not complete if your AI endpoint is a
  local server you are away from. Apple's on-device model works anywhere.

### Resume import lost my dates or sections

Resume parsing is imperfect, especially for heavily designed PDF layouts. Open
your profile after importing and correct anything that came through wrong — the
imported result is fully editable. If a resume imports badly, an issue with a
description of the layout (please do not attach a resume with your personal
details in it) is genuinely useful.

## Privacy

See the [Privacy Policy](privacy-policy.html). In short: the developer collects
nothing, there is no analytics or tracking, and your data stays on your device
except for the requests you direct it to make.
