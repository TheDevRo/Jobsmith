#!/usr/bin/env bash
# Compose the standalone Safari Web Extension resources from the shared
# extension source (extension/src). Differences from the server-connector
# variant (ios/scripts/sync-extension-resources.sh):
#   - manifest.ios-standalone.json is installed as manifest.json
#     (adds nativeMessaging, drops the localhost handshake content script)
#   - common/api.native.js REPLACES common/api.js, turning every backend
#     HTTP call into a native message to the containing app.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STANDALONE_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$STANDALONE_ROOT/.." && pwd)"
SRC="$REPO_ROOT/extension/src"
DEST="$STANDALONE_ROOT/SafariExt/Resources"

mkdir -p "$DEST"
rsync -a --delete --exclude 'manifest.*.json' --exclude 'common/api.native.js' "$SRC/" "$DEST/"
cp "$SRC/manifest.ios-standalone.json" "$DEST/manifest.json"
cp "$SRC/common/api.native.js" "$DEST/common/api.js"

echo "Synced $SRC -> $DEST (standalone native-messaging variant)"
