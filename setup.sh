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
echo "  1. Start LM Studio (or Ollama) and load a model"
echo "  2. Run: ./start_server.sh"
echo "  3. Open: http://localhost:8888"
echo ""
echo "  On first launch the dashboard opens a setup wizard that configures your"
echo "  AI server, profile, and job search. config.yaml is created for you --"
echo "  there is nothing to edit by hand. You can skip the wizard and re-run it"
echo "  later from Settings."
echo ""
echo "  Headless box, no browser? Edit config.yaml directly instead (copy it"
echo "  from config.example.yaml and set ai.base_url / ai.models to point at"
echo "  your LM Studio server). See 'Advanced / headless install' in README.md."
echo ""
echo "  (Optional) Start Skyvern visual fallback:"
echo "    docker compose -f docker-compose.skyvern.yml up -d"
echo "  (Optional) Import n8n/workflows.json into your n8n instance"
echo "  (Optional) Get free Adzuna API keys at https://developer.adzuna.com"
