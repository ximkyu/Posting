#!/usr/bin/env python3
"""Beispiel-Episode im READY-Ordner anlegen.

Nuetzlich, um das System sofort auszuprobieren, ohne ein eigenes Video zu
benutzen::

    python tools/create_sample_episode.py                # Beispiel kopieren
    python tools/create_sample_episode.py --name video_025
    python tools/create_sample_episode.py --generate --seconds 8   # mit ffmpeg erzeugen

Es wird nichts hochgeladen - die Dateien landen nur in ``folders/READY``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from youtube_autoposter.config import load_settings  # noqa: E402
from youtube_autoposter.utils.media import find_ffmpeg  # noqa: E402

EXAMPLE_DIR = ROOT / "examples" / "ready_example"


def copy_example(target_dir: Path, name: str) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for source in sorted(EXAMPLE_DIR.iterdir()):
        destination = target_dir / f"{name}{source.suffix}"
        if destination.exists():
            print(f"  uebersprungen (existiert bereits): {destination.name}")
            created.append(destination)
            continue
        shutil.copy2(source, destination)
        created.append(destination)
        print(f"  erstellt: {destination}")
    json_path = target_dir / f"{name}.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    data["title"] = f"{data['title']} ({name})"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return created


def generate_video(target: Path, seconds: int, settings) -> bool:
    ffmpeg = find_ffmpeg(settings)
    if not ffmpeg:
        print("  ffmpeg nicht gefunden - es wird stattdessen das Beispiel kopiert.")
        print("  (Windows: https://ffmpeg.org/download.html installieren und erneut ausfuehren)")
        return False
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest",
        str(target),
    ]
    print(f"  erzeuge Testvideo ({seconds}s) mit ffmpeg ...")
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        print(f"  ffmpeg-Fehler: {completed.stderr.strip()[:400]}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Beispiel-Episode in READY/ anlegen")
    parser.add_argument("--name", default="example_video", help="Episode-ID / Basisname")
    parser.add_argument("--target", help="Zielordner (Standard: READY-Ordner aus config.json)")
    parser.add_argument("--generate", action="store_true", help="Testvideo mit ffmpeg erzeugen statt zu kopieren")
    parser.add_argument("--seconds", type=int, default=5, help="Laenge des erzeugten Testvideos")
    parser.add_argument("--config", help="Pfad zu config.json")
    args = parser.parse_args()

    settings = load_settings(args.config)
    target_dir = Path(args.target) if args.target else Path(settings.READY_FOLDER)
    print(f"Zielordner: {target_dir}")

    if args.generate and generate_video(target_dir / f"{args.name}.mp4", args.seconds, settings):
        target_dir.mkdir(parents=True, exist_ok=True)
        json_target = target_dir / f"{args.name}.json"
        template = json.loads((EXAMPLE_DIR / "example_video.json").read_text(encoding="utf-8"))
        template["title"] = f"Testvideo {args.name}"
        template["thumbnail"] = f"{args.name}.jpg"
        json_target.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copy2(EXAMPLE_DIR / "example_video.jpg", target_dir / f"{args.name}.jpg")
        print(f"  erstellt: {json_target}")
    else:
        copy_example(target_dir, args.name)

    print("\nFertig. Der Watcher erkennt die Dateien automatisch (bzw. 'JETZT SCANNEN' im Dashboard).")
    print("Hinweis: Der Upload bleibt privat - veroeffentlicht wird nur per Button.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
