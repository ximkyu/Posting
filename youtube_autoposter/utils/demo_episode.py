"""Reproduzierbare Demo-Episode fuer den READY-Ordner.

Nach dem Klonen soll das System sofort testbar sein::

    python app.py init --demo        # Struktur + Demo-Episode in einem Schritt
    python app.py demo               # nur die Demo-Episode
    python app.py demo --name folge_007 --generate --seconds 8

Es werden **keine** grossen Binaerdateien benoetigt:

1. Liegt ``examples/ready_example/`` (im Repository enthalten, wenige 100 KB),
   wird daraus kopiert.
2. Sonst wird mit ``ffmpeg`` ein kurzes Testvideo erzeugt (falls installiert).
3. Sonst wird eine kleine Platzhalter-Datei synthetisiert (MP4-Header +
   1280x720-JPEG), die fuer Erkennung, Validierung und Dry-Run ausreicht.

Es wird dabei **niemals** etwas hochgeladen - die Dateien landen nur in
``folders/READY``. Der Upload bleibt, wie ueberall in dieser Anwendung, privat.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .media import find_ffmpeg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = PROJECT_ROOT / "examples" / "ready_example"

#: Standardname der Demo-Episode (= episode_id)
DEMO_NAME = "video_001"
DEMO_TITLE = "Die verborgene Lehre des Thot"
DEMO_DESCRIPTION = (
    "Eine Reise in die Welt der alten aegyptischen Symbolik.\n\n"
    "In dieser Episode geht es um Thot, den Gott der Schrift und der Weisheit, "
    "und um die Frage, was die hermetischen Lehren fuer uns heute bedeuten.\n\n"
    "Kapitel:\n00:00 Einleitung\n00:30 Wer war Thot?\n02:00 Die Smaragdtafel\n03:00 Fazit\n\n"
    "#Thot #Aegypten #Hermetik"
)
DEMO_TAGS = ["Thot", "Aegypten", "Hermetik", "Bewusstsein", "Mystik"]
DEMO_CATEGORY_ID = "27"
DEMO_LANGUAGE = "de"

#: MP4-Header (ftyp-Box) - genug, damit die Datei als MP4 erkannt wird
_MP4_HEADER = bytes.fromhex("0000002066747970") + b"isom" + b"\x00" * 4 + b"mp41"


def demo_metadata(name: str = DEMO_NAME) -> dict[str, Any]:
    """Metadaten der Demo-Episode (gueltig gegenueber allen YouTube-Limits)."""

    return {
        "episode_id": name,
        "title": f"{DEMO_TITLE} ({name})",
        "description": DEMO_DESCRIPTION,
        "tags": list(DEMO_TAGS),
        "category_id": DEMO_CATEGORY_ID,
        "language": DEMO_LANGUAGE,
        # Wird vom System bewusst ignoriert: Jeder Upload bleibt privat.
        "privacy_status": "private",
        "thumbnail": f"{name}.jpg",
        "publish_at": None,
        "made_for_kids": False,
        "playlist_id": None,
        "channel_identifier": None,
    }


def synthesize_video(path: Path, *, size: int = 65536) -> Path:
    """Kleine MP4-Platzhalterdatei schreiben (ohne ffmpeg)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = bytes(min(max(size - len(_MP4_HEADER), 0), 1024 * 1024))
    path.write_bytes(_MP4_HEADER + payload)
    return path


def synthesize_thumbnail(path: Path, *, width: int = 1280, height: int = 720) -> Path:
    """Thumbnail als JPEG erzeugen (1280x720, weit unter 2 MB)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - Pillow ist in requirements.txt
        raise RuntimeError(
            "Pillow fehlt - bitte 'pip install -r requirements.txt' ausfuehren "
            "(fuer das Demo-Thumbnail noetig)."
        ) from None

    image = Image.new("RGB", (width, height), (18, 22, 34))
    draw = ImageDraw.Draw(image)
    for index in range(0, height, 24):
        farbe = (40 + index // 6, 60 + index // 9, 110 + index // 12)
        draw.line([(0, index), (width, index)], fill=farbe, width=8)
    draw.rectangle([(40, height - 160), (width - 40, height - 60)], fill=(240, 240, 245))
    draw.text((60, height - 140), "YouTube AutoPoster - DEMO", fill=(20, 24, 40))
    image.save(path, "JPEG", quality=88, optimize=True)
    return path


def generate_video_with_ffmpeg(
    path: Path, seconds: int, settings: Any, *, logger: logging.Logger | None = None
) -> bool:
    """Kurzes Testvideo mit ffmpeg erzeugen. True bei Erfolg."""

    log = logger or logging.getLogger(__name__)
    ffmpeg = find_ffmpeg(settings)
    if not ffmpeg:
        log.info("ffmpeg nicht gefunden - Demo-Video wird ohne ffmpeg erzeugt.")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        log.warning("ffmpeg-Fehler: %s", (completed.stderr or "").strip()[:300])
        return False
    return True


def create_demo_episode(
    settings: Any,
    *,
    name: str = DEMO_NAME,
    target: str | Path | None = None,
    generate: bool = False,
    seconds: int = 5,
    overwrite: bool = False,
    logger: logging.Logger | None = None,
) -> list[Path]:
    """Demo-Episode (mp4 + json + jpg) im READY-Ordner anlegen.

    Bereits vorhandene Dateien werden uebersprungen (ausser ``overwrite``);
    geloescht wird nichts.
    """

    log = logger or logging.getLogger(__name__)
    ziel = Path(target) if target else Path(getattr(settings, "READY_FOLDER", PROJECT_ROOT / "folders" / "READY"))
    ziel.mkdir(parents=True, exist_ok=True)

    video = ziel / f"{name}.mp4"
    metadata = ziel / f"{name}.json"
    thumbnail = ziel / f"{name}.jpg"
    erstellt: list[Path] = []

    # ---------------- Video ----------------
    if video.exists() and not overwrite:
        log.info("Demo-Video vorhanden, uebersprungen: %s", video.name)
    elif generate and generate_video_with_ffmpeg(video, seconds, settings, logger=log):
        log.info("Demo-Video mit ffmpeg erzeugt (%ss): %s", seconds, video)
    else:
        beispiel = EXAMPLE_DIR / "example_video.mp4"
        if beispiel.is_file():
            shutil.copy2(beispiel, video)
            log.info("Beispiel-Video kopiert: %s", video)
        else:
            synthesize_video(video)
            log.info("Demo-Video synthetisiert (Platzhalter): %s", video)
    erstellt.append(video)

    # ---------------- Metadaten ----------------
    vorlage_pfad = EXAMPLE_DIR / "example_video.json"
    if metadata.exists() and not overwrite:
        log.info("Demo-Metadaten vorhanden, uebersprungen: %s", metadata.name)
    else:
        if vorlage_pfad.is_file():
            try:
                daten = json.loads(vorlage_pfad.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                daten = demo_metadata(name)
            daten.update(
                {
                    "episode_id": name,
                    "title": f"{daten.get('title') or DEMO_TITLE} ({name})",
                    "thumbnail": f"{name}.jpg",
                    "privacy_status": "private",
                }
            )
        else:
            daten = demo_metadata(name)
        metadata.write_text(json.dumps(daten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log.info("Demo-Metadaten geschrieben: %s", metadata)
    erstellt.append(metadata)

    # ---------------- Thumbnail ----------------
    if thumbnail.exists() and not overwrite:
        log.info("Demo-Thumbnail vorhanden, uebersprungen: %s", thumbnail.name)
    else:
        beispiel_bild = EXAMPLE_DIR / "example_video.jpg"
        if beispiel_bild.is_file():
            shutil.copy2(beispiel_bild, thumbnail)
        else:
            synthesize_thumbnail(thumbnail)
        log.info("Demo-Thumbnail angelegt: %s", thumbnail)
    erstellt.append(thumbnail)

    return erstellt


__all__ = [
    "DEMO_NAME",
    "EXAMPLE_DIR",
    "create_demo_episode",
    "demo_metadata",
    "generate_video_with_ffmpeg",
    "synthesize_thumbnail",
    "synthesize_video",
]
