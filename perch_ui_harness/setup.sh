#!/usr/bin/env bash
# perch_ui_harness/setup.sh — One-shot environment setup for the Perch UI bridge.
# Run once per container/session before starting the MCP server.
# Idempotent: safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ANALYZER_DIR="$REPO_ROOT/analyzer"
MODELS_DIR="$ANALYZER_DIR/models"
SN_DIR="$MODELS_DIR/speciesnet"

echo "[setup] Repo root: $REPO_ROOT"

# ── 1. Python dependencies ──────────────────────────────────────────────

echo "[setup] Installing Python dependencies..."
pip3 install -q \
  mcp playwright \
  opencv-python-headless onnxruntime numpy pandas \
  pillow exifread rawpy pywebview bottle proxy_tools \
  2>&1 | tail -3

# ── 2. Playwright browser ──────────────────────────────────────────────

echo "[setup] Installing Chromium for Playwright..."
python3 -m playwright install chromium 2>&1 | tail -2

# ── 3. System tools ────────────────────────────────────────────────────

if ! command -v exiftool &>/dev/null; then
  echo "[setup] Installing exiftool..."
  apt-get install -y -qq libimage-exiftool-perl 2>&1 | tail -1
fi

# ── 4. Git LFS models (download via curl if they're pointers) ──────────

GITHUB_RAW="https://github.com/SanjaySoniLV/ProjectKestrel/raw/main"

download_if_pointer() {
  local file="$1"
  local url="$2"
  if [ ! -f "$file" ] || head -c 30 "$file" | grep -q "git-lfs"; then
    echo "[setup] Downloading $(basename "$file")..."
    curl -sL -o "$file" "$url"
    echo "[setup]   -> $(wc -c < "$file") bytes"
  else
    echo "[setup] $(basename "$file") OK ($(wc -c < "$file") bytes)"
  fi
}

download_if_pointer "$MODELS_DIR/quality.onnx" \
  "$GITHUB_RAW/analyzer/models/quality.onnx"

for model in mdv5a.onnx mdv5a.onnx.data mdv1000-cedar.onnx \
             speciesNet_v4.0.1a.onnx \
             sam_hq_vit_tiny_encoder.onnx sam_hq_vit_tiny_decoder.onnx; do
  download_if_pointer "$SN_DIR/$model" \
    "$GITHUB_RAW/analyzer/models/speciesnet/$model"
done

# ── 5. Auth token injection (optional) ─────────────────────────────────
# If KESTREL_AUTH_TOKEN is set, write it to the fallback auth file so the
# app starts in a signed-in state. This enables cloud compute / Perch
# upload testing without interactive OAuth.

if [ -n "${KESTREL_AUTH_TOKEN:-}" ]; then
  AUTH_DIR="$HOME/.local/share/project-kestrel"
  AUTH_FILE="$AUTH_DIR/auth.json"
  mkdir -p "$AUTH_DIR"
  python3 -c "
import json, time, os, sys
token = os.environ['KESTREL_AUTH_TOKEN']
bundle = {'access_token': token, 'refresh_token': '', 'expires_at': time.time() + 86400 * 30}
with open('$AUTH_FILE', 'w') as f:
    json.dump(bundle, f)
os.chmod('$AUTH_FILE', 0o600)
print('[setup] Auth token written to $AUTH_FILE')
"
else
  echo "[setup] No KESTREL_AUTH_TOKEN set — skipping auth injection"
fi

# ── 6. Verify ───────────────────────────────────────────────────────────

echo "[setup] Verifying imports..."
python3 -c "
import cv2, onnxruntime, numpy, pandas, rawpy, mcp
from playwright.sync_api import sync_playwright
print('[setup] All imports OK')
" 2>&1

echo "[setup] Done. Start the MCP server with:"
echo "  xvfb-run -a python3 $REPO_ROOT/perch_ui_harness/mcp_server.py"
