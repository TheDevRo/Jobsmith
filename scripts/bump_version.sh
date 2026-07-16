#!/bin/bash
# Single source of truth for the Jobsmith version.
#
#   scripts/bump_version.sh 0.2.5          # rewrite every version string
#   scripts/bump_version.sh 0.2.5 --check  # verify they all already agree
#
# Rewrites:
#   package.json                  .version              (the SSOT everything else follows)
#   backend/version.py            APP_VERSION
#   src-tauri/Cargo.toml          [package] version     (tauri.conf.json reads package.json)
#   ios-standalone/project.yml    MARKETING_VERSION
#   extension/src/manifest.*.json .version              (chrome/firefox/safari/ios-standalone)
#
# Not touched: CURRENT_PROJECT_VERSION (the iOS build number — that's a
# per-upload counter, pass it to xcodebuild, don't commit it).
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-}"
MODE="${2:-}"

if [ -z "$VERSION" ]; then
    echo "usage: scripts/bump_version.sh <version> [--check]" >&2
    exit 2
fi

if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "ERROR: '$VERSION' is not a x.y.z version" >&2
    exit 1
fi

# ── Read current values ───────────────────────────────────────────────────────

current_pkg()   { node -p "require('./package.json').version"; }
current_py()    { sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' backend/version.py; }
current_cargo() { sed -n '/^\[package\]/,/^\[/ s/^version = "\(.*\)"/\1/p' src-tauri/Cargo.toml; }
current_yml()   { sed -n 's/^[[:space:]]*MARKETING_VERSION:[[:space:]]*\(.*\)$/\1/p' "$1"; }
current_ext()   { node -p "require('./$1').version"; }

# Every extension manifest ships to a store independently, but the release
# guard (scripts/release.sh) now requires them to match the app version, so
# bump them here together with everything else.
EXT_MANIFESTS=(
    extension/src/manifest.chrome.json
    extension/src/manifest.firefox.json
    extension/src/manifest.safari.json
    extension/src/manifest.ios-standalone.json
)

report() {
    printf '  %-28s %s\n' "package.json" "$(current_pkg)"
    printf '  %-28s %s\n' "backend/version.py" "$(current_py)"
    printf '  %-28s %s\n' "src-tauri/Cargo.toml" "$(current_cargo)"
    printf '  %-28s %s\n' "ios-standalone/project.yml" "$(current_yml ios-standalone/project.yml)"
    for m in "${EXT_MANIFESTS[@]}"; do
        [ -f "$m" ] && printf '  %-28s %s\n' "$m" "$(current_ext "$m")"
    done
}

if [ "$MODE" = "--check" ]; then
    ok=1
    for got in "$(current_pkg)" "$(current_py)" "$(current_cargo)" \
               "$(current_yml ios-standalone/project.yml)"; do
        [ "$got" = "$VERSION" ] || ok=0
    done
    for m in "${EXT_MANIFESTS[@]}"; do
        [ -f "$m" ] || continue
        [ "$(current_ext "$m")" = "$VERSION" ] || ok=0
    done
    if [ "$ok" -eq 1 ]; then
        echo "[bump] all version strings are $VERSION"
        exit 0
    fi
    echo "ERROR: version drift (expected $VERSION):" >&2
    report >&2
    exit 1
fi

echo "[bump] current:"
report
echo "[bump] -> $VERSION"

# ── Rewrite ───────────────────────────────────────────────────────────────────

# package.json: edit via node so formatting/ordering survive.
node -e '
const fs = require("fs");
const p = JSON.parse(fs.readFileSync("package.json", "utf8"));
p.version = process.argv[1];
fs.writeFileSync("package.json", JSON.stringify(p, null, 2) + "\n");
' "$VERSION"

sed -i '' -e "s/^APP_VERSION = \".*\"/APP_VERSION = \"${VERSION}\"/" backend/version.py

# Only the [package] block — dependency versions must not be touched.
sed -i '' -e "/^\[package\]/,/^\[dependencies\]/ s/^version = \".*\"/version = \"${VERSION}\"/" \
    src-tauri/Cargo.toml

for yml in ios-standalone/project.yml; do
    [ -f "$yml" ] || continue
    sed -i '' -e "s/^\([[:space:]]*MARKETING_VERSION:[[:space:]]*\).*$/\1${VERSION}/" "$yml"
done

# Extension manifests: edit via node so formatting/ordering survive.
for m in "${EXT_MANIFESTS[@]}"; do
    [ -f "$m" ] || continue
    node -e '
const fs = require("fs");
const f = process.argv[1];
const j = JSON.parse(fs.readFileSync(f, "utf8"));
j.version = process.argv[2];
fs.writeFileSync(f, JSON.stringify(j, null, 2) + "\n");
' "$m" "$VERSION"
done

echo "[bump] now:"
report

# Cargo.lock records the app's own version too — patch it so the next build
# doesn't dirty the tree.
if [ -f src-tauri/Cargo.lock ]; then
    awk -v v="$VERSION" '
        /^name = "app"$/ { print; seen = 1; next }
        seen && /^version = / { print "version = \"" v "\""; seen = 0; next }
        { seen = 0; print }
    ' src-tauri/Cargo.lock > src-tauri/Cargo.lock.tmp
    mv src-tauri/Cargo.lock.tmp src-tauri/Cargo.lock
fi

echo "[bump] Done. Review with 'git diff', then update packaging/release-notes.md."
