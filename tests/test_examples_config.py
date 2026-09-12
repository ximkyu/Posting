"""Tests fuer die mitgelieferte Beispiel-Episode und die Konfiguration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from youtube_autoposter.config import CONFIG_FILE_NAME, Settings, load_settings
from youtube_autoposter.constants import (
    DESCRIPTION_MAX_LENGTH,
    ENFORCED_UPLOAD_PRIVACY,
    PRIVACY_PRIVATE,
    TAGS_TOTAL_MAX_LENGTH,
    TAG_MAX_LENGTH,
    THUMBNAIL_MAX_BYTES,
    TITLE_MAX_LENGTH,
)
from youtube_autoposter.errors import ConfigError
from youtube_autoposter.metadata.parser import parse_metadata_file
from youtube_autoposter.utils.media import probe_thumbnail

REPO = Path(__file__).resolve().parents[1]
BEISPIEL = REPO / "examples" / "ready_example"


# ===========================================================================
# Beispiel-Episode (muss fuer neue Nutzer sofort funktionieren)
# ===========================================================================


def test_beispielordner_ist_vollstaendig() -> None:
    assert BEISPIEL.is_dir(), f"Beispielordner fehlt: {BEISPIEL}"
    dateien = {p.name for p in BEISPIEL.iterdir()}
    assert {"example_video.mp4", "example_video.json", "example_video.jpg"} <= dateien


def test_beispiel_json_ist_gueltig() -> None:
    result = parse_metadata_file(BEISPIEL / "example_video.json")
    assert result.ok, result.error_messages()
    metadata = result.metadata

    assert 0 < len(metadata.title) <= TITLE_MAX_LENGTH
    assert 0 < len(metadata.description) <= DESCRIPTION_MAX_LENGTH
    assert metadata.tags
    assert all(len(tag) <= TAG_MAX_LENGTH for tag in metadata.tags)
    assert metadata.tags_total_length <= TAGS_TOTAL_MAX_LENGTH
    assert metadata.category_id.isdigit()
    assert metadata.privacy_status_requested == PRIVACY_PRIVATE
    assert metadata.effective_privacy == ENFORCED_UPLOAD_PRIVACY
    assert metadata.thumbnail == "example_video.jpg"
    assert metadata.made_for_kids is False
    assert metadata.publish_at is None


def test_beispiel_video_existiert_und_hat_groesse() -> None:
    video = BEISPIEL / "example_video.mp4"
    assert video.is_file()
    assert video.stat().st_size > 10_000
    # MP4-Signatur (ftyp)
    assert video.read_bytes()[4:8] == b"ftyp"


def test_beispiel_thumbnail_ist_gueltig() -> None:
    info = probe_thumbnail(BEISPIEL / "example_video.jpg")
    assert info.readable is True
    assert info.size_bytes <= THUMBNAIL_MAX_BYTES
    assert (info.width, info.height) == (1280, 720)


def test_beispiel_laesst_sich_in_den_ready_ordner_kopieren(project, settings) -> None:
    """Der komplette Beispiel-Durchlauf (ohne Upload) muss funktionieren."""

    import shutil

    for datei in BEISPIEL.iterdir():
        shutil.copy2(datei, settings.READY_FOLDER / datei.name)
    alt = os.path.getmtime(settings.READY_FOLDER / "example_video.mp4") - 3600
    for datei in settings.READY_FOLDER.iterdir():
        os.utime(datei, (alt, alt))

    from youtube_autoposter.core.context import bootstrap

    ctx = bootstrap(settings, run_recovery=False, console_logging=False)
    result, report = ctx.scanner.scan_and_intake()

    assert [c.episode_id for c in result.candidates] == ["example_video"]
    assert result.candidates[0].ok is True
    assert report.created == ["example_video"]

    record = ctx.repository.get_by_episode("example_video")
    assert record["status"] == "READY"
    assert record["title"]
    ctx.db.close()


# ===========================================================================
# Konfiguration
# ===========================================================================


def test_standardwerte_sind_sicher() -> None:
    cfg = Settings()
    assert cfg.HOST == "127.0.0.1"
    assert cfg.PORT == 8765
    assert cfg.DEFAULT_PRIVACY_STATUS == PRIVACY_PRIVATE
    assert cfg.ENFORCE_PRIVATE_UPLOAD is True
    assert cfg.MAX_CONCURRENT_UPLOADS == 1
    assert cfg.REQUIRE_METADATA_FILE is True
    assert cfg.VERIFY_HASH_AFTER_MOVE is True
    assert cfg.MARK_INTERRUPTED_ON_STARTUP is True
    assert list(cfg.OAUTH_SCOPES) == ["https://www.googleapis.com/auth/youtube"]
    assert cfg.dashboard_url == "http://127.0.0.1:8765"
    assert cfg.oauth_redirect_uri == "http://127.0.0.1:8765/auth/callback"


def test_public_in_config_wird_erzwungen_privat(project: Path) -> None:
    cfg_path = project / CONFIG_FILE_NAME
    cfg_path.write_text(
        json.dumps({"BASE_DIR": ".", "DEFAULT_PRIVACY_STATUS": "public", "ENFORCE_PRIVATE_UPLOAD": False}),
        encoding="utf-8",
    )
    cfg = load_settings(cfg_path, use_env=False)
    assert cfg.DEFAULT_PRIVACY_STATUS == PRIVACY_PRIVATE
    assert cfg.ENFORCE_PRIVATE_UPLOAD is True


def test_umgebungsvariablen_ueberschreiben_config(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = project / CONFIG_FILE_NAME
    cfg_path.write_text(
        json.dumps({"BASE_DIR": ".", "PORT": 8765, "GIBT_ES_NICHT": "wert"}), encoding="utf-8"
    )
    monkeypatch.setenv("YAP_PORT", "9001")
    monkeypatch.setenv("YAP_FILE_STABILITY_SECONDS", "3")
    monkeypatch.setenv("YAP_UNKNOWN_KEY", "egal")  # unbekannte Variablen sind harmlos

    cfg = load_settings(cfg_path, use_env=True)

    assert cfg.PORT == 9001
    assert cfg.FILE_STABILITY_SECONDS == 3
    assert "GIBT_ES_NICHT" in cfg.ignored_config_keys


def test_config_ist_portabel_relativ_zum_basisordner(project: Path) -> None:
    """Wichtig: Projektordner verschieben (z. B. anderes Laufwerk) darf nicht
    dazu fuehren, dass die Anwendung in einem alten Ordner arbeitet."""

    cfg = load_settings(base_dir=project, use_env=False, create_if_missing=True)
    inhalt = json.loads(cfg.config_path.read_text(encoding="utf-8"))

    assert inhalt["BASE_DIR"] == "."
    assert inhalt["READY_FOLDER"] == str(Path("folders") / "READY")
    assert inhalt["DATABASE_PATH"] == str(Path("data") / "youtube_autoposter.db")

    # Umzug in einen anderen Ordner
    neu = project.parent / "umgezogen"
    neu.mkdir(exist_ok=True)
    import shutil

    shutil.copytree(project / "folders", neu / "folders", dirs_exist_ok=True)
    shutil.copy(cfg.config_path, neu / CONFIG_FILE_NAME)

    cfg2 = load_settings(neu / CONFIG_FILE_NAME, use_env=False)
    assert cfg2.BASE_DIR == neu.resolve()
    assert cfg2.READY_FOLDER == (neu / "folders" / "READY").resolve()
    assert cfg2.DATABASE_PATH == (neu / "data" / "youtube_autoposter.db").resolve()
    assert str(project) not in str(cfg2.READY_FOLDER)


def test_kaputte_config_wird_gemeldet(project: Path) -> None:
    cfg_path = project / CONFIG_FILE_NAME
    cfg_path.write_text("{ kaputt", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg_path, use_env=False)


def test_config_muss_objekt_sein(project: Path) -> None:
    cfg_path = project / CONFIG_FILE_NAME
    cfg_path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg_path, use_env=False)


def test_zusaetzliche_videoformate_sind_opt_in(project: Path) -> None:
    cfg_path = project / CONFIG_FILE_NAME
    cfg_path.write_text(json.dumps({"BASE_DIR": "."}), encoding="utf-8")
    cfg = load_settings(cfg_path, use_env=False)
    assert list(cfg.ALLOWED_VIDEO_EXTENSIONS) == [".mp4"]

    cfg_path.write_text(json.dumps({"BASE_DIR": ".", "ALLOW_EXTRA_VIDEO_EXTENSIONS": True}), encoding="utf-8")
    cfg2 = load_settings(cfg_path, use_env=False)
    assert ".mp4" in cfg2.ALLOWED_VIDEO_EXTENSIONS
    assert ".mov" in cfg2.ALLOWED_VIDEO_EXTENSIONS


def test_speichern_und_laden_erhaelt_werte(project: Path) -> None:
    cfg = load_settings(base_dir=project, use_env=False, create_if_missing=True)
    cfg.PORT = 9123
    cfg.SCAN_INTERVAL_SECONDS = 42
    cfg.save()

    erneut = load_settings(cfg.config_path, use_env=False)
    assert erneut.PORT == 9123
    assert erneut.SCAN_INTERVAL_SECONDS == 42


def test_ordner_werden_angelegt(project: Path) -> None:
    import shutil

    cfg = load_settings(base_dir=project, use_env=False, create_if_missing=True)
    shutil.rmtree(project / "folders", ignore_errors=True)
    erstellt = cfg.ensure_directories()
    assert erstellt
    for name in ("READY", "PROCESSING", "UPLOADED_PRIVATE", "PUBLISHED", "FAILED", "SKIPPED"):
        assert (project / "folders" / name).is_dir()


def test_beschreibung_enthaelt_sicherheitsinfos(project: Path) -> None:
    cfg = load_settings(base_dir=project, use_env=False, create_if_missing=True)
    info = cfg.describe()
    assert info["DEFAULT_PRIVACY_STATUS"] == PRIVACY_PRIVATE
    assert info["ENFORCE_PRIVATE_UPLOAD"] is True
    assert info["HOST"] == "127.0.0.1"
    assert info["PORT"] == 8765
    assert "OAUTH_SCOPES" in info


# ===========================================================================
# .gitignore: keine Zugangsdaten im Repository
# ===========================================================================


def test_gitignore_schuetzt_credentials_und_daten() -> None:
    inhalt = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "credentials/" in inhalt or "credentials.json" in inhalt
    assert "token.json" in inhalt
    assert "data/" in inhalt or "*.db" in inhalt
    assert ".venv" in inhalt
    # Beispiel-Episode darf versioniert bleiben
    assert "examples/ready_example" in inhalt


def test_credentials_ordner_hat_eigene_gitignore() -> None:
    datei = REPO / "credentials" / ".gitignore"
    assert datei.exists()
    inhalt = datei.read_text(encoding="utf-8")
    assert "*" in inhalt
