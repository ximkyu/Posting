#!/usr/bin/env python3
"""YouTube AutoPoster - Startpunkt.

Aufrufe::

    python app.py                 Dashboard + Watch-Folder + Upload-Queue starten
    python app.py --dry-run       alles pruefen, NICHTS hochladen
    python app.py --help          alle Befehle

Unter Windows einfach ``start.bat`` doppelklicken.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Projekt-Root in den Modul-Pfad aufnehmen (Start aus beliebigem Verzeichnis)
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from youtube_autoposter.cli import main  # noqa: E402  (Import nach sys.path-Anpassung)

if __name__ == "__main__":
    raise SystemExit(main())
