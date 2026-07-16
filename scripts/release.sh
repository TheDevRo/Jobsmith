#!/bin/bash
# Cut a Jobsmith release from this machine (no CI — private repo, macOS
# Actions minutes are 10x).
#
#   scripts/release.sh                # build, tag, push, draft GitHub release
#   scripts/release.sh --dry-run      # build + stage assets, no tag/push/release
#   scripts/release.sh --publish-only # reuse the staged assets: (re)upload them
#                                     # to the existing tag/release. Use this
#                                     # after a partial run (tag pushed but
#                                     # `gh release create` failed, or an asset
#                                     # upload died) instead of deleting the tag.
#
# Prereqs: on main, clean working tree, gh CLI authed, rustup + npm install +
# .venv with pyinstaller (see README-DESKTOP.md).
set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN=""
PUBLISH_ONLY=""
case "${1:-}" in
    --dry-run)      DRY_RUN=1 ;;
    --publish-only) PUBLISH_ONLY=1 ;;
    "")             ;;
    *)              echo "usage: $0 [--dry-run|--publish-only]" >&2; exit 2 ;;
esac

# ── Guards ────────────────────────────────────────────────────────────────────

VERSION="$(node -p "require('./package.json').version")"
PY_VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' backend/version.py)"
# Only the [package] block — a dependency's version = "…" must not match.
CARGO_VERSION="$(sed -n '/^\[package\]/,/^\[dependencies\]/ s/^version = "\(.*\)"/\1/p' src-tauri/Cargo.toml)"
EXT_VERSION="$(node -p "require('./extension/src/manifest.chrome.json').version")"
TAG="v${VERSION}"

if [ "$VERSION" != "$PY_VERSION" ]; then
    echo "ERROR: package.json ($VERSION) != backend/version.py ($PY_VERSION)" >&2
    echo "       run: scripts/bump_version.sh $VERSION" >&2
    exit 1
fi

if [ "$VERSION" != "$CARGO_VERSION" ]; then
    echo "ERROR: package.json ($VERSION) != src-tauri/Cargo.toml ($CARGO_VERSION)" >&2
    echo "       run: scripts/bump_version.sh $VERSION" >&2
    exit 1
fi

if [ "$VERSION" != "$EXT_VERSION" ]; then
    echo "ERROR: package.json ($VERSION) != extension manifest ($EXT_VERSION)" >&2
    echo "       run: scripts/bump_version.sh $VERSION" >&2
    exit 1
fi

BRANCH="$(git branch --show-current)"
if [ "$BRANCH" != "main" ]; then
    echo "ERROR: releases are cut from main (on '$BRANCH')" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree not clean" >&2
    exit 1
fi

if [ -z "$DRY_RUN" ] && ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: gh is not authenticated — run 'gh auth login'" >&2
    exit 1
fi

# The notes template is versioned by hand; refuse to ship last release's notes.
NOTES_TEMPLATE="packaging/release-notes.md"
if ! grep -q "^## What's new in __VERSION__" "$NOTES_TEMPLATE"; then
    echo "ERROR: $NOTES_TEMPLATE is missing the '## What's new in __VERSION__' heading" >&2
    exit 1
fi
NOTES_FOR="$(sed -n 's/^<!-- notes-updated-for: \(.*\) -->$/\1/p' "$NOTES_TEMPLATE")"
if [ "$NOTES_FOR" != "$VERSION" ]; then
    echo "ERROR: $NOTES_TEMPLATE still holds notes for '${NOTES_FOR:-?}', not $VERSION." >&2
    echo "       Update the 'What's new' section, then set:" >&2
    echo "       <!-- notes-updated-for: $VERSION -->" >&2
    exit 1
fi

RELEASE_EXISTS=""
if [ -z "$DRY_RUN" ] && gh release view "$TAG" >/dev/null 2>&1; then
    RELEASE_EXISTS=1
fi

if [ -z "$DRY_RUN" ] && [ -z "$PUBLISH_ONLY" ]; then
    if git rev-parse "$TAG" >/dev/null 2>&1 || [ -n "$RELEASE_EXISTS" ]; then
        echo "ERROR: $TAG already exists (tag and/or GitHub release)." >&2
        echo "       Bump the version, or re-run with --publish-only to finish that release." >&2
        exit 1
    fi
fi

echo "[release] Releasing $TAG (extension v${EXT_VERSION})"

# ── Build ─────────────────────────────────────────────────────────────────────

STAGE="build/release-assets"
BUNDLE="src-tauri/target/release/bundle"

DMG="$STAGE/Jobsmith_${VERSION}_aarch64.dmg"
TARBALL="$STAGE/Jobsmith_${VERSION}_aarch64.app.tar.gz"
CHROME_ZIP="$STAGE/jobsmith-extension-chrome-v${EXT_VERSION}.zip"
FIREFOX_ZIP="$STAGE/jobsmith-extension-firefox-v${EXT_VERSION}.zip"
NOTES="$STAGE/release-notes.md"

if [ -n "$PUBLISH_ONLY" ]; then
    echo "[release] --publish-only: reusing assets in $STAGE"
    for f in "$DMG" "$TARBALL" "$CHROME_ZIP" "$FIREFOX_ZIP" "$STAGE/SHA256SUMS" "$NOTES"; do
        if [ ! -f "$f" ]; then
            echo "ERROR: missing staged asset $f — run a full release first" >&2
            exit 1
        fi
    done
else
    # build_desktop.sh already builds the extension.
    scripts/build_desktop.sh

    # ── Stage assets ──────────────────────────────────────────────────────────
    rm -rf "$STAGE"
    mkdir -p "$STAGE"

    cp "$BUNDLE/dmg/Jobsmith_${VERSION}_aarch64.dmg" "$DMG"

    # tar (not zip) preserves execute bits and the ad-hoc code signature — zip
    # transport is a common source of "app is damaged" on unsigned apps.
    tar -czf "$TARBALL" -C "$BUNDLE/macos" Jobsmith.app

    cp extension/dist/jobsmith-chrome.zip "$CHROME_ZIP"
    cp extension/dist/jobsmith-firefox.zip "$FIREFOX_ZIP"

    (cd "$STAGE" && shasum -a 256 -- * > SHA256SUMS)

    # Render the release notes template.
    sed -e "s/__VERSION__/${VERSION}/g" -e "s/__EXT_VERSION__/${EXT_VERSION}/g" \
        "$NOTES_TEMPLATE" > "$NOTES"

    echo "[release] Assets staged in $STAGE:"
    ls -lh "$STAGE"
fi

if [ -n "$DRY_RUN" ]; then
    echo "[release] Dry run — skipping tag/push/release."
    exit 0
fi

# ── Tag + publish ─────────────────────────────────────────────────────────────

# Pushing the tag also fires .github/workflows/docker-publish.yml, which
# publishes ghcr.io/thedevro/jobsmith:${VERSION} from the same commit.
git rev-parse "$TAG" >/dev/null 2>&1 || git tag "$TAG"
git push origin main
git push origin "refs/tags/${TAG}"

if [ -n "$RELEASE_EXISTS" ]; then
    echo "[release] Release $TAG exists — re-uploading assets."
    gh release upload "$TAG" --clobber \
        "$DMG" "$TARBALL" "$CHROME_ZIP" "$FIREFOX_ZIP" "$STAGE/SHA256SUMS"
else
    gh release create "$TAG" \
        --draft \
        --title "Jobsmith $TAG" \
        --notes-file "$NOTES" \
        "$DMG" "$TARBALL" "$CHROME_ZIP" "$FIREFOX_ZIP" "$STAGE/SHA256SUMS"
fi

echo "[release] Draft release $TAG ready — review and publish it on GitHub."
