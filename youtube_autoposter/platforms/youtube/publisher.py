"""Veroeffentlichen (``videos.update``) und Thumbnail (``thumbnails.set``).

Beide Funktionen sind bewusst getrennt vom Upload: Der Upload liefert IMMER
ein privates Video, und ``publish()`` darf nur nach einer ausdruecklichen
Bestaetigung aus der Oberflaeche aufgerufen werden (``confirm=True``).

Quota: ``videos.update`` = 50 Einheiten, ``thumbnails.set`` = ~50 Einheiten.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...constants import PRIVACY_PUBLIC, PRIVACY_UNLISTED, THUMBNAIL_MAX_BYTES
from ...errors import PlatformError, PublishError, StateError, ThumbnailError
from ...metadata.parser import build_update_body
from ...utils.files import human_size
from ...utils.logging_utils import redact
from ..base import PublicationJob, PublishResult, ThumbnailResult
from .api import YouTubeApi, classify_error
from .uploader import guess_mimetype, watch_url

ALLOWED_PUBLISH_STATUSES = (PRIVACY_PUBLIC, PRIVACY_UNLISTED)

THUMBNAIL_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _thumbnail_url_from_response(items: list) -> str | None:
    """Thumbnail-URL aus ``thumbnails.set`` extrahieren.

    Die API liefert ``items[0]`` als ThumbnailDetails-Objekt, die URL steckt
    je nach Verfuegbarkeit unter ``maxres``/``standard``/``high``/``medium``/
    ``default``. Aeltere/abweichende Antworten enthalten ``url`` direkt.
    """

    if not items:
        return None
    first = items[0] or {}
    if not isinstance(first, dict):
        return None
    if first.get("url"):
        return str(first["url"])
    for quality in ("maxres", "standard", "high", "medium", "default"):
        entry = first.get(quality)
        if isinstance(entry, dict) and entry.get("url"):
            return str(entry["url"])
    return None


class YouTubePublisher:
    """Oeffentlich-Schalten und Thumbnail-Verwaltung."""

    def __init__(self, api: YouTubeApi, *, settings: Any = None, logger: logging.Logger | None = None) -> None:
        self.api = api
        self.settings = settings
        self.logger = logger or logging.getLogger("youtube_autoposter.youtube.publisher")

    # ------------------------------------------------------------------

    def publish(
        self,
        job: PublicationJob,
        *,
        confirm: bool = False,
        privacy_status: str = PRIVACY_PUBLIC,
    ) -> PublishResult:
        """``privacyStatus`` eines bereits hochgeladenen Videos aendern.

        Sicherheit:

        * Ohne ``confirm=True`` wird die Aktion verweigert - der Aufruf muss
          aus dem bestaetigten Button in der Oberflaeche kommen.
        * Es wird NUR ``public``/``unlisted`` akzeptiert (niemand braucht hier
          ``private``, dafuer gibt es den Upload).
        * Nach dem API-Aufruf wird die Antwort geprueft: Bleibt das Video
          trotz Erfolgsmeldung privat (nicht auditiertes Projekt), wird das
          als :class:`PrivateLockError` gemeldet und NICHT als Erfolg
          verbucht.
        """

        if not confirm:
            raise StateError(
                "Veroeffentlichen wurde ohne ausdrueckliche Bestaetigung versucht - abgebrochen. "
                "(Sicherheitsregel: keine automatische Veroeffentlichung)"
            )
        target = str(privacy_status or PRIVACY_PUBLIC).lower()
        if target not in ALLOWED_PUBLISH_STATUSES:
            raise StateError(f"Ungueltiger Ziel-Status '{privacy_status}' (erlaubt: public, unlisted)")
        if not job.platform_video_id:
            raise PublishError("Keine YouTube-Video-ID vorhanden - Veroeffentlichen nicht moeglich")

        body = build_update_body(
            job.metadata,
            job.platform_video_id,
            privacy_status=target,
            settings=self.settings,
        )
        service = self.api.service
        request = service.videos().update(part="status", body=body)

        self.logger.info(
            "[%s] Veroeffentlichen gestartet (YouTube-ID %s -> %s)",
            job.episode_id,
            job.platform_video_id,
            target,
        )
        try:
            response = self.api.execute(request, method="videos.update", action="publish")
        except PlatformError as exc:
            self.logger.error("[%s] Veroeffentlichen fehlgeschlagen: %s", job.episode_id, redact(str(exc)))
            raise

        status_block = response.get("status") or {}
        snippet = response.get("snippet") or {}
        actual = str(status_block.get("privacyStatus") or "").lower()
        published_at = snippet.get("publishedAt")

        if actual and actual != target:
            error = PublishError(
                f"YouTube hat den Status nicht auf '{target}' gesetzt (Antwort: '{actual}'). "
                f"Ursache haeufig: Das API-Projekt ist nicht auditiert, Videos bleiben privat "
                f"gesperrt. Audit-Formular: https://support.google.com/youtube/contact/yt_api_form",
                reason=status_block.get("rejectionReason") or "privacyNotApplied",
            )
            self.logger.error("[%s] %s", job.episode_id, error.message)
            raise error

        self.logger.info(
            "[%s] Veroeffentlicht: %s (%s)",
            job.episode_id,
            watch_url(job.platform_video_id),
            actual or target,
        )
        return PublishResult(
            platform_video_id=job.platform_video_id,
            privacy_status=actual or target,
            url=watch_url(job.platform_video_id),
            published_at=published_at,
            quota_units=self.api.quota_units_for("videos.update"),
            raw=response,
        )

    # ------------------------------------------------------------------

    def set_thumbnail(self, youtube_video_id: str, thumbnail_path: str | Path) -> ThumbnailResult:
        """Thumbnail setzen. Fehler werden als Ergebnis zurueckgegeben,
        damit sie den bereits erfolgreichen Upload nicht entwerten."""

        path = Path(thumbnail_path)
        if not youtube_video_id:
            return ThumbnailResult(False, message="Keine YouTube-Video-ID vorhanden")
        if not path.exists():
            return ThumbnailResult(False, message=f"Thumbnail-Datei fehlt: {path}")

        size = path.stat().st_size
        if size <= 0:
            return ThumbnailResult(False, message=f"Thumbnail ist leer (0 Byte): {path.name}")
        if size > THUMBNAIL_MAX_BYTES:
            return ThumbnailResult(
                False,
                message=(
                    f"Thumbnail ist {human_size(size)} gross - YouTube erlaubt maximal 2 MB. "
                    "Bitte verkleinern/komprimieren und erneut versuchen."
                ),
            )

        mimetype = THUMBNAIL_MIME.get(path.suffix.lower())
        if mimetype is None:
            return ThumbnailResult(
                False,
                message=f"Nicht unterstuetztes Thumbnail-Format '{path.suffix}' (erlaubt: .jpg, .jpeg, .png)",
            )

        try:
            from googleapiclient.http import MediaFileUpload

            media = MediaFileUpload(str(path), mimetype=mimetype, resumable=False)
            request = self.api.service.thumbnails().set(videoId=youtube_video_id, media_body=media)
            response = self.api.execute(request, method="thumbnails.set", action="thumbnail")
        except PlatformError as exc:
            message = redact(str(exc))
            if exc.status_code == 403:
                message += (
                    " | Hinweis: Eigene Thumbnails erfordern ein verifiziertes YouTube-Konto "
                    "(Telefonnummer-Bestaetigung)."
                )
            self.logger.warning("[%s] Thumbnail konnte nicht gesetzt werden: %s", youtube_video_id, message)
            return ThumbnailResult(False, message=message, quota_units=self.api.quota_units_for("thumbnails.set"))
        except Exception as exc:  # noqa: BLE001 - Thumbnail darf den Ablauf nie stoppen
            message = redact(str(exc))
            self.logger.warning("[%s] Thumbnail-Fehler: %s", youtube_video_id, message)
            return ThumbnailResult(False, message=message)

        items = response.get("items") or []
        url = _thumbnail_url_from_response(items)
        self.logger.info("[%s] Thumbnail gesetzt: %s", youtube_video_id, path.name)
        return ThumbnailResult(
            True,
            url=url,
            message="Thumbnail erfolgreich gesetzt",
            quota_units=self.api.quota_units_for("thumbnails.set"),
        )

    # ------------------------------------------------------------------

    def rethrow_as_publish_error(self, exc: Exception) -> PublishError:
        """Hilfsfunktion fuer Aufrufer, die rohe Exceptions normalisieren wollen."""

        if isinstance(exc, PublishError):
            return exc
        return classify_error(exc, action="publish")


__all__ = ["YouTubePublisher", "ThumbnailError", "ALLOWED_PUBLISH_STATUSES"]
