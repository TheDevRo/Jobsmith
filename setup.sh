#!/bin/bash
set -e

echo "=== Jobsmith Setup ==="
echo ""

# Create directories
mkdir -p data resumes data/screenshots

# ── Step 1: Python virtual environment ───────────────────────────────────────
echo "[1/6] Creating Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

# ── Step 2: Python dependencies ───────────────────────────────────────────────
echo "[2/6] Installing Python dependencies..."
pip install -r requirements.txt

# ── Step 3: Playwright browser (Python) ───────────────────────────────────────
echo "[3/6] Installing Playwright Chromium browser (Python)..."
playwright install chromium

# ── Step 4: Root Node.js dependencies ─────────────────────────────────────────
echo "[4/6] Installing root Node.js dependencies..."
npm install --silent

# ── Step 5: Stagehand service dependencies ────────────────────────────────────
echo "[5/6] Installing stagehand-service Node.js dependencies..."
(cd stagehand-service && npm install --silent)

# Install Playwright Chromium for Stagehand's Node.js runtime separately —
# the Python playwright install doesn't share binaries with the Node package.
echo "      Installing Playwright Chromium for Stagehand..."
(cd stagehand-service && npx playwright install chromium)

# Copy .env if it doesn't exist yet
if [ ! -f stagehand-service/.env ]; then
    cp stagehand-service/.env.example stagehand-service/.env
    echo "      Created stagehand-service/.env — edit LM_STUDIO_BASE_URL and LM_STUDIO_MODEL before starting"
fi

# ── Step 6: Initialize database ───────────────────────────────────────────────
echo "[6/6] Initializing database..."
python3 -c "import asyncio; from backend.database import init_db; asyncio.run(init_db())"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config.yaml with your profile and preferences"
echo "  2. Edit stagehand-service/.env:"
echo "       LM_STUDIO_BASE_URL — URL of your LM Studio server"
echo "       LM_STUDIO_MODEL    — name of the model loaded in LM Studio"
echo "  3. Start LM Studio and load a model"
echo "  4. Run: ./start_server.sh"
echo "  5. Open: http://localhost:8888"
echo ""
echo "  (Optional) Start Skyvern visual fallback:"
echo "    docker compose -f docker-compose.skyvern.yml up -d"
echo "  (Optional) Import n8n/workflows.json into your n8n instance"
echo "  (Optional) Get free Adzuna API keys at https://developer.adzuna.com"
