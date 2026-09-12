#!/usr/bin/env bash
# =====================================================================
#  YouTube AutoPoster - Tests (Linux/macOS/WSL)
#  Die Tests nutzen Mocks: Es wird NIEMALS ein echtes Video hochgeladen.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

if [[ -x ".venv/bin/python" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
exec python -m pytest tests -v "$@"
