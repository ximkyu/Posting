"""Flask-Anwendung erstellen und lokal ausliefern.

Standard: ``http://127.0.0.1:8765`` (nur Loopback - die Anwendung steuert ein
Google-Konto und gehoert nicht ins Netz). Fuer Tests/Entwicklung kann mit
``--host 0.0.0.0`` gebunden werden; dann erscheint ausdruecklich eine Warnung.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from .constants import APP_NAME, __version__
from .core.context import AppContext, bootstrap
from .errors import AutoPosterError
from .ui.routes import create_blueprint
from .utils.logging_utils import redact

UI_DIR = Path(__file__).resolve().parent / "ui"


def create_app(ctx: AppContext | None = None, *, settings: Any = None, dry_run: bool = False) -> Flask:
    """App-Factory: Flask-Instanz inkl. Blueprint und Fehlerbehandlung."""

    context = ctx or bootstrap(settings, dry_run=dry_run)
    app = Flask(
        APP_NAME.replace(" ", "_"),
        template_folder=str(UI_DIR / "templates"),
        static_folder=str(UI_DIR / "static"),
        static_url_path="/app-static",
    )
    app.config["CTX"] = context
    # Nur lokale Sitzungen (CSRF-Token, OAuth-State) - kein Secret aus einer Datei
    app.secret_key = secrets.token_urlsafe(32)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 300
    app.config["TRAP_HTTP_EXCEPTIONS"] = False
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    app.register_blueprint(create_blueprint())

    @app.errorhandler(AutoPosterError)
    def _handle_app_error(exc: AutoPosterError) -> Any:
        context.logger.error("Anwendungsfehler: %s", redact(str(exc)))
        if request.path.startswith("/api/") or request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "message": str(exc)}), 400
        return render_template("error.html", active="", message=str(exc), title="Fehler"), 400

    @app.errorhandler(404)
    def _handle_404(_exc: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": "Nicht gefunden"}), 404
        return render_template("error.html", active="", message="Seite nicht gefunden", title="404"), 404

    @app.errorhandler(500)
    def _handle_500(exc: Any) -> Any:
        context.logger.exception("Interner Fehler: %s", exc)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": "Interner Fehler"}), 500
        return render_template(
            "error.html", active="", message="Interner Fehler - Details stehen im Protokoll.", title="500"
        ), 500

    @app.route("/version")
    def version() -> Any:
        return jsonify({"app": APP_NAME, "version": __version__, "dry_run": context.dry_run})

    return app


def _warn_if_public(host: str, logger: logging.Logger) -> None:
    if host in {"0.0.0.0", "::", ""}:
        logger.warning(
            "ACHTUNG: Die Oberflaeche ist an %s gebunden und damit im lokalen Netz erreichbar. "
            "Jeder mit Netzwerkzugriff koennte Videos veroeffentlichen. "
            "Fuer den normalen Betrieb HOST=127.0.0.1 in config.json verwenden.",
            host or "0.0.0.0",
        )


def serve(
    app: Flask | None = None,
    ctx: AppContext | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool | None = None,
    start_background: bool = True,
) -> None:
    """Anwendung starten und bis STRG+C laufen lassen."""

    context = ctx or (app.config["CTX"] if app else bootstrap())
    flask_app = app or create_app(context)
    settings = context.settings

    bind_host = host or settings.HOST
    bind_port = int(port or settings.PORT)
    _warn_if_public(bind_host, context.logger)

    if start_background:
        context.start_background()

    should_open = settings.OPEN_BROWSER if open_browser is None else open_browser
    if should_open and not context.dry_run:
        url = f"http://{'127.0.0.1' if bind_host in ('0.0.0.0', '::', '') else bind_host}:{bind_port}"
        threading.Timer(1.2, lambda: _open_browser_safe(url, context.logger)).start()

    banner = [
        "",
        "=" * 72,
        f"  {APP_NAME} v{__version__}",
        f"  Dashboard:   http://{'127.0.0.1' if bind_host in ('0.0.0.0', '::') else bind_host}:{bind_port}",
        f"  READY:       {settings.READY_FOLDER}",
        f"  Datenbank:   {settings.DATABASE_PATH}",
        f"  Protokoll:   {settings.log_file_path}",
        f"  Dry-Run:     {'JA - es wird nichts hochgeladen' if context.dry_run else 'nein'}",
        "",
        "  Jeder Upload erfolgt PRIVAT. Oeffentlich nur per Button 'VEROEFFENTLICHEN'.",
        "  Beenden mit STRG+C.",
        "=" * 72,
        "",
    ]
    text = "\n".join(banner)
    print(text, flush=True)
    context.logger.info("Web-Oberflaeche startet auf %s:%s", bind_host, bind_port)

    use_waitress = bool(settings.USE_WAITRESS)
    if use_waitress:
        try:
            from waitress import serve as waitress_serve
        except Exception:  # noqa: BLE001 - Flask-Dev-Server reicht ebenfalls
            use_waitress = False

    try:
        if use_waitress:
            context.logger.info("Server: waitress")
            waitress_serve(flask_app, host=bind_host, port=bind_port, threads=8, channel_timeout=300)
        else:
            context.logger.info("Server: Flask (Entwicklungs-Server)")
            flask_app.run(
                host=bind_host,
                port=bind_port,
                debug=bool(settings.DEBUG),
                use_reloader=False,
                threaded=True,
            )
    except KeyboardInterrupt:
        context.logger.info("STRG+C empfangen - Anwendung wird beendet")
    except OSError as exc:
        context.logger.error(
            "Web-Server konnte nicht starten (%s). Laeuft die Anwendung bereits, oder ist Port %s belegt?",
            exc,
            bind_port,
        )
        raise SystemExit(2) from exc
    finally:
        context.stop_background()
        context.logger.info("Hintergrund-Dienste gestoppt. Bis zum naechsten Mal.")


def _open_browser_safe(url: str, logger: logging.Logger) -> None:
    try:
        time.sleep(0.2)
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - kein Browser ist kein Fehler
        logger.debug("Browser konnte nicht geoeffnet werden: %s", exc)


def run(*, host: str | None = None, port: int | None = None, **kwargs: Any) -> None:
    """Kurzform: Anwendung bauen und starten."""

    ctx = bootstrap(**kwargs)
    serve(create_app(ctx), ctx, host=host, port=port)


__all__ = ["create_app", "serve", "run"]
