#!/usr/bin/env bash
# Guard against code drift between the bundled iOS Apply scripts
# (ios-standalone/App/Apply/JS) and their single-source originals
# (extension/src/common). Comments and blank lines are ignored — each copy's
# header intentionally describes its own host — but any CODE difference fails.
#
# Run from anywhere:  ios-standalone/scripts/check_apply_js_sync.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"

strip() { sed 's|//.*$||' "$1" | sed 's/[[:space:]]*$//' | grep -v '^[[:space:]]*$'; }

status=0
for f in snapshot fill workday_auth; do
  ios="$root/ios-standalone/App/Apply/JS/$f.js"
  ext="$root/extension/src/common/$f.js"
  if [ ! -f "$ios" ] || [ ! -f "$ext" ]; then
    echo "MISSING: $f.js not present in both trees" >&2
    status=1
    continue
  fi
  if ! diff -q <(strip "$ios") <(strip "$ext") >/dev/null; then
    echo "DRIFT: $f.js code differs between ios-standalone and extension — edit both" >&2
    diff <(strip "$ios") <(strip "$ext") | head -40 >&2 || true
    status=1
  fi
done

[ "$status" -eq 0 ] && echo "Apply JS copies are code-identical."
exit "$status"
