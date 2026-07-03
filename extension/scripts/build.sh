#!/usr/bin/env bash
# Build extension/dist/chrome and extension/dist/firefox from extension/src.
# Each dist dir is a load-unpacked-ready directory with the correct manifest.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SRC="$ROOT/src"
DIST="$ROOT/dist"

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

echo "Built:"
echo "  $DIST/chrome   (load unpacked in chrome://extensions)"
echo "  $DIST/firefox  (Load Temporary Add-on in about:debugging)"
echo "  $DIST/jobsmith-chrome.zip"
echo "  $DIST/jobsmith-firefox.zip"
