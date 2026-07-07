#!/usr/bin/env bash
# Run the iOS UI smoke tests against a live backend on 127.0.0.1:8888.
# Starts the repo backend if nothing is listening, and stops it afterwards
# if (and only if) this script started it.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IOS_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$IOS_ROOT/.." && pwd)"
SIM_NAME="${JOBSMITH_SIM:-iPhone 17 Pro}"

STARTED_BACKEND=""
if ! curl -s -m 3 http://127.0.0.1:8888/api/stats > /dev/null; then
  echo "No backend on :8888 — starting one from the repo venv..."
  (cd "$REPO_ROOT" && .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8888 &> /tmp/jobsmith-uitest-backend.log & echo $! > /tmp/jobsmith-uitest-backend.pid)
  STARTED_BACKEND=1
  for _ in $(seq 1 30); do
    curl -s -m 2 http://127.0.0.1:8888/api/stats > /dev/null && break
    sleep 1
  done
fi

cleanup() {
  if [ -n "$STARTED_BACKEND" ] && [ -f /tmp/jobsmith-uitest-backend.pid ]; then
    kill "$(cat /tmp/jobsmith-uitest-backend.pid)" 2>/dev/null || true
    rm -f /tmp/jobsmith-uitest-backend.pid
  fi
}
trap cleanup EXIT

cd "$IOS_ROOT"
xcodegen generate
xcodebuild -project Jobsmith.xcodeproj -scheme Jobsmith \
  -destination "platform=iOS Simulator,name=$SIM_NAME" test
