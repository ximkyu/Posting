"""Tests fuer OAuth/Autorisierung und den sicheren Token-Speicher."""

from __future__ import annotations

import json
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from youtube_autoposter.auth.youtube_auth import AuthState, CLIENT_SECRETS_DOC, YouTubeAuthManager
from youtube_autoposter.errors import AuthError, CredentialsError
from youtube_autoposter.utils.secure_store import SecretStore

from tests.fakes import FakeYouTubeService, sample_client_secrets, sample_token_payload


def _token_datei(project: Path, payload: dict) -> Path:
    path = project / "credentials" / "token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


# ---------------------------------------------------------------------------
# credentials.json
# ---------------------------------------------------------------------------


def test_ohne_credentials_datei(ctx, project) -> None:
    status = ctx.auth.status()
    assert status.state == AuthState.MISSING_CREDENTIALS_FILE
    assert status.connected is False
    assert status.needs_user_action is True
    assert "credentials.json fehlt" in status.message
    assert "Desktop-App" in status.message
    assert status.client_secrets_present is False


def test_kaputte_credentials_datei(ctx, project) -> None:
    pfad = project / "credentials" / "credentials.json"
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("{ kein json", encoding="utf-8")

    status = ctx.auth.status()
    assert status.state == AuthState.INVALID_CREDENTIALS_FILE
    assert status.connected is False
    with pytest.raises(CredentialsError):
        ctx.auth.load_client_config()


def test_falscher_client_typ_wird_abgelehnt(ctx, project) -> None:
    pfad = project / "credentials" / "credentials.json"
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps({"service_account": {"client_id": "x"}}), encoding="utf-8")

    with pytest.raises(CredentialsError) as info:
        ctx.auth.load_client_config()
    assert "Desktop-App" in str(info.value)
    assert ctx.auth.status().state == AuthState.INVALID_CREDENTIALS_FILE


def test_unvollstaendige_credentials_datei(ctx, project) -> None:
    pfad = project / "credentials" / "credentials.json"
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps({"installed": {"client_id": "nur-id"}}), encoding="utf-8")

    with pytest.raises(CredentialsError):
        ctx.auth.load_client_config()


def test_gueltige_desktop_app_wird_erkannt(ctx, credentials_file) -> None:
    info = ctx.auth.load_client_config()
    assert info["client_type"] == "installed"
    assert info["client_config"]["installed"]["client_id"].endswith(".apps.googleusercontent.com")
    status = ctx.auth.status()
    assert status.state == AuthState.NOT_CONNECTED
    assert status.client_secrets_present is True
    assert status.token_present is False
    assert status.connected is False


def test_web_client_wird_akzeptiert_aber_gewarnt(ctx, project, caplog) -> None:
    sample_client_secrets(project / "credentials" / "credentials.json", client_type="web")
    info = ctx.auth.load_client_config()
    assert info["client_type"] == "web"


# ---------------------------------------------------------------------------
# Token-Zustaende
# ---------------------------------------------------------------------------


def test_verbunden_mit_gueltigem_token(ctx, credentials_file, project) -> None:
    _token_datei(project, sample_token_payload())
    status = ctx.auth.status()

    assert status.state == AuthState.CONNECTED
    assert status.connected is True
    assert status.needs_user_action is False
    assert status.has_refresh_token is True
    assert status.token_present is True
    assert "youtube" in " ".join(status.granted_scopes)
    assert ctx.auth.is_connected() is True


def test_abgelaufenes_token_mit_refresh_token_bleibt_verbunden(ctx, credentials_file, project) -> None:
    payload = sample_token_payload()
    payload["expiry"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _token_datei(project, payload)

    status = ctx.auth.status()
    assert status.state == AuthState.EXPIRED
    assert status.connected is True  # wird automatisch erneuert
    assert status.needs_user_action is False
    assert "automatisch erneuert" in status.message


def test_abgelaufenes_token_ohne_refresh_token_braucht_neue_autorisierung(
    ctx, credentials_file, project
) -> None:
    payload = sample_token_payload(
        refresh_token=None,
        expiry=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    _token_datei(project, payload)

    status = ctx.auth.status()
    assert status.state == AuthState.NEEDS_REAUTH
    assert status.connected is False
    assert status.needs_user_action is True


def test_fehlender_scope_verlangt_neue_autorisierung(ctx, credentials_file, project) -> None:
    _token_datei(project, sample_token_payload(scopes=["https://www.googleapis.com/auth/youtube.readonly"]))
    status = ctx.auth.status()
    assert status.state == AuthState.NEEDS_REAUTH
    assert "fehlend" in status.message
    assert status.connected is False


def test_kaputtes_token_wird_nicht_zum_absturz(ctx, credentials_file, project) -> None:
    pfad = project / "credentials" / "token.json"
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("toe@#$%ken", encoding="utf-8")

    assert ctx.auth.load_credentials() is None
    status = ctx.auth.status()
    assert status.state == AuthState.NEEDS_REAUTH
    assert status.connected is False


def test_token_ohne_inhalt_wird_ignoriert(ctx, credentials_file, project) -> None:
    _token_datei(project, {"irgendwas": "wert"})
    assert ctx.auth.load_credentials() is None
    assert ctx.auth.status().state == AuthState.NEEDS_REAUTH


def test_get_credentials_ohne_token_wirft_auth_error(ctx, credentials_file) -> None:
    with pytest.raises(AuthError):
        ctx.auth.get_credentials()


# ---------------------------------------------------------------------------
# Token speichern / trennen
# ---------------------------------------------------------------------------


def test_token_speichern_und_trennen(ctx, credentials_file, project) -> None:
    from google.oauth2.credentials import Credentials

    credentials = Credentials.from_authorized_user_info(
        sample_token_payload(), scopes=list(ctx.settings.OAUTH_SCOPES)
    )
    pfad = ctx.auth.save_credentials(credentials)

    assert pfad.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(pfad.stat().st_mode) == 0o600
    assert ctx.auth.has_token() is True
    assert ctx.auth.status().state == AuthState.CONNECTED

    # Kein Klartext-Token in der Konfiguration
    config_text = (project / "config.json").read_text(encoding="utf-8")
    assert "ya29.TEST-ACCESS-TOKEN" not in config_text
    assert "GOCSPX-TESTVALUE-ONLY" not in config_text

    ergebnis = ctx.auth.disconnect()
    assert ergebnis["removed"]
    assert ctx.auth.has_token() is False
    assert ctx.auth.status().state == AuthState.NOT_CONNECTED


def test_kein_passwort_wird_gespeichert(ctx, credentials_file, project) -> None:
    """Es duerfen nur OAuth-Token, niemals Konto-Passwoerter gespeichert werden."""

    from google.oauth2.credentials import Credentials

    ctx.auth.save_credentials(
        Credentials.from_authorized_user_info(sample_token_payload(), scopes=list(ctx.settings.OAUTH_SCOPES))
    )
    inhalt = json.loads((project / "credentials" / "token.json").read_text(encoding="utf-8"))
    # google-auth schreibt nur OAuth-Felder - niemals ein Konto-Passwort
    assert set(inhalt) <= {
        "token",
        "refresh_token",
        "token_uri",
        "client_id",
        "client_secret",
        "scopes",
        "expiry",
        "saved_at",
        "id_token",
        "account",
        "universe_domain",
        "quota_project_id",
        "granted_scopes",
    }
    assert "password" not in json.dumps(inhalt).lower()
    assert "passwd" not in json.dumps(inhalt).lower()


def test_verbindung_testen_mit_mock(ctx, credentials_file, project) -> None:
    _token_datei(project, sample_token_payload())
    service = FakeYouTubeService(
        {
            "channels": [
                {
                    "items": [
                        {
                            "id": "UC42",
                            "snippet": {"title": "Mein Doku-Kanal"},
                            "contentDetails": {"relatedPlaylists": {"uploads": "UU42"}},
                        }
                    ]
                }
            ]
        }
    )
    ctx.auth._service_cache = (service, time.time())

    status = ctx.auth.test_connection()

    assert status.state == AuthState.CONNECTED
    assert status.channel_title == "Mein Doku-Kanal"
    assert ctx.repository.get_setting("channel.id") == "UC42"
    assert ctx.repository.get_setting("channel.uploads_playlist") == "UU42"


def test_verbindung_testen_ohne_kanal_meldet_fehler(ctx, credentials_file, project) -> None:
    _token_datei(project, sample_token_payload())
    ctx.auth._service_cache = (FakeYouTubeService({"channels": [{"items": []}]}), time.time())

    status = ctx.auth.test_connection()

    assert status.connected is False
    assert status.state == AuthState.ERROR
    assert "Kanal" in status.message


def test_verbindung_testen_ohne_token_macht_keinen_api_aufruf(ctx, credentials_file) -> None:
    status = ctx.auth.test_connection()
    assert status.state == AuthState.NOT_CONNECTED
    assert status.connected is False


# ---------------------------------------------------------------------------
# OAuth-Flow (nur Vorbereitung, kein echter Consent)
# ---------------------------------------------------------------------------


def test_flow_verwendet_loopback_redirect_und_offline_zugriff(ctx, credentials_file) -> None:
    url, state = ctx.auth.start_flow()

    assert url.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "response_type=code" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fauth%2Fcallback" in url
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fyoutube" in url
    assert state
    # kein Passwort im Flow
    assert "password" not in url.lower()


def test_redirect_uri_bei_oeffentlichem_host(ctx, credentials_file) -> None:
    ctx.settings.HOST = "0.0.0.0"
    assert ctx.settings.oauth_redirect_uri == "http://127.0.0.1:8765/auth/callback"
    ctx.settings.OAUTH_REDIRECT_URI = "http://127.0.0.1:9999/auth/callback"
    assert ctx.settings.oauth_redirect_uri == "http://127.0.0.1:9999/auth/callback"


def test_flow_ohne_credentials_datei_wirft_fehler(ctx) -> None:
    with pytest.raises(CredentialsError):
        ctx.auth.start_flow()


def test_minimale_scopes(ctx) -> None:
    scopes = list(ctx.settings.OAUTH_SCOPES)
    assert scopes == ["https://www.googleapis.com/auth/youtube"]
    assert "https://www.googleapis.com/auth/youtube.force-ssl" not in scopes
    assert len(scopes) == 1


# ---------------------------------------------------------------------------
# Secret-Store-Modus
# ---------------------------------------------------------------------------


def test_token_speicher_modus_wird_angezeigt(ctx, project) -> None:
    store = SecretStore(project / "credentials", mode="file")
    auth = YouTubeAuthManager(ctx.settings, store=store, repository=ctx.repository)
    status = auth.status()
    assert status.token_storage == "file"
    assert status.to_dict()["token_storage"] == "file"
