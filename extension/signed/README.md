# Signed Firefox extension artifacts

Mozilla-signed `.xpi` files, committed so every distribution channel can
serve a permanently installable Firefox extension:

- **Desktop app**: PyInstaller bundles `extension/dist/` (build.sh copies
  these files into `dist/firefox/web-ext-artifacts/`).
- **Docker / source checkouts**: the backend also reads this directory
  directly — no signing credentials exist in CI, so the committed artifact
  is the only way those users get a signed XPI.

The backend serves the highest version found (parsed from the
`...-X.Y.Z.xpi` filename suffix; see `_latest_signed_xpi` in
`backend/routers/extension.py`).

## Releasing a new extension version

1. Bump `"version"` in `extension/src/manifest.firefox.json` (AMO rejects
   duplicate versions) and `manifest.chrome.json` to match.
2. `extension/scripts/build.sh`
3. `cd extension/dist/firefox && npx web-ext sign --channel=unlisted \
       --api-key=<AMO JWT issuer> --api-secret=<AMO JWT secret>`
4. Copy the new `web-ext-artifacts/*.xpi` here and commit it.
5. Rebuild the desktop app (`scripts/build_desktop.sh`) AFTER signing so
   the DMG bundles the new artifact.

Old versions can be deleted once no release ships them.
