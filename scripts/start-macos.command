#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Case PDF Downloader is not set up yet."
  echo "Run scripts/setup-macos.sh in Terminal first."
  read -r -p "Press Enter to close."
  exit 1
fi
.venv/bin/python run.py
