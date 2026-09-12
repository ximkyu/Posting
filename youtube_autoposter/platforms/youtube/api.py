"""Duennes, robustes YouTube-API-Layer.

Hier passieren drei Dinge:

1. **API-Aufrufe werden dokumentiert und gezaehlt** (Quota-Tracking).
   Jede Methode nennt in ihrem Docstring den offiziellen Quota-Preis.
2. **Fehler werden klassifiziert** (retriable / quota / private-Lock /
   insufficient scope), damit die Pipeline sinnvoll reagieren kann.
3. **Es wird gecacht, was nicht pro Aufruf noetig ist** - das Dashboard
   loest bewusst KEINEN YouTube-Request aus.

Verwendete Endpunkte (ausschliesslich offizielle YouTube Data API v3):

=======================  ==============================  =========================
Methode                  Zweck                           Quota (offiziell)
=======================  ==============================  =========================
``channels.list``        Kanal-ID, Name, Uploads-Playlist 1 Einheit
``videos.insert``        Upload (resumable, privat)      1 Einheit im Bucket
                                                         "Video Uploads"
                                                         (max. 100 Aufrufe/Tag;
                                                         frueher 1600 Einheiten)
``thumbnails.set``       Thumbnail setzen                ~50 Einheiten
``videos.update``        privacyStatus -> public         50 Einheiten
``videos.list``          Status eines Videos pruefen      1 Einheit
``playlistItems.list``   Upload-Abgleich nach Crash       1 Einheit
``videoCategories.list`` Kategorie-ID pruefen (gecacht)   1 Einheit
=======================  ==============================  =========================
"""

from __future__ import annotations

import json
import logging
import socket
import time
from datetime import date
from typing import Any, Callable

from ...constants import (
    NON_RETRIABLE_REASONS,
    PRIVATE_LOCK_HELP_URL,
    PRIVATE_LOCK_HINTS,
    QUOTA_COSTS,
    RETRIABLE_STATUS_CODES,
)
from ...errors import (
    AuthError,
    AuthExpiredError,
    InsufficientScopeError,
    PlatformError,
    PrivateLockError,
    PublishError,
    QuotaExceededError,
    UploadError,
)
from ...utils.files import now_iso, parse_iso
from ...utils.logging_utils import redact

#: Netzwerkfehler, die einen erneuten Versuch lohnen
RETRIABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    socket.timeout,
    TimeoutError,
    ConnectionError,
    BrokenPipeError,
    OSError,
)


# ---------------------------------------------------------------------------
# Fehler-Klassifizierung
# ---------------------------------------------------------------------------


def parse_http_error(exc: Exception) -> tuple[int | None, str | None, str]:
    """Aus einem ``HttpError`` Status, Grund und lesbare Meldung extrahieren."""

    status: int | None = None
    reason: str | None = None
    message = redact(str(exc))

    response = getattr(exc, "resp", None)
    if response is not None:
        raw_status = getattr(response, "status", None)
        try:
            status = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status = None

    content = getattr(exc, "content", None)
    if content:
        try:
            payload = json.loads(content.decode("utf-8") if isinstance(content, bytes) else str(content))
        except (ValueError, AttributeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error") or {}
            errors = error.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                reason = errors[0].get("reason") or reason
                message = errors[0].get("message") or error.get("message") or message
            elif error.get("message"):
                message = error["message"]
            reason = reason or error.get("status")

    if reason is None:
        details = getattr(exc, "error_details", None)
        if isinstance(details, str) and details:
            reason = details
        elif isinstance(details, list) and details and isinstance(details[0], dict):
            reason = details[0].get("reason")
            message = details[0].get("message") or message

    return status, reason, str(message).strip()


def is_private_lock(status: int | None, reason: str | None, message: str) -> bool:
    """Erkennen, ob ein Projekt nicht auditiert ist (Videos bleiben privat)."""

    haystack = f"{reason or ''} {message}".lower()
    return any(hint.lower() in haystack for hint in PRIVATE_LOCK_HINTS) and (status in (400, 403) or reason in {
        "forbiddenPrivacySetting",
        "privacyError",
    })


def classify_error(exc: Exception, *, action: str = "api") -> PlatformError:
    """Exception in den passenden Anwendungstyp uebersetzen."""

    error_cls: Callable[..., PlatformError] = UploadError if action == "upload" else PublishError if action == "publish" else PlatformError

    # google-auth / googleapiclient werfen HttpError; alles andere bleibt generisch
    status, reason, message = parse_http_error(exc) if hasattr(exc, "resp") or hasattr(exc, "content") else (None, None, str(exc))

    if isinstance(exc, AuthExpiredError) or isinstance(exc, AuthError):
        return AuthError(str(exc), status_code=status, reason=reason)

    # google-auth: Token konnte nicht erneuert werden -> Mensch muss neu verbinden
    auth_names = {
        "refresherror",
        "defaultcredentialserror",
        "useraccesstokenrefresherror",
        "googleautherror",
        "oauth2autherror",
        "mutualtlserror",
    }
    if type(exc).__name__.lower() in auth_names or isinstance(exc, AuthError):
        return AuthError(
            f"Die YouTube-Autorisierung ist ungueltig oder abgelaufen: {message} "
            "Bitte in der Oberflaeche 'Mit YouTube verbinden' erneut ausfuehren.",
            status_code=status,
            reason=reason or type(exc).__name__,
        )

    if status in (401, 403) and reason in {
        "authError",
        "invalidCredentials",
        "unauthorized",
        "invalidToken",
        "invalid_grant",
        "unauthorizedClient",
    }:
        return AuthError(
            f"Die YouTube-Autorisierung ist ungueltig oder abgelaufen: {message} "
            "Bitte in der Oberflaeche 'Mit YouTube verbinden' erneut ausfuehren.",
            status_code=status,
            reason=reason,
        )

    if status == 403 and reason in {"insufficientPermissions", "forbidden"} and "scope" in message.lower():
        return InsufficientScopeError(
            f"Unzureichende OAuth-Rechte: {message}",
            status_code=status,
            reason=reason,
        )

    if reason in {"quotaExceeded", "dailyLimitExceeded", "userRateLimitExceeded"}:
        return QuotaExceededError(
            f"YouTube-Quota erschoepft ({reason}): {message}. "
            "Das Kontingent wird um Mitternacht Pacific Time zurueckgesetzt.",
            status_code=status,
            reason=reason,
        )

    if is_private_lock(status, reason, message):
        return PrivateLockError(
            f"YouTube verweigert das Oeffentlich-Schalten: {message} "
            f"Hintergrund: API-Projekte, die nach dem 28.07.2020 erstellt und nicht von YouTube "
            f"auditiert wurden, duerfen nur privat hochladen. Audit/Quota-Formular: "
            f"{PRIVATE_LOCK_HELP_URL}",
            status_code=status,
            reason=reason or "privacyError",
        )

    retriable = bool(status and status in RETRIABLE_STATUS_CODES) or reason in {"backendError", "internalError"}
    if reason in NON_RETRIABLE_REASONS:
        retriable = False
    if status is None and is_retriable_exception(exc):
        # Transport-Fehler (Netz weg, Timeout, SSL, Verbindung abgerissen):
        # kein HTTP-Status, aber eindeutig temporaer -> wiederholen.
        retriable = True

    return error_cls(
        message or f"YouTube-API-Fehler ({action})",
        status_code=status,
        reason=reason,
        retriable=retriable,
    )


def is_retriable_exception(exc: Exception) -> bool:
    if isinstance(exc, PlatformError):
        return bool(exc.retriable)
    if isinstance(exc, RETRIABLE_EXCEPTIONS):
        # SSL-Fehler sind ebenfalls temporaer (Netz unterbrochen)
        return True
    name = type(exc).__name__.lower()
    return any(token in name for token in ("timeout", "connection", "socket", "ssl", "brokenpipe"))


# ---------------------------------------------------------------------------
# API-Zugriff
# ---------------------------------------------------------------------------


class YouTubeApi:
    """Kapselt alle YouTube-Aufrufe der Anwendung."""

    def __init__(
        self,
        auth: Any,
        *,
        settings: Any = None,
        repository: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.auth = auth
        self.settings = settings
        self.repository = repository
        self.logger = logger or logging.getLogger("youtube_autoposter.youtube.api")
        self.max_retries = int(getattr(settings, "MAX_RETRIES", 3) or 3)
        self.retry_delay = float(getattr(settings, "RETRY_DELAY", 5.0) or 5.0)
        self.backoff_factor = float(getattr(settings, "RETRY_BACKOFF_FACTOR", 2.0) or 2.0)
        self.max_delay = float(getattr(settings, "RETRY_MAX_DELAY", 120.0) or 120.0)

    # ------------------------------------------------------------------

    @property
    def service(self) -> Any:
        return self.auth.build_service()

    def quota_units_for(self, method: str) -> int:
        entry = QUOTA_COSTS.get(method) or {}
        return int(entry.get("units", 0))

    def track_quota(self, method: str, *, success: bool = True) -> None:
        """Verbrauchte Quota-Einheiten pro Tag in der Datenbank festhalten."""

        if self.repository is None:
            return
        key = f"quota.{date.today().isoformat()}"
        try:
            data = json.loads(self.repository.get_setting(key) or "{}")
        except ValueError:
            data = {}
        calls = data.setdefault("calls", {})
        calls[method] = int(calls.get(method, 0)) + 1
        data["units"] = int(data.get("units", 0)) + self.quota_units_for(method)
        data["uploads"] = int(data.get("uploads", 0)) + (1 if method == "videos.insert" and success else 0)
        data["updated_at"] = now_iso()
        self.repository.set_setting(key, json.dumps(data, ensure_ascii=False))

    def quota_today(self) -> dict[str, Any]:
        if self.repository is None:
            return {}
        key = f"quota.{date.today().isoformat()}"
        try:
            data = json.loads(self.repository.get_setting(key) or "{}")
        except ValueError:
            data = {}
        data.setdefault("units", 0)
        data.setdefault("calls", {})
        data.setdefault("uploads", 0)
        data["upload_limit_per_day"] = int(
            (QUOTA_COSTS.get("videos.insert") or {}).get("daily_limit", 100)
        )
        data["default_bucket_limit"] = 10000
        data["documented_costs"] = {
            method: {"units": info["units"], "bucket": info.get("bucket", "default"), "description": info["description"]}
            for method, info in QUOTA_COSTS.items()
        }
        return data

    # ------------------------------------------------------------------

    def execute(
        self,
        request: Any,
        *,
        method: str,
        action: str = "api",
        max_retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        """API-Request mit Backoff ausfuehren und Quota buchen."""

        retries = self.max_retries if max_retries is None else int(max_retries)
        delay = self.retry_delay
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                result = request.execute()
                self.track_quota(method)
                return result if isinstance(result, dict) else {"result": result}
            except Exception as exc:  # noqa: BLE001 - klassifiziert weiter
                last_error = exc
                classified = classify_error(exc, action=action)
                if not is_retriable_exception(classified) or attempt >= retries:
                    self.track_quota(method, success=False)
                    raise classified from exc
                self.logger.warning(
                    "%s: temporaerer Fehler (%s), Versuch %d/%d - warte %.1fs",
                    method,
                    classified.reason or classified.status_code or type(exc).__name__,
                    attempt + 1,
                    retries + 1,
                    delay,
                )
                sleep(delay)
                delay = min(self.max_delay, delay * self.backoff_factor)

        # Sollte nicht erreichbar sein - defensive Absicherung
        raise classify_error(last_error or RuntimeError("unbekannter Fehler"), action=action)

    # ------------------------------------------------------------------
    # Kanal
    # ------------------------------------------------------------------

    def fetch_channel_info(self, credentials: Any | None = None) -> dict[str, Any]:
        """``channels.list?part=id,snippet,contentDetails&mine=true`` - 1 Einheit."""

        service = self.auth.build_service() if credentials is None else self._service_for(credentials)
        request = service.channels().list(part="id,snippet,contentDetails", mine=True, maxResults=1)
        data = self.execute(request, method="channels.list", action="read")
        items = data.get("items") or []
        if not items:
            raise AuthError(
                "Das autorisierte Google-Konto hat keinen YouTube-Kanal. "
                "Bitte mit dem Konto autorisieren, zu dem der Kanal gehoert "
                "(bei Markenkonten das richtige Konto waehlen)."
            )
        channel = items[0]
        related = (channel.get("contentDetails") or {}).get("relatedPlaylists") or {}
        snippet = channel.get("snippet") or {}
        return {
            "id": channel.get("id"),
            "title": snippet.get("title"),
            "description": snippet.get("description"),
            "uploads_playlist": related.get("uploads"),
            "country": snippet.get("country"),
            "thumbnail": ((snippet.get("thumbnails") or {}).get("default") or {}).get("url"),
        }

    def _service_for(self, credentials: Any) -> Any:
        from googleapiclient.discovery import build

        return build("youtube", "v3", credentials=credentials, cache_discovery=True, static_discovery=True)

    # ------------------------------------------------------------------
    # Videos
    # ------------------------------------------------------------------

    def get_video_status(self, youtube_video_id: str) -> dict[str, Any]:
        """``videos.list?part=id,status,snippet,contentDetails`` - 1 Einheit."""

        service = self.service
        request = service.videos().list(
            part="id,status,snippet,contentDetails", id=youtube_video_id, maxResults=1
        )
        data = self.execute(request, method="videos.list", action="read")
        items = data.get("items") or []
        if not items:
            return {"found": False, "id": youtube_video_id}
        video = items[0]
        status = video.get("status") or {}
        snippet = video.get("snippet") or {}
        details = video.get("contentDetails") or {}
        thumbnails = snippet.get("thumbnails") or {}
        best = thumbnails.get("maxres") or thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
        return {
            "found": True,
            "id": video.get("id"),
            "privacy_status": status.get("privacyStatus"),
            "upload_status": status.get("uploadStatus"),
            "rejection_reason": status.get("rejectionReason"),
            "failure_reason": status.get("failureReason"),
            "made_for_kids": status.get("madeForKids"),
            "self_declared_made_for_kids": status.get("selfDeclaredMadeForKids"),
            "publish_at": status.get("publishAt"),
            "title": snippet.get("title"),
            "published_at": snippet.get("publishedAt"),
            "duration": details.get("duration"),
            "thumbnail_url": best.get("url"),
            "raw": video,
        }

    def list_channel_uploads(self, limit: int = 25) -> list[dict[str, Any]]:
        """Letzte Uploads des eigenen Kanals (2 Einheiten: playlistItems + videos)."""

        if self.repository is not None:
            playlist_id = self.repository.get_setting("channel.uploads_playlist")
        else:
            playlist_id = None
        if not playlist_id:
            info = self.fetch_channel_info()
            playlist_id = info.get("uploads_playlist")
        if not playlist_id:
            return []

        service = self.service
        request = service.playlistItems().list(
            part="contentDetails", playlistId=playlist_id, maxResults=min(50, max(1, limit))
        )
        data = self.execute(request, method="playlistItems.list", action="read")
        ids = [
            (item.get("contentDetails") or {}).get("videoId")
            for item in (data.get("items") or [])
            if (item.get("contentDetails") or {}).get("videoId")
        ]
        if not ids:
            return []
        videos_request = service.videos().list(
            part="id,snippet,status,contentDetails", id=",".join(ids)
        )
        videos = self.execute(videos_request, method="videos.list", action="read")
        out: list[dict[str, Any]] = []
        for video in videos.get("items") or []:
            snippet = video.get("snippet") or {}
            status = video.get("status") or {}
            details = video.get("contentDetails") or {}
            out.append(
                {
                    "id": video.get("id"),
                    "title": snippet.get("title"),
                    "published_at": snippet.get("publishedAt"),
                    "privacy_status": status.get("privacyStatus"),
                    "duration": details.get("duration"),
                }
            )
        return out

    def find_upload_by_title(
        self,
        title: str,
        *,
        since: str | None = None,
        duration_seconds: float | None = None,
        limit: int = 25,
    ) -> str | None:
        """Sucht einen vorhandenen Upload mit gleichem Titel (Crash-Recovery).

        Nur offizielle API-Aufrufe, nur auf dem eigenen Kanal, nur auf
        ausdruecklichen Benutzerwunsch (kein automatischer Abgleich).
        """

        if not title:
            return None
        since_dt = parse_iso(since)
        candidates = self.list_channel_uploads(limit=limit)
        for candidate in candidates:
            if (candidate.get("title") or "").strip() != title.strip():
                continue
            published = parse_iso(candidate.get("published_at"))
            if since_dt and published and published < since_dt:
                continue
            if duration_seconds:
                seconds = iso8601_duration_seconds(candidate.get("duration"))
                if seconds and abs(seconds - duration_seconds) > 2:
                    continue
            return candidate.get("id")
        return None

    # ------------------------------------------------------------------
    # Kategorien
    # ------------------------------------------------------------------

    def video_categories(self, language: str = "de", *, use_cache: bool = True) -> list[str]:
        """``videoCategories.list`` - 1 Einheit, Ergebnis wird gecacht."""

        language = (language or "de").split("-")[0]
        cache_key = f"categories.{language}"
        if self.repository is not None and use_cache:
            try:
                cached = json.loads(self.repository.get_setting(cache_key) or "{}")
            except ValueError:
                cached = {}
            cached_at = parse_iso(cached.get("cached_at"))
            ttl_hours = int(getattr(self.settings, "CATEGORY_CACHE_HOURS", 168) or 0)
            if cached.get("ids") and cached_at and ttl_hours:
                age_hours = (parse_iso(now_iso()) - cached_at).total_seconds() / 3600.0
                if age_hours < ttl_hours:
                    return [str(i) for i in cached["ids"]]

        service = self.service
        request = service.videoCategories().list(part="id,snippet", regionCode=(language or "DE").upper()[:2])
        data = self.execute(request, method="videoCategories.list", action="read")
        ids = [str(item.get("id")) for item in (data.get("items") or []) if item.get("id")]
        if self.repository is not None and ids:
            self.repository.set_setting(
                cache_key, json.dumps({"ids": ids, "cached_at": now_iso()}, ensure_ascii=False)
            )
        return ids


def iso8601_duration_seconds(value: str | None) -> float | None:
    """'PT29M41S' -> 1781.0 (kleiner Parser fuer contentDetails.duration)."""

    if not value:
        return None
    text = str(value).strip().upper()
    if not text.startswith("P"):
        return None
    total = 0.0
    number = ""
    for char in text[1:]:
        if char.isdigit() or char == ".":
            number += char
            continue
        if number:
            amount = float(number)
            number = ""
            if char == "D":
                total += amount * 86400
            elif char == "H":
                total += amount * 3600
            elif char == "M":
                total += amount * 60
            elif char == "S":
                total += amount
    return total or None


__all__ = [
    "YouTubeApi",
    "classify_error",
    "is_private_lock",
    "is_retriable_exception",
    "iso8601_duration_seconds",
    "parse_http_error",
]
