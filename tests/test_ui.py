"""Tests fuer die lokale Web-Oberflaeche (Flask, 127.0.0.1:8765).

Schwerpunkt: Der Veroeffentlichen-Button darf nur mit ausdruecklicher
Bestaetigung wirken, der Standardfokus liegt auf ABBRECHEN, CSRF wird
geprueft, und das Dashboard verbraucht keine YouTube-Quota.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from youtube_autoposter.constants import PRIVACY_PRIVATE, PRIVACY_PUBLIC, VideoStatus

from tests.fakes import FakePlatform, sample_client_secrets, sample_token_payload


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _csrf(client) -> str:
    """CSRF-Token aus der Sitzung holen (vorher muss eine Seite geladen werden)."""

    client.get("/")
    with client.session_transaction() as session:
        return session["csrf_token"]


def _post(client, url: str, **data) -> tuple[int, dict]:
    token = data.pop("csrf_token", None) or _csrf(client)
    payload = {"csrf_token": token, **data}
    response = client.post(url, data=payload, headers={"X-Requested-With": "fetch"})
    try:
        body = response.get_json()
    except Exception:  # pragma: no cover
        body = {"raw": response.get_data(as_text=True)}
    return response.status_code, body or {}


def _privat(ctx, fake_platform, make_episode, episode_id: str = "ui_01", **kwargs) -> int:
    make_episode(episode_id, **kwargs)
    ctx.scanner.scan_and_intake()
    video_id = int(ctx.repository.get_by_episode(episode_id)["id"])
    outcome = ctx.pipeline.process(video_id)
    assert outcome.status == VideoStatus.UPLOADED_PRIVATE.value
    return video_id


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_laedt(client, ctx) -> None:
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "YouTube AutoPoster" in html
    assert "JETZT SCANNEN" in html
    assert "UPLOAD AUSF" in html  # UPLOAD AUSFUeHREN (HTML-Escaping)
    assert str(ctx.settings.READY_FOLDER) in html
    # Sicherheitshinweis im Fussbereich
    assert "privat" in html


def test_dashboard_zeigt_episode_und_veroeffentlichen_button(client, ctx, fake_platform, make_episode) -> None:
    _privat(ctx, fake_platform, make_episode, "ui_02", title="Meine private Folge")
    html = client.get("/").get_data(as_text=True)

    assert "ui_02" in html
    assert "Meine private Folge" in html
    assert "FFENTLICHEN" in html  # VER&Ouml;FFENTLICHEN
    assert 'data-publish' in html
    assert VideoStatus.UPLOADED_PRIVATE.value in html or "PRIVAT" in html.upper()


def test_sicherheitsdialog_fokus_auf_abbrechen(client) -> None:
    """Anforderung 30: Der Default-Fokus darf NICHT auf 'Veroeffentlichen' liegen."""

    html = client.get("/").get_data(as_text=True)
    assert 'id="publishModal"' in html
    assert 'id="publishCancel"' in html
    assert 'id="publishConfirm"' in html
    # ABBRECHEN steht vor VEROEFFENTLICHEN (Reihenfolge = Tab-Reihenfolge)
    assert html.index('id="publishCancel"') < html.index('id="publishConfirm"')
    assert "ABBRECHEN" in html
    # Kein autofocus auf dem Bestaetigen-Button
    confirm_tag = html[html.index('id="publishConfirm"') - 200 : html.index('id="publishConfirm"') + 200]
    assert "autofocus" not in confirm_tag


def test_javascript_setzt_fokus_auf_abbrechen(client) -> None:
    js = client.get("/static/app.js").get_data(as_text=True)
    assert "cancelButton.focus()" in js
    assert 'body.set("confirm", "yes")' in js
    assert 'body.set("expected_status"' in js


def test_dashboard_verbraucht_keine_youtube_quota(client, ctx, fake_platform, make_episode) -> None:
    _privat(ctx, fake_platform, make_episode, "ui_03")
    fake_platform.reset()

    for _ in range(3):
        client.get("/")
    client.get("/api/status")
    client.get("/queue")

    assert fake_platform.call_count("upload") == 0
    assert fake_platform.call_count("publish") == 0
    assert fake_platform.call_count("fetch_status") == 0
    assert fake_platform.call_count("find_existing_upload") == 0


def test_dashboard_filter_und_suche(client, ctx, fake_platform, make_episode) -> None:
    _privat(ctx, fake_platform, make_episode, "ui_04", title="Alpha Folge")
    make_episode("ui_05", title="Beta Folge")
    ctx.scanner.scan_and_intake()

    html_privat = client.get("/?filter=private").get_data(as_text=True)
    assert "ui_04" in html_privat
    assert "ui_05" not in html_privat

    html_ready = client.get("/?filter=ready").get_data(as_text=True)
    assert "ui_05" in html_ready

    html_suche = client.get("/?q=beta").get_data(as_text=True)
    assert "ui_05" in html_suche
    assert "ui_04" not in html_suche

    html_fehler = client.get("/?filter=failed").get_data(as_text=True)
    assert "ui_04" not in html_fehler


def test_episoden_detailseite(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_06", title="Detail Folge")
    response = client.get(f"/videos/{video_id}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "ui_06" in html
    assert "Detail Folge" in html
    assert "youtube.com/watch?v=" in html
    assert "FFENTLICHEN" in html
    assert "1920 x 1080" in html or "1920" in html


def test_detailseite_unbekannt(client) -> None:
    assert client.get("/videos/9999").status_code == 404
    response = client.get("/api/videos/9999", headers={"Accept": "application/json"})
    assert response.status_code == 404
    body = response.get_json()
    assert body["ok"] is False
    assert "Nicht gefunden" in body["message"]


# ---------------------------------------------------------------------------
# Veroeffentlichen ueber die Oberflaeche
# ---------------------------------------------------------------------------


def test_veroeffentlichen_ohne_csrf_wird_abgelehnt(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_10")

    response = client.post(
        f"/videos/{video_id}/publish",
        data={"confirm": "yes", "expected_status": VideoStatus.UPLOADED_PRIVATE.value},
        headers={"X-Requested-With": "fetch"},
    )
    body = response.get_json()

    assert response.status_code == 400
    assert body["ok"] is False
    assert "CSRF" in body["message"]
    assert fake_platform.call_count("publish") == 0
    assert ctx.repository.get(video_id)["status"] == VideoStatus.UPLOADED_PRIVATE.value


def test_veroeffentlichen_ohne_bestaetigung_wird_abgelehnt(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_11")

    status, body = _post(client, f"/videos/{video_id}/publish", confirm="no")

    assert status == 400
    assert body["ok"] is False
    assert "nicht bestaetigt" in body["message"]
    assert fake_platform.call_count("publish") == 0
    assert ctx.repository.get(video_id)["status"] == VideoStatus.UPLOADED_PRIVATE.value


def test_veroeffentlichen_mit_bestaetigung(client, ctx, fake_platform, make_episode, settings) -> None:
    episode = make_episode("ui_12")
    video_id = _privat(ctx, fake_platform, make_episode, "ui_12b", title="Bereit")
    fake_platform.reset()

    status, body = _post(
        client,
        f"/videos/{video_id}/publish",
        confirm="yes",
        expected_status=VideoStatus.UPLOADED_PRIVATE.value,
    )

    assert status == 200
    assert body["ok"] is True
    assert "youtube.com/watch?v=" in body["message"]
    record = ctx.repository.get(video_id)
    assert record["status"] == VideoStatus.PUBLISHED.value
    assert record["published_privacy_status"] == PRIVACY_PUBLIC
    assert fake_platform.published[0]["privacy_status"] == PRIVACY_PUBLIC
    assert Path(record["video_path"]).parent == settings.PUBLISHED_FOLDER / "ui_12b"


def test_veroeffentlichen_bei_geaendertem_status_abgelehnt(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_13")

    status, body = _post(
        client, f"/videos/{video_id}/publish", confirm="yes", expected_status=VideoStatus.READY.value
    )

    assert status == 400
    assert "Status hat sich geaendert" in body["message"]
    assert fake_platform.call_count("publish") == 0


def test_veroeffentlichen_vor_upload_abgelehnt(client, ctx, fake_platform, make_episode) -> None:
    make_episode("ui_14")
    ctx.scanner.scan_and_intake()
    video_id = int(ctx.repository.get_by_episode("ui_14")["id"])

    status, body = _post(
        client, f"/videos/{video_id}/publish", confirm="yes", expected_status=VideoStatus.READY.value
    )

    assert status == 400
    assert fake_platform.call_count("publish") == 0


def test_doppeltes_veroeffentlichen_abgelehnt(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_15")
    _post(client, f"/videos/{video_id}/publish", confirm="yes", expected_status=VideoStatus.UPLOADED_PRIVATE.value)

    status, body = _post(client, f"/videos/{video_id}/publish", confirm="yes")
    assert status == 400
    assert "bereits veroeffentlicht" in body["message"]
    assert fake_platform.call_count("publish") == 1


def test_veroeffentlichen_als_html_formular_leitet_weiter(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_16")
    token = _csrf(client)

    response = client.post(
        f"/videos/{video_id}/publish",
        data={
            "csrf_token": token,
            "confirm": "yes",
            "expected_status": VideoStatus.UPLOADED_PRIVATE.value,
        },
    )
    assert response.status_code == 302
    assert f"/videos/{video_id}" in response.headers["Location"]


# ---------------------------------------------------------------------------
# Buttons: Scannen, Upload, Pause, Retry
# ---------------------------------------------------------------------------


def test_button_jetzt_scannen(client, ctx, make_episode) -> None:
    make_episode("ui_20")
    status, body = _post(client, "/actions/scan")

    assert status == 200
    assert body["ok"] is True
    assert "Scan abgeschlossen" in body["message"]
    assert ctx.repository.get_by_episode("ui_20") is not None


def test_button_upload_ausfuehren_mit_worker(client, ctx, fake_platform, make_episode) -> None:
    from youtube_autoposter.scheduler.watcher import UploadWorker

    ctx.worker = UploadWorker(ctx)
    make_episode("ui_21")
    ctx.scanner.scan_and_intake()

    status, body = _post(client, "/actions/upload")

    assert status == 200
    assert body["ok"] is True
    assert "Upload-Queue gestartet" in body["message"]
    ctx.worker.stop(timeout=2)
    ctx.worker = None


def test_button_upload_ohne_worker_stellt_in_queue(client, ctx, fake_platform, make_episode) -> None:
    make_episode("ui_22")
    ctx.scanner.scan_and_intake()

    status, body = _post(client, "/actions/upload")

    assert status == 200
    assert "in die Queue gestellt" in body["message"]
    assert fake_platform.call_count("upload") == 0  # kein Upload im HTTP-Request


def test_button_pause_und_fortsetzen(client, ctx) -> None:
    status, body = _post(client, "/actions/pause", paused="1")
    assert status == 200
    assert ctx.repository.get_bool_setting("worker.paused") is True

    status2, body2 = _post(client, "/actions/pause", paused="0")
    assert status2 == 200
    assert ctx.repository.get_bool_setting("worker.paused") is False


def test_retry_button(client, ctx, fake_platform, make_episode) -> None:
    make_episode("ui_23", raw_json=json.dumps({"title": "x"}))
    ctx.scanner.scan_and_intake()
    video_id = int(ctx.repository.get_by_episode("ui_23")["id"])
    ctx.pipeline.process(video_id)
    assert ctx.repository.get(video_id)["status"] == VideoStatus.FAILED.value

    status, body = _post(client, f"/videos/{video_id}/retry")

    assert status == 200
    assert body["ok"] is True
    assert ctx.repository.get(video_id)["status"] == VideoStatus.READY.value


def test_recover_button_findet_unterbrochenen_upload(client, ctx, make_episode) -> None:
    adapter = FakePlatform(find_existing="ytid_ui")
    ctx.registry.register(adapter, default=True)

    make_episode("ui_24")
    ctx.scanner.scan_and_intake()
    video_id = int(ctx.repository.get_by_episode("ui_24")["id"])
    ctx.repository.set_status(video_id, VideoStatus.INTERRUPTED.value)

    status, body = _post(client, f"/videos/{video_id}/recover")

    assert status == 200
    assert body["ok"] is True
    record = ctx.repository.get(video_id)
    assert record["youtube_video_id"] == "ytid_ui"
    assert record["status"] == VideoStatus.UPLOADED_PRIVATE.value
    assert record["effective_privacy_status"] == PRIVACY_PRIVATE
    assert adapter.call_count("upload") == 0


def test_check_button_ohne_youtube_id(client, ctx, fake_platform, make_episode) -> None:
    make_episode("ui_24b")
    ctx.scanner.scan_and_intake()
    video_id = int(ctx.repository.get_by_episode("ui_24b")["id"])

    status, body = _post(client, f"/videos/{video_id}/check")
    assert status == 400
    assert "YouTube-Video-ID" in body["message"]
    assert fake_platform.call_count("fetch_status") == 0


def test_check_button_fragt_youtube_status_ab(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_24c")
    fake_platform.reset()

    status, body = _post(client, f"/videos/{video_id}/check")

    assert status == 200
    assert body["ok"] is True
    assert "YouTube-Status: private" in body["message"]
    assert fake_platform.call_count("fetch_status") == 1


def test_archiv_button(client, ctx, fake_platform, make_episode, settings) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_25")
    status, body = _post(client, f"/videos/{video_id}/archive", target="READY")
    assert status == 200
    assert ctx.repository.get(video_id)["folder_state"] == "READY"


def test_thumbnail_button(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_26")
    fake_platform.reset()
    status, body = _post(client, f"/videos/{video_id}/thumbnail")
    assert status == 200
    assert fake_platform.call_count("set_thumbnail") == 1


# ---------------------------------------------------------------------------
# API-Endpunkte
# ---------------------------------------------------------------------------


def test_api_status(client, ctx, fake_platform) -> None:
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.get_json()

    assert data["app"] == "YouTube AutoPoster"
    assert data["version"]
    assert data["dry_run"] is False
    assert data["auth"]["state"] == "MISSING_CREDENTIALS_FILE"
    assert data["platforms"][0]["name"] == "youtube"
    assert "stats" in data and "counts" in data["stats"]
    assert data["config"]["HOST"] == "127.0.0.1"
    assert data["config"]["PORT"] == 8765
    assert data["config"]["DEFAULT_PRIVACY_STATUS"] == PRIVACY_PRIVATE


def test_api_videos_und_einzelvideo(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_30")

    liste = client.get("/api/videos").get_json()
    assert liste["stats"]["total"] >= 1
    assert liste["videos"][0]["episode_id"] == "ui_30"
    assert liste["videos"][0]["effective_privacy_status"] == PRIVACY_PRIVATE

    einzeln = client.get(f"/api/videos/{video_id}").get_json()
    assert einzeln["episode_id"] == "ui_30"
    assert einzeln["youtube_url"]


def test_health(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] in {"ok", "warning", "error"}
    assert data["version"]


def test_thumb_liefert_datei_oder_platzhalter(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_31")
    response = client.get(f"/thumb/{video_id}")
    assert response.status_code == 200
    assert response.mimetype in {"image/jpeg", "image/png", "image/svg+xml"}

    make_episode("ui_32", with_thumbnail=False)
    ctx.scanner.scan_and_intake()
    ohne = int(ctx.repository.get_by_episode("ui_32")["id"])
    response2 = client.get(f"/thumb/{ohne}")
    assert response2.status_code == 200
    assert response2.mimetype == "image/svg+xml"
    assert "kein Vorschaubild" in response2.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Weitere Seiten
# ---------------------------------------------------------------------------


def test_setup_seite_erklaert_google_cloud(client, ctx) -> None:
    response = client.get("/setup")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Desktop-App" in html
    assert "YouTube Data API v3" in html
    assert "credentials.json" in html
    assert "OAuth" in html
    # kein Passwort wird abgefragt
    assert 'type="password"' not in html


def test_queue_und_logs_seiten(client, ctx, fake_platform, make_episode, settings) -> None:
    settings.LOG_TO_DATABASE = True
    make_episode("ui_40", title="Wartende Folge")
    ctx.scanner.scan_and_intake()
    ctx.repository.add_log("ERROR", "Testfehler", episode_id="ui_40")

    queue_response = client.get("/queue")
    assert queue_response.status_code == 200
    queue_html = queue_response.get_data(as_text=True)
    assert "ui_40" in queue_html
    assert "Wartende Folge" in queue_html

    logs = client.get("/logs")
    assert logs.status_code == 200
    assert "Testfehler" in logs.get_data(as_text=True)


def test_dry_run_kennzeichnung_und_sperren(settings, make_episode) -> None:
    from youtube_autoposter.app import create_app
    from youtube_autoposter.core.context import bootstrap

    dry_ctx = bootstrap(settings, dry_run=True, run_recovery=False, console_logging=False)
    dry_ctx.registry.register(FakePlatform(), default=True)
    make_episode("ui_50")
    dry_ctx.scanner.scan_and_intake()
    video_id = int(dry_ctx.repository.get_by_episode("ui_50")["id"])

    app = create_app(dry_ctx)
    app.config.update(TESTING=True)
    client = app.test_client()

    html = client.get("/").get_data(as_text=True)
    assert "DRY RUN" in html

    token = _csrf(client)
    response = client.post(
        f"/videos/{video_id}/publish",
        data={"csrf_token": token, "confirm": "yes"},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 400
    assert "Dry-Run" in response.get_json()["message"]

    response2 = client.post(
        "/actions/upload",
        data={"csrf_token": token},
        headers={"X-Requested-With": "fetch"},
    )
    assert response2.status_code == 400
    assert "Dry-Run" in response2.get_json()["message"]
    assert dry_ctx.registry.get("youtube").call_count("upload") == 0
    dry_ctx.db.close()


# ---------------------------------------------------------------------------
# OAuth-Seiten
# ---------------------------------------------------------------------------


def test_auth_start_ohne_credentials_zeigt_anleitung(client, ctx) -> None:
    response = client.get("/auth/start")
    assert response.status_code == 302
    assert "/setup" in response.headers["Location"]


def test_auth_start_mit_credentials_leitet_zu_google(client, ctx, credentials_file) -> None:
    response = client.get("/auth/start")
    assert response.status_code == 302
    ziel = response.headers["Location"]
    assert ziel.startswith("https://accounts.google.com/")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765" in ziel
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fyoutube" in ziel


def test_auth_callback_mit_fehler(client, ctx, credentials_file) -> None:
    response = client.get("/auth/callback?error=access_denied&state=x")
    assert response.status_code in (302, 400)


def test_auth_test_ohne_verbindung(client, ctx, credentials_file) -> None:
    status, body = _post(client, "/auth/test")
    assert status in (200, 400)
    assert body["ok"] is False or "NOT CONNECTED" in json.dumps(body)


def test_auth_disconnect_loescht_token(client, ctx, credentials_file, project) -> None:
    token_path = project / "credentials" / "token.json"
    token_path.write_text(json.dumps(sample_token_payload()), encoding="utf-8")
    token_path.chmod(0o600)
    assert ctx.auth.status().connected is True

    status, body = _post(client, "/auth/disconnect")
    assert status == 200
    assert ctx.auth.has_token() is False
    assert ctx.auth.status().state.value == "NOT CONNECTED"


def test_verbindungs_banner_im_dashboard(client, ctx, credentials_file, project) -> None:
    token_path = project / "credentials" / "token.json"
    token_path.write_text(json.dumps(sample_token_payload()), encoding="utf-8")
    token_path.chmod(0o600)
    ctx.repository.set_setting("channel.title", "Mein Kanal")

    html = client.get("/").get_data(as_text=True)
    assert "CONNECTED" in html
    assert "Mein Kanal" in html


# ---------------------------------------------------------------------------
# 15 - Idempotenz des Veroeffentlichens (Doppelklick, laufender API-Aufruf)
# ---------------------------------------------------------------------------


def test_veroeffentlichen_waehrend_publishing_abgelehnt(client, ctx, fake_platform, make_episode) -> None:
    """Waehrend PUBLISHING darf kein zweiter API-Aufruf ausgeloest werden."""

    video_id = _privat(ctx, fake_platform, make_episode, "ui_30")
    ctx.repository.set_status(video_id, VideoStatus.PUBLISHING.value)
    fake_platform.reset()

    status, body = _post(
        client,
        f"/videos/{video_id}/publish",
        confirm="yes",
        expected_status=VideoStatus.UPLOADED_PRIVATE.value,
    )
    assert status == 400
    assert body["ok"] is False
    assert fake_platform.call_count("publish") == 0

    # Auch mit "passendem" erwartetem Status: PUBLISHING ist nicht veroeffentlichbar
    status2, body2 = _post(
        client,
        f"/videos/{video_id}/publish",
        confirm="yes",
        expected_status=VideoStatus.PUBLISHING.value,
    )
    assert status2 == 400
    assert body2["ok"] is False
    assert fake_platform.call_count("publish") == 0
    assert ctx.repository.get(video_id)["status"] == VideoStatus.PUBLISHING.value


def test_zwei_schnelle_klicks_loesen_nur_einen_api_aufruf_aus(client, ctx, fake_platform, make_episode) -> None:
    video_id = _privat(ctx, fake_platform, make_episode, "ui_31")
    token = _csrf(client)
    daten = {
        "csrf_token": token,
        "confirm": "yes",
        "expected_status": VideoStatus.UPLOADED_PRIVATE.value,
    }

    erste = client.post(
        f"/videos/{video_id}/publish", data=daten, headers={"X-Requested-With": "fetch"}
    )
    zweite = client.post(
        f"/videos/{video_id}/publish", data=daten, headers={"X-Requested-With": "fetch"}
    )

    assert erste.status_code == 200 and erste.get_json()["ok"] is True
    assert zweite.status_code == 400 and zweite.get_json()["ok"] is False
    assert fake_platform.call_count("publish") == 1

    record = ctx.repository.get(video_id)
    assert record["status"] == VideoStatus.PUBLISHED.value
    assert record["published_at"]

    html = client.get("/").get_data(as_text=True)
    assert "VER&Ouml;FFENTLICHT" in html  # "VERÖFFENTLICHT ✓"
    assert "data-publish" not in html  # kein veroeffentlichen-Button mehr
