#!/usr/bin/env python3
"""Beispiel-/Demo-Episode im READY-Ordner anlegen.

Duenn ueber der Anwendungsschicht: die eigentliche Logik liegt in
``youtube_autoposter.utils.demo_episode`` und wird genauso von
``python app.py init --demo`` bzw. ``python app.py demo`` verwendet.

    python tools/create_sample_episode.py                          # Beispiel kopieren
    python tools/create_sample_episode.py --name video_025
    python tools/create_sample_episode.py --generate --seconds 8   # mit ffmpeg erzeugen

Es wird nichts hochgeladen - die Dateien landen nur in ``folders/READY``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from youtube_autoposter.config import load_settings  # noqa: E402
from youtube_autoposter.utils.demo_episode import DEMO_NAME, create_demo_episode  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Beispiel-Episode in READY/ anlegen")
    parser.add_argument("--name", default="example_video", help="Episode-ID / Basisname")
    parser.add_argument("--target", help="Zielordner (Standard: READY-Ordner aus config.json)")
    parser.add_argument("--generate", action="store_true", help="Testvideo mit ffmpeg erzeugen statt zu kopieren")
    parser.add_argument("--seconds", type=int, default=5, help="Laenge des erzeugten Testvideos")
    parser.add_argument("--overwrite", action="store_true", help="vorhandene Dateien ersetzen")
    parser.add_argument("--config", help="Pfad zu config.json")
    args = parser.parse_args()

    settings = load_settings(args.config)
    ziel = Path(args.target) if args.target else Path(settings.READY_FOLDER)
    print(f"Zielordner: {ziel}")

    pfade = create_demo_episode(
        settings,
        name=args.name or DEMO_NAME,
        target=ziel,
        generate=args.generate,
        seconds=args.seconds,
        overwrite=args.overwrite,
    )
    for pfad in pfade:
        print(f"  vorhanden: {pfad}")

    print("\nFertig. Der Watcher erkennt die Dateien automatisch (bzw. 'JETZT SCANNEN' im Dashboard).")
    print("Hinweis: Der Upload bleibt privat - veroeffentlicht wird nur per Button.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
