"""Resumable Upload ueber ``videos.insert`` (YouTube Data API v3).

Robustheit:

* Der Upload laeuft in Chunks (Standard 8 MB) ueber den offiziellen
  **resumable upload**-Mechanismus von ``googleapiclient``.
* Bei temporaeren Fehlern (500/502/503/504, Netzwerkabbruch, Timeout) wird
  mit exponentiellem Backoff erneut angesetzt - zuerst innerhalb derselben
  Upload-Session (der Server merkt sich den Fortschritt), bei zerstoerter
  Session mit einer neuen Session.
* Erst wenn ``MAX_RETRIES`` erschoepft sind, gilt der Upload als FAILED.
* Die Sicherheitsregel ist doppelt abgesichert: ``force_private=True`` UND
  eine harte Pruefung des fertigen Request-Bodys vor dem Absenden.

Quota: 1 Aufruf pro Tag im Bucket "Video Uploads" (max. 100/Tag).
"""

from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path
from typing import Any, Callable

from ...constants import ENFORCED_UPLOAD_PRIVACY
from ...errors import PlatformError, UploadError
from ...metadata.parser import build_upload_body
from ...utils.files import human_size
from ...utils.logging_utils import redact
from ..base import PublicationJob, UploadResult
from .api import YouTubeApi, classify_error, is_retriable_exception, parse_http_error

MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".3gp": "video/3gpp",
}


def guess_mimetype(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in MIME_TYPES:
        return MIME_TYPES[suffix]
    guessed, _encoding = mimetypes.guess_type(str(path))
    if guessed and guessed.startswith("video/"):
        return guessed
    return "video/*"


def watch_url(youtube_video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={youtube_video_id}"


class YouTubeUploader:
    """Fuehrt den eigentlichen Upload aus."""

    def __init__(self, api: YouTubeApi, *, settings: Any = None, logger: logging.Logger | None = None) -> None:
        self.api = api
        self.settings = settings
        self.logger = logger or logging.getLogger("youtube_autoposter.youtube.uploader")
        self.max_retries = int(getattr(settings, "MAX_RETRIES", 3) or 3)
        self.retry_delay = float(getattr(settings, "RETRY_DELAY", 5.0) or 5.0)
        self.backoff_factor = float(getattr(settings, "RETRY_BACKOFF_FACTOR", 2.0) or 2.0)
        self.max_delay = float(getattr(settings, "RETRY_MAX_DELAY", 120.0) or 120.0)
        self.chunk_size = int(getattr(settings, "UPLOAD_CHUNK_SIZE", 8 * 1024 * 1024) or -1)
        self.progress_log_seconds = int(getattr(settings, "PROGRESS_LOG_SECONDS", 20) or 20)

    # ------------------------------------------------------------------

    def build_body(self, job: PublicationJob) -> dict[str, Any]:
        """Request-Body bauen und die Private-Regel endgueltig absichern."""

        body = build_upload_body(job.metadata, settings=self.settings, force_private=True)
        privacy = str(((body.get("status") or {}).get("privacyStatus") or "")).lower()
        if privacy != ENFORCED_UPLOAD_PRIVACY:
            # Darf durch keine Konfiguration/JSON passieren - doppelte Sicherung.
            raise UploadError(
                f"Sicherheitsregel verletzt: Upload-Privacy waere '{privacy}'. Abgebrochen.",
                reason="privacyGuard",
            )
        body["status"]["privacyStatus"] = ENFORCED_UPLOAD_PRIVACY
        return body

    # ------------------------------------------------------------------

    def upload(
        self,
        job: PublicationJob,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> UploadResult:
        """Video hochladen. Wirft :class:`UploadError` bei endgueltigem Scheitern."""

        path = Path(job.video_path)
        if not path.exists():
            raise UploadError(f"Videodatei fehlt fuer den Upload: {path}")
        if path.stat().st_size <= 0:
            raise UploadError(f"Videodatei ist leer (0 Byte): {path}")

        body = self.build_body(job)
        file_size = path.stat().st_size
        mimetype = guess_mimetype(path)

        service = self.api.service
        notify = job.metadata.notify_subscribers
        if notify is None:
            # Private Uploads sollen keine Abonnenten-Benachrichtigung ausloesen.
            notify = False

        self.logger.info(
            "[%s] Upload gestartet: %s (%s), Ziel-Privacy: %s",
            job.episode_id,
            path.name,
            human_size(file_size),
            ENFORCED_UPLOAD_PRIVACY,
        )

        request = self._create_request(service, body, path, mimetype, notify)
        attempt = 0
        response: dict[str, Any] | None = None
        last_progress = 0.0
        last_log = time.monotonic()
        started = time.monotonic()

        while response is None:
            try:
                progress, response = request.next_chunk()
                if progress is not None:
                    fraction = float(progress.progress() or 0.0)
                    sent = int(file_size * fraction)
                    last_log = self._report_progress(job, sent, file_size, fraction, started, last_log)
                    last_progress = fraction
            except Exception as exc:  # noqa: BLE001 - klassifizieren und entscheiden
                classified = classify_error(exc, action="upload")
                if not is_retriable_exception(classified):
                    self._log_failure(job, classified, attempt, time.monotonic() - started)
                    raise classified from exc

                attempt += 1
                if attempt > self.max_retries:
                    self._log_failure(job, classified, attempt - 1, time.monotonic() - started)
                    raise classified from exc

                delay = min(self.max_delay, self.retry_delay * (self.backoff_factor ** (attempt - 1)))
                self.logger.warning(
                    "[%s] Upload-Versuch %d/%d fehlgeschlagen (%s) - neuer Versuch in %.0fs",
                    job.episode_id,
                    attempt,
                    self.max_retries + 1,
                    classified.reason or classified.status_code or type(exc).__name__,
                    delay,
                )
                sleep(delay)

                if self._needs_new_session(exc, request):
                    self.logger.info("[%s] Neue Upload-Session wird gestartet", job.episode_id)
                    request = self._create_request(service, body, path, mimetype, notify)
                continue

        youtube_id = str(response.get("id") or "").strip()
        if not youtube_id:
            error = UploadError(
                "YouTube hat keine Video-ID zurueckgegeben",
                details=redact(str(response))[:400],
            )
            self._log_failure(job, error, attempt, time.monotonic() - started)
            raise error

        status_block = response.get("status") or {}
        snippet = response.get("snippet") or {}
        privacy = str(status_block.get("privacyStatus") or ENFORCED_UPLOAD_PRIVACY).lower()
        thumbnails = snippet.get("thumbnails") or {}
        best = (
            thumbnails.get("maxres")
            or thumbnails.get("high")
            or thumbnails.get("medium")
            or thumbnails.get("default")
            or {}
        )

        elapsed = time.monotonic() - started
        self.logger.info(
            "[%s] Upload abgeschlossen in %.1fs - YouTube-ID: %s, Privacy: %s",
            job.episode_id,
            elapsed,
            youtube_id,
            privacy,
        )

        return UploadResult(
            platform_video_id=youtube_id,
            url=watch_url(youtube_id),
            privacy_status=privacy,
            thumbnail_url=best.get("url"),
            quota_units=self.api.quota_units_for("videos.insert"),
            raw=response,
            resumable=True,
            bytes_sent=file_size,
        )

    # ------------------------------------------------------------------

    def _create_request(self, service: Any, body: dict[str, Any], path: Path, mimetype: str, notify: bool) -> Any:
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(
            str(path),
            mimetype=mimetype,
            chunksize=self.chunk_size if self.chunk_size and self.chunk_size > 0 else -1,
            resumable=True,
        )
        return service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
            notifySubscribers=bool(notify),
        )

    def _needs_new_session(self, exc: Exception, request: Any) -> bool:
        """Wann ist die laufende Upload-Session nicht mehr nutzbar?"""

        name = type(exc).__name__
        if name in {"ResumableUploadError", "InvalidChunkSizeError", "UnexpectedMethodError"}:
            return True
        if not hasattr(exc, "resp"):
            # Transport-Fehler (Socket, SSL, httplib2) -> Session neu aufbauen
            return True

        status_code, _reason, _message = parse_http_error(exc)
        progress = int(getattr(request, "resumable_progress", 0) or 0)
        # Ohne HTTP-Status oder vor dem ersten Byte ist ein Neuaufbau sauber
        # und billig; bei 5xx mit bereits gesendeten Bytes wird dieselbe
        # Session fortgesetzt (genau dafuer ist sie da).
        return status_code is None or progress == 0

    def _report_progress(
        self,
        job: PublicationJob,
        sent: int,
        total: int,
        fraction: float,
        started: float,
        last_log: float,
    ) -> float:
        """Fortschritt melden und den Zeitpunkt der letzten Logzeile liefern."""

        if job.progress_cb is not None:
            try:
                job.progress_cb(sent, total)
            except Exception:  # noqa: BLE001 - Fortschritt darf nie den Upload stoppen
                pass
        now = time.monotonic()
        if now - last_log < self.progress_log_seconds:
            return last_log
        elapsed = max(0.1, now - started)
        speed = sent / elapsed
        self.logger.info(
            "[%s] Upload-Fortschritt: %.0f%% (%s von %s, %.1f MB/s)",
            job.episode_id,
            fraction * 100,
            human_size(sent),
            human_size(total),
            speed / (1024 * 1024),
        )
        return now

    def _log_failure(self, job: PublicationJob, error: PlatformError, attempts: int, elapsed: float) -> None:
        self.logger.error(
            "[%s] Upload fehlgeschlagen nach %.0fs und %d Versuch(en): %s",
            job.episode_id,
            elapsed,
            attempts + 1,
            redact(str(error)),
        )


__all__ = ["YouTubeUploader", "guess_mimetype", "watch_url"]
