#!/bin/bash
# Cut a Jobsmith release from this machine (no CI — private repo, macOS
# Actions minutes are 10x).
#
#   scripts/release.sh            # build, tag, push, draft GitHub release
#   scripts/release.sh --dry-run  # build + stage assets, no tag/push/release
#
# Prereqs: clean working tree, gh CLI authed, rustup + npm install + .venv
# with pyinstaller (see README-DESKTOP.md).
set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN=""
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# ── Guards ────────────────────────────────────────────────────────────────────

VERSION="$(node -p "require('./package.json').version")"
PY_VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' backend/version.py)"
EXT_VERSION="$(node -p "require('./extension/src/manifest.chrome.json').version")"
TAG="v${VERSION}"

if [ "$VERSION" != "$PY_VERSION" ]; then
    echo "ERROR: package.json ($VERSION) != backend/version.py ($PY_VERSION)" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree not clean" >&2
    exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "ERROR: tag $TAG already exists" >&2
    exit 1
fi

echo "[release] Releasing $TAG (extension v${EXT_VERSION})"

# ── Build ─────────────────────────────────────────────────────────────────────

scripts/build_desktop.sh
extension/scripts/build.sh

# ── Stage assets ──────────────────────────────────────────────────────────────

STAGE="build/release-assets"
rm -rf "$STAGE"
mkdir -p "$STAGE"

BUNDLE="src-tauri/target/release/bundle"
cp "$BUNDLE/dmg/Jobsmith_${VERSION}_aarch64.dmg" "$STAGE/"

# tar (not zip) preserves execute bits and the ad-hoc code signature — zip
# transport is a common source of "app is damaged" on unsigned apps.
tar -czf "$STAGE/Jobsmith_${VERSION}_aarch64.app.tar.gz" \
    -C "$BUNDLE/macos" Jobsmith.app

cp extension/dist/jobsmith-chrome.zip \
   "$STAGE/jobsmith-extension-chrome-v${EXT_VERSION}.zip"
cp extension/dist/jobsmith-firefox.zip \
   "$STAGE/jobsmith-extension-firefox-v${EXT_VERSION}.zip"

(cd "$STAGE" && shasum -a 256 -- * > SHA256SUMS)

# Render the release notes template.
NOTES="$STAGE/release-notes.md"
sed -e "s/__VERSION__/${VERSION}/g" -e "s/__EXT_VERSION__/${EXT_VERSION}/g" \
    packaging/release-notes.md > "$NOTES"

echo "[release] Assets staged in $STAGE:"
ls -lh "$STAGE"

if [ -n "$DRY_RUN" ]; then
    echo "[release] Dry run — skipping tag/push/release."
    exit 0
fi

# ── Tag + publish ─────────────────────────────────────────────────────────────

# Pushing the tag also fires .github/workflows/docker-publish.yml, which
# publishes ghcr.io/thedevro/jobsmith:${VERSION} from the same commit.
git tag "$TAG"
git push origin main --tags

gh release create "$TAG" \
    --draft \
    --title "Jobsmith $TAG" \
    --notes-file "$NOTES" \
    "$STAGE/Jobsmith_${VERSION}_aarch64.dmg" \
    "$STAGE/Jobsmith_${VERSION}_aarch64.app.tar.gz" \
    "$STAGE/jobsmith-extension-chrome-v${EXT_VERSION}.zip" \
    "$STAGE/jobsmith-extension-firefox-v${EXT_VERSION}.zip" \
    "$STAGE/SHA256SUMS"

echo "[release] Draft release $TAG created — review and publish it on GitHub."
