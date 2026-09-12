"""Plattform-Abstraktion.

V1 implementiert nur YouTube (``platforms/youtube``). Damit spaeter
Instagram/Meta, TikTok oder Spotify ergaenzt werden koennen, ohne die
Pipeline anzufassen, arbeitet der Kern ausschliesslich mit dieser
Schnittstelle:

* :class:`PublicationJob`  - was veroeffentlicht werden soll
* :class:`UploadResult`    - Ergebnis eines Uploads
* :class:`PublishResult`   - Ergebnis einer Veroeffentlichung
* :class:`PlatformAdapter` - die eigentliche Plattform-Anbindung

Neue Plattform = neue Unterklasse + Eintrag in
``platforms/registry.py``. Die Datenbank speichert bereits jetzt pro
Episode eine ``platform``-Spalte.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..metadata.parser import EpisodeMetadata

ProgressCallback = Callable[[int, int], None]


@dataclass
class PublicationJob:
    """Ein Auftrag an eine Plattform (generisch, nicht YouTube-spezifisch)."""

    #: Datenbank-ID der Episode
    video_id: int
    episode_id: str
    platform: str
    video_path: str
    metadata: EpisodeMetadata
    thumbnail_path: str | None = None
    file_size: int = 0
    file_hash: str | None = None
    #: Bereits vergebene Plattform-ID (bei Wiederaufnahme/Update)
    platform_video_id: str | None = None
    #: Fortschritts-Rueckmeldung (bytes_sent, bytes_total)
    progress_cb: ProgressCallback | None = None
    #: Zusaetzliche, plattformspezifische Daten
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def attempt(self) -> int:
        return int(self.options.get("attempt", 1))


@dataclass
class UploadResult:
    platform_video_id: str
    url: str = ""
    privacy_status: str = "private"
    thumbnail_url: str | None = None
    quota_units: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    resumable: bool = True
    bytes_sent: int = 0


@dataclass
class PublishResult:
    platform_video_id: str
    privacy_status: str
    url: str = ""
    published_at: str | None = None
    quota_units: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThumbnailResult:
    ok: bool
    url: str | None = None
    message: str = ""
    quota_units: int = 0


@dataclass
class PlatformInfo:
    """Auskunft ueber die Plattform-Anbindung (fuer Dashboard/Dry-Run)."""

    name: str
    label: str
    available: bool
    message: str = ""
    account: str | None = None
    channel: str | None = None
    scopes: list[str] = field(default_factory=list)
    quota_today: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "available": self.available,
            "message": self.message,
            "account": self.account,
            "channel": self.channel,
            "scopes": list(self.scopes),
            "quota_today": dict(self.quota_today),
        }


class PlatformAdapter(ABC):
    """Schnittstelle, die jede Plattform implementieren muss."""

    #: interner Name (z. B. 'youtube', 'meta', 'tiktok', 'spotify')
    name: str = "base"
    #: Anzeige-Name
    label: str = "Plattform"

    # ------------------------------------------------------------------

    @abstractmethod
    def info(self) -> PlatformInfo:
        """Verfuegbarkeit/Verbindungsstatus OHNE unnoetige API-Aufrufe."""

    @abstractmethod
    def is_ready(self) -> tuple[bool, str]:
        """Kann jetzt hochgeladen werden? (Konfiguration + Autorisierung)"""

    @abstractmethod
    def upload(self, job: PublicationJob) -> UploadResult:
        """Datei hochladen - IMMER als privat/unsichtbar."""

    @abstractmethod
    def set_thumbnail(self, platform_video_id: str, thumbnail_path: str) -> ThumbnailResult:
        """Thumbnail setzen (darf den Upload nicht blockieren)."""

    @abstractmethod
    def publish(self, job: PublicationJob, *, confirm: bool = False, privacy_status: str = "public") -> PublishResult:
        """Inhalt oeffentlich schalten - nur mit ausdruecklicher Bestaetigung."""

    # ------------------------------------------------------------------
    # optionale Erweiterungen (mit sinnvollen Defaults)
    # ------------------------------------------------------------------

    def fetch_status(self, platform_video_id: str) -> dict[str, Any]:
        """Aktuellen Status bei der Plattform abfragen."""

        raise NotImplementedError

    def find_existing_upload(
        self,
        *,
        title: str,
        since: str | None = None,
        duration_seconds: float | None = None,
        limit: int = 25,
    ) -> str | None:
        """Nach einem moeglicherweise schon vorhandenen Upload suchen.

        Wird fuer die Wiederanlauf-Erkennung nach einem Crash benutzt, damit
        nicht versehentlich ein zweites Mal hochgeladen wird.
        """

        return None

    def validate_online(self, metadata: EpisodeMetadata) -> Iterable[str]:
        """Optionale Online-Pruefung (z. B. Kategorie-ID). Liefert Warnungen."""

        return []
