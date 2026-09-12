"""YouTube-Plattform-Adapter (V1).

Buendelt Authentifizierung, Upload, Thumbnail und Veroeffentlichung hinter
der generischen :class:`PlatformAdapter`-Schnittstelle. Weitere Plattformen
(meta/tiktok/spotify) koennen spaeter als Geschwisterpaket ergaenzt werden.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ...auth.youtube_auth import AuthState, YouTubeAuthManager
from ...constants import ENFORCED_UPLOAD_PRIVACY, PRIVACY_PUBLIC
from ...errors import StateError, UploadError
from ...metadata.parser import EpisodeMetadata
from ..base import (
    PlatformAdapter,
    PlatformInfo,
    PublicationJob,
    PublishResult,
    ThumbnailResult,
    UploadResult,
)
from .api import YouTubeApi
from .publisher import YouTubePublisher
from .uploader import YouTubeUploader


class YouTubePlatform(PlatformAdapter):
    """Offizielle YouTube-Data-API-v3-Anbindung."""

    name = "youtube"
    label = "YouTube"

    def __init__(
        self,
        auth: YouTubeAuthManager,
        *,
        settings: Any = None,
        repository: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.auth = auth
        self.settings = settings
        self.repository = repository
        self.logger = logger or logging.getLogger("youtube_autoposter.youtube")
        self.api = YouTubeApi(auth, settings=settings, repository=repository, logger=self.logger)
        self.uploader = YouTubeUploader(self.api, settings=settings, logger=self.logger)
        self.publisher = YouTubePublisher(self.api, settings=settings, logger=self.logger)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def info(self) -> PlatformInfo:
        status = self.auth.status()
        quota = self.api.quota_today()
        message = status.message
        if status.state == AuthState.CONNECTED:
            uploads = int(quota.get("uploads") or 0)
            limit = int(quota.get("upload_limit_per_day") or 100)
            message = (
                f"{status.message} | Uploads heute: {uploads}/{limit} | "
                f"Quota-Einheiten heute: {quota.get('units', 0)}"
            )
        return PlatformInfo(
            name=self.name,
            label=self.label,
            available=bool(status.connected and status.client_secrets_present),
            message=message,
            account=status.account_email,
            channel=status.channel_title,
            scopes=status.granted_scopes or status.scopes,
            quota_today=quota,
        )

    def is_ready(self) -> tuple[bool, str]:
        """Kann jetzt hochgeladen werden? (ohne API-Aufruf)"""

        if not self.auth.has_client_secrets():
            return False, (
                f"credentials.json fehlt ({self.auth.client_secrets_path}). "
                "Bitte den OAuth-Client (Typ: Desktop-App) aus der Google Cloud Console ablegen."
            )
        if not self.auth.has_token():
            return False, "Keine YouTube-Verbindung. Bitte einmalig 'YouTube verbinden' ausfuehren."

        status = self.auth.status()
        if status.state == AuthState.CONNECTED:
            return True, status.message
        if status.state == AuthState.EXPIRED:
            # Refresh-Token vorhanden: wird beim naechsten Aufruf automatisch erneuert
            return True, status.message
        if status.state == AuthState.NEEDS_REAUTH:
            return False, f"Erneute Google-Autorisierung erforderlich: {status.message}"
        return False, status.message or "YouTube-Verbindung nicht bereit"

    # ------------------------------------------------------------------
    # Aktionen
    # ------------------------------------------------------------------

    def upload(self, job: PublicationJob) -> UploadResult:
        if job.platform not in (self.name, "", None):
            raise StateError(f"Job ist fuer Plattform '{job.platform}' - nicht fuer YouTube")
        result = self.uploader.upload(job)
        if result.privacy_status and result.privacy_status.lower() != ENFORCED_UPLOAD_PRIVACY:
            # Sollte nie passieren; falls doch, ist das ein Sicherheitsproblem.
            raise UploadError(
                f"YouTube meldet Privacy '{result.privacy_status}' statt 'private' - "
                "Upload wurde abgebrochen, bitte im YouTube Studio pruefen.",
                details=result.platform_video_id,
            )
        return result

    def set_thumbnail(self, platform_video_id: str, thumbnail_path: str) -> ThumbnailResult:
        return self.publisher.set_thumbnail(platform_video_id, thumbnail_path)

    def publish(
        self,
        job: PublicationJob,
        *,
        confirm: bool = False,
        privacy_status: str = PRIVACY_PUBLIC,
    ) -> PublishResult:
        return self.publisher.publish(job, confirm=confirm, privacy_status=privacy_status)

    def fetch_status(self, platform_video_id: str) -> dict[str, Any]:
        return self.api.get_video_status(platform_video_id)

    def find_existing_upload(
        self,
        *,
        title: str,
        since: str | None = None,
        duration_seconds: float | None = None,
        limit: int = 25,
    ) -> str | None:
        return self.api.find_upload_by_title(
            title, since=since, duration_seconds=duration_seconds, limit=limit
        )

    def validate_online(self, metadata: EpisodeMetadata) -> Iterable[str]:
        """Kategorie-ID online pruefen (1 Einheit, Ergebnis wird gecacht)."""

        warnings: list[str] = []
        if not getattr(self.settings, "VALIDATE_CATEGORY_ONLINE", False):
            return warnings
        if not metadata.category_id:
            return warnings
        try:
            ready, reason = self.is_ready()
            if not ready:
                return [f"Kategorie-Pruefung uebersprungen: {reason}"]
            categories = self.api.video_categories(metadata.language or "de")
        except Exception as exc:  # noqa: BLE001 - Pruefung ist optional
            return [f"Kategorie-Pruefung nicht moeglich: {exc}"]
        if categories and metadata.category_id not in categories:
            warnings.append(
                f"category_id '{metadata.category_id}' ist fuer diese Sprache/Region nicht "
                f"gueltig (erlaubt waeren z. B. {', '.join(categories[:8])} ...)"
            )
        return warnings


__all__ = ["YouTubePlatform"]
