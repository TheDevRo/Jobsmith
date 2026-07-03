#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "=== Jobsmith ==="
echo ""

# ── First-run setup ───────────────────────────────────────────────────────────

if [ ! -d ".venv" ]; then
    echo "[setup] Creating Python virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate

    echo "[setup] Installing Python dependencies..."
    pip install -q -r requirements.txt

    echo "[setup] Installing Playwright browsers (Python)..."
    playwright install chromium

    echo "[setup] Installing root Node.js dependencies..."
    npm install --silent

    echo "[setup] Initializing database..."
    python3 -c "import asyncio; from backend.database import init_db; asyncio.run(init_db())"

    mkdir -p data resumes data/screenshots
    echo ""
    echo "Python setup complete!"
    echo ""
else
    source .venv/bin/activate
    mkdir -p data/screenshots
fi

# ── Start Python backend ──────────────────────────────────────────────────────

# Bind address comes from config.yaml (server.host / server.port). The default
# is loopback-only; set server.host to 0.0.0.0 to deliberately expose the
# dashboard to your LAN (the API has no auth beyond the extension token, so
# only do that on a network you trust).
read -r HOST PORT <<< "$(python3 - <<'PYEOF'
import yaml
try:
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    cfg = {}
server = cfg.get("server") or {}
print(server.get("host") or "127.0.0.1", server.get("port") or 8888)
PYEOF
)"

if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ] && [ "$HOST" != "::1" ]; then
    echo "[WARNING] Binding to $HOST — the dashboard and API are reachable from your network."
fi

echo "[start] Starting server at http://localhost:$PORT"
python3 -m uvicorn backend.main:app --host "$HOST" --port "$PORT" --reload
