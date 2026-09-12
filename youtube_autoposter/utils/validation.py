"""Validierung VOR dem Upload.

Drei Bausteine:

1. :func:`validate_metadata`  - JSON-Inhalt gegen YouTube-Grenzwerte
2. :func:`validate_video_file` - Existenz/Groesse/Format + ffprobe-Analyse
3. :func:`validate_thumbnail`  - Format/Groesse/Lesbarkeit (nie upload-blockierend)

Das Ergebnis (:class:`ValidationResult`) wird 1:1 in der Datenbank gespeichert
und in der Oberflaeche angezeigt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import (
    DESCRIPTION_MAX_LENGTH,
    TAGS_TOTAL_MAX_LENGTH,
    TAG_MAX_LENGTH,
    THUMBNAIL_MAX_BYTES,
    TITLE_MAX_LENGTH,
    VIDEO_MAX_BYTES,
)
from ..metadata.parser import EpisodeMetadata
from .files import file_size, format_duration, human_size
from .media import MediaInfo, ThumbnailInfo, probe_thumbnail, probe_video

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
LEVEL_INFO = "info"

#: YouTube akzeptiert Videos bis 12 Stunden Laenge
MAX_DURATION_SECONDS = 12 * 3600

RECOMMENDED_THUMBNAIL = (1280, 720)


@dataclass(frozen=True)
class Issue:
    level: str
    code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message, "field": self.field}

    def __str__(self) -> str:  # pragma: no cover - Komfort
        return f"[{self.level.upper()}] {self.message}"


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)
    media_info: MediaInfo | None = None
    thumbnail_info: ThumbnailInfo | None = None
    checked_files: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == LEVEL_ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == LEVEL_WARNING]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.level == LEVEL_INFO]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, level: str, code: str, message: str, field_name: str | None = None) -> Issue:
        issue = Issue(level, code, message, field_name)
        self.issues.append(issue)
        return issue

    def error(self, code: str, message: str, field_name: str | None = None) -> Issue:
        return self.add(LEVEL_ERROR, code, message, field_name)

    def warning(self, code: str, message: str, field_name: str | None = None) -> Issue:
        return self.add(LEVEL_WARNING, code, message, field_name)

    def info(self, code: str, message: str, field_name: str | None = None) -> Issue:
        return self.add(LEVEL_INFO, code, message, field_name)

    def extend(self, issues: list[Issue]) -> None:
        self.issues.extend(issues)

    @property
    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "OK"
        parts = []
        if self.errors:
            parts.append(f"{len(self.errors)} Fehler")
        if self.warnings:
            parts.append(f"{len(self.warnings)} Warnungen")
        return ", ".join(parts) or "OK"

    def error_messages(self) -> list[str]:
        return [issue.message for issue in self.errors]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "issues": [issue.to_dict() for issue in self.issues],
            "media_info": self.media_info.to_dict() if self.media_info else None,
            "thumbnail_info": self.thumbnail_info.to_dict() if self.thumbnail_info else None,
            "checked_files": dict(self.checked_files),
        }


# ---------------------------------------------------------------------------
# Metadaten
# ---------------------------------------------------------------------------


def validate_metadata(metadata: EpisodeMetadata | None, settings: Any = None) -> ValidationResult:
    result = ValidationResult()
    if metadata is None:
        result.error("metadata_missing", "Keine Metadaten vorhanden (JSON-Datei fehlt oder ist ungueltig)")
        return result

    if not metadata.title or not metadata.title.strip():
        result.error("title_missing", "Titel fehlt ('title' muss angegeben sein)", "title")
    elif len(metadata.title) > TITLE_MAX_LENGTH:
        result.error(
            "title_too_long",
            f"Titel ist {len(metadata.title)} Zeichen lang, erlaubt sind maximal {TITLE_MAX_LENGTH}",
            "title",
        )

    if not metadata.description or not metadata.description.strip():
        result.error("description_missing", "Beschreibung fehlt ('description' muss angegeben sein)", "description")
    elif len(metadata.description) > DESCRIPTION_MAX_LENGTH:
        result.error(
            "description_too_long",
            f"Beschreibung ist {len(metadata.description)} Zeichen lang, erlaubt sind maximal "
            f"{DESCRIPTION_MAX_LENGTH}",
            "description",
        )

    for tag in metadata.tags:
        if not isinstance(tag, str):
            result.error("tag_invalid_type", f"Tag hat falschen Datentyp: {tag!r}", "tags")
        elif len(tag) > TAG_MAX_LENGTH:
            result.error("tag_too_long", f"Tag '{tag[:25]}...' ueberschreitet {TAG_MAX_LENGTH} Zeichen", "tags")
    if metadata.tags_total_length > TAGS_TOTAL_MAX_LENGTH:
        result.error(
            "tags_too_long_total",
            f"Alle Tags zusammen: {metadata.tags_total_length} Zeichen, erlaubt sind maximal "
            f"{TAGS_TOTAL_MAX_LENGTH}",
            "tags",
        )

    if metadata.category_id:
        if not metadata.category_id.isdigit():
            result.error(
                "category_invalid",
                f"category_id '{metadata.category_id}' ist keine gueltige YouTube-Kategorie "
                f"(Zahl als Text erwartet, z. B. \"27\")",
                "category_id",
            )
    else:
        default_category = getattr(settings, "DEFAULT_CATEGORY_ID", "") if settings else ""
        if default_category:
            result.info(
                "category_default",
                f"Keine category_id angegeben - Standardwert '{default_category}' wird verwendet",
                "category_id",
            )
        else:
            result.error("category_missing", "Keine category_id angegeben und kein Standardwert konfiguriert", "category_id")

    if metadata.language and len(metadata.language) > 64:
        result.warning("language_suspicious", f"'language' sieht ungewoehnlich aus: {metadata.language!r}", "language")

    if metadata.publish_at:
        result.info(
            "publish_at_ignored",
            "publish_at wird in V1 nicht an YouTube gesendet (keine automatische Veroeffentlichung)",
            "publish_at",
        )

    if metadata.privacy_status_requested and metadata.privacy_status_requested != "private":
        result.warning(
            "privacy_overridden",
            f"JSON verlangt '{metadata.privacy_status_requested}' - wird ignoriert, Upload erfolgt privat",
            "privacy_status",
        )

    return result


# ---------------------------------------------------------------------------
# Videodatei
# ---------------------------------------------------------------------------


def validate_video_file(
    path: str | Path | None,
    settings: Any = None,
    *,
    run_probe: bool = True,
) -> ValidationResult:
    result = ValidationResult()
    if not path:
        result.error("video_missing", "Keine Videodatei gefunden")
        return result

    video_path = Path(path)
    result.checked_files["video"] = str(video_path)

    if not video_path.exists():
        result.error("video_missing", f"Videodatei existiert nicht: {video_path}")
        return result
    if not video_path.is_file():
        result.error("video_not_a_file", f"Pfad ist keine Datei: {video_path}")
        return result

    allowed = tuple(getattr(settings, "ALLOWED_VIDEO_EXTENSIONS", (".mp4",)) if settings else (".mp4",))
    suffix = video_path.suffix.lower()
    if suffix not in allowed:
        result.error(
            "video_unsupported_format",
            f"Nicht unterstuetztes Videoformat '{suffix or '(ohne Endung)'}'. "
            f"Erlaubt: {', '.join(allowed)}. "
            f"(Konfiguration: ALLOWED_VIDEO_EXTENSIONS / ALLOW_EXTRA_VIDEO_EXTENSIONS)",
            "video",
        )

    size = file_size(video_path)
    if size <= 0:
        result.error("video_empty", f"Videodatei ist leer (0 Byte): {video_path.name}")
        return result
    if size > VIDEO_MAX_BYTES:
        result.error(
            "video_too_large",
            f"Videodatei ist {human_size(size)} gross - YouTube erlaubt maximal 256 GB",
            "video",
        )

    if not run_probe:
        return result

    media_info = probe_video(video_path, settings)
    result.media_info = media_info

    if not media_info.probe_ok:
        require = bool(getattr(settings, "REQUIRE_FFPROBE", False)) if settings else False
        message = media_info.probe_error or "Videodatei konnte nicht analysiert werden"
        if require:
            result.error("video_unreadable", f"Technische Pruefung fehlgeschlagen: {message}", "video")
        else:
            result.warning(
                "video_probe_skipped",
                f"Technische Pruefung nicht moeglich ({message}). Upload wird nicht blockiert, "
                f"da REQUIRE_FFPROBE=false.",
                "video",
            )
        return result

    if media_info.duration_seconds is not None:
        if media_info.duration_seconds <= 0:
            result.error("video_duration_invalid", "Videodatei hat keine gueltige Dauer", "video")
        elif media_info.duration_seconds > MAX_DURATION_SECONDS:
            result.error(
                "video_too_long",
                f"Video ist {format_duration(media_info.duration_seconds)} lang - YouTube erlaubt "
                f"maximal 12 Stunden",
                "video",
            )
        elif media_info.duration_seconds < 1:
            result.warning("video_very_short", "Video ist kuerzer als 1 Sekunde", "video")

    if not media_info.width or not media_info.height:
        result.warning("video_resolution_unknown", "Bildaufloesung konnte nicht ermittelt werden", "video")
    elif media_info.height > 2160:
        result.info("video_resolution_high", f"Sehr hohe Aufloesung: {media_info.resolution}", "video")

    if not media_info.has_audio:
        result.warning("video_no_audio", "Kein Audio-Stream gefunden", "video")

    if media_info.video_codec and media_info.video_codec.lower() not in {"h264", "avc1", "hevc", "h265", "vp9", "av1", "mpeg4"}:
        result.info(
            "video_codec_unusual",
            f"Ungewoehnlicher Video-Codec: {media_info.video_codec} (YouTube empfiehlt H.264)",
            "video",
        )

    result.info(
        "video_probe_ok",
        f"Technische Pruefung OK: {media_info.resolution}, {media_info.duration_text}, "
        f"{media_info.codec_text}, {media_info.size_text}",
        "video",
    )
    return result


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------


def validate_thumbnail(path: str | Path | None, settings: Any = None) -> ValidationResult:
    """Thumbnail pruefen. Ein Fehler hier blockiert den Upload NIE."""

    result = ValidationResult()
    if not path:
        result.info("thumbnail_missing", "Kein Thumbnail gefunden - Video wird trotzdem hochgeladen")
        return result

    thumb_path = Path(path)
    result.checked_files["thumbnail"] = str(thumb_path)

    if not thumb_path.exists():
        result.info(
            "thumbnail_missing",
            f"Thumbnail nicht gefunden ({thumb_path.name}) - Video wird trotzdem hochgeladen",
        )
        return result

    allowed = tuple(getattr(settings, "ALLOWED_THUMBNAIL_EXTENSIONS", (".jpg", ".jpeg", ".png")) if settings else (".jpg", ".jpeg", ".png"))
    if thumb_path.suffix.lower() not in allowed:
        result.error(
            "thumbnail_unsupported_format",
            f"Nicht unterstuetztes Thumbnail-Format '{thumb_path.suffix}' (erlaubt: {', '.join(allowed)}). "
            f"Das Video wird trotzdem hochgeladen, das Thumbnail wird uebersprungen.",
            "thumbnail",
        )

    info = probe_thumbnail(thumb_path)
    result.thumbnail_info = info

    if info.size_bytes <= 0:
        result.error("thumbnail_empty", "Thumbnail-Datei ist leer (0 Byte) - wird uebersprungen", "thumbnail")
    elif info.size_bytes > THUMBNAIL_MAX_BYTES:
        result.error(
            "thumbnail_too_large",
            f"Thumbnail ist {info.size_text} gross - YouTube erlaubt maximal 2 MB. "
            f"Das Video wird trotzdem hochgeladen, das Thumbnail wird uebersprungen.",
            "thumbnail",
        )

    if not info.readable:
        result.error(
            "thumbnail_unreadable",
            f"Thumbnail konnte nicht gelesen werden: {info.error or 'unbekannter Fehler'}. "
            f"Das Video wird trotzdem hochgeladen.",
            "thumbnail",
        )
    else:
        if info.width and info.height:
            ratio = info.width / info.height
            if abs(ratio - (16 / 9)) > 0.06:
                result.warning(
                    "thumbnail_aspect_ratio",
                    f"Thumbnail-Seitenverhaeltnis {info.width}x{info.height} weicht von 16:9 ab "
                    f"(empfohlen: {RECOMMENDED_THUMBNAIL[0]}x{RECOMMENDED_THUMBNAIL[1]})",
                    "thumbnail",
                )
            if (info.width, info.height) != RECOMMENDED_THUMBNAIL and info.width < RECOMMENDED_THUMBNAIL[0]:
                result.warning(
                    "thumbnail_small",
                    f"Thumbnail ist kleiner als die empfohlenen {RECOMMENDED_THUMBNAIL[0]}x{RECOMMENDED_THUMBNAIL[1]} Pixel",
                    "thumbnail",
                )
        result.info(
            "thumbnail_ok",
            f"Thumbnail OK: {info.format}, {info.width or '?'}x{info.height or '?'}, {info.size_text}",
            "thumbnail",
        )

    return result


# ---------------------------------------------------------------------------
# Gesamt-Validierung einer Episode
# ---------------------------------------------------------------------------


def validate_episode(
    *,
    metadata: EpisodeMetadata | None,
    video_path: str | Path | None,
    thumbnail_path: str | Path | None = None,
    settings: Any = None,
    run_probe: bool = True,
) -> ValidationResult:
    """Alles zusammen pruefen: Metadaten + Video + Thumbnail."""

    combined = ValidationResult()

    meta_result = validate_metadata(metadata, settings)
    combined.extend(meta_result.issues)

    video_result = validate_video_file(video_path, settings, run_probe=run_probe)
    combined.extend(video_result.issues)
    combined.media_info = video_result.media_info
    combined.checked_files.update(video_result.checked_files)

    thumb_result = validate_thumbnail(thumbnail_path, settings)
    combined.thumbnail_info = thumb_result.thumbnail_info
    combined.checked_files.update(thumb_result.checked_files)
    for issue in thumb_result.issues:
        # Thumbnail-Probleme duerfen einen Upload niemals verhindern:
        # Fehler werden zu Warnungen herabgestuft.
        if issue.level == LEVEL_ERROR:
            combined.add(LEVEL_WARNING, issue.code, issue.message, issue.field)
        else:
            combined.issues.append(issue)

    return combined


def describe_media(media_info: MediaInfo | None) -> str:
    if not media_info:
        return "keine technischen Daten"
    if not media_info.probe_ok:
        return media_info.probe_error or "Analyse nicht moeglich"
    return (
        f"{media_info.resolution} | {media_info.duration_text} | {media_info.codec_text} | "
        f"{media_info.size_text}"
    )
