# Owner: turning on GitHub Pages

This folder is the source for the public Privacy Policy and Support pages that
App Store Connect requires. It is not part of the app build.

## 1. Enable Pages (one time)

On github.com/TheDevRo/Jobsmith:

**Settings → Pages → Build and deployment**

- **Source:** Deploy from a branch
- **Branch:** `main`
- **Folder:** `/docs`
- **Save**

The first build takes a minute or two. When it is done, the Pages settings page
shows the live site URL.

Note: these files must be merged to `main` for this to work — Pages is building
from the `main` branch, not from a feature branch.

## 2. The URLs to paste into App Store Connect

The site root is `https://thedevro.github.io/Jobsmith/`.

Jekyll renders each `.md` file in `docs/` to a matching `.html` page, so:

| File | URL |
| --- | --- |
| `docs/privacy-policy.md` | `https://thedevro.github.io/Jobsmith/privacy-policy.html` |
| `docs/support.md` | `https://thedevro.github.io/Jobsmith/support.html` |
| `docs/index.md` | `https://thedevro.github.io/Jobsmith/` |

**Use the `.html` URLs** — they are the ones Jekyll actually produces and are
guaranteed to resolve. (GitHub Pages also serves the extensionless form,
`.../privacy-policy`, but the `.html` form is the safe one to hand to Apple.)

In App Store Connect:

- **Privacy Policy URL** (App Information) → the `privacy-policy.html` URL
- **Support URL** (the version's App Store listing) → the `support.html` URL

Both must be live and publicly reachable *before* you submit, or review will
reject the build. Open them in a private browser window to confirm.

## 3. Before you submit — TODOs

Two placeholders must be replaced. Search the folder for `TODO-ADD-CONTACT-EMAIL`:

- `docs/privacy-policy.md` — contact email in the "Contact" section
- `docs/support.md` — contact email in the "Getting help" section

Apple expects a way to reach you. GitHub Issues alone is usually accepted for
the Support URL, but a real email address is safer and is expected for privacy
inquiries. Use an address you actually monitor.

## Notes

- `_config.yml` sets the Jekyll theme (`jekyll-theme-minimal`) so the pages
  render as a styled site instead of raw markdown.
- The "Last updated" date in the privacy policy is `2026-07-14`. Bump it
  whenever the policy's substance changes.
- Keep the privacy policy consistent with `PrivacyInfo.xcprivacy` in the app
  (tracking disabled, no collected data types). If the app ever adds analytics,
  a crash reporter, or an ad SDK, both files must change together.
