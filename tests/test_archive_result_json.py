"""Archivierung (pro Episode ein Unterordner) und upload_result.json.

Deckt die Anforderungen ab:

* 19 - Dateiarchivierung: READY -> UPLOADED_PRIVATE -> PUBLISHED bzw. FAILED/SKIPPED,
  pro Episode ein eigener Unterordner, Originale werden niemals geloescht.
* 20 - Statusdatei: zusaetzlich zur SQLite-Datenbank eine JSON-Ergebnisdatei
  (``upload_result.json``) mit episode_id, youtube_video_id, youtube_url,
  upload_status, uploaded_at, published_at.
* 12 - Thumbnail-Fehler duerfen den Upload nicht kippen und muessen sauber
  dokumentiert sein (VIDEO UPLOADED / THUMBNAIL FAILED).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from youtube_autoposter.constants import PRIVACY_PUBLIC, VideoStatus
from youtube_autoposter.utils.files import safe_folder_name, sha256_file

RESULT = "upload_result.json"


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _intake(ctx) -> int:
    ctx.scanner.scan_and_intake()
    record = ctx.repository.list_videos(limit=1)[0]
    return int(record["id"])


def _record(ctx, video_id: int) -> dict[str, Any]:
    return ctx.repository.get(video_id)


def _result(folder: Path, episode_id: str) -> dict[str, Any]:
    path = folder / episode_id / RESULT
    assert path.is_file(), f"{path} fehlt"
    return json.loads(path.read_text(encoding="utf-8"))


def _all_files(root: Path) -> set[str]:
    return {p.name for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def _hochladen(ctx, fake_platform, make_episode, episode_id: str, **kwargs) -> dict[str, Any]:
    make_episode(episode_id, **kwargs)
    video_id = _intake(ctx)
    outcome = ctx.pipeline.process(video_id)
    assert outcome.status == VideoStatus.UPLOADED_PRIVATE.value, outcome.message
    return _record(ctx, video_id)


# ---------------------------------------------------------------------------
# 19/20 - Archiv-Unterordner und Ergebnisdatei nach dem Upload
# ---------------------------------------------------------------------------


def test_archiv_bekommt_unterordner_und_ergebnisdatei(ctx, fake_platform, make_episode, settings) -> None:
    record = _hochladen(ctx, fake_platform, make_episode, "folge_100", title="Folge 100 – Äöü")

    archiv = settings.UPLOADED_PRIVATE_FOLDER / "folge_100"
    assert archiv.is_dir()
    assert _all_files(settings.UPLOADED_PRIVATE_FOLDER) == {
        "folge_100.mp4",
        "folge_100.json",
        "folge_100.jpg",
        RESULT,
    }
    assert Path(record["video_path"]).parent == archiv
    assert Path(record["archived_path"]) == archiv
    assert not list(settings.READY_FOLDER.iterdir())
    assert not list(settings.PROCESSING_FOLDER.iterdir())

    ergebnis = _result(settings.UPLOADED_PRIVATE_FOLDER, "folge_100")
    assert ergebnis["episode_id"] == "folge_100"
    assert ergebnis["platform"] == "youtube"
    assert ergebnis["title"] == "Folge 100 – Äöü"  # UTF-8 bleibt lesbar
    assert ergebnis["youtube_video_id"] == record["youtube_video_id"]
    assert ergebnis["youtube_url"] == f"https://www.youtube.com/watch?v={record['youtube_video_id']}"
    assert ergebnis["upload_status"] == "UPLOADED_PRIVATE"
    assert ergebnis["uploaded_at"]
    assert ergebnis["published_at"] is None
    assert ergebnis["effective_privacy_status"] == "private"
    assert ergebnis["privacy_status_requested"] == "private"
    assert ergebnis["thumbnail"]["set"] is True
    assert ergebnis["thumbnail"]["error"] is None
    assert ergebnis["error_code"] is None
    assert ergebnis["needs_attention"] is False
    assert ergebnis["retry_count"] == 0
    assert len(ergebnis["file_hash"]) == 64
    assert ergebnis["file_size"] == record["file_size"]
    assert ergebnis["files"]["folder"] == str(archiv)
    assert Path(ergebnis["files"]["video"]).is_file()
    assert ergebnis["updated_at"]


def test_originaldateien_bleiben_nach_archivierung_erhalten(ctx, fake_platform, make_episode, settings) -> None:
    episode = make_episode("folge_101")
    hashes = {
        Path(episode["video_path"]).name: sha256_file(episode["video_path"]),
        Path(episode["metadata_path"]).name: sha256_file(episode["metadata_path"]),
        Path(episode["thumbnail_path"]).name: sha256_file(episode["thumbnail_path"]),
    }

    video_id = _intake(ctx)
    ctx.pipeline.process(video_id)
    ctx.pipeline.publish(video_id, confirm=True, privacy_status=PRIVACY_PUBLIC)

    archiv = settings.PUBLISHED_FOLDER / "folge_101"
    for name, expected in hashes.items():
        datei = archiv / name
        assert datei.is_file(), f"{name} fehlt im Archiv"
        assert sha256_file(datei) == expected  # unveraendert, nichts geloescht
    assert _all_files(settings.PUBLISHED_FOLDER) == set(hashes) | {RESULT}


def test_ergebnisdatei_wird_nach_veroeffentlichung_aktualisiert(ctx, fake_platform, make_episode, settings) -> None:
    record = _hochladen(ctx, fake_platform, make_episode, "folge_102")
    video_id = int(record["id"])

    outcome = ctx.pipeline.publish(video_id, confirm=True, privacy_status=PRIVACY_PUBLIC)
    assert outcome.status == VideoStatus.PUBLISHED.value

    ergebnis = _result(settings.PUBLISHED_FOLDER, "folge_102")
    assert ergebnis["upload_status"] == "PUBLISHED"
    assert ergebnis["published_at"]
    assert ergebnis["youtube_video_id"] == record["youtube_video_id"]
    assert ergebnis["files"]["folder"] == str(settings.PUBLISHED_FOLDER / "folge_102")

    # Altes Archiv ist vollstaendig geraeumt (keine leeren Ordner, keine alte Kopie)
    assert not list(settings.UPLOADED_PRIVATE_FOLDER.iterdir())
    assert _all_files(settings.UPLOADED_PRIVATE_FOLDER) == set()


def test_ergebnisdatei_dokumentiert_fehler(ctx, fake_platform, make_episode, settings) -> None:
    make_episode("folge_103", raw_json='{"title": "ohne beschreibung"}')
    video_id = _intake(ctx)
    outcome = ctx.pipeline.process(video_id)

    assert outcome.status == VideoStatus.FAILED.value
    ergebnis = _result(settings.FAILED_FOLDER, "folge_103")
    assert ergebnis["upload_status"] == "FAILED"
    assert ergebnis["error_code"] == "metadata_invalid"
    assert ergebnis["error_message"]
    assert ergebnis["youtube_video_id"] is None
    assert ergebnis["published_at"] is None
    assert ergebnis["needs_attention"] is True
    # Dateien sind isoliert, aber vorhanden (keine Loeschung)
    assert Path(ergebnis["files"]["video"]).is_file()
    assert Path(ergebnis["files"]["metadata"]).is_file()


def test_ergebnisdatei_bei_dublette_verweist_auf_original(ctx, fake_platform, make_episode, settings) -> None:
    erste = _hochladen(ctx, fake_platform, make_episode, "folge_104")

    # identischer Dateiinhalt unter anderem Namen
    make_episode("folge_104_kopie")
    import shutil

    shutil.copy2(erste["video_path"], settings.READY_FOLDER / "folge_104_kopie.mp4")
    ctx.scanner.scan_and_intake()
    kopie_id = int(ctx.repository.get_by_episode("folge_104_kopie")["id"])

    outcome = ctx.pipeline.process(kopie_id)
    assert outcome.status == VideoStatus.SKIPPED_DUPLICATE.value
    assert fake_platform.call_count("upload") == 1

    ergebnis = _result(settings.SKIPPED_FOLDER, "folge_104_kopie")
    assert ergebnis["upload_status"] == "SKIPPED_DUPLICATE"
    assert ergebnis["error_code"] == "duplicate"
    assert ergebnis["youtube_video_id"] == erste["youtube_video_id"]
    assert ergebnis["youtube_url"] == erste["youtube_url"]


def test_thumbnail_fehler_wird_dokumentiert_upload_bleibt_privat(ctx, fake_platform, make_episode, settings) -> None:
    fake_platform.fail_thumbnail = "403 forbidden - Konto nicht verifiziert"
    record = _hochladen(ctx, fake_platform, make_episode, "folge_105")

    assert record["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert record["thumbnail_set"] == 0
    assert record["thumbnail_error"]
    assert record["effective_privacy_status"] == "private"

    ergebnis = _result(settings.UPLOADED_PRIVATE_FOLDER, "folge_105")
    assert ergebnis["upload_status"] == "UPLOADED_PRIVATE"  # VIDEO UPLOADED
    assert ergebnis["thumbnail"]["set"] is False  # THUMBNAIL FAILED
    assert "403" in ergebnis["thumbnail"]["error"]
    assert ergebnis["youtube_video_id"]
    # Thumbnail-Fehler kippt den Upload nicht
    assert fake_platform.call_count("upload") == 1


def test_manueller_archive_befehl_nutzt_unterordner(ctx, fake_platform, make_episode, settings) -> None:
    record = _hochladen(ctx, fake_platform, make_episode, "folge_106")
    video_id = int(record["id"])

    outcome = ctx.pipeline.archive(video_id, target="FAILED")
    assert outcome.success is True

    ziel = settings.FAILED_FOLDER / "folge_106"
    assert ziel.is_dir()
    assert _all_files(settings.FAILED_FOLDER) == {
        "folge_106.mp4",
        "folge_106.json",
        "folge_106.jpg",
        RESULT,
    }
    ergebnis = json.loads((ziel / RESULT).read_text(encoding="utf-8"))
    assert ergebnis["episode_id"] == "folge_106"
    assert ergebnis["youtube_video_id"] == record["youtube_video_id"]
    assert not list(settings.UPLOADED_PRIVATE_FOLDER.iterdir())


# ---------------------------------------------------------------------------
# Konfiguration der Archivierung
# ---------------------------------------------------------------------------


def test_flaches_archiv_wenn_unterordner_abgeschaltet(ctx, fake_platform, make_episode, settings) -> None:
    settings.ARCHIVE_IN_SUBFOLDER = False
    record = _hochladen(ctx, fake_platform, make_episode, "folge_107")

    assert Path(record["video_path"]).parent == settings.UPLOADED_PRIVATE_FOLDER
    assert _all_files(settings.UPLOADED_PRIVATE_FOLDER) == {
        "folge_107.mp4",
        "folge_107.json",
        "folge_107.jpg",
        RESULT,
    }
    ergebnis = json.loads((settings.UPLOADED_PRIVATE_FOLDER / RESULT).read_text(encoding="utf-8"))
    assert ergebnis["upload_status"] == "UPLOADED_PRIVATE"


def test_ergebnisdatei_abschaltbar(ctx, fake_platform, make_episode, settings) -> None:
    settings.WRITE_RESULT_JSON = False
    _hochladen(ctx, fake_platform, make_episode, "folge_108")

    assert _all_files(settings.UPLOADED_PRIVATE_FOLDER) == {
        "folge_108.mp4",
        "folge_108.json",
        "folge_108.jpg",
    }
    assert RESULT not in _all_files(settings.UPLOADED_PRIVATE_FOLDER)


def test_eigener_dateiname_fuer_ergebnisdatei(ctx, fake_platform, make_episode, settings) -> None:
    settings.RESULT_JSON_FILENAME = "ergebnis.json"
    _hochladen(ctx, fake_platform, make_episode, "folge_109")

    ziel = settings.UPLOADED_PRIVATE_FOLDER / "folge_109" / "ergebnis.json"
    assert ziel.is_file()
    assert json.loads(ziel.read_text(encoding="utf-8"))["episode_id"] == "folge_109"


def test_unsichere_episode_id_wird_zu_sicherem_ordnernamen(ctx, fake_platform, make_episode, settings) -> None:
    make_episode("folge_110")
    video_id = _intake(ctx)
    ctx.repository.update(video_id, episode_id="folge: 110/test")

    outcome = ctx.pipeline.process(video_id)
    assert outcome.status == VideoStatus.UPLOADED_PRIVATE.value

    ordner = settings.UPLOADED_PRIVATE_FOLDER / "folge_ 110_test"
    assert ordner.is_dir()
    assert (ordner / RESULT).is_file()


def test_neue_ergebnisdatei_ersetzt_alte_kopie(ctx, fake_platform, make_episode, settings) -> None:
    make_episode("folge_111", raw_json='{"title": "kaputt"}')
    video_id = _intake(ctx)
    ctx.pipeline.process(video_id)
    assert (settings.FAILED_FOLDER / "folge_111" / RESULT).is_file()

    # JSON reparieren und erneut verarbeiten
    kaputt = Path(_record(ctx, video_id)["metadata_path"])
    kaputt.write_text(
        json.dumps({"title": "Repariert", "description": "Jetzt mit Beschreibung."}), encoding="utf-8"
    )
    assert ctx.pipeline.retry(video_id).status == VideoStatus.READY.value
    assert ctx.pipeline.process(video_id).status == VideoStatus.UPLOADED_PRIVATE.value

    ergebnis = _result(settings.UPLOADED_PRIVATE_FOLDER, "folge_111")
    assert ergebnis["upload_status"] == "UPLOADED_PRIVATE"
    assert ergebnis["error_code"] is None
    assert _all_files(settings.FAILED_FOLDER) == set()


# ---------------------------------------------------------------------------
# Ordnernamen-Sanitizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,erwartet",
    [
        ("episode_001", "episode_001"),
        ("Folge 12", "Folge 12"),
        ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
        ("..", "episode"),
        ("", "episode"),
        ("   ", "episode"),
        ("CON", "_CON"),
        ("nul.json", "_nul.json"),
        ("  folge  ", "folge"),
        ("folge.", "folge"),
    ],
)
def test_safe_folder_name(name: str, erwartet: str) -> None:
    assert safe_folder_name(name) == erwartet


def test_safe_folder_name_begrenzt_laenge() -> None:
    lang = safe_folder_name("x" * 500)
    assert lang == "x" * 80
    assert safe_folder_name("y" * 200, max_length=10) == "y" * 10


def test_safe_folder_name_fallback() -> None:
    assert safe_folder_name("", fallback="episode_7") == "episode_7"
    assert safe_folder_name("***", fallback="folge_9") == "___"
