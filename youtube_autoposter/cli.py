"""Kommandozeile.

Wichtigste Aufrufe::

    python app.py                     Anwendung starten (Dashboard + Watcher)
    python app.py --dry-run           Umgebung + Episoden pruefen, OHNE Upload
    python app.py scan                READY-Ordner einmal durchsuchen
    python app.py upload              Queue einmal im Vordergrund abarbeiten
    python app.py status              Statistik ausgeben
    python app.py auth login          OAuth einmalig per Browser durchfuehren
    python app.py auth status         Verbindungsstatus anzeigen
    python app.py publish <episode>   Video oeffentlich schalten (mit Abfrage!)
    python app.py retry <episode>     Fehlgeschlagene Episode erneut einreihen
    python app.py recover <episode>   Nach abgebrochenem Upload auf YouTube suchen
    python app.py logs -n 100         Letzte Protokollzeilen
    python app.py init                Ordner + Datenbank + config.json anlegen

Es gibt bewusst keinen Befehl, der mehrere Videos ohne Rueckfrage
oeffentlich schaltet.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .constants import APP_NAME, __version__
from .core.context import AppContext, bootstrap
from .core.dry_run import run_dry_run
from .core.models import VideoRecord
from .errors import AutoPosterError, StateError
from .utils.files import format_local, human_size
from .utils.logging_utils import get_logger
from .utils.media import probe_available


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description=f"{APP_NAME} - lokales YouTube-Upload-System (V1)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Sicherheit:\n"
            "  * Jeder Upload erfolgt als PRIVATE.\n"
            "  * Oeffentlich wird ein Video nur durch den Button VEROEFFENTLICHEN\n"
            "    im Dashboard bzw. 'python app.py publish <episode>' mit Rueckfrage.\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--config", metavar="PFAD", help="Pfad zu config.json")
    parser.add_argument("--host", help=f"Bind-Adresse (Standard: {None}, siehe config.json)")
    parser.add_argument("--port", type=int, help="Port der Web-Oberflaeche")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Log-Level")
    parser.add_argument("--dry-run", action="store_true", help="Nur pruefen, niemals hochladen")
    parser.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch oeffnen")
    parser.add_argument("--no-watch", action="store_true", help="Watch-Folder deaktivieren")
    parser.add_argument("--no-worker", action="store_true", help="Upload-Worker nicht starten")
    parser.add_argument("--quiet", action="store_true", help="Keine Konsolen-Logs")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=[
            "run",
            "serve",
            "dry-run",
            "doctor",
            "scan",
            "upload",
            "validate",
            "status",
            "auth",
            "publish",
            "retry",
            "recover",
            "archive",
            "logs",
            "init",
            "config",
            "reset",
        ],
        help="Aktion (Standard: run)",
    )
    parser.add_argument("args", nargs="*", help="Argumente fuer die Aktion")

    parser.add_argument("--serve", action="store_true", help="Zusaetzlich zur --dry-run-Pruefung die UI starten")
    parser.add_argument("--limit", type=int, help="Maximale Anzahl Episoden (upload/logs)")
    parser.add_argument("--online", action="store_true", help="Verbindungstest gegen YouTube ausfuehren")
    parser.add_argument("--yes", action="store_true", help="Rueckfragen ueberspringen (publish/retry/reset)")
    parser.add_argument("--follow", action="store_true", help="Protokoll fortlaufend anzeigen")
    parser.add_argument("--write", action="store_true", help="config.json mit den aktuellen Werten schreiben")
    parser.add_argument("--target", help="Zielordner fuer 'archive' (READY, FAILED, UPLOADED_PRIVATE, ...)")
    parser.add_argument("--unlisted", action="store_true", help="publish: 'unlisted' statt 'public'")
    return parser


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def _print(text: str = "") -> None:
    print(text, flush=True)


def _kv_table(rows: Sequence[tuple[str, Any]], indent: int = 2) -> None:
    width = max((len(str(key)) for key, _value in rows), default=10)
    for key, value in rows:
        _print(f"{' ' * indent}{str(key).ljust(width)} : {value}")


def _apply_cli_overrides(ctx_settings: Any, args: argparse.Namespace) -> None:
    if args.log_level:
        ctx_settings.LOG_LEVEL = args.log_level
    if args.host:
        ctx_settings.HOST = args.host
    if args.port:
        ctx_settings.PORT = int(args.port)
    if args.no_watch:
        ctx_settings.WATCH_ENABLED = False
    if args.no_browser:
        ctx_settings.OPEN_BROWSER = False


def _make_context(args: argparse.Namespace, *, dry_run: bool = False) -> AppContext:
    from .config import load_settings

    settings = load_settings(args.config)
    _apply_cli_overrides(settings, args)
    return bootstrap(settings, dry_run=dry_run or args.dry_run, console_logging=not args.quiet)


def _find_episode(ctx: AppContext, identifier: str) -> VideoRecord | None:
    identifier = str(identifier or "").strip()
    if not identifier:
        return None
    if identifier.isdigit():
        row = ctx.repository.get(int(identifier))
        if row:
            return VideoRecord.from_row(row)
    row = ctx.repository.get_by_episode(identifier)
    if row:
        return VideoRecord.from_row(row)
    matches = ctx.repository.list_videos(search=identifier, limit=5)
    if len(matches) == 1:
        return VideoRecord.from_row(matches[0])
    if matches:
        _print(f"Mehrere Treffer fuer '{identifier}':")
        for match in matches:
            _print(f"  - {match['episode_id']} ({match['status']}) -> {match.get('title') or '-'}")
    return None


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------


def cmd_run(ctx: AppContext, args: argparse.Namespace) -> int:
    from .app import create_app, serve

    if args.no_worker and ctx.worker is None:
        pass
    app = create_app(ctx)
    serve(
        app,
        ctx,
        host=args.host,
        port=args.port,
        open_browser=False if args.no_browser else None,
        start_background=not args.no_worker,
    )
    return 0


def cmd_dry_run(ctx: AppContext, args: argparse.Namespace) -> int:
    report = run_dry_run(ctx, online_auth_check=args.online)
    _print(report.render())
    if args.serve:
        _print("\nDry-Run-Oberflaeche wird gestartet (keine Uploads moeglich) ...")
        return cmd_run(ctx, args)
    return 0 if report.ok else 1


def cmd_scan(ctx: AppContext, args: argparse.Namespace) -> int:
    result, report = ctx.scanner.scan_and_intake()
    _print(f"READY-Ordner: {ctx.settings.READY_FOLDER}")
    _print(f"Zeitpunkt:    {format_local(result.scanned_at)}")
    _print("")
    if not result.candidates:
        _print("Keine Dateien gefunden. Beispiel:")
        _print("  video_001.mp4 + video_001.json (+ optional video_001.jpg) in READY/ ablegen")
    for candidate in result.candidates:
        marker = "OK  " if candidate.ok else ("WAIT" if not candidate.stable and not candidate.errors else "FEHL")
        _print(f"[{marker}] {candidate.episode_id}")
        _print(f"        {candidate.summary()}")
    for error in result.errors:
        _print(f"[FEHL] {error}")
    _print("")
    _kv_table(
        [
            ("Neu aufgenommen", ", ".join(report.created) or "-"),
            ("Aktualisiert", ", ".join(report.updated) or "-"),
            ("Mit Fehlern", ", ".join(report.failed) or "-"),
            ("Wartet (kopiert)", ", ".join(report.waiting) or "-"),
            ("Bereits verarbeitet", ", ".join(report.blocked) or "-"),
        ]
    )
    if args.limit is None and ctx.worker is None:
        _print("\nHinweis: Im Vordergrund laeuft kein Worker. 'python app.py upload' startet die Queue.")
    return 0


def cmd_upload(ctx: AppContext, args: argparse.Namespace) -> int:
    if ctx.dry_run:
        _print("Dry-Run aktiv: Es wird nichts hochgeladen.")
        return cmd_dry_run(ctx, args)

    from .scheduler.watcher import UploadWorker

    ready, reason = ctx.youtube.is_ready()
    if not ready:
        _print(f"Upload nicht moeglich: {reason}")
        return 1

    worker = UploadWorker(ctx)
    _print("Upload-Queue wird abgearbeitet (1 Upload gleichzeitig, STRG+C zum Abbrechen) ...")
    outcomes = worker.process_all(max_items=args.limit)
    if not outcomes:
        _print("Queue ist leer - nichts zu tun.")
        return 0
    _print("")
    for outcome in outcomes:
        marker = "OK  " if outcome.success else ("SKIP" if outcome.skipped else "FEHL")
        _print(f"[{marker}] {outcome.episode_id} -> {outcome.status}: {outcome.message}")
        for warning in outcome.warnings:
            _print(f"        Hinweis: {warning}")
    return 0 if all(o.success or o.skipped for o in outcomes) else 1


def cmd_validate(ctx: AppContext, args: argparse.Namespace) -> int:
    from .metadata.parser import parse_metadata_file
    from .utils.validation import validate_episode

    target = Path(args.args[0]) if args.args else Path(ctx.settings.READY_FOLDER)
    if not target.exists():
        _print(f"Pfad nicht gefunden: {target}")
        return 1

    files = [target] if target.is_file() else sorted(p for p in target.iterdir() if p.is_file())
    candidates = ctx.scanner.discover_candidates(target if target.is_dir() else target.parent).candidates
    if candidates:
        failures = 0
        for candidate in candidates:
            metadata = None
            if candidate.metadata_path:
                try:
                    metadata = parse_metadata_file(candidate.metadata_path).metadata
                except AutoPosterError as exc:
                    _print(f"[FEHL] {candidate.episode_id}: {exc}")
                    failures += 1
                    continue
            result = validate_episode(
                metadata=metadata,
                video_path=candidate.video_path,
                thumbnail_path=candidate.thumbnail_path,
                settings=ctx.settings,
            )
            marker = "OK  " if result.ok else "FEHL"
            _print(f"[{marker}] {candidate.episode_id}: {result.summary}")
            if result.media_info and result.media_info.probe_ok:
                for line in result.media_info.summary_lines():
                    _print(f"        {line}")
            for issue in result.errors:
                _print(f"        FEHLER: {issue.message}")
                failures += 1
            for issue in result.warnings:
                _print(f"        Hinweis: {issue.message}")
        return 1 if failures else 0

    _print(f"Keine Episoden in {target} gefunden ({len(files)} Datei(en) gesehen).")
    return 0


def cmd_status(ctx: AppContext, args: argparse.Namespace) -> int:
    stats = ctx.repository.stats()
    auth = ctx.auth.status()
    probe_ok, probe_source = probe_available(ctx.settings)
    worker_paused = ctx.repository.get_bool_setting("worker.paused", False)

    _print(f"{APP_NAME} v{__version__}")
    _print("=" * 60)
    _kv_table(
        [
            ("Dashboard", ctx.settings.dashboard_url),
            ("READY-Ordner", ctx.settings.READY_FOLDER),
            ("Datenbank", ctx.settings.DATABASE_PATH),
            ("Protokoll", ctx.settings.log_file_path),
            ("YouTube-Verbindung", "CONNECTED" if auth.connected else f"NOT CONNECTED ({auth.state.value})"),
            ("Kanal", auth.channel_title or "-"),
            ("Token-Speicher", auth.token_storage),
            ("Scopes", ", ".join(auth.granted_scopes or auth.scopes)),
            ("ffprobe/ffmpeg", probe_source),
            ("Auto-Upload", "an" if ctx.settings.AUTO_UPLOAD and not worker_paused else "aus/pausiert"),
            ("Stabilitaetspruefung", f"{ctx.settings.FILE_STABILITY_SECONDS}s / {ctx.settings.STABILITY_REQUIRED_CHECKS} Messungen"),
            ("Retries", f"max. {ctx.settings.MAX_RETRIES}, Start {ctx.settings.RETRY_DELAY}s, Faktor {ctx.settings.RETRY_BACKOFF_FACTOR}"),
            ("Standard-Privacy", f"{ctx.settings.DEFAULT_PRIVACY_STATUS} (erzwungen)"),
        ]
    )
    _print("")
    _print("STATISTIK")
    _kv_table(
        [
            ("Gesamt", stats["total"]),
            ("Bereit", stats["ready"]),
            ("Upload laeuft", stats["uploading"]),
            ("Privat hochgeladen", stats["private"]),
            ("Veroeffentlicht", stats["published"]),
            ("Fehler", stats["failed"]),
            ("Dubletten uebersprungen", stats["skipped"]),
            ("Datenvolumen", human_size(stats["bytes_total"])),
        ]
    )
    quota = ctx.youtube.info().quota_today
    if quota:
        _print("")
        _print("QUOTA HEUTE")
        _kv_table(
            [
                ("Uploads", f"{quota.get('uploads', 0)} / {quota.get('upload_limit_per_day', 100)}"),
                ("Einheiten", quota.get("units", 0)),
                ("Aufrufe", json.dumps(quota.get("calls", {}), ensure_ascii=False)),
            ]
        )

    rows = ctx.repository.list_videos(limit=10, order="newest")
    if rows:
        _print("")
        _print("LETZTE EPISODEN")
        for row in rows:
            record = VideoRecord.from_row(row)
            if not record:
                continue
            _print(
                f"  [{record.status:>17}] {record.episode_id:<24} {record.display_title[:40]:<40} "
                f"{record.youtube_video_id or '-'}"
            )
    return 0


def cmd_auth(ctx: AppContext, args: argparse.Namespace) -> int:
    action = (args.args[0] if args.args else "status").lower()

    if action in {"login", "connect", "authorize"}:
        try:
            ctx.auth.login_via_local_server(port=0, open_browser=True)
        except AutoPosterError as exc:
            _print(f"FEHLER: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001
            _print(f"FEHLER: {exc}")
            return 1
        status = ctx.auth.status()
        _print(f"Verbunden: {status.message}")
        _print(f"Kanal:     {status.channel_title or '-'} ({status.channel_id or '-'})")
        _print(f"Token:     {ctx.auth.token_path()} ({status.token_storage})")
        return 0

    if action in {"test", "check"}:
        status = ctx.auth.test_connection()
        _print(f"Status:  {'CONNECTED' if status.connected else 'NOT CONNECTED'}")
        _print(f"Meldung: {status.message}")
        if status.channel_title:
            _print(f"Kanal:   {status.channel_title} ({status.channel_id})")
        return 0 if status.connected else 1

    if action in {"logout", "disconnect", "revoke"}:
        revoke = "--revoke" in args.args or args.yes
        result = ctx.auth.disconnect(revoke=revoke)
        _print("Verbindung getrennt." + (" Token wurde bei Google widerrufen." if result.get("revoked") else ""))
        if result.get("error"):
            _print(f"Hinweis: Widerruf fehlgeschlagen: {result['error']}")
        return 0

    status = ctx.auth.status()
    _kv_table(
        [
            ("Verbindung", "CONNECTED" if status.connected else f"NOT CONNECTED ({status.state.value})"),
            ("Meldung", status.message),
            ("Kanal", f"{status.channel_title or '-'} ({status.channel_id or '-'})"),
            ("credentials.json", "vorhanden" if status.client_secrets_present else f"FEHLT: {ctx.auth.client_secrets_path}"),
            ("Token", f"{'vorhanden' if status.token_present else 'keins'} ({status.token_storage})"),
            ("Refresh-Token", "ja" if status.has_refresh_token else "nein"),
            ("Scopes (gewuenscht)", ", ".join(status.scopes)),
            ("Scopes (erteilt)", ", ".join(status.granted_scopes) or "-"),
            ("Token gueltig bis", format_local(status.expires_at) if status.expires_at else "-"),
            ("Letzter Fehler", status.last_error or "-"),
        ]
    )
    if not status.connected:
        _print("\nNaechster Schritt: python app.py auth login   (oder im Dashboard auf 'Verbinden' klicken)")
    return 0 if status.connected else 1


def cmd_publish(ctx: AppContext, args: argparse.Namespace) -> int:
    if not args.args:
        _print("Benutzung: python app.py publish <episode_id> [--unlisted]")
        return 2
    record = _find_episode(ctx, args.args[0])
    if record is None:
        _print(f"Episode '{args.args[0]}' nicht gefunden")
        return 1
    if ctx.dry_run:
        _print("Dry-Run: Es wird nichts veroeffentlicht.")
        return 0

    target = "unlisted" if args.unlisted else "public"
    _print("")
    _print("!" * 70)
    _print(f"  Dieses Video wird jetzt {target.upper()} auf YouTube gestellt:")
    _print(f"    Episode : {record.episode_id}")
    _print(f"    Titel   : {record.display_title}")
    _print(f"    ID      : {record.youtube_video_id}")
    _print(f"    Status  : {record.status}")
    _print("!" * 70)
    if not args.yes:
        try:
            answer = input(f"  Wirklich {target} schalten? Zum Bestatigen '{record.episode_id}' eingeben: ")
        except (EOFError, KeyboardInterrupt):
            _print("\nAbgebrochen.")
            return 1
        if answer.strip() != record.episode_id:
            _print("Abgebrochen - Eingabe stimmte nicht ueberein.")
            return 1

    try:
        outcome = ctx.pipeline.publish(record.id, confirm=True, privacy_status=target)
    except (StateError, AutoPosterError) as exc:
        _print(f"FEHLER: {exc}")
        return 1
    if outcome.success:
        _print(f"VEROEFFENTLICHT: {outcome.youtube_url}")
        return 0
    _print(f"FEHLGESCHLAGEN: {outcome.message}")
    return 1


def cmd_retry(ctx: AppContext, args: argparse.Namespace) -> int:
    if not args.args:
        _print("Benutzung: python app.py retry <episode_id>")
        return 2
    record = _find_episode(ctx, args.args[0])
    if record is None:
        _print("Episode nicht gefunden")
        return 1
    try:
        outcome = ctx.pipeline.retry(record.id)
    except AutoPosterError as exc:
        _print(f"FEHLER: {exc}")
        return 1
    _print(outcome.message)
    return 0


def cmd_recover(ctx: AppContext, args: argparse.Namespace) -> int:
    if not args.args:
        _print("Benutzung: python app.py recover <episode_id>")
        return 2
    record = _find_episode(ctx, args.args[0])
    if record is None:
        _print("Episode nicht gefunden")
        return 1
    if ctx.dry_run:
        _print("Dry-Run: keine YouTube-Abfragen.")
        return 0
    outcome = ctx.pipeline.check_on_platform(record.id)
    _print(outcome.message)
    return 0 if outcome.success else 1


def cmd_archive(ctx: AppContext, args: argparse.Namespace) -> int:
    if not args.args:
        _print("Benutzung: python app.py archive <episode_id> [--target ORDNER]")
        return 2
    record = _find_episode(ctx, args.args[0])
    if record is None:
        _print("Episode nicht gefunden")
        return 1
    outcome = ctx.pipeline.archive(record.id, target=args.target)
    _print(outcome.message)
    return 0


def cmd_logs(ctx: AppContext, args: argparse.Namespace) -> int:
    limit = int(args.limit or 100)
    path = Path(ctx.settings.log_file_path)
    if args.follow:
        _print(f"Folge {path} (STRG+C zum Beenden)")
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, 2)
            try:
                while True:
                    line = handle.readline()
                    if not line:
                        time.sleep(0.4)
                        continue
                    _print(line.rstrip())
            except KeyboardInterrupt:
                return 0
    if not path.exists():
        _print(f"Keine Logdatei gefunden: {path}")
        return 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    for line in lines[-limit:]:
        _print(line.rstrip())
    return 0


def cmd_init(ctx: AppContext, args: argparse.Namespace) -> int:
    created = ctx.settings.ensure_directories()
    config_path = Path(getattr(ctx.settings, "config_path", Path(ctx.settings.BASE_DIR) / "config.json"))
    if not config_path.exists() or args.write:
        ctx.settings.save(config_path)
        _print(f"config.json geschrieben: {config_path}")
    else:
        _print(f"config.json vorhanden: {config_path}")
    _print(f"Neue Ordner angelegt: {', '.join(str(p) for p in created) or '(keine - alles vorhanden)'}")
    _print(f"Datenbank initialisiert: {ctx.settings.DATABASE_PATH}")
    _print(f"Integritaet: {ctx.db.integrity_check()}")
    _print(f"Tabellen: {json.dumps(ctx.db.table_counts())}")
    credentials = ctx.auth.client_secrets_path
    if credentials.exists():
        _print(f"credentials.json: vorhanden ({credentials})")
    else:
        _print(f"credentials.json: FEHLT -> {credentials}")
        _print("  Google Cloud Console -> OAuth-Client (Typ: Desktop-App) -> JSON herunterladen")
        _print("  und als credentials/credentials.json ablegen. Anleitung: README.md")
    return 0


def cmd_config(ctx: AppContext, args: argparse.Namespace) -> int:
    if args.write:
        path = ctx.settings.save()
        _print(f"config.json geschrieben: {path}")
        return 0
    _print(json.dumps(ctx.settings.describe(), indent=2, ensure_ascii=False))
    return 0


def cmd_reset(ctx: AppContext, args: argparse.Namespace) -> int:
    """Abgebrochene/unterbrochene Uploads zuruecksetzen (ohne Dateien zu loeschen)."""

    if not args.yes:
        _print("Dies setzt alle Episoden mit Status INTERRUPTED/FAILED zurueck auf READY.")
        answer = input("Fortfahren? [j/N] ")
        if answer.strip().lower() not in {"j", "ja", "y", "yes"}:
            _print("Abgebrochen.")
            return 1
    count = 0
    for row in ctx.repository.list_videos(statuses=["INTERRUPTED", "FAILED"]):
        ctx.repository.update(int(row["id"]), status="READY", error_message=None, needs_attention=0)
        count += 1
    _print(f"{count} Episode(n) zurueckgesetzt.")
    return 0


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------


COMMANDS = {
    "run": cmd_run,
    "serve": cmd_run,
    "dry-run": cmd_dry_run,
    "doctor": cmd_dry_run,
    "scan": cmd_scan,
    "upload": cmd_upload,
    "validate": cmd_validate,
    "status": cmd_status,
    "auth": cmd_auth,
    "publish": cmd_publish,
    "retry": cmd_retry,
    "recover": cmd_recover,
    "archive": cmd_archive,
    "logs": cmd_logs,
    "init": cmd_init,
    "config": cmd_config,
    "reset": cmd_reset,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    # "python app.py --dry-run" ohne Befehl -> Pruefbericht statt Server
    command = args.command or "run"
    if args.dry_run and command in {"run", "serve"} and not args.serve:
        command = "dry-run"

    try:
        ctx = _make_context(args, dry_run=args.dry_run)
    except AutoPosterError as exc:
        _print(f"Konfigurationsfehler: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        _print(f"Initialisierung fehlgeschlagen: {exc}")
        get_logger().exception("Bootstrap-Fehler")
        return 2

    handler = COMMANDS.get(command, cmd_run)
    try:
        return int(handler(ctx, args) or 0)
    except KeyboardInterrupt:
        _print("\nAbgebrochen (STRG+C).")
        return 130
    except AutoPosterError as exc:
        _print(f"FEHLER: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - die CLI darf nie mit Traceback sterben
        ctx.logger.exception("Unerwarteter Fehler: %s", exc)
        _print(f"Unerwarteter Fehler: {exc}")
        _print("Details stehen im Protokoll: " + str(ctx.settings.log_file_path))
        return 1
    finally:
        ctx.stop_background()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
