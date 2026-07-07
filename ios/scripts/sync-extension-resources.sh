#!/usr/bin/env bash
# Compose the Safari Web Extension resources for the JobsmithAssist appex
# from the shared extension source (extension/src), the same way
# extension/scripts/build.sh composes the chrome/firefox dist dirs.
#
# Runs as an Xcode pre-build phase and can be run by hand after editing
# extension/src. The iOS variant uses manifest.safari.json, whose only
# difference is that the assist-launch handshake matches any host — on iOS
# the backend is a remote machine on your LAN, not localhost.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IOS_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$IOS_ROOT/.." && pwd)"
SRC="$REPO_ROOT/extension/src"
DEST="$IOS_ROOT/JobsmithAssist/Resources"

mkdir -p "$DEST"
rsync -a --delete --exclude 'manifest.*.json' "$SRC/" "$DEST/"
cp "$SRC/manifest.safari.json" "$DEST/manifest.json"

echo "Synced $SRC -> $DEST"
