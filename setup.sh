#!/usr/bin/env bash
# =====================================================================
#  YouTube AutoPoster - Einrichtung (Linux/macOS/WSL)
#  Windows-Nutzer verwenden setup.bat
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================================"
echo "  YouTube AutoPoster - Einrichtung"
echo "============================================================"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON_BIN="$candidate"; break; fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "[FEHLER] Kein Python gefunden. Bitte Python 3.11+ installieren."
  exit 1
fi
echo "[1/5] Python: $($PYTHON_BIN --version) ($PYTHON_BIN)"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[2/5] Virtuelle Umgebung wird angelegt (.venv)"
  "$PYTHON_BIN" -m venv .venv
else
  echo "[2/5] Virtuelle Umgebung vorhanden"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "[3/5] Abhaengigkeiten werden installiert"
python -m pip install --upgrade pip wheel >/dev/null
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt || true

echo "[4/5] Ordner, Datenbank, config.json"
python app.py init

echo "[5/5] System-Check (Dry-Run)"
python app.py --dry-run || true

cat <<'TXT'

============================================================
  Einrichtung abgeschlossen.

  Naechste Schritte:
   1. Google Cloud Projekt + YouTube Data API v3
   2. OAuth-Client (Typ: Desktop-App) erstellen, JSON herunterladen
   3. als credentials/credentials.json ablegen
   4. ./start.sh  (bzw. python app.py)
   5. Dashboard -> "Google-/YouTube-Konto einmalig verbinden"
   6. Videos nach folders/READY legen
============================================================
TXT
