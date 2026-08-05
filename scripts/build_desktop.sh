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

echo "[build] Building browser extension…"
extension/scripts/build.sh

echo "[build] Building backend sidecar with PyInstaller…"
.venv/bin/pyinstaller packaging/jobsmith-backend.spec --noconfirm \
    --distpath build/pyinstaller/dist --workpath build/pyinstaller/work

# Tauri expects the sidecar at src-tauri/binaries/<name>-<target-triple>.
mkdir -p src-tauri/binaries
cp build/pyinstaller/dist/jobsmith-backend \
   "src-tauri/binaries/jobsmith-backend-${TRIPLE}"
chmod +x "src-tauri/binaries/jobsmith-backend-${TRIPLE}"
echo "[build] Sidecar staged at src-tauri/binaries/jobsmith-backend-${TRIPLE}"

# Apple Intelligence bridge (macOS only). Needs Xcode 26+/macOS 26 SDK for
# FoundationModels; older toolchains still compile it (the framework is
# guarded) but if the Swift toolchain is missing entirely we warn rather than
# kill the build — note that `tauri build` then fails on the missing
# externalBin, so this is a warning you have to act on before bundling.
if [ "$(uname -s)" = "Darwin" ]; then
    echo "[build] Building Apple Intelligence bridge (Swift)…"
    if swift build -c release --package-path apple-bridge; then
        cp apple-bridge/.build/release/jobsmith-apple-ai \
           "src-tauri/binaries/jobsmith-apple-ai-${TRIPLE}"
        chmod +x "src-tauri/binaries/jobsmith-apple-ai-${TRIPLE}"
        echo "[build] Apple bridge staged at src-tauri/binaries/jobsmith-apple-ai-${TRIPLE}"
    else
        echo "[build] WARNING: Swift build failed — Apple Intelligence sidecar not staged." >&2
    fi
fi

if [ "$1" = "--sidecar-only" ]; then
    exit 0
fi

echo "[build] Building Tauri app (unsigned)…"
npx tauri build

echo "[build] Done. See src-tauri/target/release/bundle/"
