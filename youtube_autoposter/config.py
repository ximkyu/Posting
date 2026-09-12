"""Zentrale Konfiguration.

Eine Datei (``config.json``) im Projekt-Wurzelverzeichnis steuert das gesamte
System. Zusaetzlich koennen alle Werte per Umgebungsvariable ueberschrieben
werden (Praefix ``YAP_``, z. B. ``YAP_PORT=9000``) - praktisch fuer Tests und
fuer einen zweiten Kanal/Instanz.

Es liegen hier bewusst KEINE Secrets. OAuth-Client und Token leben in
``credentials/`` und werden von :mod:`youtube_autoposter.utils.secure_store`
verwaltet (unter Windows per DPAPI verschluesselt).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    DEFAULT_CATEGORY_ID,
    DEFAULT_LANGUAGE,
    DEFAULT_SCOPES,
    ENFORCED_UPLOAD_PRIVACY,
    PRIVACY_PRIVATE,
    SCOPE_PRESETS,
    THUMBNAIL_EXTENSIONS,
    VIDEO_EXTENSIONS_DEFAULT,
    VIDEO_EXTENSIONS_OPTIONAL,
)
from .errors import ConfigError

#: Praefix fuer Umgebungsvariablen
ENV_PREFIX = "YAP_"

#: Name der Konfigurationsdatei relativ zum Projekt-Root
CONFIG_FILE_NAME = "config.json"

_BOOL_TRUE = {"1", "true", "yes", "y", "on", "ja"}
_BOOL_FALSE = {"0", "false", "no", "n", "off", "nein"}


def project_root() -> Path:
    """Wurzelverzeichnis des Projekts (Ordner, der ``app.py`` enthaelt)."""

    return Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    """Alle einstellbaren Werte mit sinnvollen Defaults."""

    # --- Anwendung -----------------------------------------------------
    APP_NAME: str = "YouTube AutoPoster"
    BASE_DIR: Path = field(default_factory=project_root)

    # --- Ordner-Workflow ------------------------------------------------
    READY_FOLDER: Path = field(default_factory=lambda: project_root() / "folders" / "READY")
    PROCESSING_FOLDER: Path = field(default_factory=lambda: project_root() / "folders" / "PROCESSING")
    UPLOADED_PRIVATE_FOLDER: Path = field(
        default_factory=lambda: project_root() / "folders" / "UPLOADED_PRIVATE"
    )
    PUBLISHED_FOLDER: Path = field(default_factory=lambda: project_root() / "folders" / "PUBLISHED")
    FAILED_FOLDER: Path = field(default_factory=lambda: project_root() / "folders" / "FAILED")
    # zusaetzlich (Verbesserung gegenueber der Ursprungsstruktur):
    # hier landen Dubletten, die bewusst NICHT hochgeladen wurden.
    SKIPPED_FOLDER: Path = field(default_factory=lambda: project_root() / "folders" / "SKIPPED")

    # --- Datenbank / Logs ----------------------------------------------
    DATABASE_PATH: Path = field(default_factory=lambda: project_root() / "data" / "youtube_autoposter.db")
    LOG_DIR: Path = field(default_factory=lambda: project_root() / "data" / "logs")
    LOG_FILE: str = "app.log"
    LOG_LEVEL: str = "INFO"
    LOG_MAX_BYTES: int = 5 * 1024 * 1024
    LOG_BACKUP_COUNT: int = 5
    LOG_TO_DATABASE: bool = True
    LOG_KEEP_ROWS: int = 20000

    # --- Web-Oberflaeche -------------------------------------------------
    HOST: str = "127.0.0.1"
    PORT: int = 8765
    #: Wenn True wird waitress verwendet (falls installiert), sonst Flask-Dev-Server
    USE_WAITRESS: bool = True
    DEBUG: bool = False
    AUTO_REFRESH_SECONDS: int = 5
    #: Nach dem Start den Standard-Browser oeffnen (nur bei Loopback-Host)
    OPEN_BROWSER: bool = True

    # --- Watch-Folder / Stabilitaetspruefung ------------------------------
    WATCH_ENABLED: bool = True
    SCAN_INTERVAL_SECONDS: int = 10
    #: So lange muss die Dateigroesse unveraendert bleiben (Sekunden)
    FILE_STABILITY_SECONDS: int = 10
    #: Abstand zwischen zwei Groessen-Messungen (Sekunden)
    STABILITY_CHECK_INTERVAL: int = 2
    #: Wie viele unveraenderte Messungen noetig sind
    STABILITY_REQUIRED_CHECKS: int = 2
    #: Blockierendes Warten auf Stabilitaet vor dem Upload (Sekunden, 0 = aus)
    STABILITY_WAIT_BEFORE_UPLOAD: bool = True
    #: watchdog (Dateisystem-Events) nutzen, falls installiert
    USE_WATCHDOG: bool = True
    #: Auch Unterordner von READY durchsuchen
    SCAN_RECURSIVE: bool = False
    #: JSON ist Pflicht. Wenn False, wird ein Video auch ohne JSON erkannt
    #: (dann muss der Titel aus dem Dateinamen abgeleitet werden).
    REQUIRE_METADATA_FILE: bool = True

    # --- Upload-Queue -----------------------------------------------------
    AUTO_UPLOAD: bool = True
    MAX_CONCURRENT_UPLOADS: int = 1
    #: Pausieren, solange keine YouTube-Verbindung besteht
    PAUSE_WHEN_DISCONNECTED: bool = True
    WORKER_POLL_SECONDS: int = 2

    # --- Retries ----------------------------------------------------------
    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 5.0
    RETRY_BACKOFF_FACTOR: float = 2.0
    RETRY_MAX_DELAY: float = 120.0
    #: Chunk-Groesse fuer den resumable Upload (Byte). -1 = gesamte Datei.
    UPLOAD_CHUNK_SIZE: int = 8 * 1024 * 1024
    UPLOAD_TIMEOUT_SECONDS: int = 300
    #: Fortschritt alle N Sekunden in Log/DB schreiben
    PROGRESS_LOG_SECONDS: int = 20

    # --- Privatsphaere (SICHERHEITSREGEL) ---------------------------------
    #: Default, falls in der JSON nichts steht. Wird beim Upload IMMER auf
    #: private erzwungen - eine JSON kann das nicht ueberschreiben.
    DEFAULT_PRIVACY_STATUS: str = PRIVACY_PRIVATE
    ENFORCE_PRIVATE_UPLOAD: bool = True
    #: publishAt aus der JSON an YouTube senden? In V1 bewusst AUS, weil das
    #: einer automatischen Veroeffentlichung gleichkaeme.
    SEND_PUBLISH_AT: bool = False

    # --- Dateiformate -----------------------------------------------------
    ALLOWED_VIDEO_EXTENSIONS: tuple[str, ...] = VIDEO_EXTENSIONS_DEFAULT
    #: .mov/.mkv/.webm zusaetzlich akzeptieren (YouTube kann sie verarbeiten)
    ALLOW_EXTRA_VIDEO_EXTENSIONS: bool = False
    ALLOWED_THUMBNAIL_EXTENSIONS: tuple[str, ...] = THUMBNAIL_EXTENSIONS
    #: Integritaet nach dem Verschieben per Hash pruefen
    VERIFY_HASH_AFTER_MOVE: bool = True

    # --- ffprobe / ffmpeg -------------------------------------------------
    FFPROBE_PATH: str = ""
    FFMPEG_PATH: str = ""
    #: Wenn True, gilt eine fehlende ffprobe-Analyse als Fehler (Upload wird
    #: nicht gestartet). Default: False -> Warnung, Upload bleibt moeglich.
    REQUIRE_FFPROBE: bool = False
    MEDIA_PROBE_TIMEOUT_SECONDS: int = 120

    # --- Metadaten-Defaults ----------------------------------------------
    DEFAULT_CATEGORY_ID: str = DEFAULT_CATEGORY_ID
    DEFAULT_LANGUAGE: str = DEFAULT_LANGUAGE
    #: made-for-kids-Flag, wenn in der JSON nichts steht
    DEFAULT_MADE_FOR_KIDS: bool = False
    #: Kategorie-ID einmalig online gegen videoCategories.list pruefen
    #: (1 Quota-Einheit, Ergebnis wird in der DB gecacht)
    VALIDATE_CATEGORY_ONLINE: bool = True
    CATEGORY_CACHE_HOURS: int = 168
    #: containsSyntheticMedia an die API senden (nicht offiziell dokumentiert)
    SEND_SYNTHETIC_MEDIA_FLAG: bool = False

    # --- OAuth / Credentials ---------------------------------------------
    CREDENTIALS_DIR: Path = field(default_factory=lambda: project_root() / "credentials")
    CLIENT_SECRETS_FILE: str = "credentials/credentials.json"
    TOKEN_FILE: str = "token.json"
    #: auto = DPAPI unter Windows, sonst Datei mit 0600. Alternativen: dpapi|file
    TOKEN_STORAGE: str = "auto"
    OAUTH_SCOPES: tuple[str, ...] = DEFAULT_SCOPES
    #: Wird automatisch auf http://127.0.0.1:{PORT}/auth/callback gesetzt,
    #: wenn leer. Google erlaubt fuer Desktop-Clients Loopback-Redirects.
    OAUTH_REDIRECT_URI: str = ""
    #: Nach erfolgreicher Autorisierung den Browser auf diese Seite schicken
    OAUTH_SUCCESS_REDIRECT: str = "/"
    #: Kanal-Informationen (Name, Uploads-Playlist) nach dem Login abrufen
    FETCH_CHANNEL_INFO: bool = True

    # --- Sonstiges ---------------------------------------------------------
    #: Thumbnail-Anzeige: von YouTube per API gelieferte URL verwenden,
    #: wenn lokal keine Datei (mehr) vorhanden ist.
    USE_REMOTE_THUMBNAIL_FALLBACK: bool = True
    #: Beim Start abgebrochene Uploads automatisch auf INTERRUPTED setzen
    MARK_INTERRUPTED_ON_STARTUP: bool = True
    #: Nach erfolgreichem Upload die JSON/Videodatei archivieren (nie loeschen)
    ARCHIVE_AFTER_UPLOAD: bool = True
    ARCHIVE_AFTER_PUBLISH: bool = True
    #: Archiv: pro Episode ein eigener Unterordner (<ZIEL>/<episode_id>/)
    ARCHIVE_IN_SUBFOLDER: bool = True
    #: Nach Upload/Veroeffentlichung/Fehler eine Ergebnisdatei neben die
    #: archivierten Dateien schreiben (zusaetzlich zur SQLite-Datenbank)
    WRITE_RESULT_JSON: bool = True
    RESULT_JSON_FILENAME: str = "upload_result.json"

    # ------------------------------------------------------------------
    # Hilfsfunktionen
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        base = Path(self.BASE_DIR).expanduser()
        if not base.is_absolute():
            # Relative Angabe (z. B. "." aus config.json) bezieht sich auf das
            # Projektverzeichnis, nicht auf das aktuelle Arbeitsverzeichnis.
            base = (project_root() / base).resolve()
        self.BASE_DIR = base

        # Wenn BASE_DIR nicht das Projektverzeichnis ist (z. B. Test-Projekt,
        # zweiter Arbeitsplatz, portable Installation), muessen die
        # Standardpfade ebenfalls dorthin zeigen - sonst wuerde die Anwendung
        # still in einem anderen Ordner arbeiten.
        code_root = project_root()
        reanchor = base != code_root

        for f in fields(self):
            if f.name == "BASE_DIR":
                continue
            value = getattr(self, f.name)
            if isinstance(value, Path):
                value = Path(value).expanduser()
                if reanchor and value.is_absolute():
                    try:
                        value = (base / value.relative_to(code_root)).resolve()
                    except ValueError:
                        pass
                if not value.is_absolute():
                    value = (self.BASE_DIR / value).resolve()
                setattr(self, f.name, value)
        self._normalize()

    def _normalize(self) -> None:
        """Werte aufraeumen und harte Sicherheitsregeln durchsetzen."""

        self.LOG_LEVEL = str(self.LOG_LEVEL or "INFO").upper()
        self.PORT = int(self.PORT)
        self.SCAN_INTERVAL_SECONDS = max(1, int(self.SCAN_INTERVAL_SECONDS))
        self.FILE_STABILITY_SECONDS = max(0, int(self.FILE_STABILITY_SECONDS))
        self.STABILITY_CHECK_INTERVAL = max(1, int(self.STABILITY_CHECK_INTERVAL))
        self.STABILITY_REQUIRED_CHECKS = max(1, int(self.STABILITY_REQUIRED_CHECKS))
        self.MAX_RETRIES = max(0, int(self.MAX_RETRIES))
        self.MAX_CONCURRENT_UPLOADS = max(1, int(self.MAX_CONCURRENT_UPLOADS))
        self.UPLOAD_CHUNK_SIZE = int(self.UPLOAD_CHUNK_SIZE)

        exts = tuple(
            _normalize_ext(e) for e in tuple(self.ALLOWED_VIDEO_EXTENSIONS or ())
        )
        extra = tuple(_normalize_ext(e) for e in VIDEO_EXTENSIONS_OPTIONAL)
        if self.ALLOW_EXTRA_VIDEO_EXTENSIONS:
            exts = tuple(dict.fromkeys(exts + extra))
        self.ALLOWED_VIDEO_EXTENSIONS = exts or VIDEO_EXTENSIONS_DEFAULT
        self.ALLOWED_THUMBNAIL_EXTENSIONS = tuple(
            _normalize_ext(e) for e in tuple(self.ALLOWED_THUMBNAIL_EXTENSIONS or ())
        ) or THUMBNAIL_EXTENSIONS

        scopes = tuple(str(s).strip() for s in tuple(self.OAUTH_SCOPES or ()) if str(s).strip())
        if not scopes:
            scopes = DEFAULT_SCOPES
        self.OAUTH_SCOPES = scopes

        # --- Harte Sicherheitsregeln (nicht deaktivierbar) ---------------
        if str(self.DEFAULT_PRIVACY_STATUS).lower() != ENFORCED_UPLOAD_PRIVACY:
            # Wir lassen den Wert in der Datei stehen (Transparenz), erzwingen
            # aber intern private.
            self.DEFAULT_PRIVACY_STATUS = ENFORCED_UPLOAD_PRIVACY
        self.ENFORCE_PRIVATE_UPLOAD = True

    # ------------------------------------------------------------------

    @property
    def client_secrets_path(self) -> Path:
        return self._resolve(self.CLIENT_SECRETS_FILE)

    @property
    def token_path(self) -> Path:
        return self.CREDENTIALS_DIR / self.TOKEN_FILE

    @property
    def log_file_path(self) -> Path:
        return self.LOG_DIR / self.LOG_FILE

    @property
    def oauth_redirect_uri(self) -> str:
        if self.OAUTH_REDIRECT_URI:
            return self.OAUTH_REDIRECT_URI.rstrip("/")
        host = "127.0.0.1" if self.HOST in ("0.0.0.0", "", "*") else self.HOST
        return f"http://{host}:{self.PORT}/auth/callback"

    @property
    def dashboard_url(self) -> str:
        host = "127.0.0.1" if self.HOST in ("0.0.0.0", "", "*") else self.HOST
        return f"http://{host}:{self.PORT}"

    @property
    def workflow_folders(self) -> dict[str, Path]:
        return {
            "READY": self.READY_FOLDER,
            "PROCESSING": self.PROCESSING_FOLDER,
            "UPLOADED_PRIVATE": self.UPLOADED_PRIVATE_FOLDER,
            "PUBLISHED": self.PUBLISHED_FOLDER,
            "FAILED": self.FAILED_FOLDER,
            "SKIPPED": self.SKIPPED_FOLDER,
        }

    def _resolve(self, value: str | Path) -> Path:
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = (self.BASE_DIR / p).resolve()
        return p

    # ------------------------------------------------------------------
    # Serialisierung
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Path):
                try:
                    value = str(value.relative_to(self.BASE_DIR))
                except ValueError:
                    value = str(value)
            elif isinstance(value, tuple):
                value = list(value)
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, base_dir: Path | None = None) -> "Settings":
        if not is_dataclass(cls):  # pragma: no cover - defensive
            raise ConfigError("Settings ist kein Dataclass")
        known = {f.name: f for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        unknown: list[str] = []
        for key, raw in (data or {}).items():
            name = str(key).strip().upper()
            if name not in known:
                unknown.append(str(key))
                continue
            kwargs[name] = _coerce(raw, known[name].type, known[name].name)
        if unknown:
            # Unbekannte Schluessel werden ignoriert, aber protokolliert
            kwargs["_ignored_keys"] = unknown
        if base_dir is not None:
            kwargs["BASE_DIR"] = base_dir
        ignored = kwargs.pop("_ignored_keys", None)
        settings = cls(**kwargs)
        settings.ignored_config_keys = list(ignored or [])
        return settings

    def ensure_directories(self) -> list[Path]:
        """Alle Laufzeit-Ordner anlegen (idempotent)."""

        created: list[Path] = []
        targets: Iterable[Path] = [
            *self.workflow_folders.values(),
            self.LOG_DIR,
            self.CREDENTIALS_DIR,
            self.DATABASE_PATH.parent,
        ]
        for target in targets:
            target = Path(target)
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                created.append(target)
        return created

    def save(self, path: Path | None = None) -> Path:
        target = Path(path or (self.BASE_DIR / CONFIG_FILE_NAME))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=False)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return target

    def describe(self) -> dict[str, Any]:
        """Kompakte, sekret-freie Uebersicht fuer UI/Doctor."""

        return {
            "BASE_DIR": str(self.BASE_DIR),
            "READY_FOLDER": str(self.READY_FOLDER),
            "PROCESSING_FOLDER": str(self.PROCESSING_FOLDER),
            "UPLOADED_PRIVATE_FOLDER": str(self.UPLOADED_PRIVATE_FOLDER),
            "PUBLISHED_FOLDER": str(self.PUBLISHED_FOLDER),
            "FAILED_FOLDER": str(self.FAILED_FOLDER),
            "SKIPPED_FOLDER": str(self.SKIPPED_FOLDER),
            "DATABASE_PATH": str(self.DATABASE_PATH),
            "LOG_FILE": str(self.log_file_path),
            "HOST": self.HOST,
            "PORT": self.PORT,
            "DASHBOARD_URL": self.dashboard_url,
            "OAUTH_REDIRECT_URI": self.oauth_redirect_uri,
            "CLIENT_SECRETS_FILE": str(self.client_secrets_path),
            "CLIENT_SECRETS_EXISTS": self.client_secrets_path.exists(),
            "TOKEN_STORAGE": self.TOKEN_STORAGE,
            "OAUTH_SCOPES": list(self.OAUTH_SCOPES),
            "FILE_STABILITY_SECONDS": self.FILE_STABILITY_SECONDS,
            "SCAN_INTERVAL_SECONDS": self.SCAN_INTERVAL_SECONDS,
            "MAX_RETRIES": self.MAX_RETRIES,
            "RETRY_DELAY": self.RETRY_DELAY,
            "AUTO_UPLOAD": self.AUTO_UPLOAD,
            "MAX_CONCURRENT_UPLOADS": self.MAX_CONCURRENT_UPLOADS,
            "DEFAULT_PRIVACY_STATUS": self.DEFAULT_PRIVACY_STATUS,
            "ENFORCE_PRIVATE_UPLOAD": self.ENFORCE_PRIVATE_UPLOAD,
            "ALLOWED_VIDEO_EXTENSIONS": list(self.ALLOWED_VIDEO_EXTENSIONS),
            "REQUIRE_FFPROBE": self.REQUIRE_FFPROBE,
        }


# ---------------------------------------------------------------------------
# Laden
# ---------------------------------------------------------------------------


def _normalize_ext(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if not text.startswith("."):
        text = "." + text
    return text


def _coerce(raw: Any, type_hint: Any, name: str) -> Any:
    """JSON-/Env-Wert in den passenden Python-Typ ueberfuehren."""

    hint = str(type_hint)
    try:
        if isinstance(raw, Path):
            return Path(raw)
        if "tuple" in hint and isinstance(raw, (list, tuple)):
            return tuple(raw)
        if "tuple" in hint and isinstance(raw, str):
            return tuple(p.strip() for p in raw.split(",") if p.strip())
        if isinstance(raw, str) and "tuple" in hint:
            return (raw,)
        if "bool" in hint:
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in _BOOL_TRUE:
                return True
            if text in _BOOL_FALSE:
                return False
            return bool(raw)
        if "int" in hint and "Path" not in hint:
            return int(float(raw))
        if "float" in hint:
            return float(raw)
        if "Path" in hint:
            return Path(raw).expanduser()
        if "list" in hint and isinstance(raw, str):
            return [p.strip() for p in raw.split(",") if p.strip()]
        return raw
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Konfigurationswert {name}={raw!r} ist ungueltig: {exc}") from exc


def load_settings(
    config_path: str | Path | None = None,
    *,
    base_dir: str | Path | None = None,
    use_env: bool = True,
    create_if_missing: bool = False,
) -> Settings:
    """Konfiguration laden: Defaults -> config.json -> Umgebungsvariablen."""

    if base_dir:
        root = Path(base_dir).expanduser().resolve()
    elif config_path:
        # Wird eine Konfiguration ausserhalb des Projektordners angegeben,
        # gehoert das Projektverzeichnis dorthin, wo die Datei liegt.
        root = Path(config_path).expanduser().resolve().parent
    else:
        root = project_root()
    path = Path(config_path).expanduser() if config_path else root / CONFIG_FILE_NAME
    if not path.is_absolute():
        path = (root / path).resolve()

    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Konfigurationsdatei {path} ist kein gueltiges JSON: {exc.msg} "
                f"(Zeile {exc.lineno}, Spalte {exc.colno})"
            ) from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"Konfigurationsdatei {path} muss ein JSON-Objekt sein")
        data = loaded
    elif create_if_missing:
        defaults = Settings(BASE_DIR=root)
        defaults.ensure_directories()
        defaults.save(path)
        data = {}

    env_overrides = _collect_env_overrides() if use_env else {}
    data.update(env_overrides)

    settings = Settings.from_dict(data, base_dir=root)
    settings.config_path = path
    settings.config_file_exists = path.exists()
    return settings


def _collect_env_overrides() -> dict[str, Any]:
    known = {f.name for f in fields(Settings)}
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX) :].strip().upper()
        if name in known and value != "":
            out[name] = value
    return out


def get_settings() -> Settings:
    """Bequemer Zugriff auf die Standard-Konfiguration (einmalig geladen)."""

    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        _SETTINGS_CACHE = load_settings()
    return _SETTINGS_CACHE


def reset_settings_cache() -> None:
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None


def resolve_scopes(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Scope-Preset-Name ('default', 'upload_only', 'full') oder Liste aufloesen."""

    if value is None:
        return DEFAULT_SCOPES
    if isinstance(value, str):
        preset = value.strip().lower()
        if preset in SCOPE_PRESETS:
            return SCOPE_PRESETS[preset]
        return tuple(p.strip() for p in preset.split() if p.strip())
    return tuple(str(v).strip() for v in value if str(v).strip()) or DEFAULT_SCOPES


_SETTINGS_CACHE: Settings | None = None
