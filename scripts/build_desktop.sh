#!/bin/bash
# Build the Jobsmith desktop app (unsigned).
#
#   scripts/build_desktop.sh          # PyInstaller sidecar + tauri build
#   scripts/build_desktop.sh --sidecar-only
#
# Prereqs: rustup, `npm install` (brings @tauri-apps/cli), and the project
# venv with pyinstaller (.venv/bin/pip install pyinstaller). See README-DESKTOP.md.
set -e
cd "$(dirname "$0")/.."

. "$HOME/.cargo/env" 2>/dev/null || true

TRIPLE="$(rustc -vV | sed -n 's/host: //p')"
echo "[build] target triple: $TRIPLE"

echo "[build] Building backend sidecar with PyInstaller…"
.venv/bin/pyinstaller packaging/jobsmith-backend.spec --noconfirm \
    --distpath build/pyinstaller/dist --workpath build/pyinstaller/work

# Tauri expects the sidecar at src-tauri/binaries/<name>-<target-triple>.
mkdir -p src-tauri/binaries
cp build/pyinstaller/dist/jobsmith-backend \
   "src-tauri/binaries/jobsmith-backend-${TRIPLE}"
chmod +x "src-tauri/binaries/jobsmith-backend-${TRIPLE}"
echo "[build] Sidecar staged at src-tauri/binaries/jobsmith-backend-${TRIPLE}"

if [ "$1" = "--sidecar-only" ]; then
    exit 0
fi

echo "[build] Building Tauri app (unsigned)…"
npx tauri build

echo "[build] Done. See src-tauri/target/release/bundle/"
