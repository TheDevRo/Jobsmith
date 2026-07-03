#!/usr/bin/env bash
# Build extension/dist/chrome and extension/dist/firefox from extension/src.
# Each dist dir is a load-unpacked-ready directory with the correct manifest.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SRC="$ROOT/src"
DIST="$ROOT/dist"

# Preserve Mozilla-signed artifacts (`web-ext sign` output) across rebuilds.
STASH=""
if [ -d "$DIST/firefox/web-ext-artifacts" ]; then
  STASH="$(mktemp -d)"
  mv "$DIST/firefox/web-ext-artifacts" "$STASH/"
fi

rm -rf "$DIST"
mkdir -p "$DIST/chrome" "$DIST/firefox"

# Copy everything except the two variant manifests
copy_common() {
  local target="$1"
  rsync -a --exclude 'manifest.*.json' "$SRC/" "$target/"
}

copy_common "$DIST/chrome"
cp "$SRC/manifest.chrome.json" "$DIST/chrome/manifest.json"

copy_common "$DIST/firefox"
cp "$SRC/manifest.firefox.json" "$DIST/firefox/manifest.json"

# Produce zip artifacts the backend can serve for download.
# Re-zip in-place so the archive root contains the manifest (not a wrapping folder).
(cd "$DIST/chrome"  && zip -qr "$DIST/jobsmith-chrome.zip"  .)
(cd "$DIST/firefox" && zip -qr "$DIST/jobsmith-firefox.zip" .)

# Restore signed artifacts after zipping so they never end up inside the zips.
if [ -n "$STASH" ]; then
  mv "$STASH/web-ext-artifacts" "$DIST/firefox/"
  rmdir "$STASH"
fi

echo "Built:"
echo "  $DIST/chrome   (load unpacked in chrome://extensions)"
echo "  $DIST/firefox  (Load Temporary Add-on in about:debugging)"
echo "  $DIST/jobsmith-chrome.zip"
echo "  $DIST/jobsmith-firefox.zip"
