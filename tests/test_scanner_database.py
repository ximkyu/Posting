"""Tests fuer die READY-Ordner-Erkennung und die Datenbank."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from youtube_autoposter.constants import VideoStatus
from youtube_autoposter.constants import QUEUEABLE_STATUSES
from youtube_autoposter.core.models import PUBLISHABLE_STATUSES, RETRYABLE_STATUSES, VideoRecord
from youtube_autoposter.database.repository import load_json
from youtube_autoposter.scanner.folder_scanner import FolderScanner


# ===========================================================================
# Scanner: Dateipaare erkennen
# ===========================================================================


def test_paar_mp4_json_wird_erkannt(ctx, make_episode) -> None:
    episode = make_episode("folge_01")
    result = ctx.scanner.discover_candidates()

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.episode_id == "folge_01"
    assert candidate.video_path == episode["video_path"]
    assert candidate.metadata_path == episode["metadata_path"]
    assert candidate.thumbnail_path == episode["thumbnail_path"]
    assert candidate.stable is True
    assert candidate.errors == []
    assert candidate.ok is True
    assert len(result.ready) == 1


def test_gross_klein_schreibung_ist_egal(ctx, project) -> None:
    ready = project / "folders" / "READY"
    video = ready / "FOLGE_02.MP4"
    meta = ready / "folge_02.json"
    video.write_bytes(b"x" * 1024)
    meta.write_text(json.dumps({"title": "T", "description": "D"}), encoding="utf-8")
    alt = time.time() - 600
    os.utime(video, (alt, alt))
    os.utime(meta, (alt, alt))

    result = ctx.scanner.discover_candidates()
    assert len(result.candidates) == 1
    assert result.candidates[0].episode_id == "FOLGE_02"
    assert result.candidates[0].metadata_path == meta
    assert result.candidates[0].errors == []


def test_zwei_episoden_werden_getrennt(ctx, make_episode) -> None:
    make_episode("folge_01")
    make_episode("folge_02")
    result = ctx.scanner.discover_candidates()
    assert [c.episode_id for c in result.candidates] == ["folge_01", "folge_02"]


def test_ohne_json_ist_fehler_wenn_verlangt(ctx, make_episode, settings) -> None:
    settings.REQUIRE_METADATA_FILE = True
    episode = make_episode("ohne_json")
    episode["metadata_path"].unlink()

    result = ctx.scanner.discover_candidates()
    candidate = result.candidates[0]
    assert candidate.errors
    assert any("Metadaten" in e for e in candidate.errors)
    assert candidate.ok is False

    # Aufnahme markiert die Episode als FEHLER
    _, report = ctx.scanner.scan_and_intake()
    assert "ohne_json" in report.failed
    record = ctx.repository.get_by_episode("ohne_json")
    assert record["status"] == VideoStatus.FAILED.value
    assert record["needs_attention"] == 1


def test_ohne_json_erlaubt_wenn_nicht_verlangt(ctx, make_episode, settings) -> None:
    settings.REQUIRE_METADATA_FILE = False
    episode = make_episode("ohne_json_ok")
    episode["metadata_path"].unlink()

    result = ctx.scanner.discover_candidates()
    assert result.candidates[0].errors == []
    assert result.candidates[0].ok is True


def test_nur_json_ohne_video_wartet(ctx, make_episode) -> None:
    episode = make_episode("nur_json")
    episode["video_path"].unlink()

    result = ctx.scanner.discover_candidates()
    candidate = result.candidates[0]
    assert candidate.video_path is None
    assert candidate.ok is False
    assert any("Videodatei fehlt" in w for w in candidate.warnings)
    # Fehlerfrei, aber unvollstaendig -> kein FAILED-Eintrag
    assert candidate.errors == []


def test_unterstuetztes_format_wird_bemängelt(ctx, project) -> None:
    ready = project / "folders" / "READY"
    video = ready / "folge_03.avi"
    meta = ready / "folge_03.json"
    video.write_bytes(b"x" * 1024)
    meta.write_text(json.dumps({"title": "T", "description": "D"}), encoding="utf-8")
    alt = time.time() - 600
    os.utime(video, (alt, alt))
    os.utime(meta, (alt, alt))

    result = ctx.scanner.discover_candidates()
    candidate = result.candidates[0]
    assert any("Nicht unterstuetztes Videoformat" in e for e in candidate.errors)
    assert candidate.ok is False


def test_temporaere_dateien_werden_ignoriert(ctx, project) -> None:
    ready = project / "folders" / "READY"
    (ready / "folge_04.mp4.part").write_bytes(b"unfertig")
    (ready / "~$folge_05.mp4").write_bytes(b"lock")
    (ready / "Thumbs.db").write_bytes(b"mist")
    (ready / "readme.txt").write_text("hinweis", encoding="utf-8")

    result = ctx.scanner.discover_candidates()
    assert result.candidates == []
    assert any("folge_04" in f for f in result.ignored_files)


def test_leere_videodatei_ist_fehler(ctx, make_episode) -> None:
    episode = make_episode("leer", video_size=0)
    episode["video_path"].write_bytes(b"")
    alt = time.time() - 600
    os.utime(episode["video_path"], (alt, alt))

    result = ctx.scanner.discover_candidates()
    assert any("leer" in e for e in result.candidates[0].errors)


def test_fremdformat_wird_abgelehnt_und_mp4_genutzt(ctx, project) -> None:
    ready = project / "folders" / "READY"
    (ready / "folge_06.mp4").write_bytes(b"a" * 2048)
    (ready / "folge_06.avi").write_bytes(b"b" * 1024)
    (ready / "folge_06.json").write_text(json.dumps({"title": "T", "description": "D"}), encoding="utf-8")
    alt = time.time() - 600
    for item in ready.iterdir():
        os.utime(item, (alt, alt))

    result = ctx.scanner.discover_candidates()
    candidate = result.candidates[0]
    assert candidate.video_path.suffix == ".mp4"
    assert any(".avi" in e for e in candidate.errors)


def test_mehrere_erlaubte_videodateien_warnen(ctx, project, settings) -> None:
    settings.ALLOW_EXTRA_VIDEO_EXTENSIONS = True
    settings.ALLOWED_VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm")
    scanner = FolderScanner(settings, ctx.repository)

    ready = project / "folders" / "READY"
    (ready / "folge_07.mp4").write_bytes(b"a" * 2048)
    (ready / "folge_07.mov").write_bytes(b"b" * 1024)
    (ready / "folge_07.json").write_text(json.dumps({"title": "T", "description": "D"}), encoding="utf-8")
    alt = time.time() - 600
    for item in ready.iterdir():
        os.utime(item, (alt, alt))

    candidate = scanner.discover_candidates().candidates[0]
    assert candidate.video_path.suffix == ".mp4"  # mp4 hat Prioritaet
    assert any("Mehrere Videodateien" in w for w in candidate.warnings)
    assert candidate.errors == []


def test_rekursiver_scan(ctx, make_episode, settings, project) -> None:
    settings.SCAN_RECURSIVE = True
    unterordner = project / "folders" / "READY" / "staffel_2"
    make_episode("folge_07", folder=unterordner)

    result = ctx.scanner.discover_candidates()
    assert [c.episode_id for c in result.candidates] == ["folge_07"]


# ---------------------------------------------------------------------------
# Stabilitaet
# ---------------------------------------------------------------------------


def test_frische_datei_wird_nicht_aufgenommen(ctx, make_episode, settings) -> None:
    settings.FILE_STABILITY_SECONDS = 600  # 10 Minuten Ruhe verlangt
    settings.STABILITY_REQUIRED_CHECKS = 2
    scanner = FolderScanner(settings, ctx.repository)

    episode = make_episode("kopiert_gerade", stale=False)
    result = scanner.discover_candidates()
    candidate = result.candidates[0]
    assert candidate.stable is False
    assert candidate.ok is False

    intake = scanner.intake_candidate(candidate)
    assert intake is None
    assert ctx.repository.get_by_episode("kopiert_gerade") is None

    # Nach dem Kopieren (Neustart der Anwendung -> neuer Scanner) wird die
    # Episode aufgenommen, weil die Datei seit langem unveraendert ist.
    alt = time.time() - 3600
    for path in (episode["video_path"], episode["metadata_path"], episode["thumbnail_path"]):
        os.utime(path, (alt, alt))
    settings.FILE_STABILITY_SECONDS = 10
    scanner2 = FolderScanner(settings, ctx.repository)
    result2 = scanner2.discover_candidates()
    assert result2.candidates[0].stable is True
    assert scanner2.intake_candidate(result2.candidates[0]) is not None


def test_wachsende_datei_bleibt_instabil(ctx, make_episode, settings) -> None:
    settings.FILE_STABILITY_SECONDS = 5
    scanner = FolderScanner(settings, ctx.repository)
    episode = make_episode("wachsend", stale=False)
    video = episode["video_path"]

    erste = scanner.discover_candidates().candidates[0]
    assert erste.stable is False

    video.write_bytes(b"x" * 5000)
    zweite = scanner.discover_candidates().candidates[0]
    assert zweite.stable is False


# ---------------------------------------------------------------------------
# Aufnahme in die Datenbank
# ---------------------------------------------------------------------------


def test_intake_legt_datensatz_an(ctx, make_episode) -> None:
    make_episode("folge_10", title="Mein Titel")
    result, report = ctx.scanner.scan_and_intake()

    assert report.created == ["folge_10"]
    record = ctx.repository.get_by_episode("folge_10")
    assert record is not None
    assert record["status"] == VideoStatus.READY.value
    assert record["folder_state"] == "READY"
    assert record["video_filename"] == "folge_10.mp4"
    assert record["metadata_filename"] == "folge_10.json"
    assert record["thumbnail_filename"] == "folge_10.jpg"
    assert record["title"] == "Mein Titel"
    assert record["needs_attention"] == 0


def test_zweiter_scan_aktualisiert_statt_vervielfaeltigt(ctx, make_episode) -> None:
    make_episode("folge_11")
    ctx.scanner.scan_and_intake()
    _, report2 = ctx.scanner.scan_and_intake()

    assert report2.created == []
    assert report2.updated == ["folge_11"]
    assert len(ctx.repository.list_videos()) == 1


def test_bereits_hochgeladene_episode_wird_blockiert(ctx, make_episode) -> None:
    make_episode("folge_12")
    ctx.scanner.scan_and_intake()
    record = ctx.repository.get_by_episode("folge_12")
    ctx.repository.set_status(int(record["id"]), VideoStatus.UPLOADED_PRIVATE.value)

    _, report = ctx.scanner.scan_and_intake()
    assert report.blocked == ["folge_12"]
    assert ctx.repository.get_by_episode("folge_12")["status"] == VideoStatus.UPLOADED_PRIVATE.value


def test_scan_bei_leerem_ordner(ctx) -> None:
    result, report = ctx.scanner.scan_and_intake()
    assert result.candidates == []
    assert report.created == []
    assert report.to_dict()["waiting"] == []


def test_scan_bei_fehlendem_ordner(ctx, settings) -> None:
    import shutil

    shutil.rmtree(settings.READY_FOLDER, ignore_errors=True)
    result = ctx.scanner.discover_candidates()
    assert result.errors
    assert "existiert nicht" in result.errors[0]


def test_zusammenfassung_ist_lesbar(ctx, make_episode) -> None:
    make_episode("folge_13")
    candidate = ctx.scanner.discover_candidates().candidates[0]
    text = candidate.summary()
    assert "folge_13" in text
    assert "folge_13.mp4" in text


# ===========================================================================
# Datenbank
# ===========================================================================


def test_schema_wird_angelegt(ctx) -> None:
    counts = ctx.db.table_counts()
    assert "videos" in counts
    assert "uploads" in counts
    assert "logs" in counts
    assert "settings" in counts


def test_upsert_und_abfrage(ctx) -> None:
    video_id, neu = ctx.repository.upsert_episode(
        "db_01", title="Titel", video_path="/tmp/db_01.mp4", status=VideoStatus.READY.value
    )
    assert neu is True
    assert video_id > 0

    row = ctx.repository.get(video_id)
    assert row["episode_id"] == "db_01"
    assert row["title"] == "Titel"
    assert row["platform"] == "youtube"

    video_id2, neu2 = ctx.repository.upsert_episode("db_01", title="Neuer Titel")
    assert neu2 is False
    assert video_id2 == video_id
    assert ctx.repository.get(video_id)["title"] == "Neuer Titel"


def test_upsert_schuetzt_hochgeladene_datensaetze(ctx) -> None:
    video_id, _ = ctx.repository.upsert_episode("db_02", status=VideoStatus.READY.value)
    ctx.repository.set_status(video_id, VideoStatus.PUBLISHED.value)

    ctx.repository.upsert_episode("db_02", status=VideoStatus.READY.value, error_message="zurueckgesetzt")
    row = ctx.repository.get(video_id)
    assert row["status"] == VideoStatus.PUBLISHED.value
    assert row["error_message"] is None


def test_statuswechsel_nur_aus_erwartetem_status(ctx) -> None:
    video_id, _ = ctx.repository.upsert_episode("db_03", status=VideoStatus.READY.value)

    # falscher Ausgangsstatus -> keine Aenderung
    assert ctx.repository.set_status(
        video_id, VideoStatus.UPLOADED_PRIVATE.value, expected=[VideoStatus.UPLOADING.value]
    ) is False
    assert ctx.repository.get(video_id)["status"] == VideoStatus.READY.value

    # passender Ausgangsstatus -> Aenderung
    assert ctx.repository.set_status(
        video_id, VideoStatus.UPLOADING.value, expected=[VideoStatus.READY.value]
    ) is True
    assert ctx.repository.get(video_id)["status"] == VideoStatus.UPLOADING.value


def test_claim_ist_atomar(ctx) -> None:
    video_id, _ = ctx.repository.upsert_episode("db_04", status=VideoStatus.READY.value)
    assert ctx.repository.claim(video_id, [VideoStatus.READY], VideoStatus.UPLOADING) is True
    # zweiter Versuch scheitert -> kein Doppelupload durch zwei Threads
    assert ctx.repository.claim(video_id, [VideoStatus.READY], VideoStatus.UPLOADING) is False


def test_queue_liefert_alteste_zuerst(ctx) -> None:
    a, _ = ctx.repository.upsert_episode("q_alt", status=VideoStatus.READY.value)
    b, _ = ctx.repository.upsert_episode("q_neu", status=VideoStatus.READY.value)
    with ctx.db.cursor() as connection:
        connection.execute(
            "UPDATE videos SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", a)
        )

    erste = ctx.repository.next_queued()
    assert erste["episode_id"] == "q_alt"

    ctx.repository.set_status(a, VideoStatus.UPLOADING.value)
    zweite = ctx.repository.next_queued()
    assert zweite["episode_id"] == "q_neu"


def test_next_queued_filtert_status(ctx) -> None:
    ctx.repository.upsert_episode("nur_failed", status=VideoStatus.FAILED.value)
    assert ctx.repository.next_queued() is None
    gefunden = ctx.repository.next_queued(statuses=[VideoStatus.FAILED])
    assert gefunden is not None
    assert gefunden["episode_id"] == "nur_failed"


def test_hashes_und_duplikatsuche(ctx) -> None:
    a, _ = ctx.repository.upsert_episode("h_01", status=VideoStatus.READY.value, file_hash="abc123")
    b, _ = ctx.repository.upsert_episode("h_02", status=VideoStatus.UPLOADED_PRIVATE.value, file_hash="abc123")

    assert [row["id"] for row in ctx.repository.find_by_hash("abc123")] == [a, b]
    assert ctx.repository.find_by_hash("abc123", exclude_id=a)[0]["id"] == b
    gefunden = ctx.repository.find_uploaded_by_hash("abc123")
    assert gefunden is not None and gefunden["episode_id"] == "h_02"
    assert ctx.repository.find_uploaded_by_hash("gibt_es_nicht") is None
    assert ctx.repository.find_uploaded_by_episode("h_01") is None
    assert ctx.repository.find_uploaded_by_episode("h_02")["id"] == b


def test_youtube_id_suche(ctx) -> None:
    video_id, _ = ctx.repository.upsert_episode("yt_01", status=VideoStatus.UPLOADED_PRIVATE.value)
    ctx.repository.update(video_id, youtube_video_id="dQw4w9WgXcQ", youtube_url="https://youtu.be/dQw4w9WgXcQ")
    assert ctx.repository.find_by_youtube_id("dQw4w9WgXcQ")["id"] == video_id
    assert ctx.repository.find_by_youtube_id("") is None


def test_statistik_zaehlt_richtig(ctx) -> None:
    ctx.repository.upsert_episode("s_ready", status=VideoStatus.READY.value, file_size=1000)
    ctx.repository.upsert_episode("s_privat", status=VideoStatus.UPLOADED_PRIVATE.value, file_size=2000)
    ctx.repository.upsert_episode("s_pub", status=VideoStatus.PUBLISHED.value, file_size=3000)
    ctx.repository.upsert_episode("s_fail", status=VideoStatus.FAILED.value, file_size=4000)
    ctx.repository.upsert_episode("s_dup", status=VideoStatus.SKIPPED_DUPLICATE.value, file_size=5000)

    stats = ctx.repository.stats()
    assert stats["total"] == 5
    assert stats["bytes_total"] == 15000
    assert stats["ready"] == 1
    assert stats["private"] == 1
    assert stats["published"] == 1
    assert stats["failed"] == 1
    assert stats["skipped"] == 1
    counts = ctx.repository.count_by_status()
    assert counts[VideoStatus.READY.value] == 1


def test_versuche_werden_protokolliert(ctx) -> None:
    video_id, _ = ctx.repository.upsert_episode("v_01", status=VideoStatus.READY.value)
    first = ctx.repository.start_attempt(video_id, "UPLOAD")
    ctx.repository.finish_attempt(first, status="failed", error="Netzwerk weg", http_status=503)
    second = ctx.repository.start_attempt(video_id, "UPLOAD")
    assert second != first
    ctx.repository.finish_attempt(second, status="ok", youtube_video_id="ytid", bytes_sent=1234, quota_units=1)

    attempts = ctx.repository.list_attempts(video_id)
    assert len(attempts) == 2
    assert attempts[0]["phase"] == "UPLOAD"
    # neuester Versuch zuerst
    assert attempts[0]["attempt"] == 2
    assert attempts[0]["youtube_video_id"] == "ytid"
    assert attempts[1]["attempt"] == 1
    assert attempts[1]["status"] == "failed"
    # retry_count wird von der Pipeline gepflegt, nicht von der Ablage
    assert ctx.repository.get(video_id)["retry_count"] == 0


def test_einstellungen_persistieren(ctx) -> None:
    ctx.repository.set_setting("worker.paused", True)
    assert ctx.repository.get_bool_setting("worker.paused") is True
    ctx.repository.set_setting("scanner.last_scan_at", "2026-09-12T10:00:00+00:00")
    assert ctx.repository.get_setting("scanner.last_scan_at") == "2026-09-12T10:00:00+00:00"
    assert ctx.repository.get_setting("unbekannt", "standard") == "standard"
    assert "worker.paused" in ctx.repository.all_settings()
    ctx.repository.delete_setting("worker.paused")
    assert ctx.repository.get_bool_setting("worker.paused") is False


def test_logs_werden_geschrieben_und_gefiltert(ctx, settings) -> None:
    settings.LOG_TO_DATABASE = True
    for i in range(30):
        ctx.repository.add_log("INFO" if i % 2 else "ERROR", f"Meldung {i}", episode_id="log_01", action="TEST")
    logs = ctx.repository.list_logs(limit=10)
    assert len(logs) == 10
    assert logs[0]["message"].startswith("Meldung")
    assert len(ctx.repository.list_logs(level="ERROR", limit=100)) == 15
    assert len(ctx.repository.list_logs(episode_id="log_01", limit=100)) == 30
    assert ctx.repository.list_logs(episode_id="anderes", limit=100) == []


def test_logs_werden_begrenzt(ctx, settings) -> None:
    settings.LOG_TO_DATABASE = True
    for i in range(1100):
        ctx.repository.add_log("INFO", f"Zeile {i}")
    # unter der Mindestmenge passiert nichts
    assert ctx.repository.prune_logs(keep_rows=5000) == 0
    entfernt = ctx.repository.prune_logs(keep_rows=1000)
    assert entfernt == 100
    assert len(ctx.repository.list_logs(limit=5000)) == 1000
    # die aeltesten Zeilen fehlen, die neuesten bleiben
    assert "Zeile 1099" in ctx.repository.list_logs(limit=1)[0]["message"]


def test_unterbrochene_uploads_werden_markiert(ctx) -> None:
    a, _ = ctx.repository.upsert_episode("r_uploading", status=VideoStatus.UPLOADING.value)
    b, _ = ctx.repository.upsert_episode("r_publishing", status=VideoStatus.PUBLISHING.value)
    c, _ = ctx.repository.upsert_episode("r_validating", status=VideoStatus.VALIDATING.value)
    d, _ = ctx.repository.upsert_episode("r_ready", status=VideoStatus.READY.value)
    e, _ = ctx.repository.upsert_episode("r_privat", status=VideoStatus.UPLOADED_PRIVATE.value)

    betroffen = ctx.repository.reset_running_to_interrupted()
    ids = {row["id"] for row in betroffen}
    assert {a, b, c} <= ids
    assert d not in ids and e not in ids

    # Upload lief -> UNTERBROCHEN (kein automatischer Re-Upload!)
    assert ctx.repository.get(a)["status"] == VideoStatus.INTERRUPTED.value
    assert ctx.repository.get(a)["needs_attention"] == 1
    # Veroeffentlichen lief -> zurueck auf PRIVAT (idempotent)
    assert ctx.repository.get(b)["status"] == VideoStatus.UPLOADED_PRIVATE.value
    # Pruefung lief (noch nichts hochgeladen) -> wieder eingereiht
    assert ctx.repository.get(c)["status"] == VideoStatus.READY.value
    assert ctx.repository.get(d)["status"] == VideoStatus.READY.value
    assert ctx.repository.get(e)["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert [row["episode_id"] for row in ctx.repository.interrupted()] == ["r_uploading"]


def test_list_videos_filter_und_suche(ctx) -> None:
    ctx.repository.upsert_episode("l_01", status=VideoStatus.READY.value, title="Alpha")
    ctx.repository.upsert_episode("l_02", status=VideoStatus.FAILED.value, title="Beta", needs_attention=1)
    ctx.repository.upsert_episode("l_03", status=VideoStatus.PUBLISHED.value, title="Gamma")

    assert len(ctx.repository.list_videos()) == 3
    assert [r["episode_id"] for r in ctx.repository.list_videos(statuses=[VideoStatus.FAILED])] == ["l_02"]
    assert [r["episode_id"] for r in ctx.repository.list_videos(needs_attention=True)] == ["l_02"]
    assert [r["episode_id"] for r in ctx.repository.list_videos(search="alph")] == ["l_01"]
    assert len(ctx.repository.list_videos(limit=2)) == 2
    assert ctx.repository.list_videos(offset=2)[0]["episode_id"]


def test_load_json_helfer() -> None:
    assert load_json('["a", "b"]') == ["a", "b"]
    assert load_json('{"x": 1}') == {"x": 1}
    assert load_json(["bereits", "liste"]) == ["bereits", "liste"]
    assert load_json(None, default=[]) == []
    assert load_json("kaputt", default="fallback") == "fallback"


def test_video_record_aus_zeile(ctx) -> None:
    video_id, _ = ctx.repository.upsert_episode(
        "rec_01",
        status=VideoStatus.UPLOADED_PRIVATE.value,
        title="Rekord",
        youtube_video_id="yt_1",
        youtube_url="https://www.youtube.com/watch?v=yt_1",
        file_size=2048,
        duration_seconds=65.0,
        width=1280,
        height=720,
        video_codec="h264",
        audio_codec="aac",
        has_audio=True,
        tags_json=["a", "b"],
    )
    row = ctx.repository.get(video_id)
    record = VideoRecord.from_row(row)

    assert record.episode_id == "rec_01"
    assert record.status == VideoStatus.UPLOADED_PRIVATE.value
    assert record.is_uploaded is True
    assert record.is_published is False
    assert record.can_publish is True
    assert record.can_retry is False
    assert record.size_text == "2.0 KB"
    assert record.duration_text == "1:05"
    assert record.resolution_text == "1280 x 720"
    assert record.codec_text == "h264 / aac"
    assert record.display_title == "Rekord"
    assert record.youtube_watch_url == "https://www.youtube.com/watch?v=yt_1"
    assert record.tags == ["a", "b"]
    assert VideoRecord.from_row(None) is None


def _values(statuses) -> set[str]:
    return {s.value if isinstance(s, VideoStatus) else str(s) for s in statuses}


def test_statusgruppen_sind_konsistent() -> None:
    queueable = _values(QUEUEABLE_STATUSES)
    publishable = _values(PUBLISHABLE_STATUSES)
    retryable = _values(RETRYABLE_STATUSES)

    assert VideoStatus.READY.value in queueable
    assert VideoStatus.NEW.value in queueable
    assert VideoStatus.UPLOADED_PRIVATE.value in publishable
    assert VideoStatus.FAILED.value in retryable
    assert VideoStatus.INTERRUPTED.value in retryable
    # Veroeffentlichtes darf nie erneut in die Upload-Queue
    assert VideoStatus.PUBLISHED.value not in queueable
    assert VideoStatus.PUBLISHED.value not in retryable
    assert VideoStatus.UPLOADED_PRIVATE.value not in queueable


def test_datenbank_bleibt_nach_neustart_erhalten(ctx, settings) -> None:
    ctx.repository.upsert_episode("persist", status=VideoStatus.UPLOADED_PRIVATE.value)
    pfad = settings.DATABASE_PATH
    ctx.db.close()

    from youtube_autoposter.database.db import Database
    from youtube_autoposter.database.repository import VideoRepository

    db2 = Database(pfad)
    db2.initialize()
    repo2 = VideoRepository(db2)
    assert repo2.get_by_episode("persist")["status"] == VideoStatus.UPLOADED_PRIVATE.value
    db2.close()


def test_datenbank_migration_ergaenz_spalten(ctx, settings) -> None:
    """Alte Datenbank ohne neue Spalten wird beim Start ergaenzt."""

    import sqlite3

    pfad = settings.DATABASE_PATH
    ctx.db.close()
    connection = sqlite3.connect(pfad)
    connection.execute("ALTER TABLE videos DROP COLUMN channel_title")
    connection.commit()
    connection.close()

    from youtube_autoposter.database.db import Database

    db2 = Database(pfad)
    db2.initialize()
    with db2.cursor() as connection:
        spalten = {row["name"] for row in connection.execute("PRAGMA table_info(videos)").fetchall()}
    assert "channel_title" in spalten
    db2.close()
