"""Zentrale Konstanten: Stati, Ereignisse, YouTube-Limits, API-Quota-Kosten.

Alles, was an mehreren Stellen gebraucht wird und sich NICHT zur Laufzeit
aendert, lebt hier - dadurch bleibt der Rest des Codes frei von magischen
Strings.
"""

from __future__ import annotations

from enum import Enum

APP_NAME = "YouTube AutoPoster"
APP_VERSION = "1.0.0"

#: Alias, damit ``from .constants import __version__`` funktioniert
__version__ = APP_VERSION

PLATFORM_YOUTUBE = "youtube"

# ---------------------------------------------------------------------------
# Status-Maschine
# ---------------------------------------------------------------------------


class VideoStatus(str, Enum):
    """Lebenszyklus einer Episode.

    Uebersicht::

        NEW -> READY -> VALIDATING -> UPLOADING -> UPLOADED_PRIVATE
                                                      |  (manuelles Veroeffentlichen)
                                                      v
                                                 PUBLISHING -> PUBLISHED

        Jeder Schritt kann nach FAILED gehen.
        UPLOADING kann nach einem Absturz/Neustart zu INTERRUPTED werden
        (bewusst KEIN automatischer Re-Upload -> Doppelupload-Schutz).
    """

    NEW = "NEW"                                # Dateien erkannt, noch nicht geprueft
    READY = "READY"                            # validiert/erkannt, liegt in der Queue
    VALIDATING = "VALIDATING"                  # technische + Metadaten-Pruefung laeuft
    UPLOADING = "UPLOADING"                    # Upload zu YouTube laeuft
    UPLOADED_PRIVATE = "UPLOADED_PRIVATE"      # privat hochgeladen -> bereit zum Veroeffentlichen
    PUBLISH_READY = "PUBLISH_READY"            # Alias/UI-Zustand fuer UPLOADED_PRIVATE
    PUBLISHING = "PUBLISHING"                  # Veroeffentlichen laeuft
    PUBLISHED = "PUBLISHED"                    # oeffentlich auf YouTube
    FAILED = "FAILED"                          # Fehler, manuelle Aktion noetig
    PAUSED = "PAUSED"                          # wartet (z. B. auf YouTube-Verbindung)
    INTERRUPTED = "INTERRUPTED"                # Upload wurde abgebrochen (Neustart/Crash)
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"    # Dublette, bewusst nicht hochgeladen

    def __str__(self) -> str:  # pragma: no cover - Komfort
        return self.value


# Stati, in denen ein Video bereits (mindestens) einmal erfolgreich bei
# YouTube angekommen ist. Diese Stati blockieren jeden weiteren Upload.
UPLOADED_STATUSES: tuple[VideoStatus, ...] = (
    VideoStatus.UPLOADED_PRIVATE,
    VideoStatus.PUBLISH_READY,
    VideoStatus.PUBLISHING,
    VideoStatus.PUBLISHED,
)

# Stati, die von der Queue aufgenommen und verarbeitet werden duerfen.
QUEUEABLE_STATUSES: tuple[VideoStatus, ...] = (
    VideoStatus.NEW,
    VideoStatus.READY,
    VideoStatus.PAUSED,
)

# Von diesen Stati darf die Pipeline einen Verarbeitungslauf starten
# (wird atomar per UPDATE ... WHERE geprueft -> kein Doppel-Processing).
CLAIMABLE_STATUSES: tuple[VideoStatus, ...] = (
    VideoStatus.NEW,
    VideoStatus.READY,
    VideoStatus.PAUSED,
    VideoStatus.VALIDATING,
)

TERMINAL_STATUSES: tuple[VideoStatus, ...] = (
    VideoStatus.PUBLISHED,
    VideoStatus.SKIPPED_DUPLICATE,
)

# Reihenfolge + deutsche Beschriftung + Farbe fuer die Oberflaeche.
STATUS_LABELS: dict[str, str] = {
    VideoStatus.NEW.value: "NEU",
    VideoStatus.READY.value: "BEREIT",
    VideoStatus.VALIDATING.value: "PRUEFUNG",
    VideoStatus.UPLOADING.value: "UPLOAD LAEUFT",
    VideoStatus.UPLOADED_PRIVATE.value: "PRIVAT",
    VideoStatus.PUBLISH_READY.value: "BEREIT ZUR VEROEFFENTLICHUNG",
    VideoStatus.PUBLISHING.value: "VEROEFFENTLICHUNG LAEUFT",
    VideoStatus.PUBLISHED.value: "VEROEFFENTLICHT",
    VideoStatus.FAILED.value: "FEHLER",
    VideoStatus.PAUSED.value: "PAUSIERT",
    VideoStatus.INTERRUPTED.value: "UNTERBROCHEN",
    VideoStatus.SKIPPED_DUPLICATE.value: "DUBLETTE UEBERSPRUNGEN",
}

# ---------------------------------------------------------------------------
# Ereignisse / Phasen (fuer uploads- und logs-Tabelle)
# ---------------------------------------------------------------------------


class Phase(str, Enum):
    INTAKE = "INTAKE"
    VALIDATE = "VALIDATE"
    HASH = "HASH"
    MOVE = "MOVE"
    UPLOAD = "UPLOAD"
    THUMBNAIL = "THUMBNAIL"
    ARCHIVE = "ARCHIVE"
    PUBLISH = "PUBLISH"
    RECOVER = "RECOVER"
    AUTH = "AUTH"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# YouTube-Grenzwerte (offizielle YouTube Data API v3)
# ---------------------------------------------------------------------------

TITLE_MAX_LENGTH = 100            # snippet.title
DESCRIPTION_MAX_LENGTH = 5000     # snippet.description
TAGS_TOTAL_MAX_LENGTH = 500       # Summe aller Tags inkl. Trennzeichen
TAG_MAX_LENGTH = 100              # einzelner Tag
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024   # thumbnails.set: max. 2 MB
VIDEO_MAX_BYTES = 256 * 1024 * 1024 ** 3  # videos.insert: max. 256 GB

PRIVACY_PRIVATE = "private"
PRIVACY_UNLISTED = "unlisted"
PRIVACY_PUBLIC = "public"

# Sicherheit: der beim Upload an YouTube uebermittelte Wert ist IMMER private.
ENFORCED_UPLOAD_PRIVACY = PRIVACY_PRIVATE

THUMBNAIL_EXTENSIONS = (".jpg", ".jpeg", ".png")
VIDEO_EXTENSIONS_DEFAULT = (".mp4",)
VIDEO_EXTENSIONS_OPTIONAL = (".mov", ".mkv", ".webm")
METADATA_EXTENSION = ".json"

DEFAULT_CATEGORY_ID = "22"        # "People & Blogs" (Fallback, wenn nichts angegeben)
DEFAULT_LANGUAGE = "de"

# ---------------------------------------------------------------------------
# API-Quota (Stand: offizielle YouTube-Data-API-v3-Dokumentation)
#
# Wichtig: seit der Quota-Umstellung 2026 hat videos.insert ein EIGENES
# Kontingent ("Video Uploads": 100 Aufrufe/Tag, 1 Einheit in diesem Bucket)
# und zieht nicht mehr 1600 Einheiten aus dem 10.000-Einheiten-Pool.
# Beide Angaben werden hier dokumentiert, damit die UI ehrlich bleibt.
# ---------------------------------------------------------------------------

QUOTA_COSTS: dict[str, dict[str, object]] = {
    "videos.insert": {
        "units": 1,
        "bucket": "Video Uploads",
        "daily_limit": 100,
        "legacy_units": 1600,
        "description": "Video hochladen (resumable)",
    },
    "thumbnails.set": {
        "units": 50,
        "bucket": "default",
        "daily_limit": 10000,
        "description": "Thumbnail setzen",
    },
    "videos.update": {
        "units": 50,
        "bucket": "default",
        "daily_limit": 10000,
        "description": "privacyStatus auf public setzen (VEROEFFENTLICHEN)",
    },
    "videos.list": {
        "units": 1,
        "bucket": "default",
        "daily_limit": 10000,
        "description": "Video-Status abfragen",
    },
    "channels.list": {
        "units": 1,
        "bucket": "default",
        "daily_limit": 10000,
        "description": "Kanal-ID + Uploads-Playlist ermitteln",
    },
    "playlistItems.list": {
        "units": 1,
        "bucket": "default",
        "daily_limit": 10000,
        "description": "Upload-Abgleich nach abgebrochenem Upload",
    },
    "videoCategories.list": {
        "units": 1,
        "bucket": "default",
        "daily_limit": 10000,
        "description": "Kategorie-ID pruefen (wird gecacht)",
    },
}

# Offizielle OAuth-Scopes.
#
# Warum nicht nur youtube.upload?
#   videos.insert    -> youtube.upload ODER youtube ODER force-ssl ODER youtubepartner
#   thumbnails.set   -> youtube.upload ODER youtube ODER force-ssl ODER youtubepartner
#   videos.update    -> youtube ODER force-ssl ODER youtubepartner   (NICHT youtube.upload!)
#   channels.list    -> youtube ODER youtube.readonly ODER force-ssl ODER youtubepartner
#
# Da "VEROEFFENTLICHEN" zwingend videos.update braucht, ist der einzelne
# Scope https://www.googleapis.com/auth/youtube die kleinste Menge, die den
# kompletten Workflow abdeckt (kein zusaetzlicher Comment-/Partner-Zugriff).
SCOPE_YOUTUBE = "https://www.googleapis.com/auth/youtube"
SCOPE_YOUTUBE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_YOUTUBE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_YOUTUBE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"

DEFAULT_SCOPES: tuple[str, ...] = (SCOPE_YOUTUBE,)

SCOPE_PRESETS: dict[str, tuple[str, ...]] = {
    # Empfohlen: deckt Upload (privat), Thumbnail, Veroeffentlichen und
    # Kanal-Abruf ab.
    "default": (SCOPE_YOUTUBE,),
    # Nur hochladen + lesen. ACHTUNG: "VEROEFFENTLICHEN" schlaegt damit mit
    # 403 insufficientPermissions fehl, weil videos.update youtube.upload
    # nicht akzeptiert.
    "upload_only": (SCOPE_YOUTUBE_UPLOAD, SCOPE_YOUTUBE_READONLY),
    # Maximale Rechte (inkl. Kommentare etc.) - normalerweise nicht noetig.
    "full": (SCOPE_YOUTUBE, SCOPE_YOUTUBE_FORCE_SSL),
}

# HTTP-Statuscodes, fuer die ein Retry sinnvoll ist.
RETRIABLE_STATUS_CODES: frozenset[int] = frozenset({500, 502, 503, 504})

# YouTube-Fehlergruende, die NICHT erneut versucht werden duerfen.
NON_RETRIABLE_REASONS: frozenset[str] = frozenset(
    {
        "quotaExceeded",
        "dailyLimitExceeded",
        "userRateLimitExceeded",
        "uploadLimitExceeded",
        "insufficientPermissions",
        "forbidden",
        "unauthorized",
        "invalidCredentials",
        "authError",
        "invalidTitle",
        "invalidDescription",
        "invalidTags",
        "invalidCategoryId",
        "forbiddenPrivacySetting",
        "privacyError",
    }
)

# Hinweis: Projekte, die nach dem 28.07.2020 erstellt und nie von YouTube
# auditiert wurden, duerfen nur privat hochladen. Das Veroeffentlichen
# scheitert dann mit einem privacyError/forbidden. Diese Meldung wird in der
# UI erkannt und mit einer Handlungsempfehlung angereichert.
PRIVATE_LOCK_HINTS = (
    "private viewing mode",
    "locked as private",
    "privacy setting",
    "forbiddenPrivacySetting",
    "privacyError",
    "unverified",
)

PRIVATE_LOCK_HELP_URL = "https://support.google.com/youtube/contact/yt_api_form"

ALL_STATUSES = tuple(s.value for s in VideoStatus)
