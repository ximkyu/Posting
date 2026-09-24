"""Demo-/Beispiel-Episode und die zugehoerigen CLI-Befehle.

Anforderung: Nach dem Klonen muss das System reproduzierbar testbar sein
(``python app.py init --demo`` bzw. ``python app.py demo``), ohne dass grosse
Binaerdateien ins Repository muessen. Alle Tests laufen offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from youtube_autoposter.constants import PRIVACY_PRIVATE, VideoStatus
from youtube_autoposter.core.dry_run import run_dry_run
from youtube_autoposter.metadata.parser import parse_metadata_file
from youtube_autoposter.utils import demo_episode as demo
from youtube_autoposter.utils.demo_episode import (
    create_demo_episode,
    demo_metadata,
    generate_video_with_ffmpeg,
    synthesize_thumbnail,
    synthesize_video,
)
from youtube_autoposter.utils.validation import validate_metadata



# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _altern(pfade: list[Path]) -> None:
    """Dateien 'aeltern', damit die Stabilitaetspruefung sofort durchlaeuft."""

    vergangenheit = time.time() - 3600
    for pfad in pfade:
        os.utime(pfad, (vergangenheit, vergangenheit))


def _cli_project(project: Path, fake_ffprobe: Path) -> Path:
    from youtube_autoposter.config import load_settings

    cfg = load_settings(base_dir=project, use_env=False, create_if_missing=True)
    cfg.FFPROBE_PATH = str(fake_ffprobe)
    cfg.FILE_STABILITY_SECONDS = 1
    cfg.OPEN_BROWSER = False
    cfg.WATCH_ENABLED = False
    cfg.USE_WAITRESS = False
    cfg.save(cfg.config_path)
    return cfg.config_path


def _run(argv: list[str], config: Path) -> int:
    from youtube_autoposter.cli import main

    return main(["--config", str(config), "--quiet", *argv])


# ---------------------------------------------------------------------------
# Metadaten der Demo-Episode
# ---------------------------------------------------------------------------


def test_demo_metadaten_sind_gueltig(tmp_path: Path, settings) -> None:
    daten = demo_metadata("video_001")
    pfad = tmp_path / "video_001.json"
    pfad.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")

    ergebnis = parse_metadata_file(pfad)

    assert ergebnis.ok, [str(issue) for issue in ergebnis.errors]
    metadata = ergebnis.metadata
    assert metadata.title and len(metadata.title) <= 100
    assert metadata.description and len(metadata.description) <= 5000
    assert metadata.tags and sum(len(tag) for tag in metadata.tags) <= 500
    assert metadata.category_id == "27"
    assert metadata.language == "de"
    # Sicherheitsregel: Demo-Episode verlangt niemals Oeffentlichkeit
    assert metadata.effective_privacy == PRIVACY_PRIVATE

    pruefung = validate_metadata(metadata, settings)
    assert not pruefung.errors, [issue.message for issue in pruefung.errors]


def test_demo_metadaten_ignorieren_public_wunsch(tmp_path: Path, settings) -> None:
    daten = demo_metadata("video_002")
    daten["privacy_status"] = "public"  # darf den Upload nicht oeffentlich machen
    pfad = tmp_path / "video_002.json"
    pfad.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")

    ergebnis = parse_metadata_file(pfad)

    assert ergebnis.ok
    assert ergebnis.metadata.privacy_status_requested == "public"
    assert ergebnis.metadata.effective_privacy == PRIVACY_PRIVATE


# ---------------------------------------------------------------------------
# Dateien anlegen
# ---------------------------------------------------------------------------


def test_create_demo_episode_legt_drei_dateien_an(settings) -> None:
    pfade = create_demo_episode(settings, name="demo_01")

    assert [p.name for p in pfade] == ["demo_01.mp4", "demo_01.json", "demo_01.jpg"]
    for pfad in pfade:
        assert pfad.is_file()
        assert pfad.parent == settings.READY_FOLDER

    assert b"ftyp" in pfade[0].read_bytes()[:16]  # MP4-Signatur
    daten = json.loads(pfade[1].read_text(encoding="utf-8"))
    assert daten["episode_id"] == "demo_01"
    assert daten["privacy_status"] == "private"
    assert daten["thumbnail"] == "demo_01.jpg"

    from PIL import Image

    with Image.open(pfade[2]) as bild:
        assert bild.format == "JPEG"
        assert bild.size == (1280, 720)
    assert pfade[2].stat().st_size <= 2 * 1024 * 1024  # YouTube-Limit


def test_create_demo_episode_in_eigenen_zielordner(tmp_path: Path, settings) -> None:
    ziel = tmp_path / "eigenes_ziel"
    pfade = create_demo_episode(settings, name="demo_02", target=ziel)

    assert ziel.is_dir()
    assert all(p.parent == ziel for p in pfade)
    assert list(settings.READY_FOLDER.glob("demo_02.*")) == []


def test_zweiter_aufruf_ist_idempotent(settings) -> None:
    erste = create_demo_episode(settings, name="demo_03")
    stand = {p: (p.stat().st_size, p.stat().st_mtime) for p in erste}

    zweite = create_demo_episode(settings, name="demo_03")

    assert [str(p) for p in zweite] == [str(p) for p in erste]
    for pfad, (groesse, mtime) in stand.items():
        assert pfad.stat().st_size == groesse
        assert pfad.stat().st_mtime == mtime  # nichts ueberschrieben


def test_overwrite_ersetzt_vorhandene_dateien(settings) -> None:
    create_demo_episode(settings, name="demo_04")
    json_pfad = settings.READY_FOLDER / "demo_04.json"
    daten = json.loads(json_pfad.read_text(encoding="utf-8"))
    daten["title"] = "Manuell geaendert"
    json_pfad.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")

    create_demo_episode(settings, name="demo_04", overwrite=True)

    neu = json.loads(json_pfad.read_text(encoding="utf-8"))
    assert neu["title"] != "Manuell geaendert"
    assert "demo_04" in neu["title"]


def test_synthetischer_fallback_ohne_beispieldateien(settings, monkeypatch, tmp_path) -> None:
    """Auch ohne examples/ entsteht eine nutzbare Episode (Klon-Fall)."""

    monkeypatch.setattr(demo, "EXAMPLE_DIR", tmp_path / "gibt_es_nicht")

    pfade = create_demo_episode(settings, name="demo_05")

    assert all(p.is_file() for p in pfade)
    assert b"ftyp" in pfade[0].read_bytes()[:16]
    daten = json.loads(pfade[1].read_text(encoding="utf-8"))
    assert daten["title"] and daten["description"]

    from PIL import Image

    with Image.open(pfade[2]) as bild:
        assert bild.format == "JPEG"


def test_generate_video_ohne_ffmpeg_liefert_false(settings, tmp_path) -> None:
    settings.FFMPEG_PATH = ""
    settings.FFPROBE_PATH = ""

    assert generate_video_with_ffmpeg(tmp_path / "x.mp4", 3, settings) is False


def test_synthese_helfer(tmp_path: Path) -> None:
    video = synthesize_video(tmp_path / "video.mp4", size=2048)
    assert video.stat().st_size == 2048
    assert b"ftyp" in video.read_bytes()[:16]

    bild = synthesize_thumbnail(tmp_path / "bild.jpg", width=640, height=360)
    from PIL import Image

    with Image.open(bild) as offnen:
        assert offnen.size == (640, 360)


# ---------------------------------------------------------------------------
# Zusammenspiel mit Scanner, Pipeline und Dry-Run
# ---------------------------------------------------------------------------


def test_demo_episode_wird_erkannt_und_privat_hochgeladen(ctx, fake_platform, settings) -> None:
    pfade = create_demo_episode(settings, name="demo_06")
    _altern(pfade)

    ctx.scanner.scan_and_intake()
    record = ctx.repository.get_by_episode("demo_06")
    assert record is not None
    video_id = int(record["id"])

    outcome = ctx.pipeline.process(video_id)

    assert outcome.success is True, outcome.message
    assert outcome.status == VideoStatus.UPLOADED_PRIVATE.value
    aktualisiert = ctx.repository.get(video_id)
    assert aktualisiert["effective_privacy_status"] == PRIVACY_PRIVATE
    assert aktualisiert["youtube_video_id"]
    assert fake_platform.call_count("upload") == 1
    assert fake_platform.uploaded[0]["privacy"] == PRIVACY_PRIVATE
    # Archiv inkl. upload_result.json
    archiv = settings.UPLOADED_PRIVATE_FOLDER / "demo_06"
    assert (archiv / "demo_06.mp4").is_file()
    ergebnis = json.loads((archiv / "upload_result.json").read_text(encoding="utf-8"))
    assert ergebnis["upload_status"] == "UPLOADED_PRIVATE"
    assert ergebnis["published_at"] is None


def test_demo_episode_erscheint_im_dry_run_ohne_fehler(ctx, settings) -> None:
    pfade = create_demo_episode(settings, name="demo_07")
    _altern(pfade)

    bericht = run_dry_run(ctx)

    assert bericht.upload_attempted is False
    assert "Upload versucht: NEIN" in bericht.render()
    episoden = [ep for ep in bericht.episodes if ep.episode_id == "demo_07"]
    assert len(episoden) == 1
    assert episoden[0].errors == []
    assert episoden[0].title


# ---------------------------------------------------------------------------
# CLI: init --demo und demo
# ---------------------------------------------------------------------------


def test_cli_init_demo_legt_struktur_und_episode_an(project: Path, fake_ffprobe: Path, capsys) -> None:
    import shutil

    shutil.rmtree(project / "data", ignore_errors=True)
    config = _cli_project(project, fake_ffprobe)

    assert _run(["init", "--demo"], config) == 0

    ausgabe = capsys.readouterr().out
    assert "DEMO-EPISODE" in ausgabe
    assert "config.json" in ausgabe
    assert (project / "data" / "youtube_autoposter.db").exists()
    for endung in (".mp4", ".json", ".jpg"):
        assert (project / "folders" / "READY" / f"video_001{endung}").is_file()


def test_cli_demo_mit_eigenem_namen(project: Path, fake_ffprobe: Path, capsys) -> None:
    config = _cli_project(project, fake_ffprobe)

    assert _run(["demo", "--name", "folge_007"], config) == 0

    ausgabe = capsys.readouterr().out
    assert "folge_007" in ausgabe
    for endung in (".mp4", ".json", ".jpg"):
        assert (project / "folders" / "READY" / f"folge_007{endung}").is_file()


def test_cli_demo_wird_gescannt_und_ohne_credentials_nicht_hochgeladen(
    project: Path, fake_ffprobe: Path, fake_platform, capsys
) -> None:
    config = _cli_project(project, fake_ffprobe)
    assert _run(["init", "--demo"], config) == 0
    capsys.readouterr()

    # Dateien altern lassen, damit die Stabilitaetspruefung nicht wartet
    _altern(list((project / "folders" / "READY").glob("video_001.*")))

    assert _run(["scan"], config) == 0
    assert "video_001" in capsys.readouterr().out

    assert _run(["status"], config) == 0
    status_text = capsys.readouterr().out
    assert "video_001" in status_text
    assert "MISSING_CREDENTIALS_FILE" in status_text or "FEHLT" in status_text

    # Ohne credentials.json: kein Upload-Versuch, Episode bleibt in der Queue
    assert _run(["upload"], config) != 0
    assert fake_platform.call_count("upload") == 0


def test_tools_skript_erzeugt_episode(tmp_path: Path, project: Path, fake_ffprobe: Path) -> None:
    """tools/create_sample_episode.py delegiert an dieselbe Logik."""

    config = _cli_project(project, fake_ffprobe)
    skript = Path(__file__).resolve().parent.parent / "tools" / "create_sample_episode.py"
    assert skript.is_file()

    fertig = subprocess.run(
        [sys.executable, str(skript), "--config", str(config), "--name", "tool_01"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert fertig.returncode == 0, fertig.stdout + fertig.stderr
    for endung in (".mp4", ".json", ".jpg"):
        assert (project / "folders" / "READY" / f"tool_01{endung}").is_file()


def test_repo_beispiel_und_demo_stimmen_ueberein(settings) -> None:
    """Die committete Beispiel-Episode ist dieselbe Quelle wie die Demo."""

    assert demo.EXAMPLE_DIR.is_dir()
    assert (demo.EXAMPLE_DIR / "example_video.mp4").is_file()
    assert (demo.EXAMPLE_DIR / "example_video.json").is_file()
    assert (demo.EXAMPLE_DIR / "example_video.jpg").is_file()

    pfade = create_demo_episode(settings, name="demo_08")
    original = json.loads((demo.EXAMPLE_DIR / "example_video.json").read_text(encoding="utf-8"))
    kopie = json.loads(pfade[1].read_text(encoding="utf-8"))
    assert kopie["description"] == original["description"]
    assert kopie["episode_id"] == "demo_08"
    assert kopie["privacy_status"] == "private"
    # keine grossen Dateien: Beispiel bleibt klein
    for quelle in demo.EXAMPLE_DIR.iterdir():
        assert quelle.stat().st_size < 500_000, quelle.name


@pytest.mark.parametrize("name", ["video_001", "folge 12", "äöü-Episode"])
def test_demo_namen_werden_sicher_uebernommen(settings, name: str) -> None:
    pfade = create_demo_episode(settings, name=name)
    daten = json.loads(pfade[1].read_text(encoding="utf-8"))
    assert daten["episode_id"] == name
    assert daten["thumbnail"] == f"{name}.jpg"
