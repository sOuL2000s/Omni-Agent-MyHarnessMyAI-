#!/usr/bin/env bash
# Linux / macOS / WSL installer (no Docker).
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Install Python 3.9+ first"; exit 1; }
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
echo
echo "Done. Next:  ./omni-ui   (web UI - add your API keys under 'Keys & models')"
echo "Terminal chat: ./omni      Check keys: ./omni check --all"