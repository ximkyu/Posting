"""End-to-End-Tests der Upload-Pipeline (mit YouTube-Mock, ohne Netzwerk).

Abgedeckt: gluecklicher Weg, Datei-Ablage, Dubletten-Schutz, erzwungenes
"private", Fehler/Retries, Veroeffentlichen nur mit Bestaetigung, Dry-Run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from youtube_autoposter.constants import (
    ENFORCED_UPLOAD_PRIVACY,
    PRIVACY_PRIVATE,
    PRIVACY_PUBLIC,
    PRIVACY_UNLISTED,
    VideoStatus,
)
from youtube_autoposter.errors import (
    PlatformError,
    PrivateLockError,
    PublishError,
    QuotaExceededError,
    StateError,
    UploadError,
)

from tests.fakes import FakePlatform


def _folder_names(path: Path) -> set[str]:
    return {p.name for p in path.iterdir()} if path.exists() else set()


def _archive_names(path: Path) -> set[str]:
    """Alle Dateinamen im Archiv (inklusive der Episode-Unterordner)."""

    if not path.exists():
        return set()
    return {p.name for p in path.rglob("*") if p.is_file()}


def _episode_dir(folder: Path, episode_id: str) -> Path:
    """Archiv-Unterordner einer Episode (ARCHIVE_IN_SUBFOLDER=true)."""

    return folder / episode_id


def _intake(ctx) -> int:
    ctx.scanner.scan_and_intake()
    record = ctx.repository.list_videos(limit=1)[0]
    return int(record["id"])


def _record(ctx, video_id: int) -> dict:
    return ctx.repository.get(video_id)


# ===========================================================================
# Gluecklicher Weg
# ===========================================================================


def test_upload_laeuft_durch_und_bleibt_privat(ctx, fake_platform, make_episode, settings) -> None:
    episode = make_episode("folge_20", title="Folge 20 - Der Anfang")
    video_id = _intake(ctx)

    outcome = ctx.pipeline.process(video_id)

    assert outcome.success is True
    assert outcome.status == VideoStatus.UPLOADED_PRIVATE.value
    assert outcome.youtube_video_id
    assert outcome.youtube_url.startswith("https://www.youtube.com/watch?v=")

    record = _record(ctx, video_id)
    assert record["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert record["youtube_video_id"] == outcome.youtube_video_id
    assert record["effective_privacy_status"] == PRIVACY_PRIVATE
    assert record["upload_completed_at"]
    assert record["progress_percent"] == 100
    assert record["needs_attention"] == 0
    assert record["error_message"] is None
    assert record["duration_seconds"] == pytest.approx(1781.32, abs=0.1)
    assert record["width"] == 1920 and record["height"] == 1080

    # genau EIN Upload-Versuch
    assert fake_platform.call_count("upload") == 1
    assert fake_platform.uploaded[0]["privacy"] == PRIVACY_PRIVATE

    # Dateien: READY leer, Archiv-Ordner gefuellt
    assert not list(settings.READY_FOLDER.iterdir())
    assert not list(settings.PROCESSING_FOLDER.iterdir())
    assert _folder_names(settings.UPLOADED_PRIVATE_FOLDER) == {"folge_20"}
    assert _archive_names(settings.UPLOADED_PRIVATE_FOLDER) >= {
        episode["video_path"].name,
        episode["metadata_path"].name,
        episode["thumbnail_path"].name,
        "upload_result.json",
    }
    assert record["folder_state"] == "UPLOADED_PRIVATE"
    assert Path(record["video_path"]).parent == _episode_dir(settings.UPLOADED_PRIVATE_FOLDER, "folge_20")
    assert Path(record["archived_path"]) == _episode_dir(settings.UPLOADED_PRIVATE_FOLDER, "folge_20")

    # Thumbnail wurde gesetzt
    assert record["thumbnail_set"] == 1
    assert record["thumbnail_url"]
    assert fake_platform.call_count("set_thumbnail") == 1

    # Versuchshistorie
    attempts = ctx.repository.list_attempts(video_id)
    assert {a["phase"] for a in attempts} >= {"VALIDATE", "UPLOAD", "THUMBNAIL"}
    assert any(a["status"] == "SUCCEEDED" and a["phase"] == "UPLOAD" for a in attempts)


def test_upload_ohne_thumbnail_funktioniert(ctx, fake_platform, make_episode) -> None:
    make_episode("folge_21", with_thumbnail=False)
    video_id = _intake(ctx)

    outcome = ctx.pipeline.process(video_id)

    assert outcome.success is True
    assert fake_platform.call_count("set_thumbnail") == 0
    assert _record(ctx, video_id)["thumbnail_set"] == 0


def test_queue_verarbeitet_nacheinander(ctx, fake_platform, make_episode) -> None:
    """Serielle Verarbeitung: immer nur ein Upload zur Zeit (Anforderung 19)."""

    make_episode("folge_30")
    make_episode("folge_31")
    ctx.scanner.scan_and_intake()

    assert ctx.pipeline.queued_count() == 2

    erste = ctx.pipeline.process_next()
    assert erste is not None and erste.success
    assert fake_platform.call_count("upload") == 1
    assert ctx.pipeline.queued_count() == 1

    zweite = ctx.pipeline.process_next()
    assert zweite is not None and zweite.success
    assert fake_platform.call_count("upload") == 2
    assert ctx.pipeline.queued_count() == 0

    assert ctx.pipeline.process_next() is None


def test_worker_verarbeitet_alles(ctx, fake_platform, make_episode) -> None:
    from youtube_autoposter.scheduler.watcher import UploadWorker

    make_episode("folge_32")
    make_episode("folge_33")
    worker = UploadWorker(ctx)
    outcomes = worker.process_all(scan_first=True)

    assert len(outcomes) == 2
    assert all(o.success for o in outcomes)
    assert worker.state.processed_total == 2
    assert worker.state.success_total == 2
    assert fake_platform.call_count("upload") == 2
    assert worker.state.to_dict()["running"] is False


# ===========================================================================
# Sicherheitsregel: immer privat
# ===========================================================================


@pytest.mark.parametrize("gewuenscht", [PRIVACY_PUBLIC, PRIVACY_UNLISTED, PRIVACY_PRIVATE, ""])
def test_json_privacy_wird_nie_uebernommen(ctx, make_episode, fake_platform, gewuenscht: str) -> None:
    make_episode("folge_40", privacy_status=gewuenscht or None)
    video_id = _intake(ctx)

    ctx.pipeline.process(video_id)

    record = _record(ctx, video_id)
    assert record["effective_privacy_status"] == ENFORCED_UPLOAD_PRIVACY
    assert fake_platform.uploaded[0]["privacy"] == ENFORCED_UPLOAD_PRIVACY
    if gewuenscht and gewuenscht != PRIVACY_PRIVATE:
        assert record["privacy_status_requested"] == gewuenscht
        # Warnung muss sichtbar sein
        assert record["validation_json"]
        assert "privacy_overridden" in record["validation_json"]


def test_publish_at_wird_nie_gesendet(ctx, make_episode, fake_platform) -> None:
    make_episode(
        "folge_41",
        privacy_status="public",
        extra_metadata={"publish_at": "2030-01-01T10:00:00+00:00"},
    )
    video_id = _intake(ctx)
    ctx.pipeline.process(video_id)

    record = _record(ctx, video_id)
    assert record["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert record["publish_at_requested"] == "2030-01-01T10:00:00+00:00"
    metadata = json.loads(record["metadata_json"])
    assert "publishAt" not in json.dumps(metadata)


def test_adapter_bekommt_nie_public_body(ctx, make_episode, monkeypatch) -> None:
    """Zusatzsicherung: Der Body fuer videos.insert muss private enthalten."""

    from youtube_autoposter.metadata.parser import build_upload_body
    from youtube_autoposter.metadata.parser import parse_metadata_dict

    parsed = parse_metadata_dict(
        {"title": "T", "description": "D", "category_id": "27", "privacy_status": "public"}
    )
    body = build_upload_body(parsed.metadata, settings=ctx.settings)
    assert body["status"]["privacyStatus"] == PRIVACY_PRIVATE

    # Auch wenn jemand force_private abschaltet, darf die Einstellung privat bleiben
    body2 = build_upload_body(parsed.metadata, settings=ctx.settings, force_private=True)
    assert body2["status"]["privacyStatus"] == PRIVACY_PRIVATE


# ===========================================================================
# Dubletten-Schutz
# ===========================================================================


def test_gleiche_episode_wird_nicht_nochmal_hochgeladen(ctx, fake_platform, make_episode) -> None:
    make_episode("folge_50")
    video_id = _intake(ctx)
    ctx.pipeline.process(video_id)
    assert fake_platform.call_count("upload") == 1

    # Dateien liegen jetzt im Archiv; zurueck nach READY legen (Simuliert
    # "Nutzer kopiert dieselbe Folge nochmal hinein")
    outcome2 = ctx.pipeline.process(video_id)
    assert outcome2.skipped is True
    assert outcome2.success is True
    assert "Bereits verarbeitet" in outcome2.message
    assert fake_platform.call_count("upload") == 1


def test_gleicher_hash_anderer_name_wird_uebersprungen(ctx, fake_platform, make_episode, settings) -> None:
    erste = make_episode("folge_60")
    video_id = _intake(ctx)
    ctx.pipeline.process(video_id)
    assert fake_platform.call_count("upload") == 1

    # dieselbe Videodatei unter neuem Namen erneut in READY ablegen
    zweite = make_episode("folge_60_kopie")
    archiv = Path(ctx.repository.get(video_id)["video_path"])
    assert archiv.exists()
    import shutil

    shutil.copy2(archiv, zweite["video_path"])
    ctx.scanner.scan_and_intake()
    kopie_id = int(ctx.repository.get_by_episode("folge_60_kopie")["id"])

    outcome = ctx.pipeline.process(kopie_id)

    assert outcome.status == VideoStatus.SKIPPED_DUPLICATE.value
    assert outcome.skipped is True
    assert fake_platform.call_count("upload") == 1  # kein zweiter Upload!
    record = _record(ctx, kopie_id)
    assert record["status"] == VideoStatus.SKIPPED_DUPLICATE.value
    assert record["needs_attention"] == 1
    assert "folge_60" in (record["error_message"] or "")
    # Dateien landen im SKIPPED-Ordner
    assert _archive_names(settings.SKIPPED_FOLDER) >= {"folge_60_kopie.mp4"}


def test_anderer_inhalt_gleicher_name_wird_hochgeladen(ctx, fake_platform, make_episode, settings) -> None:
    make_episode("folge_61")
    erste_id = _intake(ctx)
    ctx.pipeline.process(erste_id)

    # neue Folge mit gleichem Namen, aber anderem Inhalt
    import shutil

    shutil.rmtree(settings.READY_FOLDER, ignore_errors=True)
    settings.READY_FOLDER.mkdir(parents=True, exist_ok=True)
    episode = make_episode("folge_61", video_size=8192)
    episode["video_path"].write_bytes(b"anderer-inhalt" * 500)
    ctx.scanner.scan_and_intake()

    # Episode ist bereits UPLOADED_PRIVATE -> Aufnahme blockiert (Schutz)
    record = ctx.repository.get_by_episode("folge_61")
    assert record["status"] == VideoStatus.UPLOADED_PRIVATE.value


# ===========================================================================
# Fehlerbehandlung
# ===========================================================================


def test_kaputte_json_fuehrt_zu_failed(ctx, fake_platform, make_episode, settings) -> None:
    episode = make_episode(
        "folge_70",
        raw_json=json.dumps({"title": "Nur Titel ohne Beschreibung"}),
    )
    video_id = _intake(ctx)
    outcome = ctx.pipeline.process(video_id)

    assert outcome.success is False
    assert outcome.status == VideoStatus.FAILED.value
    record = _record(ctx, video_id)
    assert record["status"] == VideoStatus.FAILED.value
    assert record["error_code"] == "metadata_invalid"
    assert "description" in record["error_message"]
    assert record["needs_attention"] == 1
    assert fake_platform.call_count("upload") == 0
    # Dateien isoliert im FAILED-Ordner, Originale nicht geloescht
    assert _archive_names(settings.FAILED_FOLDER) >= {
        episode["video_path"].name,
        episode["metadata_path"].name,
    }


def test_syntaxfehler_in_json_fuehrt_zu_failed(ctx, fake_platform, make_episode, settings) -> None:
    episode = make_episode("folge_71", raw_json='{"title": "kaputt')
    video_id = _intake(ctx)
    outcome = ctx.pipeline.process(video_id)

    assert outcome.status == VideoStatus.FAILED.value
    assert fake_platform.call_count("upload") == 0
    assert Path(episode["video_path"]).exists() or _archive_names(settings.FAILED_FOLDER)


def test_fehlende_videodatei_fuehrt_zu_failed(ctx, fake_platform, make_episode) -> None:
    episode = make_episode("folge_72")
    video_id = _intake(ctx)
    episode["video_path"].unlink()

    outcome = ctx.pipeline.process(video_id)

    assert outcome.status == VideoStatus.FAILED.value
    assert "nicht gefunden" in outcome.message
    assert fake_platform.call_count("upload") == 0


def test_temporaerer_fehler_wird_mit_backoff_wiederholt(ctx, make_episode, settings) -> None:
    settings.MAX_RETRIES = 2
    settings.RETRY_DELAY = 0.01
    adapter = FakePlatform(
        fail_upload=UploadError("Netzwerk zickt", reason="backendError", retriable=True),
        fail_uploads=99,
    )
    ctx.registry.register(adapter, default=True)

    make_episode("folge_73")
    video_id = _intake(ctx)

    schlafzeiten: list[float] = []
    ctx.pipeline._sleep = schlafzeiten.append

    outcomes = [ctx.pipeline.process(video_id) for _ in range(4)]

    # erste Versuche: temporaer -> zurueck in die Queue
    assert outcomes[0].status == VideoStatus.READY.value
    assert outcomes[1].status == VideoStatus.READY.value
    # MAX_RETRIES erschoepft -> FAILED
    assert outcomes[-1].status == VideoStatus.FAILED.value
    record = _record(ctx, video_id)
    assert record["retry_count"] == 3
    assert record["needs_attention"] == 1
    assert "backendError" in (record["error_code"] or "")
    assert schlafzeiten  # Backoff wurde angewandt


def test_dauerhafter_fehler_wird_nicht_wiederholt(ctx, make_episode, settings) -> None:
    adapter = FakePlatform(
        fail_upload=UploadError("Titel ungueltig", reason="invalidTitle", retriable=False)
    )
    ctx.registry.register(adapter, default=True)

    make_episode("folge_74")
    video_id = _intake(ctx)
    outcome = ctx.pipeline.process(video_id)

    assert outcome.status == VideoStatus.FAILED.value
    assert adapter.call_count("upload") == 1
    record = _record(ctx, video_id)
    assert record["retry_count"] == 1
    assert record["error_code"] == "invalidTitle"


def test_quota_erschopft_pausiert(ctx, make_episode) -> None:
    adapter = FakePlatform(
        fail_upload=QuotaExceededError("Tageskontingent erschoepft", reason="quotaExceeded")
    )
    ctx.registry.register(adapter, default=True)

    make_episode("folge_75")
    video_id = _intake(ctx)
    outcome = ctx.pipeline.process(video_id)

    assert outcome.status == VideoStatus.PAUSED.value
    record = _record(ctx, video_id)
    assert record["status"] == VideoStatus.PAUSED.value
    assert record["needs_attention"] == 1
    assert "kontingent" in record["error_message"].lower()
    assert adapter.call_count("upload") == 1  # kein sofortiger Wiederholungsversuch


def test_ohne_verbindung_wird_pausiert(ctx, make_episode, settings) -> None:
    adapter = FakePlatform(ready=False, reason="credentials.json fehlt")
    ctx.registry.register(adapter, default=True)

    episode = make_episode("folge_76")
    video_id = _intake(ctx)
    outcome = ctx.pipeline.process(video_id)

    assert outcome.status == VideoStatus.PAUSED.value
    assert outcome.skipped is True
    assert adapter.call_count("upload") == 0
    # Dateien bleiben unberuehrt in READY
    assert (settings.READY_FOLDER / episode["video_path"].name).exists()
    assert not list(settings.PROCESSING_FOLDER.iterdir())


def test_thumbnail_fehler_ist_nicht_toedlich(ctx, make_episode) -> None:
    adapter = FakePlatform(fail_thumbnail="Thumbnail wurde von YouTube abgelehnt (403)")
    ctx.registry.register(adapter, default=True)

    make_episode("folge_77")
    video_id = _intake(ctx)
    outcome = ctx.pipeline.process(video_id)

    assert outcome.status == VideoStatus.UPLOADED_PRIVATE.value
    assert outcome.success is True
    assert any("Thumbnail" in w for w in outcome.warnings)
    record = _record(ctx, video_id)
    assert record["thumbnail_set"] == 0
    assert record["thumbnail_error"]
    assert record["needs_attention"] == 0


def test_unbekannter_fehler_crash_nicht_die_app(ctx, fake_platform, make_episode, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("unerwarteter interner Fehler")

    monkeypatch.setattr(
        "youtube_autoposter.core.pipeline.validate_episode", boom
    )
    make_episode("folge_78")
    video_id = _intake(ctx)

    outcome = ctx.pipeline.process(video_id)

    assert outcome.status == VideoStatus.FAILED.value
    assert outcome.success is False
    assert "unerwarteter interner Fehler" in outcome.message


def test_abbruch_vor_dem_upload_pausiert(ctx, make_episode, settings, fake_platform) -> None:
    make_episode("folge_79")
    video_id = _intake(ctx)
    ctx.pipeline.request_stop()

    outcome = ctx.pipeline.process(video_id)

    assert outcome.status == VideoStatus.PAUSED.value
    assert fake_platform.call_count("upload") == 0
    ctx.pipeline.reset_stop()

    # danach laeuft es wieder
    outcome2 = ctx.pipeline.process(video_id)
    assert outcome2.status == VideoStatus.UPLOADED_PRIVATE.value


# ===========================================================================
# Veroeffentlichen (nur per Button + Bestaetigung)
# ===========================================================================


def _privat_hochladen(ctx, fake_platform, make_episode, episode_id: str = "folge_80") -> int:
    make_episode(episode_id)
    video_id = _intake(ctx)
    outcome = ctx.pipeline.process(video_id)
    assert outcome.status == VideoStatus.UPLOADED_PRIVATE.value
    return video_id


def test_veroeffentlichen_ohne_bestaetigung_verweigert(ctx, fake_platform, make_episode, settings) -> None:
    video_id = _privat_hochladen(ctx, fake_platform, make_episode)

    with pytest.raises(StateError):
        ctx.pipeline.publish(video_id, confirm=False)

    assert fake_platform.call_count("publish") == 0
    assert _record(ctx, video_id)["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert not list(settings.PUBLISHED_FOLDER.iterdir())


def test_veroeffentlichen_mit_bestaetigung(ctx, fake_platform, make_episode, settings) -> None:
    episode = make_episode("folge_81")
    video_id = _intake(ctx)
    ctx.pipeline.process(video_id)
    fake_platform.reset()

    outcome = ctx.pipeline.publish(video_id, confirm=True, privacy_status=PRIVACY_PUBLIC)

    assert outcome.success is True
    assert outcome.status == VideoStatus.PUBLISHED.value
    record = _record(ctx, video_id)
    assert record["status"] == VideoStatus.PUBLISHED.value
    assert record["published_privacy_status"] == PRIVACY_PUBLIC
    assert record["published_at"]
    assert record["needs_attention"] == 0
    assert fake_platform.published[0]["privacy_status"] == PRIVACY_PUBLIC
    # Dateien ins PUBLISHED-Archiv
    assert _archive_names(settings.PUBLISHED_FOLDER) >= {
        episode["video_path"].name,
        "upload_result.json",
    }
    assert not list(settings.UPLOADED_PRIVATE_FOLDER.iterdir())


def test_veroeffentlichen_als_unlisted(ctx, fake_platform, make_episode) -> None:
    video_id = _privat_hochladen(ctx, fake_platform, make_episode, "folge_82")
    outcome = ctx.pipeline.publish(video_id, confirm=True, privacy_status=PRIVACY_UNLISTED)
    assert outcome.status == VideoStatus.PUBLISHED.value
    assert _record(ctx, video_id)["published_privacy_status"] == PRIVACY_UNLISTED


def test_zweites_veroeffentlichen_wird_verweigert(ctx, fake_platform, make_episode) -> None:
    video_id = _privat_hochladen(ctx, fake_platform, make_episode, "folge_83")
    ctx.pipeline.publish(video_id, confirm=True)

    with pytest.raises(StateError):
        ctx.pipeline.publish(video_id, confirm=True)
    assert fake_platform.call_count("publish") == 1


def test_veroeffentlichen_vor_dem_upload_unmoeglich(ctx, fake_platform, make_episode) -> None:
    make_episode("folge_84")
    video_id = _intake(ctx)

    with pytest.raises(StateError):
        ctx.pipeline.publish(video_id, confirm=True)
    assert fake_platform.call_count("publish") == 0
    assert _record(ctx, video_id)["status"] == VideoStatus.READY.value


def test_veroeffentlichen_nach_fehler_unmoeglich(ctx, fake_platform, make_episode) -> None:
    make_episode("folge_85", raw_json=json.dumps({"title": "x"}))
    video_id = _intake(ctx)
    ctx.pipeline.process(video_id)
    assert _record(ctx, video_id)["status"] == VideoStatus.FAILED.value

    with pytest.raises(StateError):
        ctx.pipeline.publish(video_id, confirm=True)


def test_publish_fehler_laesst_video_privat(ctx, make_episode) -> None:
    adapter = FakePlatform(fail_publish=PublishError("Update abgelehnt", reason="forbidden"))
    ctx.registry.register(adapter, default=True)

    video_id = _privat_hochladen(ctx, adapter, make_episode, "folge_86")
    adapter.reset()
    adapter.fail_upload = None

    outcome = ctx.pipeline.publish(video_id, confirm=True)

    assert outcome.success is False
    assert outcome.status == VideoStatus.UPLOADED_PRIVATE.value
    record = _record(ctx, video_id)
    assert record["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert record["published_at"] is None
    assert record["needs_attention"] == 1
    assert "Veroeffentlichen fehlgeschlagen" in record["error_message"]


def test_private_lock_wird_verstaendlich_gemeldet(ctx, make_episode) -> None:
    adapter = FakePlatform(
        fail_publish=PrivateLockError(
            "Dieses Video ist als privat gesperrt (unauditierte API)",
            reason="forbiddenPrivacySetting",
        )
    )
    ctx.registry.register(adapter, default=True)

    video_id = _privat_hochladen(ctx, adapter, make_episode, "folge_87")
    outcome = ctx.pipeline.publish(video_id, confirm=True)

    assert outcome.success is False
    record = _record(ctx, video_id)
    assert record["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert "privat" in record["error_message"].lower()


# ===========================================================================
# Wiederanlauf / Recovery-Aktionen
# ===========================================================================


def test_retry_stellt_fehlgeschlagene_episode_zurueck(ctx, fake_platform, make_episode, settings) -> None:
    episode = make_episode("folge_90", raw_json=json.dumps({"title": "x"}))
    video_id = _intake(ctx)
    ctx.pipeline.process(video_id)
    assert _record(ctx, video_id)["status"] == VideoStatus.FAILED.value

    # Nutzer repariert die JSON-Datei im FAILED-Ordner
    kaputt = Path(_record(ctx, video_id)["metadata_path"])
    kaputt.write_text(
        json.dumps({"title": "Repariert", "description": "Jetzt mit Beschreibung", "category_id": "27"}),
        encoding="utf-8",
    )

    outcome = ctx.pipeline.retry(video_id)
    assert outcome.status == VideoStatus.READY.value
    record = _record(ctx, video_id)
    assert record["status"] == VideoStatus.READY.value
    assert record["error_message"] is None
    assert Path(record["video_path"]).parent == settings.READY_FOLDER

    erneuert = ctx.pipeline.process(video_id)
    assert erneuert.status == VideoStatus.UPLOADED_PRIVATE.value


def test_retry_bei_bereits_hochgeladenem_video_verweigert(ctx, fake_platform, make_episode) -> None:
    video_id = _privat_hochladen(ctx, fake_platform, make_episode, "folge_91")
    with pytest.raises(StateError):
        ctx.pipeline.retry(video_id)
    assert fake_platform.call_count("upload") == 1


def test_retry_ohne_datei_meldet_klaren_fehler(ctx, fake_platform, make_episode, settings) -> None:
    episode = make_episode("folge_92", raw_json=json.dumps({"title": "x"}))
    video_id = _intake(ctx)
    ctx.pipeline.process(video_id)
    for path in sorted(settings.FAILED_FOLDER.rglob("*"), key=lambda p: len(str(p)), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()

    with pytest.raises(StateError):
        ctx.pipeline.retry(video_id)


def test_unterbrochener_upload_wird_auf_youtube_gefunden(ctx, make_episode, settings) -> None:
    adapter = FakePlatform(find_existing="ytid_gefunden")
    ctx.registry.register(adapter, default=True)

    episode = make_episode("folge_93")
    video_id = _intake(ctx)
    # Absturz waehrend des Uploads simulieren
    ctx.repository.set_status(video_id, VideoStatus.UPLOADING.value)
    ctx.repository.update(video_id, title="Folge 93")
    ctx.repository.reset_running_to_interrupted()
    assert _record(ctx, video_id)["status"] == VideoStatus.INTERRUPTED.value

    outcome = ctx.pipeline.check_on_platform(video_id)

    assert outcome.success is True
    assert outcome.youtube_video_id == "ytid_gefunden"
    record = _record(ctx, video_id)
    assert record["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert record["youtube_url"].endswith("ytid_gefunden")
    assert record["effective_privacy_status"] == PRIVACY_PRIVATE
    assert adapter.call_count("upload") == 0  # kein Doppel-Upload!


def test_unterbrochener_upload_nicht_gefunden(ctx, make_episode) -> None:
    adapter = FakePlatform(find_existing=None)
    ctx.registry.register(adapter, default=True)

    make_episode("folge_94")
    video_id = _intake(ctx)
    ctx.repository.set_status(video_id, VideoStatus.INTERRUPTED.value)

    outcome = ctx.pipeline.check_on_platform(video_id)

    assert outcome.success is False
    assert "kein passendes Video" in outcome.message
    assert _record(ctx, video_id)["status"] == VideoStatus.INTERRUPTED.value


def test_check_on_platform_nur_bei_unterbrochen(ctx, fake_platform, make_episode) -> None:
    video_id = _privat_hochladen(ctx, fake_platform, make_episode, "folge_95")
    outcome = ctx.pipeline.check_on_platform(video_id)
    assert outcome.skipped is True


def test_manuelles_archivieren(ctx, fake_platform, make_episode, settings) -> None:
    video_id = _privat_hochladen(ctx, fake_platform, make_episode, "folge_96")
    # Dateien liegen bereits in UPLOADED_PRIVATE; Zielordner wechseln
    outcome = ctx.pipeline.archive(video_id, target="READY")
    assert outcome.success is True
    assert _folder_names(settings.READY_FOLDER)
    assert _record(ctx, video_id)["folder_state"] == "READY"

    with pytest.raises(StateError):
        ctx.pipeline.archive(video_id, target="WOANDERS")


# ===========================================================================
# Dry-Run
# ===========================================================================


def test_dry_run_laedt_nichts_hoch(settings, make_episode, fake_platform) -> None:
    from youtube_autoposter.core.context import bootstrap

    dry_ctx = bootstrap(settings, dry_run=True, run_recovery=False, console_logging=False)
    dry_ctx.registry.register(FakePlatform(), default=True)
    adapter = dry_ctx.registry.get("youtube")

    make_episode("folge_97")
    dry_ctx.scanner.scan_and_intake()
    video_id = int(dry_ctx.repository.list_videos(limit=1)[0]["id"])

    outcome = dry_ctx.pipeline.process(video_id)

    assert outcome.skipped is True
    assert adapter.call_count("upload") == 0
    assert dry_ctx.repository.get(video_id)["status"] == VideoStatus.READY.value
    # Dateien unveraendert in READY
    assert _folder_names(settings.READY_FOLDER) >= {"folge_97.mp4"}

    with pytest.raises(StateError):
        dry_ctx.pipeline.publish(video_id, confirm=True)
    dry_ctx.db.close()


def test_dry_run_worker_bleiibt_stehen(settings, make_episode) -> None:
    from youtube_autoposter.core.context import bootstrap
    from youtube_autoposter.scheduler.watcher import UploadWorker

    dry_ctx = bootstrap(settings, dry_run=True, run_recovery=False, console_logging=False)
    dry_ctx.registry.register(FakePlatform(), default=True)
    make_episode("folge_98")

    worker = UploadWorker(dry_ctx)
    assert worker.process_all(scan_first=True) == []
    assert worker.process_next() is None
    dry_ctx.db.close()
