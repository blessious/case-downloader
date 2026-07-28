#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
export PLAYWRIGHT_BROWSERS_PATH=0
.venv/bin/python -m playwright install chromium
.venv/bin/pyinstaller --noconfirm --clean CasePDFDownloader.spec

echo
echo "macOS package created in dist/Case PDF Downloader"
