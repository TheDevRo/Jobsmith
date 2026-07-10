#!/bin/bash
set -e

echo "=== Jobsmith Setup ==="
echo ""

# Create directories
mkdir -p data resumes data/screenshots

# ── Step 1: Python virtual environment ───────────────────────────────────────
echo "[1/5] Creating Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

# ── Step 2: Python dependencies ───────────────────────────────────────────────
echo "[2/5] Installing Python dependencies..."
pip install -r requirements.txt

# ── Step 3: Playwright browser (Python) ───────────────────────────────────────
echo "[3/5] Installing Playwright Chromium browser (Python)..."
playwright install chromium

# ── Step 4: Root Node.js dependencies ─────────────────────────────────────────
echo "[4/5] Installing root Node.js dependencies..."
npm install --silent

# ── Step 5: Initialize database ───────────────────────────────────────────────
echo "[5/5] Initializing database..."
python3 -c "import asyncio; from backend.database import init_db; asyncio.run(init_db())"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config.yaml with your profile and preferences"
echo "       (set ai.base_url / ai.model to point at your LM Studio server)"
echo "  2. Start LM Studio and load a model"
echo "  3. Run: ./start_server.sh"
echo "  4. Open: http://localhost:8888"
echo ""
echo "  (Optional) Start Skyvern visual fallback:"
echo "    docker compose -f docker-compose.skyvern.yml up -d"
echo "  (Optional) Import n8n/workflows.json into your n8n instance"
echo "  (Optional) Get free Adzuna API keys at https://developer.adzuna.com"
