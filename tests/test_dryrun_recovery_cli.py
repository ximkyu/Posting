"""Tests fuer Dry-Run, Wiederanlauf, Watcher/Worker und die Befehlszeile."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from youtube_autoposter.constants import PRIVACY_PRIVATE, VideoStatus
from youtube_autoposter.core.dry_run import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, run_dry_run
from youtube_autoposter.core.recovery import startup_recovery
from youtube_autoposter.errors import StateError

from tests.fakes import FakePlatform, sample_client_secrets, sample_token_payload


# ===========================================================================
# Dry-Run
# ===========================================================================


def test_dry_run_prueft_umgebung_und_episode(ctx, make_episode) -> None:
    make_episode("dry_01", title="Trockenlauf Folge")
    report = run_dry_run(ctx)

    assert report.upload_attempted is False
    namen = [check.name for check in report.checks]
    assert any("Python" in name for name in namen)
    assert any("Konfiguration" in name for name in namen)
    assert any("Ordner" in name for name in namen)
    assert any("Datenbank" in name for name in namen)
    assert any("credentials" in name.lower() or "OAuth" in name for name in namen)
    assert any("ffprobe" in name.lower() or "ffmpeg" in name.lower() for name in namen)
    assert any("Port" in name for name in namen)

    assert len(report.episodes) == 1
    episode = report.episodes[0]
    assert episode.episode_id == "dry_01"
    assert episode.ok is True
    assert episode.video  # Pfad zur Videodatei
    assert episode.metadata
    assert episode.thumbnail
    assert episode.resolution == "1920 x 1080"
    assert episode.duration_text == "29:41"
    assert episode.size_text

    text = report.render()
    assert "dry_01" in text
    assert "Trockenlauf Folge" in text
    assert report.ok is False  # credentials.json fehlt in diesem Test


def test_dry_run_meldet_kaputte_json(ctx, make_episode) -> None:
    make_episode("dry_02", raw_json=json.dumps({"title": "ohne Beschreibung"}))
    report = run_dry_run(ctx)

    episode = report.episodes[0]
    assert episode.ok is False
    assert episode.errors
    assert any("description" in fehler for fehler in episode.errors)
    assert report.ok is False
    assert report.errors


def test_dry_run_meldet_fehlendes_video(ctx, make_episode) -> None:
    episode = make_episode("dry_03")
    episode["video_path"].unlink()
    report = run_dry_run(ctx)

    check = report.episodes[0]
    assert check.ok is False
    assert not check.video
    assert "Keine Videodatei" in check.message or check.errors


def test_dry_run_meldet_fehlende_credentials_und_token(ctx, credentials_file, project) -> None:
    report = run_dry_run(ctx)
    auth_check = next(c for c in report.checks if "OAuth" in c.name or "Verbindung" in c.name or "Token" in c.name)
    assert auth_check.level in (LEVEL_WARN, LEVEL_ERROR)

    # Mit Token ist die Verbindung vorhanden
    token = project / "credentials" / "token.json"
    token.write_text(json.dumps(sample_token_payload()), encoding="utf-8")
    token.chmod(0o600)
    report2 = run_dry_run(ctx)
    auth_check2 = next(c for c in report2.checks if "OAuth" in c.name or "Verbindung" in c.name or "Token" in c.name)
    assert auth_check2.level == LEVEL_OK


def test_dry_run_macht_keine_api_aufrufe(ctx, make_episode) -> None:
    adapter = FakePlatform()
    ctx.registry.register(adapter, default=True)
    make_episode("dry_04")

    run_dry_run(ctx)

    assert adapter.call_count("upload") == 0
    assert adapter.call_count("publish") == 0
    assert adapter.call_count("fetch_status") == 0


def test_dry_run_laesst_dateien_in_ruecksicht(ctx, make_episode, settings) -> None:
    episode = make_episode("dry_05")
    run_dry_run(ctx)

    # nichts verschoben, nichts geaendert
    assert (settings.READY_FOLDER / episode["video_path"].name).exists()
    assert not list(settings.PROCESSING_FOLDER.iterdir())
    assert not list(settings.UPLOADED_PRIVATE_FOLDER.iterdir())
    assert ctx.repository.get_by_episode("dry_05") is None  # keine DB-Aenderung


def test_dry_run_erkennt_instabile_datei(ctx, make_episode, settings) -> None:
    settings.FILE_STABILITY_SECONDS = 3600
    make_episode("dry_06", stale=False)
    report = run_dry_run(ctx)
    check = report.episodes[0]
    assert check.stable is False


# ===========================================================================
# Wiederanlauf (Neustart ohne Doppel-Upload)
# ===========================================================================


def test_recovery_markiert_unterbrochene_uploads(ctx, make_episode) -> None:
    make_episode("rec_01")
    ctx.scanner.scan_and_intake()
    video_id = int(ctx.repository.get_by_episode("rec_01")["id"])
    ctx.repository.set_status(video_id, VideoStatus.UPLOADING.value)

    report = startup_recovery(ctx)

    assert "rec_01" in report.interrupted
    assert report.has_findings is True
    record = ctx.repository.get(video_id)
    assert record["status"] == VideoStatus.INTERRUPTED.value
    assert record["needs_attention"] == 1
    assert "kein automatischer" in (record["error_message"] or "").lower() or "Re-Upload" in record["error_message"]
    # und es wird NICHT automatisch erneut hochgeladen
    assert video_id not in [int(r["id"]) for r in ctx.repository.list_videos(statuses=[VideoStatus.READY])]


def test_recovery_setzt_unterbrochenes_veroeffentlichen_zurueck(ctx) -> None:
    video_id, _ = ctx.repository.upsert_episode(
        "rec_02", status=VideoStatus.PUBLISHING.value, youtube_video_id="ytid"
    )
    report = startup_recovery(ctx)

    assert "rec_02" in report.republished_pending
    record = ctx.repository.get(video_id)
    assert record["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert record["published_at"] is None


def test_recovery_meldet_fehlende_dateien(ctx, make_episode, settings) -> None:
    episode = make_episode("rec_03")
    ctx.scanner.scan_and_intake()
    video_id = int(ctx.repository.get_by_episode("rec_03")["id"])
    Path(episode["video_path"]).unlink()

    report = startup_recovery(ctx)

    assert "rec_03" in report.missing_files
    assert ctx.repository.get(video_id)["status"] == VideoStatus.FAILED.value


def test_recovery_meldet_verwaiste_dateien_ohne_sie_zu_loeschen(ctx, settings) -> None:
    waise = settings.PROCESSING_FOLDER / "waise.mp4"
    waise.write_bytes(b"inhalt")

    report = startup_recovery(ctx)

    assert str(waise) in report.orphans
    assert waise.exists()  # niemals geloescht
    assert any("nicht geloescht" in m for m in report.messages)


def test_recovery_haltet_bekannte_dateien_in_processing_nicht_fuer_verwaist(
    ctx, make_episode, settings
) -> None:
    """Video, JSON und Thumbnail einer unterbrochenen Episode gehoeren zusammen."""

    import shutil

    episode = make_episode("rec_07")
    ctx.scanner.scan_and_intake()
    video_id = int(ctx.repository.get_by_episode("rec_07")["id"])
    ctx.repository.set_status(video_id, VideoStatus.UPLOADING.value)

    pfade: dict[str, str] = {}
    for schluessel in ("video_path", "metadata_path", "thumbnail_path"):
        quelle = Path(episode[schluessel])
        ziel = settings.PROCESSING_FOLDER / quelle.name
        shutil.move(str(quelle), str(ziel))
        pfade[schluessel] = str(ziel)
    ctx.repository.update(video_id, **pfade)

    report = startup_recovery(ctx)

    assert report.orphans == []
    assert ctx.repository.get(video_id)["status"] == VideoStatus.INTERRUPTED.value
    for pfad in pfade.values():
        assert Path(pfad).exists()  # nichts geloescht


def test_recovery_reiht_bereite_episoden_wieder_ein(ctx, make_episode) -> None:
    make_episode("rec_04")
    ctx.scanner.scan_and_intake()

    report = startup_recovery(ctx)
    assert "rec_04" in report.requeued
    assert ctx.repository.get_by_episode("rec_04")["status"] == VideoStatus.READY.value


def test_recovery_laessst_hochgeladene_in_ruhe(ctx) -> None:
    ctx.repository.upsert_episode("rec_05", status=VideoStatus.UPLOADED_PRIVATE.value)
    ctx.repository.upsert_episode("rec_06", status=VideoStatus.PUBLISHED.value)

    report = startup_recovery(ctx)

    assert "rec_05" not in report.interrupted
    assert ctx.repository.get_by_episode("rec_05")["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert ctx.repository.get_by_episode("rec_06")["status"] == VideoStatus.PUBLISHED.value


def test_recovery_kann_deaktiviert_werden(ctx, settings) -> None:
    settings.MARK_INTERRUPTED_ON_STARTUP = False
    ctx.repository.upsert_episode("rec_07", status=VideoStatus.UPLOADING.value)

    report = startup_recovery(ctx)

    assert report.has_findings is False
    assert ctx.repository.get_by_episode("rec_07")["status"] == VideoStatus.UPLOADING.value


def test_bootstrap_fuehrt_recovery_aus(settings, make_episode) -> None:
    from youtube_autoposter.core.context import bootstrap

    make_episode("rec_08")
    context = bootstrap(settings, run_recovery=True, console_logging=False)
    context.scanner.scan_and_intake()
    video_id = int(context.repository.get_by_episode("rec_08")["id"])
    context.repository.set_status(video_id, VideoStatus.UPLOADING.value)
    context.db.close()

    # Neustart der Anwendung
    neu = bootstrap(settings, run_recovery=True, console_logging=False)
    assert neu.repository.get(video_id)["status"] == VideoStatus.INTERRUPTED.value
    assert neu.repository.get_setting("recovery.last_findings")
    neu.db.close()


# ===========================================================================
# Watcher / Worker
# ===========================================================================


def test_worker_verarbeitet_seriell_und_pause_beachtet(ctx, fake_platform, make_episode) -> None:
    from youtube_autoposter.scheduler.watcher import UploadWorker

    make_episode("work_01")
    make_episode("work_02")
    worker = UploadWorker(ctx)

    worker.set_paused(True)
    assert worker.is_paused is True
    assert worker.process_all(scan_first=True) == []
    assert fake_platform.call_count("upload") == 0

    worker.set_paused(False)
    outcomes = worker.process_all(scan_first=True)
    assert len(outcomes) == 2
    assert fake_platform.call_count("upload") == 2
    assert worker.state.success_total == 2
    assert worker.state.to_dict()["paused"] is False


def test_worker_zustaende_werden_gespeichert(ctx, fake_platform, make_episode) -> None:
    from youtube_autoposter.scheduler.watcher import UploadWorker

    make_episode("work_03")
    worker = UploadWorker(ctx)
    worker.process_all(scan_first=True)

    assert ctx.repository.get_setting("worker.last_run_at")
    assert ctx.repository.get_setting("worker.last_message")
    assert worker.state.history
    assert worker.state.history[-1]["episode_id"] == "work_03"


def test_worker_hintergrundthread_startet_und_stoppt(ctx, fake_platform, make_episode) -> None:
    from youtube_autoposter.scheduler.watcher import UploadWorker

    ctx.settings.AUTO_UPLOAD = True  # automatischer Upload im Hintergrund
    ctx.settings.WORKER_POLL_SECONDS = 1
    worker = UploadWorker(ctx)
    worker.start()
    assert worker.state.running is True

    make_episode("work_04")
    ctx.scanner.scan_and_intake()
    worker.wake()

    zeitpunkt = time.time() + 10
    while time.time() < zeitpunkt and fake_platform.call_count("upload") == 0:
        time.sleep(0.1)
    assert fake_platform.call_count("upload") == 1

    worker.stop(timeout=5)
    assert worker.state.running is False


def test_watcher_scan_now_erkennt_neue_datei(ctx, fake_platform, make_episode) -> None:
    from youtube_autoposter.scheduler.watcher import FolderWatcher

    watcher = FolderWatcher(ctx)
    bericht = watcher.scan_now()
    assert bericht["created"] == []

    make_episode("watch_01")
    bericht2 = watcher.scan_now()
    assert "watch_01" in bericht2["created"]
    assert ctx.repository.get_by_episode("watch_01") is not None
    assert watcher.state()["running"] is False


def test_watcher_wartet_auf_stabile_datei(ctx, fake_platform, make_episode, settings) -> None:
    from youtube_autoposter.scheduler.watcher import FolderWatcher

    settings.FILE_STABILITY_SECONDS = 3600
    watcher = FolderWatcher(ctx)
    make_episode("watch_02", stale=False)

    bericht = watcher.scan_now()

    assert "watch_02" in bericht["waiting"]
    assert ctx.repository.get_by_episode("watch_02") is None
    assert fake_platform.call_count("upload") == 0


def test_watcher_hintergrund_scan(ctx, fake_platform, make_episode, settings) -> None:
    from youtube_autoposter.scheduler.watcher import FolderWatcher

    settings.SCAN_INTERVAL_SECONDS = 1
    settings.USE_WATCHDOG = False
    watcher = FolderWatcher(ctx)
    watcher.start()
    try:
        assert watcher.state()["running"] is True
        make_episode("watch_03")
        zeitpunkt = time.time() + 10
        while time.time() < zeitpunkt and ctx.repository.get_by_episode("watch_03") is None:
            time.sleep(0.1)
        assert ctx.repository.get_by_episode("watch_03") is not None
    finally:
        watcher.stop(timeout=5)
    assert watcher.state()["running"] is False


# ===========================================================================
# Befehlszeile
# ===========================================================================


@pytest.fixture
def cli_project(project: Path, fake_ffprobe: Path) -> Path:
    """Befehle werden gegen ein temporaeres Projekt gefahren."""

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


def test_cli_init_legt_struktur_an(cli_project: Path, project: Path, capsys) -> None:
    # frisches Projekt ohne Daten
    import shutil

    shutil.rmtree(project / "data", ignore_errors=True)
    code = _run(["init"], cli_project)
    assert code == 0
    assert (project / "folders" / "READY").exists()
    assert (project / "data" / "youtube_autoposter.db").exists()
    ausgabe = capsys.readouterr().out
    assert "Ordner" in ausgabe
    assert "config.json" in ausgabe


def test_cli_scan_und_status(cli_project: Path, project: Path, capsys, make_episode) -> None:
    make_episode("cli_01", title="CLI Folge")
    assert _run(["scan"], cli_project) == 0
    ausgabe = capsys.readouterr().out
    assert "cli_01" in ausgabe
    assert "Neu aufgenommen" in ausgabe

    assert _run(["status"], cli_project) == 0
    status_text = capsys.readouterr().out
    assert "STATISTIK" in status_text
    assert "CLI Folge" in status_text
    assert "PRIVAT" in status_text.upper() or "private" in status_text


def test_cli_validate_zeigt_technische_daten(cli_project: Path, make_episode, capsys) -> None:
    make_episode("cli_02")
    assert _run(["scan"], cli_project) == 0
    capsys.readouterr()

    assert _run(["validate"], cli_project) == 0
    text = capsys.readouterr().out
    assert "1920 x 1080" in text
    assert "29:41" in text


def test_cli_dry_run_exit_code_1_ohne_credentials(cli_project: Path, capsys) -> None:
    code = _run(["--dry-run"], cli_project)
    assert code == 1
    text = capsys.readouterr().out
    assert "credentials.json" in text.lower() or "OAuth" in text


def test_cli_dry_run_exit_code_0_mit_verbindung(cli_project: Path, project: Path, capsys, make_episode) -> None:
    sample_client_secrets(project / "credentials" / "credentials.json")
    token = project / "credentials" / "token.json"
    token.write_text(json.dumps(sample_token_payload()), encoding="utf-8")
    token.chmod(0o600)
    make_episode("cli_03")

    code = _run(["--dry-run"], cli_project)
    text = capsys.readouterr().out

    assert code == 0
    assert "cli_03" in text
    assert "NICHTS hochgeladen" in text
    assert "Upload versucht: NEIN" in text


def test_cli_upload_ohne_verbindung_bricht_ab(cli_project: Path, capsys, make_episode) -> None:
    make_episode("cli_04")
    _run(["scan"], cli_project)
    capsys.readouterr()

    code = _run(["upload"], cli_project)
    assert code == 1
    assert "Upload nicht moeglich" in capsys.readouterr().out


def test_cli_upload_im_dry_run_laedt_nichts(cli_project: Path, capsys, make_episode) -> None:
    make_episode("cli_05")
    code = _run(["upload", "--dry-run"], cli_project)
    assert code in (0, 1)
    assert "Dry-Run" in capsys.readouterr().out


def test_cli_config_anzeigen_und_schreiben(cli_project: Path, capsys) -> None:
    assert _run(["config"], cli_project) == 0
    text = capsys.readouterr().out
    assert "DEFAULT_PRIVACY_STATUS" in text
    assert "private" in text
    assert "ENFORCE_PRIVATE_UPLOAD" in text

    assert _run(["config", "--write"], cli_project) == 0
    capsys.readouterr()


def test_cli_auth_status_und_test(cli_project: Path, capsys) -> None:
    # ohne credentials.json: Status 1 (nicht verbunden), aber kein Absturz
    assert _run(["auth", "status"], cli_project) == 1
    text = capsys.readouterr().out
    assert "NOT CONNECTED" in text
    assert "credentials.json" in text
    assert "Desktop-App" in text

    assert _run(["auth", "test"], cli_project) == 1
    assert "NOT CONNECTED" in capsys.readouterr().out


def test_cli_publish_verlangt_bestaetigung(cli_project: Path, capsys, monkeypatch) -> None:
    from youtube_autoposter.config import load_settings
    from youtube_autoposter.core.context import bootstrap

    settings = load_settings(cli_project, use_env=False)
    context = bootstrap(settings, run_recovery=False, console_logging=False)
    video_id, _ = context.repository.upsert_episode(
        "cli_06", status=VideoStatus.UPLOADED_PRIVATE.value, youtube_video_id="ytid_cli"
    )
    context.db.close()

    # Nutzer bricht ab
    monkeypatch.setattr("builtins.input", lambda *a: "nein")
    code = _run(["publish", "cli_06"], cli_project)
    text = capsys.readouterr().out
    assert code != 0
    assert "abgebrochen" in text.lower() or "nicht bestaetigt" in text.lower()

    # Ohne --yes muss die Episode-ID getippt werden
    monkeypatch.setattr("builtins.input", lambda *a: "falsche_id")
    code2 = _run(["publish", "cli_06"], cli_project)
    assert code2 != 0
    capsys.readouterr()


def test_cli_logs_und_recover(cli_project: Path, capsys, make_episode) -> None:
    make_episode("cli_09")
    assert _run(["scan"], cli_project) == 0
    capsys.readouterr()

    assert _run(["logs", "--limit", "5"], cli_project) == 0
    capsys.readouterr()

    # recover braucht eine Episode-ID
    assert _run(["recover"], cli_project) == 2
    assert "Benutzung" in capsys.readouterr().out

    from youtube_autoposter.config import load_settings
    from youtube_autoposter.core.context import bootstrap

    settings = load_settings(cli_project, use_env=False)
    context = bootstrap(settings, run_recovery=False, console_logging=False)
    video_id = int(context.repository.get_by_episode("cli_09")["id"])
    context.repository.set_status(video_id, VideoStatus.INTERRUPTED.value)
    context.db.close()

    # ohne Verbindung: sauberer Fehler statt Absturz
    assert _run(["recover", "cli_09"], cli_project) in (0, 1)
    capsys.readouterr()


def test_cli_unbekannter_befehl(cli_project: Path) -> None:
    from youtube_autoposter.cli import main

    with pytest.raises(SystemExit) as info:
        main(["--config", str(cli_project), "gibt_es_nicht"])
    assert info.value.code == 2


def test_cli_retry_ueber_episoden_id(cli_project: Path, capsys, make_episode) -> None:
    make_episode("cli_07", raw_json=json.dumps({"title": "x"}))
    _run(["scan"], cli_project)
    capsys.readouterr()
    _run(["upload"], cli_project)  # ohne Verbindung -> kein Upload
    capsys.readouterr()

    from youtube_autoposter.config import load_settings
    from youtube_autoposter.core.context import bootstrap

    settings = load_settings(cli_project, use_env=False)
    context = bootstrap(settings, run_recovery=False, console_logging=False)
    video_id = int(context.repository.get_by_episode("cli_07")["id"])
    context.repository.set_status(video_id, VideoStatus.FAILED.value)
    context.db.close()

    assert _run(["retry", "cli_07", "--yes"], cli_project) == 0
    assert "Warteschlange" in capsys.readouterr().out

    context2 = bootstrap(load_settings(cli_project, use_env=False), run_recovery=False, console_logging=False)
    assert context2.repository.get(video_id)["status"] == VideoStatus.READY.value
    context2.db.close()


def test_cli_unbekannte_episode(cli_project: Path, capsys) -> None:
    assert _run(["retry", "gibt_es_nicht"], cli_project) != 0
    assert "nicht gefunden" in capsys.readouterr().out.lower()
