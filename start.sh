#!/usr/bin/env bash
# =====================================================================
#  YouTube AutoPoster - Start (Linux/macOS/WSL)
#  Beenden mit STRG+C
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[FEHLER] Virtuelle Umgebung fehlt - bitte zuerst ./setup.sh ausfuehren."
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
exec python app.py "$@"
