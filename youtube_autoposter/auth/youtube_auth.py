"""OAuth 2.0 fuer das eigene Google-/YouTube-Konto.

Ablauf:

1. ``credentials/credentials.json`` (OAuth-Client vom Typ **Desktop-App**)
   aus der Google Cloud Console ablegen.
2. Einmalig autorisieren - entweder ueber das Dashboard
   (``/auth/start`` -> Google -> ``/auth/callback``) oder per CLI
   (``python app.py auth login``).
3. Das Token (inkl. Refresh-Token) wird lokal gespeichert:
   unter Windows per DPAPI verschluesselt, sonst als Datei mit
   beschraenkten Rechten. Danach ist keine erneute Anmeldung noetig;
   abgelaufene Access-Tokens werden automatisch erneuert.

Es wird NIE ein Google-Passwort gespeichert oder abgefragt.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ..constants import SCOPE_YOUTUBE, SCOPE_YOUTUBE_UPLOAD
from ..errors import AuthError, AuthExpiredError, CredentialsError, InsufficientScopeError
from ..utils.files import now_iso, parse_iso
from ..utils.logging_utils import get_logger, redact
from ..utils.secure_store import SecretStore

#: Wie lange vor dem Ablauf ein Token proaktiv erneuert wird
EXPIRY_SAFETY_SECONDS = 300

#: Lebensdauer eines pending OAuth-Flows (Sekunden)
FLOW_TTL_SECONDS = 600

CLIENT_SECRETS_DOC = (
    "https://console.cloud.google.com/apis/credentials "
    "-> OAuth-Client-ID erstellen -> Typ: Desktop-App -> JSON herunterladen"
)


class AuthState(str, Enum):
    CONNECTED = "CONNECTED"
    NOT_CONNECTED = "NOT CONNECTED"
    EXPIRED = "EXPIRED"
    NEEDS_REAUTH = "NEEDS_REAUTH"
    MISSING_CREDENTIALS_FILE = "MISSING_CREDENTIALS_FILE"
    INVALID_CREDENTIALS_FILE = "INVALID_CREDENTIALS_FILE"
    ERROR = "ERROR"


@dataclass
class AuthStatus:
    state: AuthState = AuthState.NOT_CONNECTED
    connected: bool = False
    needs_user_action: bool = True
    message: str = ""
    channel_id: str | None = None
    channel_title: str | None = None
    account_email: str | None = None
    scopes: list[str] = field(default_factory=list)
    granted_scopes: list[str] = field(default_factory=list)
    expires_at: str | None = None
    has_refresh_token: bool = False
    client_secrets_present: bool = False
    token_present: bool = False
    token_storage: str = "file"
    last_check: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "connected": self.connected,
            "needs_user_action": self.needs_user_action,
            "message": self.message,
            "channel_id": self.channel_id,
            "channel_title": self.channel_title,
            "account_email": self.account_email,
            "scopes": list(self.scopes),
            "granted_scopes": list(self.granted_scopes),
            "expires_at": self.expires_at,
            "has_refresh_token": self.has_refresh_token,
            "client_secrets_present": self.client_secrets_present,
            "token_present": self.token_present,
            "token_storage": self.token_storage,
            "last_check": self.last_check,
            "last_error": self.last_error,
        }


class YouTubeAuthManager:
    """Verwaltet OAuth-Flow, Token-Speicher und den API-Service."""

    TOKEN_NAME = "token"

    def __init__(
        self,
        settings: Any,
        store: SecretStore | None = None,
        repository: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SecretStore(settings.CREDENTIALS_DIR, mode=getattr(settings, "TOKEN_STORAGE", "auto"))
        self.repository = repository
        self.logger = logger or get_logger("auth")
        self._lock = threading.RLock()
        self._flows: dict[str, tuple[Any, float, str]] = {}
        self._service_cache: tuple[Any, float] | None = None

    # ------------------------------------------------------------------
    # Client-Secrets
    # ------------------------------------------------------------------

    @property
    def client_secrets_path(self) -> Path:
        return Path(self.settings.client_secrets_path)

    def has_client_secrets(self) -> bool:
        return self.client_secrets_path.exists()

    def load_client_config(self) -> dict[str, Any]:
        """``credentials.json`` lesen und pruefen."""

        path = self.client_secrets_path
        if not path.exists():
            raise CredentialsError(
                f"OAuth-Client-Datei fehlt: {path}\n"
                f"Bitte die von Google heruntergeladene JSON-Datei dort als 'credentials.json' ablegen.\n"
                f"Herkunft: {CLIENT_SECRETS_DOC}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise CredentialsError(f"credentials.json ist kein gueltiges JSON: {exc.msg} (Zeile {exc.lineno})") from exc
        except OSError as exc:
            raise CredentialsError(f"credentials.json konnte nicht gelesen werden: {exc}") from exc

        if not isinstance(data, dict):
            raise CredentialsError("credentials.json muss ein JSON-Objekt sein")

        if "installed" in data:
            client_type = "installed"
        elif "web" in data:
            client_type = "web"
        else:
            raise CredentialsError(
                "credentials.json enthaelt weder 'installed' noch 'web'. "
                "Erwartet wird ein OAuth-Client vom Typ 'Desktop-App'."
            )

        block = data[client_type]
        if not block.get("client_id") or not block.get("client_secret"):
            raise CredentialsError("credentials.json ist unvollstaendig (client_id/client_secret fehlen)")
        if client_type == "web":
            self.logger.warning(
                "Der OAuth-Client ist vom Typ 'Webanwendung'. Empfohlen wird 'Desktop-App' - "
                "bei Web-Clients muss die Redirect-URI %s exakt in der Google Cloud Console "
                "hinterlegt sein.",
                self.settings.oauth_redirect_uri,
            )
        return {"client_type": client_type, "client_config": data}

    # ------------------------------------------------------------------
    # OAuth-Flow (Web-Oberflaeche)
    # ------------------------------------------------------------------

    def create_flow(self, redirect_uri: str | None = None) -> Any:
        from google_auth_oauthlib.flow import Flow  # lokaler Import: optionale Abhaengigkeit

        info = self.load_client_config()
        flow = Flow.from_client_config(
            info["client_config"],
            scopes=list(self.settings.OAUTH_SCOPES),
            redirect_uri=redirect_uri or self.settings.oauth_redirect_uri,
        )
        return flow

    def start_flow(self, redirect_uri: str | None = None) -> tuple[str, str]:
        """Autorisierungs-URL erzeugen und Flow fuer den Callback merken."""

        flow = self.create_flow(redirect_uri)
        state = secrets.token_urlsafe(24)
        url, _state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        with self._lock:
            self._prune_flows()
            self._flows[state] = (flow, time.time(), str(_state or state))
        self.logger.info("OAuth-Flow gestartet (Redirect: %s)", flow.redirect_uri)
        return url, state

    def complete_flow(
        self,
        code: str,
        *,
        state: str,
        redirect_uri: str | None = None,
    ) -> Any:
        """Autorisierungs-Code gegen ein Token tauschen und speichern."""

        with self._lock:
            entry = self._flows.pop(state, None)
        if entry is None:
            raise AuthError(
                "OAuth-Sitzung ist abgelaufen oder unbekannt. Bitte 'YouTube verbinden' erneut starten.",
                details=f"state={state[:6]}...",
            )
        flow, _created, _state = entry
        if _state != state:
            raise AuthError("OAuth-State stimmt nicht ueberein (moeglicher CSRF-Versuch)")
        try:
            flow.fetch_token(
                code=code,
                redirect_uri=redirect_uri or flow.redirect_uri,
            )
        except Exception as exc:  # noqa: BLE001 - Library-Fehler sauber melden
            message = redact(str(exc))
            self.logger.error("OAuth-Token-Austausch fehlgeschlagen: %s", message)
            raise AuthError(f"Google hat die Autorisierung abgelehnt: {message}") from exc

        credentials = flow.credentials
        self.save_credentials(credentials)
        self.logger.info("YouTube-Konto erfolgreich verbunden (Token gespeichert)")
        if self.repository is not None:
            self.repository.set_setting("auth.connected_at", now_iso())
            self.repository.set_setting("auth.last_error", "")
        if getattr(self.settings, "FETCH_CHANNEL_INFO", True):
            try:
                self.fetch_channel_info(credentials)
            except Exception as exc:  # noqa: BLE001 - nicht kritisch
                self.logger.warning("Kanalinformationen konnten nicht geladen werden: %s", redact(str(exc)))
        return credentials

    def _prune_flows(self) -> None:
        cutoff = time.time() - FLOW_TTL_SECONDS
        for key in [k for k, (_flow, created, _state) in self._flows.items() if created < cutoff]:
            self._flows.pop(key, None)

    # ------------------------------------------------------------------
    # OAuth-Flow (CLI / Desktop, ohne eigenes Web-Callback)
    # ------------------------------------------------------------------

    def login_via_local_server(self, *, port: int = 0, open_browser: bool = True) -> Any:
        """Komfort-Login fuer die Kommandozeile (``python app.py auth login``)."""

        from google_auth_oauthlib.flow import InstalledAppFlow

        info = self.load_client_config()
        flow = InstalledAppFlow.from_client_config(
            info["client_config"],
            scopes=list(self.settings.OAUTH_SCOPES),
        )
        credentials = flow.run_local_server(
            port=port,
            open_browser=open_browser,
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            authorization_prompt_message=(
                "Browser oeffnet sich gleich. Falls nicht, bitte diese URL oeffnen:\n{url}"
            ),
            success_message=(
                "Verbindung hergestellt. Dieses Fenster kann geschlossen werden - "
                "zurueck zum Dashboard."
            ),
        )
        self.save_credentials(credentials)
        self.logger.info("YouTube-Konto per CLI verbunden")
        if self.repository is not None:
            self.repository.set_setting("auth.connected_at", now_iso())
        if getattr(self.settings, "FETCH_CHANNEL_INFO", True):
            try:
                self.fetch_channel_info(credentials)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Kanalinformationen konnten nicht geladen werden: %s", redact(str(exc)))
        return credentials

    # ------------------------------------------------------------------
    # Token-Speicher
    # ------------------------------------------------------------------

    def token_path(self) -> Path:
        return self.store.path_for(self.TOKEN_NAME)

    def has_token(self) -> bool:
        return self.store.exists(self.TOKEN_NAME)

    def save_credentials(self, credentials: Any) -> Path:
        """Token sicher speichern (DPAPI unter Windows, sonst 0600-Datei)."""

        try:
            payload = json.loads(credentials.to_json())
        except Exception as exc:  # noqa: BLE001
            raise AuthError(f"Token konnte nicht serialisiert werden: {exc}") from exc
        payload["saved_at"] = now_iso()
        target = self.store.save(self.TOKEN_NAME, payload)
        self.logger.info(
            "OAuth-Token gespeichert (%s, Modus: %s)", target.name, self.store.effective_mode
        )
        self._service_cache = None
        return target

    def load_credentials(self, *, scopes: list[str] | None = None) -> Any | None:
        from google.oauth2.credentials import Credentials

        data = self.store.load(self.TOKEN_NAME)
        if not data:
            return None
        info = {key: value for key, value in data.items() if key != "saved_at"}
        if not info.get("refresh_token") and not info.get("token"):
            return None
        # Wichtig: Die im Token tatsaechlich erteilten Scopes muessen erhalten
        # bleiben - nur so kann die Anwendung erkennen, dass eine Autorisierung
        # zu wenige Rechte hat (und der Nutzer sich erneut verbinden muss).
        stored_scopes = [str(s) for s in (info.get("scopes") or []) if s]
        try:
            credentials = Credentials.from_authorized_user_info(
                info, scopes or stored_scopes or list(self.settings.OAUTH_SCOPES)
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Gespeichertes Token ist ungueltig: %s", redact(str(exc)))
            return None
        # Pflichtfelder sicherstellen (aeltere Token-Formate)
        if not credentials.token_uri:
            credentials.token_uri = "https://oauth2.googleapis.com/token"
        client_config = self._safe_client_config()
        if client_config and not credentials.client_id:
            block = client_config.get("installed") or client_config.get("web") or {}
            credentials.client_id = block.get("client_id")
            credentials.client_secret = block.get("client_secret")
        return credentials

    def _safe_client_config(self) -> dict[str, Any] | None:
        try:
            return self.load_client_config()["client_config"]
        except CredentialsError:
            return None

    def get_credentials(self, *, refresh: bool = True, required_scopes: list[str] | None = None) -> Any:
        """Gueltige Credentials liefern - erneuert das Token bei Bedarf automatisch."""

        credentials = self.load_credentials()
        if credentials is None:
            if not self.has_token():
                raise AuthError(
                    "Keine YouTube-Verbindung vorhanden. Bitte einmalig im Dashboard auf "
                    "'Google-/YouTube-Konto verbinden' klicken."
                )
            raise AuthExpiredError(
                "Gespeichertes Token ist ungueltig oder nicht lesbar - erneute Google-Autorisierung erforderlich."
            )

        scopes = required_scopes or list(self.settings.OAUTH_SCOPES)
        if refresh:
            self._ensure_valid(credentials)

        if scopes and credentials.scopes and not credentials.has_scopes(scopes):
            missing = [s for s in scopes if s not in (credentials.scopes or set())]
            raise InsufficientScopeError(
                "Die gespeicherte Autorisierung reicht fuer diese Aktion nicht aus. "
                f"Es fehlen folgende Scopes: {', '.join(missing)}. "
                "Bitte die Verbindung einmalig erneuern (Dashboard -> 'Erneut verbinden').",
            )
        return credentials

    def _ensure_valid(self, credentials: Any) -> None:
        if credentials.valid:
            return
        expiry = getattr(credentials, "expiry", None)
        if expiry is not None:
            try:
                seconds_left = (expiry - expiry.now(expiry.tzinfo)).total_seconds()
            except Exception:  # noqa: BLE001
                seconds_left = 0
            if seconds_left > EXPIRY_SAFETY_SECONDS:
                return
        if not credentials.refresh_token:
            raise AuthExpiredError(
                "Access-Token ist abgelaufen und es ist kein Refresh-Token vorhanden. "
                "Erneute Google-Autorisierung erforderlich."
            )
        try:
            from google.auth.transport.requests import Request

            credentials.refresh(Request())
        except Exception as exc:  # noqa: BLE001
            message = redact(str(exc))
            self.logger.warning("Automatische Token-Erneuerung fehlgeschlagen: %s", message)
            if self.repository is not None:
                self.repository.set_setting("auth.last_error", message)
            raise AuthExpiredError(
                f"Token konnte nicht automatisch erneuert werden ({message}). "
                "Erneute Google-Autorisierung erforderlich."
            ) from exc
        self.save_credentials(credentials)
        self.logger.info("Access-Token wurde automatisch erneuert")

    def disconnect(self, *, revoke: bool = False) -> dict[str, Any]:
        """Token loeschen (optional bei Google widerrufen)."""

        revoked = False
        error = None
        if revoke:
            credentials = self.load_credentials()
            if credentials is not None and credentials.refresh_token:
                try:
                    from google.auth.transport.requests import Request

                    credentials.revoke(Request())
                    revoked = True
                except Exception as exc:  # noqa: BLE001
                    error = redact(str(exc))
        removed = self.store.delete(self.TOKEN_NAME)
        self._service_cache = None
        if self.repository is not None:
            self.repository.set_setting("auth.connected_at", "")
        self.logger.info("YouTube-Verbindung getrennt (%d Datei(en) entfernt)", len(removed))
        return {"revoked": revoked, "removed": [str(p) for p in removed], "error": error}

    # ------------------------------------------------------------------
    # Status / Service
    # ------------------------------------------------------------------

    def status(self, *, check_expiry: bool = True) -> AuthStatus:
        """Verbindungsstatus OHNE API-Aufruf (Dashboard bleibt quota-frei)."""

        status = AuthStatus(
            scopes=list(self.settings.OAUTH_SCOPES),
            client_secrets_present=self.has_client_secrets(),
            token_present=self.has_token(),
            token_storage=self.store.effective_mode,
            last_check=now_iso(),
        )
        if self.repository is not None:
            status.channel_id = self.repository.get_setting("channel.id")
            status.channel_title = self.repository.get_setting("channel.title")
            status.account_email = self.repository.get_setting("auth.account_email")
            status.last_error = self.repository.get_setting("auth.last_error") or None

        if not status.client_secrets_present:
            status.state = AuthState.MISSING_CREDENTIALS_FILE
            status.message = (
                f"credentials.json fehlt: {self.client_secrets_path}\n"
                f"{CLIENT_SECRETS_DOC}"
            )
            return status

        try:
            self.load_client_config()
        except CredentialsError as exc:
            status.state = AuthState.INVALID_CREDENTIALS_FILE
            status.message = str(exc)
            return status

        if not status.token_present:
            status.state = AuthState.NOT_CONNECTED
            status.message = "Noch keine Verbindung - Google-/YouTube-Konto einmalig verbinden."
            return status

        credentials = self.load_credentials()
        if credentials is None:
            status.state = AuthState.NEEDS_REAUTH
            status.message = "Gespeichertes Token ist ungueltig. Erneute Google-Autorisierung erforderlich."
            return status

        status.has_refresh_token = bool(credentials.refresh_token)
        status.granted_scopes = sorted(credentials.scopes or [])
        expiry = getattr(credentials, "expiry", None)
        if expiry is not None:
            status.expires_at = expiry.isoformat()

        if check_expiry and not credentials.valid:
            if not status.has_refresh_token:
                status.state = AuthState.NEEDS_REAUTH
                status.message = "Token abgelaufen und kein Refresh-Token - erneute Autorisierung erforderlich."
                return status
            status.state = AuthState.EXPIRED
            status.message = (
                "Access-Token abgelaufen - wird beim naechsten API-Aufruf automatisch erneuert "
                "(Refresh-Token vorhanden)."
            )
            status.connected = True
            status.needs_user_action = False
            return status

        missing = [s for s in status.scopes if s not in set(status.granted_scopes)]
        if missing:
            status.state = AuthState.NEEDS_REAUTH
            status.message = (
                "Die gespeicherte Autorisierung enthaelt nicht alle benoetigten Rechte "
                f"(fehlend: {', '.join(missing)}). Bitte einmalig erneut verbinden."
            )
            status.needs_user_action = True
            return status

        status.state = AuthState.CONNECTED
        status.connected = True
        status.needs_user_action = False
        status.message = "Verbunden" + (f" mit {status.channel_title}" if status.channel_title else "")
        return status

    def is_connected(self) -> bool:
        try:
            return self.status().connected
        except Exception:  # noqa: BLE001
            return False

    def build_service(self, *, scopes: list[str] | None = None) -> Any:
        """Autorisierten YouTube-API-Client bauen ( Discovery-Doc wird gecacht)."""

        with self._lock:
            if self._service_cache is not None:
                service, created = self._service_cache
                if time.time() - created < 1800:
                    return service
        credentials = self.get_credentials(required_scopes=scopes)
        from googleapiclient.discovery import build

        service = build(
            "youtube",
            "v3",
            credentials=credentials,
            cache_discovery=True,
            static_discovery=True,
        )
        with self._lock:
            self._service_cache = (service, time.time())
        return service

    def fetch_channel_info(self, credentials: Any | None = None) -> dict[str, Any]:
        """Kanal-ID, Name und Uploads-Playlist ermitteln (1 Quota-Einheit)."""

        from ..platforms.youtube.api import YouTubeApi

        api = YouTubeApi(self, settings=self.settings, repository=self.repository, logger=self.logger)
        info = api.fetch_channel_info(credentials=credentials)
        if info and self.repository is not None:
            self.repository.set_setting("channel.id", info.get("id") or "")
            self.repository.set_setting("channel.title", info.get("title") or "")
            self.repository.set_setting("channel.uploads_playlist", info.get("uploads_playlist") or "")
            self.repository.set_setting("auth.connected_at", now_iso())
        return info

    def test_connection(self) -> AuthStatus:
        """Expliziter Test aus dem Dashboard: 'Verbindung testen' (1 Einheit)."""

        status = self.status()
        if not status.client_secrets_present or not status.token_present:
            return status
        try:
            info = self.fetch_channel_info()
        except AuthError as exc:
            status.state = AuthState.NEEDS_REAUTH if isinstance(exc, AuthExpiredError) else AuthState.ERROR
            status.connected = False
            status.needs_user_action = True
            status.message = str(exc)
            status.last_error = str(exc)
            if self.repository is not None:
                self.repository.set_setting("auth.last_error", str(exc))
            return status
        except Exception as exc:  # noqa: BLE001
            status.state = AuthState.ERROR
            status.connected = False
            status.message = f"Verbindungstest fehlgeschlagen: {redact(str(exc))}"
            status.last_error = status.message
            return status

        status.state = AuthState.CONNECTED
        status.connected = True
        status.needs_user_action = False
        status.channel_id = info.get("id")
        status.channel_title = info.get("title")
        status.message = f"Verbindung OK - Kanal: {info.get('title') or 'unbekannt'}"
        status.last_error = None
        if self.repository is not None:
            self.repository.set_setting("auth.last_error", "")
        return status


__all__ = [
    "AuthState",
    "AuthStatus",
    "YouTubeAuthManager",
    "CLIENT_SECRETS_DOC",
    "SCOPE_YOUTUBE",
    "SCOPE_YOUTUBE_UPLOAD",
    "parse_iso",
]
