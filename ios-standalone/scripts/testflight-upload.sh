#!/bin/zsh
# Archive JobsmithStandalone and upload it to App Store Connect / TestFlight.
#
# This file is git-ignored on purpose (it carries the API key config inline).
# The private .p8 lives OUTSIDE the repo at ~/.appstoreconnect/private_keys/.
#
# Usage:
#   ./scripts/testflight-upload.sh
#
# Prerequisite (one time, manual — the API cannot create it):
#   An app record must exist in App Store Connect for the bundle ID below.
#   App Store Connect > Apps > (+) New App > pick com.thedevro.jobsmith.standalone.
#
# What it does each run:
#   1. Bumps CURRENT_PROJECT_VERSION in project.yml (ASC rejects a reused build #).
#   2. Regenerates the Xcode project (xcodegen generate).
#   3. Archives the JobsmithStandalone scheme for a generic iOS device (Release),
#      letting cloud signing create the distribution cert + App Store profiles
#      for both the app and the Share extension.
#   4. Exports and uploads to App Store Connect.
#
# Requires an *Admin*-role App Store Connect API key (App Manager cannot mint
# the distribution certificate — "Cloud signing permission error").
set -euo pipefail

# --- Config (App Store Connect API — same account/team as StoryShare) ---------
ASC_KEY_ID="7GMN3N5L47"
ASC_ISSUER_ID="025c350a-d9b0-4e50-a2a5-37e79429f292"
ASC_KEY_PATH="$HOME/.appstoreconnect/private_keys/AuthKey_7GMN3N5L47.p8"
TEAM_ID="37R25GNY5A"
SCHEME="JobsmithStandalone"
# ------------------------------------------------------------------------------

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[[ -f "$ASC_KEY_PATH" ]] || { echo "error: API key not found at $ASC_KEY_PATH" >&2; exit 1; }

ARCHIVE="build/${SCHEME}.xcarchive"
EXPORT_OPTS="build/ExportOptions.plist"
mkdir -p build

# ExportOptions.plist is generated here so nothing extra needs committing.
cat > "$EXPORT_OPTS" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key><string>app-store-connect</string>
    <key>destination</key><string>upload</string>
    <key>teamID</key><string>${TEAM_ID}</string>
    <key>signingStyle</key><string>automatic</string>
    <key>uploadSymbols</key><true/>
    <key>manageAppVersionAndBuildNumber</key><false/>
</dict>
</plist>
PLIST

# 1. Bump the build number so ASC accepts the upload.
CUR="$(grep -E 'CURRENT_PROJECT_VERSION:' project.yml | head -1 | grep -oE '[0-9]+')"
NEXT=$((CUR + 1))
sed -i '' "s/CURRENT_PROJECT_VERSION: ${CUR}/CURRENT_PROJECT_VERSION: ${NEXT}/" project.yml
echo "==> Build number ${CUR} -> ${NEXT}"

# 2. Regenerate the project.
echo "==> Regenerating project (xcodegen)"
xcodegen generate

# 3. Archive.
echo "==> Archiving ${SCHEME} (device, Release, auto-provisioning)"
xcodebuild archive \
  -project JobsmithStandalone.xcodeproj \
  -scheme "$SCHEME" \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath "$ARCHIVE" \
  -allowProvisioningUpdates \
  -authenticationKeyPath "$ASC_KEY_PATH" \
  -authenticationKeyID "$ASC_KEY_ID" \
  -authenticationKeyIssuerID "$ASC_ISSUER_ID"

# 4. Export + upload.
echo "==> Exporting + uploading to App Store Connect"
xcodebuild -exportArchive \
  -archivePath "$ARCHIVE" \
  -exportOptionsPlist "$EXPORT_OPTS" \
  -allowProvisioningUpdates \
  -authenticationKeyPath "$ASC_KEY_PATH" \
  -authenticationKeyID "$ASC_KEY_ID" \
  -authenticationKeyIssuerID "$ASC_ISSUER_ID"

echo "==> Uploaded build ${NEXT}. It appears in App Store Connect > TestFlight in ~15-60 min,"
echo "    then add it to a tester group (external groups also need Beta App Review)."
