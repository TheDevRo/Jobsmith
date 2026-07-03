# PyInstaller spec for the desktop sidecar backend.
#
# Build (from repo root):
#   .venv/bin/pyinstaller packaging/jobsmith-backend.spec --distpath src-tauri/binaries-build
#
# Then rename dist/jobsmith-backend to
#   src-tauri/binaries/jobsmith-backend-<target-triple>
# (scripts/build_desktop.sh does all of this).

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

repo_root = Path(SPECPATH).parent

# Packages that ship non-Python data files (JS payloads, configs) alongside
# their modules. PyInstaller only grabs .py by default, so pull the rest in.
datas = [
    # _CODE_ROOT assets — land at _MEIPASS root, where
    # backend/app_state.py's __file__-relative lookups expect them.
    (str(repo_root / "frontend"), "frontend"),
    (str(repo_root / "extension" / "dist"), "extension/dist"),
    (str(repo_root / "config.example.yaml"), "."),
]
datas += collect_data_files("playwright_stealth")
datas += collect_data_files("playwright")

stealth_datas, stealth_binaries, stealth_hidden = collect_all("playwright_stealth")
datas += stealth_datas

a = Analysis(
    [str(repo_root / "packaging" / "desktop_entry.py")],
    pathex=[str(repo_root)],
    binaries=stealth_binaries,
    datas=datas,
    hiddenimports=stealth_hidden + [
        # uvicorn's dynamically-imported machinery
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "aiosqlite",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="jobsmith-backend",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    onefile=True,
)
