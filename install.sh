#!/usr/bin/env bash
# Linux / macOS / WSL installer (no Docker).
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Install Python 3.9+ first"; exit 1; }
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
if command -v npm >/dev/null; then
  read -p "Install OmniRoute gateway globally with npm? [y/N] " a
  [ "$a" = "y" ] && npm install -g omniroute
else
  echo "Node.js not found - skip OmniRoute, or install Node and rerun."
fi
echo
echo "Done. Next:"
echo "  1) run 'omniroute' (dashboard http://localhost:20128), add OpenRouter key, create an API key"
echo "  2) put that key + model in .env"
echo "  3) ./omni check   then   ./omni"
