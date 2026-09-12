"""Dry-Run / System-Check.

``python app.py --dry-run`` prueft die komplette Umgebung und alle Episoden im
READY-Ordner, ohne auch nur einen einzigen YouTube-Aufruf zum Hochladen zu
machen. Beispiel-Ausgabe::

    SYSTEM
      [OK]    Python 3.12.1
      [OK]    Ordnerstruktur vollstaendig
      [WARN]  ffprobe nicht gefunden
      [FEHLER] credentials.json fehlt

    EPISODEN (READY)
      READY
      Episode 001 [OK]
      Episode 002 [OK]
      Episode 003 [FEHLER] Missing description
"""

from __future__ import annotations

import importlib
import platform
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import APP_NAME, __version__
from ..utils.files import format_duration, human_size, now_iso
from ..utils.media import probe_available
from ..utils.validation import validate_episode

LEVEL_OK = "OK"
LEVEL_WARN = "WARN"
LEVEL_ERROR = "FEHLER"

REQUIRED_MODULES = (
    ("flask", "Web-Oberflaeche"),
    ("googleapiclient", "YouTube Data API Client"),
    ("google.auth", "Google Authentifizierung"),
    ("google_auth_oauthlib", "OAuth 2.0 Flow"),
)

OPTIONAL_MODULES = (
    ("watchdog", "Dateisystem-Events (sonst Polling)"),
    ("waitress", "WSGI-Server fuer Windows (sonst Flask-Dev-Server)"),
    ("PIL", "Thumbnail-Pruefung (sonst Header-Check)"),
)


@dataclass
class CheckResult:
    name: str
    level: str
    message: str
    details: str = ""

    @property
    def ok(self) -> bool:
        return self.level != LEVEL_ERROR

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "level": self.level, "message": self.message, "details": self.details}


@dataclass
class EpisodeCheck:
    episode_id: str
    ok: bool
    message: str = ""
    title: str = ""
    video: str | None = None
    metadata: str | None = None
    thumbnail: str | None = None
    size_text: str = ""
    duration_text: str = ""
    resolution: str = ""
    codecs: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "ok": self.ok,
            "message": self.message,
            "title": self.title,
            "video": self.video,
            "metadata": self.metadata,
            "thumbnail": self.thumbnail,
            "size_text": self.size_text,
            "duration_text": self.duration_text,
            "resolution": self.resolution,
            "codecs": self.codecs,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "stable": self.stable,
        }


@dataclass
class DryRunReport:
    checks: list[CheckResult] = field(default_factory=list)
    episodes: list[EpisodeCheck] = field(default_factory=list)
    generated_at: str = field(default_factory=now_iso)
    upload_attempted: bool = False   # immer False - harte Garantie

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks) and all(ep.ok for ep in self.episodes)

    @property
    def errors(self) -> list[str]:
        return [c.message for c in self.checks if c.level == LEVEL_ERROR] + [
            f"{ep.episode_id}: {m}" for ep in self.episodes for m in ep.errors
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "generated_at": self.generated_at,
            "upload_attempted": self.upload_attempted,
            "checks": [c.to_dict() for c in self.checks],
            "episodes": [ep.to_dict() for ep in self.episodes],
            "errors": self.errors,
        }

    # ------------------------------------------------------------------

    def render(self) -> str:
        lines: list[str] = []
        lines.append(f"{APP_NAME} v{__version__} - DRY RUN (es wird NICHTS hochgeladen)")
        lines.append("=" * 72)
        lines.append("SYSTEM")
        for check in self.checks:
            marker = {"OK": "[OK]   ", "WARN": "[WARN] ", "FEHLER": "[FEHLER]"}[check.level]
            lines.append(f"  {marker} {check.name}: {check.message}")
            if check.details:
                for detail in check.details.splitlines():
                    lines.append(f"           {detail}")
        lines.append("")
        lines.append(f"EPISODEN IM READY-ORDNER ({len(self.episodes)})")
        if not self.episodes:
            lines.append("  (keine Dateien gefunden - READY/ ist leer)")
        else:
            lines.append("  " + ("READY" if all(ep.ok for ep in self.episodes) else "MIT FEHLERN"))
            for episode in self.episodes:
                marker = "[OK]" if episode.ok else "[FEHLER]"
                lines.append(f"  {marker} Episode {episode.episode_id}")
                if episode.title:
                    lines.append(f"        TITEL {episode.title}")
                if episode.resolution or episode.duration_text:
                    lines.append(
                        f"        VIDEO {episode.resolution} | {episode.duration_text} | "
                        f"{episode.codecs} | {episode.size_text}"
                    )
                for warning in episode.warnings:
                    lines.append(f"        Hinweis: {warning}")
                for error in episode.errors:
                    lines.append(f"        Fehler: {error}")
        lines.append("")
        lines.append("ERGEBNIS: " + ("ALLES BEREIT" if self.ok else "PROBLEME GEFUNDEN"))
        if not self.ok:
            lines.append("Bitte die obigen Punkte beheben und erneut pruefen.")
        lines.append("Upload versucht: NEIN (Dry-Run fuehrt niemals echte Uploads aus)")
        return "\n".join(lines)


def _check_python() -> CheckResult:
    version = platform.python_version()
    info = sys.version_info
    if info < (3, 11):
        return CheckResult(
            "Python",
            LEVEL_ERROR,
            f"Python {version} ist zu alt - benoetigt wird Python 3.11 oder neuer (empfohlen 3.12)",
        )
    return CheckResult("Python", LEVEL_OK, f"Python {version} ({sys.executable})")


def _check_modules() -> tuple[list[CheckResult], list[CheckResult]]:
    ok: list[CheckResult] = []
    problems: list[CheckResult] = []
    for module, purpose in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
            ok.append(CheckResult(f"Modul {module}", LEVEL_OK, purpose))
        except Exception as exc:  # noqa: BLE001
            problems.append(
                CheckResult(
                    f"Modul {module}",
                    LEVEL_ERROR,
                    f"{purpose} fehlt: {exc}",
                    "Abhilfe: pip install -r requirements.txt",
                )
            )
    optional: list[CheckResult] = []
    for module, purpose in OPTIONAL_MODULES:
        try:
            importlib.import_module(module)
            optional.append(CheckResult(f"Modul {module} (optional)", LEVEL_OK, purpose))
        except Exception:  # noqa: BLE001
            optional.append(CheckResult(f"Modul {module} (optional)", LEVEL_WARN, f"nicht installiert - {purpose}"))
    return ok + optional, problems


def _check_folders(ctx: Any) -> CheckResult:
    missing: list[str] = []
    unwritable: list[str] = []
    for name, folder in ctx.settings.workflow_folders.items():
        path = Path(folder)
        if not path.exists():
            missing.append(f"{name}: {path}")
            continue
        probe = path / ".write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            unwritable.append(f"{name}: {exc}")
    details = []
    if missing:
        details.append("fehlend: " + "; ".join(missing))
    if unwritable:
        details.append("nicht beschreibbar: " + "; ".join(unwritable))
    if unwritable:
        return CheckResult("Ordnerstruktur", LEVEL_ERROR, "Ordner fehlen oder sind nicht beschreibbar", "\n".join(details))
    if missing:
        return CheckResult("Ordnerstruktur", LEVEL_WARN, "Ordner werden beim Start automatisch angelegt", "\n".join(details))
    return CheckResult("Ordnerstruktur", LEVEL_OK, "alle Ordner vorhanden und beschreibbar", "\n".join(details))


def _check_database(ctx: Any) -> CheckResult:
    try:
        integrity = ctx.db.integrity_check()
        counts = ctx.db.table_counts()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Datenbank", LEVEL_ERROR, f"Zugriff fehlgeschlagen: {exc}")
    if integrity != "ok":
        return CheckResult("Datenbank", LEVEL_ERROR, f"Integritaetspruefung: {integrity}")
    return CheckResult(
        "Datenbank",
        LEVEL_OK,
        f"{ctx.settings.DATABASE_PATH.name} (Videos: {counts.get('videos', 0)}, "
        f"Uploads: {counts.get('uploads', 0)}, Logs: {counts.get('logs', 0)})",
    )


def _check_credentials(ctx: Any) -> CheckResult:
    auth = ctx.auth
    if not auth.has_client_secrets():
        return CheckResult(
            "credentials.json",
            LEVEL_ERROR,
            f"fehlt: {auth.client_secrets_path}",
            "Google Cloud Console -> OAuth-Client (Typ: Desktop-App) erstellen,\n"
            "JSON herunterladen und als credentials/credentials.json ablegen.\n"
            "Anleitung: README.md bzw. http://127.0.0.1:8765/setup",
        )
    try:
        info = auth.load_client_config()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("credentials.json", LEVEL_ERROR, f"ungueltig: {exc}")
    client_type = info.get("client_type")
    level = LEVEL_OK if client_type == "installed" else LEVEL_WARN
    message = f"vorhanden (Client-Typ: {client_type})"
    details = "" if client_type == "installed" else (
        "Empfohlen wird ein OAuth-Client vom Typ 'Desktop-App' (installed).\n"
        "Bei 'web' muss die Redirect-URI exakt in der Google Cloud Console hinterlegt sein:\n"
        f"{ctx.settings.oauth_redirect_uri}"
    )
    return CheckResult("credentials.json", level, message, details)


def _check_auth(ctx: Any, *, online: bool) -> CheckResult:
    status = ctx.auth.status()
    if not status.token_present:
        return CheckResult(
            "OAuth-Verbindung",
            LEVEL_WARN,
            "noch nicht verbunden",
            "Nach dem Start im Dashboard auf 'Google-/YouTube-Konto verbinden' klicken\n"
            "oder 'python app.py auth login' ausfuehren.",
        )
    if status.state.value == "NEEDS_REAUTH":
        return CheckResult("OAuth-Verbindung", LEVEL_ERROR, status.message)
    if online:
        tested = ctx.auth.test_connection()
        if not tested.connected:
            return CheckResult("OAuth-Verbindung", LEVEL_ERROR, tested.message, "Bitte im Dashboard erneut verbinden.")
        return CheckResult(
            "OAuth-Verbindung",
            LEVEL_OK,
            f"verbunden (Kanal: {tested.channel_title or 'unbekannt'}, ID: {tested.channel_id or '-'})",
            f"Scopes: {', '.join(tested.granted_scopes or tested.scopes)}",
        )
    level = LEVEL_OK if status.connected else LEVEL_WARN
    return CheckResult(
        "OAuth-Verbindung",
        level,
        status.message or status.state.value,
        f"Scopes: {', '.join(status.granted_scopes or status.scopes)}\n"
        f"Token-Speicher: {status.token_storage}",
    )


def _check_media_tools(ctx: Any) -> CheckResult:
    available, source = probe_available(ctx.settings)
    if available:
        return CheckResult("ffprobe/ffmpeg", LEVEL_OK, f"verfuegbar ({source})")
    level = LEVEL_ERROR if ctx.settings.REQUIRE_FFPROBE else LEVEL_WARN
    return CheckResult(
        "ffprobe/ffmpeg",
        level,
        "nicht gefunden - technische Video-Pruefung (Aufloesung/Dauer/Codec) entfaellt",
        "Windows: https://ffmpeg.org/download.html -> Build herunterladen,\n"
        "z. B. nach C:\\ffmpeg entpacken und C:\\ffmpeg\\bin zum PATH hinzufuegen\n"
        "(oder FFPROBE_PATH in config.json setzen).",
    )


def _check_port(ctx: Any) -> CheckResult:
    host = ctx.settings.HOST
    port = int(ctx.settings.PORT)
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        try:
            sock.bind((probe_host, port))
        except OSError as exc:
            return CheckResult(
                "Web-Port",
                LEVEL_WARN,
                f"Port {port} ist belegt ({exc})",
                "Anderen Port in config.json setzen (PORT) oder die laufende Instanz beenden.",
            )
    return CheckResult("Web-Port", LEVEL_OK, f"{host}:{port} ist frei -> {ctx.settings.dashboard_url}")


def _check_config(ctx: Any) -> CheckResult:
    cfg = ctx.settings
    details = [
        f"Konfigurationsdatei: {getattr(cfg, 'config_path', '(Defaults)')}",
        f"Bereitgestellt von: {getattr(cfg, 'config_file_exists', False) and 'config.json' or 'Standardwerten'}",
        f"DEFAULT_PRIVACY_STATUS: {cfg.DEFAULT_PRIVACY_STATUS} (erzwungen, nicht ueberschreibbar)",
        f"Stabilitaetspruefung: {cfg.FILE_STABILITY_SECONDS}s / {cfg.STABILITY_REQUIRED_CHECKS} Messungen",
        f"Retries: max. {cfg.MAX_RETRIES}, Startverzögerung {cfg.RETRY_DELAY}s, Faktor {cfg.RETRY_BACKOFF_FACTOR}",
        f"Scopes: {', '.join(cfg.OAUTH_SCOPES)}",
    ]
    ignored = getattr(cfg, "ignored_config_keys", []) or []
    if ignored:
        details.append("Ignorierte (unbekannte) Schluessel: " + ", ".join(ignored))
    return CheckResult("Konfiguration", LEVEL_OK, "geladen", "\n".join(details))


def _check_episode(
    ctx: Any,
    candidate: Any,
    *,
    run_probe: bool = True,
) -> EpisodeCheck:
    episode = EpisodeCheck(
        episode_id=candidate.episode_id,
        ok=True,
        video=str(candidate.video_path) if candidate.video_path else None,
        metadata=str(candidate.metadata_path) if candidate.metadata_path else None,
        thumbnail=str(candidate.thumbnail_path) if candidate.thumbnail_path else None,
        size_text=human_size(candidate.size) if candidate.size else "-",
        stable=candidate.stable,
    )
    episode.errors.extend(candidate.errors)
    episode.warnings.extend(candidate.warnings)
    if not candidate.stable:
        episode.warnings.append(f"Datei noch nicht stabil: {candidate.stability_reason}")

    metadata = None
    if candidate.metadata_path:
        try:
            from ..metadata.parser import parse_metadata_file

            parsed = parse_metadata_file(candidate.metadata_path)
            metadata = parsed.metadata
            episode.title = metadata.title or ""
            episode.warnings.extend(str(w.message) for w in parsed.warnings)
            episode.errors.extend(parsed.error_messages())
        except Exception as exc:  # noqa: BLE001
            episode.errors.append(f"JSON fehlerhaft: {exc}")
    elif ctx.settings.REQUIRE_METADATA_FILE:
        episode.errors.append("Metadaten-Datei (.json) fehlt")

    result = validate_episode(
        metadata=metadata,
        video_path=candidate.video_path,
        thumbnail_path=candidate.thumbnail_path,
        settings=ctx.settings,
        run_probe=run_probe,
    )
    episode.errors.extend(result.error_messages())
    episode.warnings.extend(w.message for w in result.warnings)
    if result.media_info and result.media_info.probe_ok:
        episode.duration_text = result.media_info.duration_text
        episode.resolution = result.media_info.resolution
        episode.codecs = result.media_info.codec_text
        episode.size_text = result.media_info.size_text
        if result.media_info.duration_seconds:
            episode.duration_text = format_duration(result.media_info.duration_seconds)
    elif result.media_info and result.media_info.probe_error:
        episode.warnings.append(result.media_info.probe_error)

    # Dubletten-Hinweis (rein lokal, ohne API)
    existing = ctx.repository.get_by_episode(candidate.episode_id)
    if existing and existing.get("status") in {"UPLOADED_PRIVATE", "PUBLISH_READY", "PUBLISHING", "PUBLISHED"}:
        episode.warnings.append(
            f"Bereits hochgeladen (Status {existing.get('status')}, YouTube-ID "
            f"{existing.get('youtube_video_id')}) - wird nicht erneut hochgeladen"
        )
    if existing and existing.get("file_hash") and candidate.video_path:
        duplicates = ctx.repository.find_uploaded_by_hash(
            str(existing["file_hash"]), exclude_id=int(existing["id"])
        )
        if duplicates:
            episode.warnings.append(
                f"Identische Datei wurde bereits als '{duplicates.get('episode_id')}' hochgeladen"
            )

    episode.ok = not episode.errors
    episode.message = "bereit" if episode.ok else "; ".join(episode.errors[:3])
    return episode


def run_dry_run(
    ctx: Any,
    *,
    root: str | Path | None = None,
    online_auth_check: bool = False,
    run_probe: bool = True,
) -> DryRunReport:
    """Kompletter Trockenlauf: Umgebung + alle Episoden, ohne Uploads."""

    report = DryRunReport()
    report.checks.append(_check_python())
    module_results, module_problems = _check_modules()
    report.checks.extend(module_results)
    report.checks.extend(module_problems)
    report.checks.append(_check_config(ctx))
    report.checks.append(_check_folders(ctx))
    report.checks.append(_check_database(ctx))
    report.checks.append(_check_credentials(ctx))
    report.checks.append(_check_auth(ctx, online=online_auth_check))
    report.checks.append(_check_media_tools(ctx))
    report.checks.append(_check_port(ctx))

    scan = ctx.scanner.discover_candidates(root)
    for error in scan.errors:
        report.checks.append(CheckResult("READY-Ordner", LEVEL_ERROR, error))
    for candidate in scan.candidates:
        report.episodes.append(_check_episode(ctx, candidate, run_probe=run_probe))

    report.upload_attempted = False
    return report


__all__ = [
    "CheckResult",
    "DryRunReport",
    "EpisodeCheck",
    "LEVEL_ERROR",
    "LEVEL_OK",
    "LEVEL_WARN",
    "run_dry_run",
]
