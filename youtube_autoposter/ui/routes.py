"""Flask-Routen der lokalen Weboberflaeche.

Grundsaetze:

* Das Dashboard liest **ausschliesslich die lokale SQLite-Datenbank** - pro
  Seitenaufruf wird KEIN YouTube-API-Request ausgeloest (Quota-Schutz).
* YouTube-Aufrufe passieren nur durch explizite Benutzeraktionen
  (VEROEFFENTLICHEN, Verbindung testen, Status pruefen, Upload-Abgleich).
* Alle veraendernden Aktionen sind POST + CSRF-Token; das Veroeffentlichen
  verlangt zusaetzlich ``confirm=yes``.
"""

from __future__ import annotations

import logging
import secrets
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from ..constants import STATUS_LABELS, VideoStatus
from ..core.models import PUBLISHABLE_STATUSES, RETRYABLE_STATUSES, VideoRecord
from ..database.repository import load_json
from ..errors import AuthError, AutoPosterError, PlatformError, StateError
from ..utils.files import format_local, human_size
from ..utils.logging_utils import redact
from ..utils.media import probe_available

PLACEHOLDER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">
<rect width="320" height="180" rx="10" fill="#1f2430"/>
<g fill="none" stroke="#5c6784" stroke-width="6">
  <rect x="96" y="60" width="128" height="72" rx="8"/>
  <circle cx="160" cy="96" r="18"/>
</g>
<text x="160" y="156" font-family="Segoe UI, Arial" font-size="13" fill="#8d97b3" text-anchor="middle">kein Vorschaubild</text>
</svg>
"""


def get_ctx() -> Any:
    return current_app.config["CTX"]


def wants_json() -> bool:
    if request.method == "POST":
        if request.form.get("format") == "json":
            return True
        if request.headers.get("X-Requested-With") == "fetch":
            return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def action_response(
    *,
    ok: bool,
    message: str,
    redirect_to: str | None = None,
    level: str = "info",
    data: dict[str, Any] | None = None,
) -> Response:
    """Einheitliche Antwort fuer Buttons: JSON fuer fetch(), sonst Redirect."""

    if wants_json():
        payload: dict[str, Any] = {"ok": ok, "message": message, "level": level}
        if data:
            payload.update(data)
        status = 200 if ok else 400
        return jsonify(payload), status  # type: ignore[return-value]
    flash(message, level if level in {"info", "success", "warning", "error"} else "info")
    return redirect(redirect_to or request.referrer or url_for("ui.dashboard"))


def require_csrf(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        expected = session.get("csrf_token")
        if not expected or not token or not secrets.compare_digest(str(token), str(expected)):
            return action_response(
                ok=False,
                message="Sitzung ist abgelaufen (CSRF-Token ungueltig). Bitte die Seite neu laden.",
                level="error",
            )
        return view(*args, **kwargs)

    return wrapper


def _video_or_404(video_id: int) -> dict[str, Any]:
    ctx = get_ctx()
    row = ctx.repository.get(video_id)
    if not row:
        abort(404)
    return row


def create_blueprint() -> Blueprint:
    bp = Blueprint(
        "ui",
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )

    # ------------------------------------------------------------------
    # Hilfsdaten fuer die Templates
    # ------------------------------------------------------------------

    @bp.before_request
    def _ensure_csrf() -> None:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(24)

    @bp.context_processor
    def _inject_globals() -> dict[str, Any]:
        ctx = get_ctx()
        auth_status = ctx.auth.status()
        return {
            "ctx": ctx,
            "settings": ctx.settings,
            "app_name": ctx.settings.APP_NAME,
            "auth": auth_status,
            "auth_dict": auth_status.to_dict(),
            "status_labels": STATUS_LABELS,
            "csrf_token": session.get("csrf_token", ""),
            "platform_infos": [info.to_dict() for info in ctx.registry.infos()],
            "probe": probe_available(ctx.settings),
            "publishable": PUBLISHABLE_STATUSES,
            "retryable": RETRYABLE_STATUSES,
            "now_local": lambda value: format_local(value),
            "human_size": human_size,
        }

    def _filtered_videos() -> tuple[list[VideoRecord], str, str]:
        ctx = get_ctx()
        filter_name = (request.args.get("filter") or "all").lower()
        search = (request.args.get("q") or "").strip()
        order = (request.args.get("order") or "newest").lower()
        mapping = {
            "all": None,
            "ready": [VideoStatus.NEW.value, VideoStatus.READY.value, VideoStatus.PAUSED.value],
            "running": [VideoStatus.VALIDATING.value, VideoStatus.UPLOADING.value, VideoStatus.PUBLISHING.value],
            "private": [VideoStatus.UPLOADED_PRIVATE.value, VideoStatus.PUBLISH_READY.value],
            "published": [VideoStatus.PUBLISHED.value],
            "failed": [VideoStatus.FAILED.value, VideoStatus.INTERRUPTED.value],
            "attention": None,
            "skipped": [VideoStatus.SKIPPED_DUPLICATE.value],
        }
        statuses = mapping.get(filter_name, None)
        rows = ctx.repository.list_videos(
            statuses=statuses, search=search or None, order=order, limit=500
        )
        if filter_name == "attention":
            rows = [row for row in rows if row.get("needs_attention")]
        records = [record for record in (VideoRecord.from_row(row) for row in rows) if record]
        return records, filter_name, search

    # ------------------------------------------------------------------
    # Seiten
    # ------------------------------------------------------------------

    @bp.route("/")
    def dashboard() -> str:
        ctx = get_ctx()
        records, filter_name, search = _filtered_videos()
        stats = ctx.repository.stats()
        return render_template(
            "index.html",
            active="dashboard",
            records=records,
            stats=stats,
            filter_name=filter_name,
            search=search,
            worker_state=ctx.worker.state.to_dict() if ctx.worker else {"running": False},
            watcher_state=ctx.watcher.state() if ctx.watcher else {"running": False},
            paused=ctx.repository.get_bool_setting("worker.paused", False),
            quota=ctx.youtube.info().quota_today if hasattr(ctx.youtube, "info") else {},
            recovery=load_json(ctx.repository.get_setting("recovery.last_findings"), None),
        )

    @bp.route("/videos/<int:video_id>")
    def video_detail(video_id: int) -> str:
        ctx = get_ctx()
        row = _video_or_404(video_id)
        record = VideoRecord.from_row(row)
        attempts = ctx.repository.list_attempts(video_id)
        logs = ctx.repository.list_logs(limit=200, episode_id=record.episode_id if record else None)
        validation = record.validation if record else {}
        return render_template(
            "video.html",
            active="dashboard",
            record=record,
            attempts=attempts,
            logs=logs,
            validation=validation,
            media_info=record.media_info if record else {},
            thumbnail_info=record.thumbnail_info if record else {},
            metadata=record.metadata if record else {},
        )

    @bp.route("/setup")
    def setup_page() -> str:
        ctx = get_ctx()
        return render_template(
            "setup.html",
            active="setup",
            config_description=ctx.settings.describe(),
            scopes=list(ctx.settings.OAUTH_SCOPES),
            redirect_uri=ctx.settings.oauth_redirect_uri,
            settings_rows=ctx.repository.all_settings(),
            database_counts=ctx.db.table_counts(),
            integrity=ctx.db.integrity_check(),
        )

    @bp.route("/logs")
    def logs_page() -> str:
        ctx = get_ctx()
        level = (request.args.get("level") or "").upper()
        limit = min(1000, max(20, int(request.args.get("limit") or 200)))
        logs = ctx.repository.list_logs(limit=limit, level=level or None)
        return render_template(
            "logs.html",
            active="logs",
            logs=logs,
            level=level,
            limit=limit,
            log_file=str(ctx.settings.log_file_path),
        )

    @bp.route("/queue")
    def queue_page() -> str:
        ctx = get_ctx()
        rows = ctx.repository.list_videos(
            statuses=[
                VideoStatus.NEW.value,
                VideoStatus.READY.value,
                VideoStatus.PAUSED.value,
                VideoStatus.VALIDATING.value,
                VideoStatus.UPLOADING.value,
            ],
            order="oldest",
            limit=500,
        )
        return render_template(
            "queue.html",
            active="queue",
            records=[r for r in (VideoRecord.from_row(row) for row in rows) if r],
            worker_state=ctx.worker.state.to_dict() if ctx.worker else {"running": False},
            paused=ctx.repository.get_bool_setting("worker.paused", False),
        )

    # ------------------------------------------------------------------
    # JSON-API (fuer Auto-Refresh im Dashboard)
    # ------------------------------------------------------------------

    @bp.route("/health")
    def health() -> Response:
        ctx = get_ctx()
        auth = ctx.auth.status()
        return jsonify(
            {
                "status": "ok",
                "app": ctx.settings.APP_NAME,
                "version": ctx.version,
                "dry_run": ctx.dry_run,
                "youtube_connected": auth.connected,
                "database": ctx.db.table_counts(),
            }
        )

    @bp.route("/api/status")
    def api_status() -> Response:
        ctx = get_ctx()
        payload = ctx.to_dict()
        payload["records"] = []
        return jsonify(payload)

    @bp.route("/api/videos")
    def api_videos() -> Response:
        ctx = get_ctx()
        records, filter_name, search = _filtered_videos()
        return jsonify(
            {
                "filter": filter_name,
                "search": search,
                "stats": ctx.repository.stats(),
                "videos": [record.to_dict() for record in records],
            }
        )

    @bp.route("/api/videos/<int:video_id>")
    def api_video(video_id: int) -> Response:
        ctx = get_ctx()
        row = _video_or_404(video_id)
        record = VideoRecord.from_row(row)
        return jsonify(record.to_dict() if record else {})

    @bp.route("/thumb/<int:video_id>")
    def thumbnail(video_id: int) -> Any:
        ctx = get_ctx()
        row = _video_or_404(video_id)
        record = VideoRecord.from_row(row)
        if record and record.thumbnail_path:
            path = Path(record.thumbnail_path)
            if path.exists() and path.is_file():
                return send_file(path, conditional=True, max_age=600)
        if (
            ctx.settings.USE_REMOTE_THUMBNAIL_FALLBACK
            and record
            and record.thumbnail_url
        ):
            # Von YouTube per API gelieferter Thumbnail-Link (kein Scraping)
            return redirect(record.thumbnail_url, code=302)
        return Response(PLACEHOLDER_SVG, mimetype="image/svg+xml")

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    @bp.route("/auth/start")
    def auth_start() -> Any:
        ctx = get_ctx()
        try:
            url, state = ctx.auth.start_flow(redirect_uri=ctx.settings.oauth_redirect_uri)
        except AutoPosterError as exc:
            return action_response(ok=False, message=str(exc), level="error", redirect_to=url_for("ui.setup_page"))
        except Exception as exc:  # noqa: BLE001
            return action_response(
                ok=False,
                message=f"OAuth-Flow konnte nicht gestartet werden: {redact(str(exc))}",
                level="error",
                redirect_to=url_for("ui.setup_page"),
            )
        session["oauth_state"] = state
        return redirect(url, code=302)

    @bp.route("/auth/callback")
    def auth_callback() -> Any:
        ctx = get_ctx()
        error = request.args.get("error")
        if error:
            description = request.args.get("error_description") or ""
            return action_response(
                ok=False,
                message=f"Google hat die Autorisierung abgebrochen: {error} {description}".strip(),
                level="error",
                redirect_to=url_for("ui.setup_page"),
            )
        code = request.args.get("code")
        state = request.args.get("state")
        expected = session.pop("oauth_state", None)
        if not code or not state:
            return action_response(
                ok=False,
                message="Unvollstaendige OAuth-Antwort von Google.",
                level="error",
                redirect_to=url_for("ui.setup_page"),
            )
        if expected and expected != state:
            return action_response(
                ok=False,
                message="OAuth-State stimmt nicht ueberein - bitte erneut verbinden.",
                level="error",
                redirect_to=url_for("ui.setup_page"),
            )
        try:
            ctx.auth.complete_flow(code, state=state, redirect_uri=ctx.settings.oauth_redirect_uri)
        except AutoPosterError as exc:
            return action_response(
                ok=False,
                message=f"Verbindung fehlgeschlagen: {exc}",
                level="error",
                redirect_to=url_for("ui.setup_page"),
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("youtube_autoposter").exception("OAuth-Fehler")
            return action_response(
                ok=False,
                message=f"Verbindung fehlgeschlagen: {redact(str(exc))}",
                level="error",
                redirect_to=url_for("ui.setup_page"),
            )

        status = ctx.auth.status()
        message = (
            f"YouTube verbunden: {status.channel_title or status.message}."
            " Hochgeladene Videos bleiben privat, bis du sie per Button veroeffentlichst."
        )
        ctx.logger.info(message)
        # Pausierte Episoden (warteten auf die Verbindung) wieder freigeben
        try:
            resumed = 0
            for row in ctx.repository.list_videos(statuses=[VideoStatus.PAUSED.value]):
                ctx.repository.update(
                    int(row["id"]), status=VideoStatus.READY.value, error_message=None, needs_attention=0
                )
                resumed += 1
            if resumed:
                message += f" {resumed} pausierte Episode(n) wurden wieder in die Queue gestellt."
        except Exception:  # noqa: BLE001
            pass
        if ctx.worker:
            ctx.worker.wake()
        if ctx.watcher:
            ctx.watcher.notify()
        return action_response(
            ok=True, message=message, level="success", redirect_to=url_for("ui.dashboard")
        )

    @bp.route("/auth/test", methods=["POST"])
    @require_csrf
    def auth_test() -> Any:
        ctx = get_ctx()
        try:
            status = ctx.auth.test_connection()
        except Exception as exc:  # noqa: BLE001
            return action_response(ok=False, message=f"Verbindungstest fehlgeschlagen: {redact(str(exc))}", level="error")
        level = "success" if status.connected else "error"
        return action_response(
            ok=status.connected,
            message=status.message,
            level=level,
            data={"auth": status.to_dict()},
        )

    @bp.route("/auth/disconnect", methods=["POST"])
    @require_csrf
    def auth_disconnect() -> Any:
        ctx = get_ctx()
        revoke = request.form.get("revoke") in {"1", "true", "yes", "on"}
        result = ctx.auth.disconnect(revoke=revoke)
        message = "YouTube-Verbindung getrennt (Token lokal geloescht)."
        if result.get("revoked"):
            message = "YouTube-Verbindung getrennt und Token bei Google widerrufen."
        if result.get("error"):
            message += f" Widerruf fehlgeschlagen: {result['error']}"
        return action_response(ok=True, message=message, level="warning")

    # ------------------------------------------------------------------
    # Aktionen
    # ------------------------------------------------------------------

    @bp.route("/actions/scan", methods=["POST"])
    @require_csrf
    def action_scan() -> Any:
        ctx = get_ctx()
        report = ctx.trigger_scan()
        created = len(report.get("created") or [])
        failed = len(report.get("failed") or [])
        waiting = len(report.get("waiting") or [])
        blocked = len(report.get("blocked") or [])
        message = (
            f"Scan abgeschlossen: {created} neu, {failed} mit Fehlern, "
            f"{waiting} warten (Datei noch nicht stabil), {blocked} bereits verarbeitet."
        )
        level = "warning" if failed else "success"
        return action_response(ok=True, message=message, level=level, data={"report": report})

    @bp.route("/actions/upload", methods=["POST"])
    @require_csrf
    def action_upload() -> Any:
        ctx = get_ctx()
        if ctx.dry_run:
            return action_response(
                ok=False,
                message="Dry-Run aktiv: Es werden keine Uploads ausgefuehrt.",
                level="warning",
            )
        if ctx.repository.get_bool_setting("worker.paused", False):
            return action_response(
                ok=False,
                message=(
                    "Uploads sind pausiert. Bitte zuerst 'Fortsetzen' waehlen - "
                    "erst danach wird etwas hochgeladen (immer privat)."
                ),
                level="warning",
            )
        if ctx.worker is None:
            outcomes = ctx.pipeline.queue_all_ready()
            return action_response(
                ok=True,
                message=f"Worker laeuft nicht im Hintergrund - {outcomes} Episode(n) in die Queue gestellt.",
                level="info",
            )
        started = ctx.worker.process_all_async()
        if not started:
            return action_response(
                ok=False,
                message="Es laeuft bereits ein Upload-Durchlauf.",
                level="warning",
            )
        queued = ctx.repository.stats()["queued"]
        return action_response(
            ok=True,
            message=f"Upload-Queue gestartet ({queued} Episode(n) warten). Hochgeladen wird immer privat.",
            level="success",
        )

    @bp.route("/actions/pause", methods=["POST"])
    @require_csrf
    def action_pause() -> Any:
        ctx = get_ctx()
        paused = request.form.get("paused", "1") in {"1", "true", "yes", "on"}
        if ctx.worker:
            ctx.worker.set_paused(paused)
        else:
            ctx.repository.set_setting("worker.paused", "1" if paused else "0")
        return action_response(
            ok=True,
            message="Automatische Uploads pausiert." if paused else "Automatische Uploads fortgesetzt.",
            level="warning" if paused else "success",
        )

    # ------------------------------------------------------------------
    # Episoden-Aktionen
    # ------------------------------------------------------------------

    @bp.route("/videos/<int:video_id>/publish", methods=["POST"])
    @require_csrf
    def video_publish(video_id: int) -> Any:
        ctx = get_ctx()
        row = _video_or_404(video_id)
        confirm = str(request.form.get("confirm", "")).lower()
        expected_status = request.form.get("expected_status") or ""

        if confirm not in {"yes", "true", "1", "ja"}:
            return action_response(
                ok=False,
                message="Veroeffentlichen wurde nicht bestaetigt - es wurde nichts geaendert.",
                level="warning",
            )
        if expected_status and row.get("status") != expected_status:
            return action_response(
                ok=False,
                message=(
                    f"Status hat sich geaendert (erwartet {expected_status}, aktuell {row.get('status')}). "
                    "Bitte Seite neu laden."
                ),
                level="warning",
            )
        if ctx.dry_run:
            return action_response(ok=False, message="Dry-Run: Es wird nichts veroeffentlicht.", level="warning")

        try:
            outcome = ctx.pipeline.publish(video_id, confirm=True)
        except StateError as exc:
            return action_response(ok=False, message=str(exc), level="warning")
        except AuthError as exc:
            return action_response(ok=False, message=f"YouTube-Autorisierung noetig: {exc}", level="error")
        except PlatformError as exc:
            return action_response(ok=False, message=redact(str(exc)), level="error")
        except AutoPosterError as exc:
            return action_response(ok=False, message=str(exc), level="error")
        except Exception as exc:  # noqa: BLE001
            ctx.logger.exception("Veroeffentlichen fehlgeschlagen")
            return action_response(ok=False, message=f"Unerwarteter Fehler: {redact(str(exc))}", level="error")

        if outcome.success:
            return action_response(
                ok=True,
                message=f"VEROEFFENTLICHT: {outcome.youtube_url or outcome.message}",
                level="success",
                data=outcome.to_dict(),
                redirect_to=url_for("ui.video_detail", video_id=video_id),
            )
        return action_response(
            ok=False,
            message=outcome.message or "Veroeffentlichen fehlgeschlagen",
            level="error",
            data=outcome.to_dict(),
        )

    @bp.route("/videos/<int:video_id>/retry", methods=["POST"])
    @require_csrf
    def video_retry(video_id: int) -> Any:
        ctx = get_ctx()
        _video_or_404(video_id)
        try:
            outcome = ctx.pipeline.retry(video_id)
        except StateError as exc:
            return action_response(ok=False, message=str(exc), level="warning")
        except AutoPosterError as exc:
            return action_response(ok=False, message=str(exc), level="error")
        if ctx.worker:
            ctx.worker.wake()
        return action_response(ok=True, message=outcome.message, level="success", data=outcome.to_dict())

    @bp.route("/videos/<int:video_id>/recover", methods=["POST"])
    @require_csrf
    def video_recover(video_id: int) -> Any:
        ctx = get_ctx()
        _video_or_404(video_id)
        if ctx.dry_run:
            return action_response(ok=False, message="Dry-Run: keine YouTube-Abfragen.", level="warning")
        try:
            outcome = ctx.pipeline.check_on_platform(video_id)
        except (StateError, AuthError, PlatformError, AutoPosterError) as exc:
            return action_response(ok=False, message=str(exc), level="error")
        return action_response(
            ok=outcome.success,
            message=outcome.message,
            level="success" if outcome.success else "warning",
            data=outcome.to_dict(),
        )

    @bp.route("/videos/<int:video_id>/archive", methods=["POST"])
    @require_csrf
    def video_archive(video_id: int) -> Any:
        ctx = get_ctx()
        _video_or_404(video_id)
        target = request.form.get("target") or None
        try:
            outcome = ctx.pipeline.archive(video_id, target=target)
        except (StateError, AutoPosterError) as exc:
            return action_response(ok=False, message=str(exc), level="error")
        return action_response(
            ok=outcome.success, message=outcome.message, level="info", data=outcome.to_dict()
        )

    @bp.route("/videos/<int:video_id>/check", methods=["POST"])
    @require_csrf
    def video_check(video_id: int) -> Any:
        """Aktuellen Status bei YouTube abfragen (1 Quota-Einheit, manuell)."""

        ctx = get_ctx()
        row = _video_or_404(video_id)
        record = VideoRecord.from_row(row)
        if not record or not record.youtube_video_id:
            return action_response(ok=False, message="Keine YouTube-Video-ID gespeichert.", level="warning")
        try:
            info = ctx.youtube.fetch_status(record.youtube_video_id)
        except (AuthError, PlatformError, AutoPosterError) as exc:
            return action_response(ok=False, message=str(exc), level="error")
        if not info.get("found"):
            return action_response(
                ok=False,
                message="Video wurde auf YouTube nicht gefunden (geloescht oder anderes Konto).",
                level="warning",
            )
        privacy = str(info.get("privacy_status") or "").lower()
        updates: dict[str, Any] = {"error_message": None, "needs_attention": 0}
        if info.get("thumbnail_url"):
            updates["thumbnail_url"] = info["thumbnail_url"]
        if privacy == "public" and record.status != VideoStatus.PUBLISHED.value:
            updates["status"] = VideoStatus.PUBLISHED.value
            updates["published_privacy_status"] = "public"
            updates["published_at"] = info.get("published_at") or record.published_at
        elif privacy in {"private", "unlisted"} and record.status in PUBLISHABLE_STATUSES:
            updates["effective_privacy_status"] = privacy
        ctx.repository.update(record.id, **updates)
        return action_response(
            ok=True,
            message=(
                f"YouTube-Status: {privacy or 'unbekannt'} "
                f"(uploadStatus: {info.get('upload_status') or '-'}, "
                f"Titel: {info.get('title') or '-'})"
            ),
            level="success",
            data={"youtube": {key: value for key, value in info.items() if key != "raw"}},
        )

    @bp.route("/videos/<int:video_id>/thumbnail", methods=["POST"])
    @require_csrf
    def video_thumbnail(video_id: int) -> Any:
        """Thumbnail (erneut) setzen - ~50 Quota-Einheiten, nur manuell."""

        ctx = get_ctx()
        row = _video_or_404(video_id)
        record = VideoRecord.from_row(row)
        if not record:
            abort(404)
        if not record.youtube_video_id:
            return action_response(ok=False, message="Keine YouTube-Video-ID vorhanden.", level="warning")
        if not record.thumbnail_path or not Path(record.thumbnail_path).exists():
            return action_response(
                ok=False,
                message=f"Thumbnail-Datei nicht gefunden: {record.thumbnail_path or '(kein Pfad)'}",
                level="warning",
            )
        result = ctx.youtube.set_thumbnail(record.youtube_video_id, record.thumbnail_path)
        ctx.repository.update(
            record.id,
            thumbnail_set=1 if result.ok else 0,
            thumbnail_url=result.url or record.thumbnail_url,
            thumbnail_error=None if result.ok else result.message,
        )
        return action_response(
            ok=result.ok,
            message=result.message or ("Thumbnail gesetzt" if result.ok else "Thumbnail-Fehler"),
            level="success" if result.ok else "error",
        )

    return bp


__all__ = ["create_blueprint", "PLACEHOLDER_SVG"]
