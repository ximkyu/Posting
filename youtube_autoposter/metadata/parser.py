"""JSON-Metadaten parsen und in YouTube-API-Payloads uebersetzen.

Unterstuetztes Format (Minimalbeispiel)::

    {
      "title": "Die verborgene Lehre des Thot",
      "description": "Eine Reise in die Welt der alten aegyptischen Symbolik ...",
      "tags": ["Thot", "Aegypten", "Hermetik"],
      "category_id": "27",
      "language": "de",
      "privacy_status": "private"
    }

Optionale Felder: ``thumbnail``, ``publish_at``, ``playlist_id``,
``channel_identifier``, ``made_for_kids``, ``self_declared_made_for_kids``,
``contains_synthetic_media``, ``embeddable``, ``license``,
``notify_subscribers``, ``audio_language``.

SICHERHEITSREGEL: Egal was in ``privacy_status`` steht - beim Upload wird
immer ``private`` gesendet (siehe :func:`build_upload_body`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import (
    DEFAULT_CATEGORY_ID,
    DESCRIPTION_MAX_LENGTH,
    ENFORCED_UPLOAD_PRIVACY,
    PRIVACY_PRIVATE,
    PRIVACY_PUBLIC,
    PRIVACY_UNLISTED,
    TAGS_TOTAL_MAX_LENGTH,
    TAG_MAX_LENGTH,
    TITLE_MAX_LENGTH,
)
from ..errors import MetadataError
from ..utils.files import parse_iso, to_iso

#: Alternative Schreibweisen, die ebenfalls akzeptiert werden.
ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("titel", "video_title", "videotitle", "name"),
    "description": ("beschreibung", "desc", "descr", "video_description", "text"),
    "tags": ("keywords", "keyword", "schlagwoerter", "tag_list"),
    "category_id": ("categoryid", "category", "kategorie", "youtube_category", "catid"),
    "language": ("lang", "default_language", "defaultlanguage", "sprache"),
    "audio_language": ("defaultaudiolanguage", "audio_lang", "tonsprache"),
    "privacy_status": ("privacy", "privacystatus", "sichtbarkeit", "visibility"),
    "thumbnail": ("thumb", "thumbnail_file", "thumbnail_filename", "vorschaubild", "cover"),
    "publish_at": ("publishat", "scheduled_at", "schedule", "publish_date", "veroeffentlichen_am"),
    "playlist_id": ("playlistid", "playlist"),
    "channel_identifier": ("channel_id", "channelid", "channel", "kanal"),
    "made_for_kids": ("madeforkids", "kinder", "for_kids"),
    "self_declared_made_for_kids": ("selfdeclaredmadeforkids", "coppa"),
    "contains_synthetic_media": ("containssyntheticmedia", "synthetic_media", "ai_generated", "ki_inhalt"),
    "embeddable": ("einbettbar"),
    "license": ("lizenz"),
    "notify_subscribers": ("notifysubscribers", "abonnenten_benachrichtigen"),
}

_ALIAS_LOOKUP: dict[str, str] = {}
for _canonical, _variants in ALIASES.items():
    _ALIAS_LOOKUP[_canonical] = _canonical
    for _variant in _variants:
        _ALIAS_LOOKUP[_variant] = _canonical

VALID_PRIVACY = (PRIVACY_PRIVATE, PRIVACY_UNLISTED, PRIVACY_PUBLIC)


@dataclass
class EpisodeMetadata:
    """Normalisierte Metadaten einer Episode."""

    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category_id: str = ""
    language: str = ""
    audio_language: str = ""
    privacy_status_requested: str = ""
    publish_at: str | None = None
    publish_at_raw: str | None = None
    thumbnail: str | None = None
    playlist_id: str | None = None
    channel_identifier: str | None = None
    made_for_kids: bool | None = None
    self_declared_made_for_kids: bool | None = None
    contains_synthetic_media: bool | None = None
    embeddable: bool | None = None
    license: str | None = None
    notify_subscribers: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------

    @property
    def effective_privacy(self) -> str:
        """Beim Upload verwendeter Wert - immer private (Sicherheitsregel)."""

        return ENFORCED_UPLOAD_PRIVACY

    @property
    def effective_made_for_kids(self) -> bool:
        if self.self_declared_made_for_kids is not None:
            return bool(self.self_declared_made_for_kids)
        if self.made_for_kids is not None:
            return bool(self.made_for_kids)
        return False

    @property
    def tags_total_length(self) -> int:
        # YouTube zaehlt die Tags inkl. der Trennzeichen
        return sum(len(tag) + 2 for tag in self.tags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "category_id": self.category_id,
            "language": self.language,
            "audio_language": self.audio_language,
            "privacy_status_requested": self.privacy_status_requested,
            "effective_privacy_status": self.effective_privacy,
            "publish_at": self.publish_at,
            "thumbnail": self.thumbnail,
            "playlist_id": self.playlist_id,
            "channel_identifier": self.channel_identifier,
            "made_for_kids": self.made_for_kids,
            "self_declared_made_for_kids": self.self_declared_made_for_kids,
            "effective_made_for_kids": self.effective_made_for_kids,
            "contains_synthetic_media": self.contains_synthetic_media,
            "embeddable": self.embeddable,
            "license": self.license,
            "notify_subscribers": self.notify_subscribers,
            "extra": dict(self.extra),
            "source_path": self.source_path,
        }


@dataclass
class MetadataIssue:
    level: str          # error | warning | info
    code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message, "field": self.field}

    def __str__(self) -> str:  # pragma: no cover - Komfort
        return f"[{self.level.upper()}] {self.message}"


@dataclass
class ParseResult:
    metadata: EpisodeMetadata
    errors: list[MetadataIssue] = field(default_factory=list)
    warnings: list[MetadataIssue] = field(default_factory=list)
    infos: list[MetadataIssue] = field(default_factory=list)

    @property
    def issues(self) -> list[MetadataIssue]:
        return [*self.errors, *self.warnings, *self.infos]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, issue: MetadataIssue) -> None:
        if issue.level == "error":
            self.errors.append(issue)
        elif issue.level == "warning":
            self.warnings.append(issue)
        else:
            self.infos.append(issue)

    def error_messages(self) -> list[str]:
        return [issue.message for issue in self.errors]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "metadata": self.metadata.to_dict(),
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
            "infos": [i.to_dict() for i in self.infos],
        }


# ---------------------------------------------------------------------------
# Parsen
# ---------------------------------------------------------------------------


def _canonical_keys(data: dict[str, Any]) -> dict[str, Any]:
    """JSON-Schluessel auf kanonische Namen abbilden."""

    out: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    for key, value in data.items():
        normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
        canonical = _ALIAS_LOOKUP.get(normalized)
        if canonical:
            out[canonical] = value
        else:
            unknown[str(key)] = value
    if unknown:
        out["__unknown__"] = unknown
    return out


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1", "ja", "wahr"}:
        return True
    if text in {"false", "no", "n", "0", "nein", "falsch"}:
        return False
    return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return str(value)


def _as_tags(value: Any, result: ParseResult) -> list[str]:
    tags: list[str] = []
    if value is None:
        return tags
    if isinstance(value, str):
        result.add(
            MetadataIssue(
                "warning",
                "tags_string",
                "'tags' wurde als Text gefunden und an Kommas getrennt - "
                "empfohlen wird eine JSON-Liste: [\"Tag1\", \"Tag2\"]",
                "tags",
            )
        )
        candidates: list[Any] = [part for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        result.add(
            MetadataIssue(
                "error",
                "tags_invalid_type",
                "'tags' muss eine Liste aus Texten sein (z. B. [\"Thot\", \"Aegypten\"])",
                "tags",
            )
        )
        return tags

    for entry in candidates:
        if isinstance(entry, (dict, list)):
            result.add(
                MetadataIssue(
                    "error",
                    "tags_invalid_entry",
                    f"Ungueltiger Tag-Eintrag: {entry!r} (nur Texte erlaubt)",
                    "tags",
                )
            )
            continue
        text = _as_text(entry).strip()
        if not text:
            continue
        if text not in tags:
            tags.append(text)
    return tags


def parse_metadata_dict(data: Any, *, source_path: str | None = None) -> ParseResult:
    """Ein (bereits geladenes) JSON-Objekt in :class:`EpisodeMetadata` ueberfuehren."""

    if not isinstance(data, dict):
        raise MetadataError(
            "Metadaten muessen ein JSON-Objekt sein",
            details=f"gefunden: {type(data).__name__}",
        )

    result = ParseResult(metadata=EpisodeMetadata())
    values = _canonical_keys(data)
    unknown = values.pop("__unknown__", {})
    result.metadata.raw = dict(data)
    result.metadata.source_path = source_path
    if unknown:
        result.metadata.extra = dict(unknown)
        result.add(
            MetadataIssue(
                "info",
                "unknown_fields",
                "Unbekannte Felder werden gespeichert, aber nicht an YouTube gesendet: "
                + ", ".join(sorted(unknown)[:8]),
            )
        )

    metadata = result.metadata
    metadata.title = _as_text(values.get("title")).strip()
    metadata.description = _as_text(values.get("description")).strip()
    metadata.tags = _as_tags(values.get("tags"), result)

    category = values.get("category_id")
    if category is None:
        metadata.category_id = ""
    elif isinstance(category, bool):
        result.add(MetadataIssue("error", "category_invalid", "'category_id' muss eine Zahl als Text sein, z. B. \"27\"", "category_id"))
    else:
        metadata.category_id = _as_text(category).strip()
        if metadata.category_id.endswith(".0"):
            metadata.category_id = metadata.category_id[:-2]
        if metadata.category_id and not metadata.category_id.isdigit():
            result.add(
                MetadataIssue(
                    "error",
                    "category_invalid",
                    f"'category_id' muss eine numerische YouTube-Kategorie sein (gefunden: {metadata.category_id!r})",
                    "category_id",
                )
            )

    metadata.language = _as_text(values.get("language")).strip()
    metadata.audio_language = _as_text(values.get("audio_language")).strip()

    privacy = _as_text(values.get("privacy_status")).strip().lower()
    metadata.privacy_status_requested = privacy
    if privacy and privacy not in VALID_PRIVACY:
        result.add(
            MetadataIssue(
                "error",
                "privacy_invalid",
                f"'privacy_status' ungueltig: {privacy!r} (erlaubt: private, unlisted, public)",
                "privacy_status",
            )
        )
    if privacy and privacy != PRIVACY_PRIVATE:
        result.add(
            MetadataIssue(
                "warning",
                "privacy_overridden",
                f"ACHTUNG: '{privacy}' in der JSON wird ignoriert. Sicherheitsregel: "
                f"jeder Upload erfolgt zunaechst als '{PRIVACY_PRIVATE}'. "
                f"Veroeffentlicht wird ausschliesslich ueber den Button in der Oberflaeche.",
                "privacy_status",
            )
        )
    if not privacy:
        metadata.privacy_status_requested = ""

    publish_at = values.get("publish_at")
    if publish_at not in (None, "", "null"):
        metadata.publish_at_raw = _as_text(publish_at).strip()
        moment = parse_iso(metadata.publish_at_raw)
        if moment is None:
            result.add(
                MetadataIssue(
                    "warning",
                    "publish_at_invalid",
                    f"'publish_at' konnte nicht als Datum gelesen werden: {metadata.publish_at_raw!r}",
                    "publish_at",
                )
            )
        else:
            metadata.publish_at = to_iso(moment)
            result.add(
                MetadataIssue(
                    "info",
                    "publish_at_ignored",
                    "'publish_at' wird in V1 NICHT an YouTube gesendet (keine automatische "
                    "Veroeffentlichung). Der Wert dient nur als Hinweis im Dashboard.",
                    "publish_at",
                )
            )

    metadata.thumbnail = _as_text(values.get("thumbnail")).strip() or None
    metadata.playlist_id = _as_text(values.get("playlist_id")).strip() or None
    metadata.channel_identifier = _as_text(values.get("channel_identifier")).strip() or None
    metadata.made_for_kids = _as_bool(values.get("made_for_kids"))
    metadata.self_declared_made_for_kids = _as_bool(values.get("self_declared_made_for_kids"))
    metadata.contains_synthetic_media = _as_bool(values.get("contains_synthetic_media"))
    metadata.embeddable = _as_bool(values.get("embeddable"))
    metadata.notify_subscribers = _as_bool(values.get("notify_subscribers"))

    license_value = _as_text(values.get("license")).strip().lower()
    if license_value:
        if license_value in {"youtube", "creativecommon", "creativecommons"}:
            metadata.license = "creativeCommon" if license_value.startswith("creative") else "youtube"
        else:
            result.add(
                MetadataIssue(
                    "warning",
                    "license_invalid",
                    f"'license' ungueltig: {license_value!r} (erlaubt: youtube, creativeCommon)",
                    "license",
                )
            )

    # --- Pflichtfelder ------------------------------------------------
    if not metadata.title:
        result.add(MetadataIssue("error", "title_missing", "'title' fehlt oder ist leer", "title"))
    elif len(metadata.title) > TITLE_MAX_LENGTH:
        result.add(
            MetadataIssue(
                "error",
                "title_too_long",
                f"'title' ist {len(metadata.title)} Zeichen lang - YouTube erlaubt maximal "
                f"{TITLE_MAX_LENGTH} Zeichen",
                "title",
            )
        )

    if not metadata.description:
        result.add(
            MetadataIssue("error", "description_missing", "'description' fehlt oder ist leer", "description")
        )
    elif len(metadata.description) > DESCRIPTION_MAX_LENGTH:
        result.add(
            MetadataIssue(
                "error",
                "description_too_long",
                f"'description' ist {len(metadata.description)} Zeichen lang - YouTube erlaubt "
                f"maximal {DESCRIPTION_MAX_LENGTH} Zeichen",
                "description",
            )
        )

    for tag in metadata.tags:
        if len(tag) > TAG_MAX_LENGTH:
            result.add(
                MetadataIssue(
                    "error",
                    "tag_too_long",
                    f"Tag '{tag[:30]}...' ist {len(tag)} Zeichen lang - maximal {TAG_MAX_LENGTH}",
                    "tags",
                )
            )
    if metadata.tags_total_length > TAGS_TOTAL_MAX_LENGTH:
        result.add(
            MetadataIssue(
                "error",
                "tags_too_long_total",
                f"Alle Tags zusammen sind {metadata.tags_total_length} Zeichen - YouTube erlaubt "
                f"maximal {TAGS_TOTAL_MAX_LENGTH} Zeichen",
                "tags",
            )
        )

    return result


def parse_metadata_file(path: str | Path) -> ParseResult:
    """JSON-Datei lesen und parsen. Wirft :class:`MetadataError` bei Defekten."""

    file = Path(path)
    if not file.exists():
        raise MetadataError(f"Metadaten-Datei nicht gefunden: {file}")
    if not file.is_file():
        raise MetadataError(f"Metadaten-Pfad ist keine Datei: {file}")
    try:
        raw = file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise MetadataError(f"Metadaten-Datei konnte nicht gelesen werden: {exc}") from exc
    if not raw.strip():
        raise MetadataError(f"Metadaten-Datei ist leer: {file}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MetadataError(
            f"JSON ist syntaktisch fehlerhaft: {exc.msg} (Zeile {exc.lineno}, Spalte {exc.colno})",
            details=str(file),
        ) from exc
    return parse_metadata_dict(data, source_path=str(file))


# ---------------------------------------------------------------------------
# YouTube-Payload
# ---------------------------------------------------------------------------


def build_upload_body(
    metadata: EpisodeMetadata,
    *,
    settings: Any = None,
    force_private: bool = True,
) -> dict[str, Any]:
    """Body fuer ``videos.insert`` (part=snippet,status).

    ``force_private`` ist die zentrale Sicherheitsregel und kann von keiner
    JSON-Datei ueberschrieben werden.
    """

    default_category = getattr(settings, "DEFAULT_CATEGORY_ID", DEFAULT_CATEGORY_ID) if settings else DEFAULT_CATEGORY_ID
    default_language = getattr(settings, "DEFAULT_LANGUAGE", "de") if settings else "de"
    default_kids = bool(getattr(settings, "DEFAULT_MADE_FOR_KIDS", False)) if settings else False
    send_publish_at = bool(getattr(settings, "SEND_PUBLISH_AT", False)) if settings else False
    send_synthetic = bool(getattr(settings, "SEND_SYNTHETIC_MEDIA_FLAG", False)) if settings else False

    snippet: dict[str, Any] = {
        "title": metadata.title,
        "description": metadata.description,
        "categoryId": metadata.category_id or str(default_category),
    }
    if metadata.tags:
        snippet["tags"] = list(metadata.tags)
    if metadata.language:
        snippet["defaultLanguage"] = metadata.language
    elif default_language:
        snippet["defaultLanguage"] = default_language
    if metadata.audio_language:
        snippet["defaultAudioLanguage"] = metadata.audio_language

    status: dict[str, Any] = {
        # Harte Regel: Upload immer privat.
        "privacyStatus": ENFORCED_UPLOAD_PRIVACY if force_private else (metadata.privacy_status_requested or PRIVACY_PRIVATE),
        "selfDeclaredMadeForKids": bool(
            metadata.self_declared_made_for_kids
            if metadata.self_declared_made_for_kids is not None
            else (metadata.made_for_kids if metadata.made_for_kids is not None else default_kids)
        ),
    }
    if metadata.embeddable is not None:
        status["embeddable"] = bool(metadata.embeddable)
    if metadata.license:
        status["license"] = metadata.license
    if send_synthetic and metadata.contains_synthetic_media is not None:
        status["containsSyntheticMedia"] = bool(metadata.contains_synthetic_media)
    if send_publish_at and force_private is False and metadata.publish_at:
        # Nur im (nicht verwendeten) Nicht-Privat-Modus relevant.
        status["publishAt"] = metadata.publish_at

    return {"snippet": snippet, "status": status}


def build_update_body(
    metadata: EpisodeMetadata,
    youtube_video_id: str,
    *,
    privacy_status: str,
    settings: Any = None,
) -> dict[str, Any]:
    """Body fuer ``videos.update`` (z. B. Veroeffentlichen).

    Wichtig laut API-Doku:

    * ``id`` ist Pflicht.
    * Wird ``snippet`` aktualisiert, muessen ``snippet.title`` UND
      ``snippet.categoryId`` gesetzt sein - deshalb senden wir beim
      Veroeffentlichen bewusst NUR den ``status``-Part.
    * ``status.selfDeclaredMadeForKids`` muss bei Status-Updates mitgegeben
      werden, sonst lehnt die API ab.
    """

    send_synthetic = bool(getattr(settings, "SEND_SYNTHETIC_MEDIA_FLAG", False)) if settings else False
    status: dict[str, Any] = {
        "privacyStatus": privacy_status,
        "selfDeclaredMadeForKids": metadata.effective_made_for_kids,
    }
    if metadata.embeddable is not None:
        status["embeddable"] = bool(metadata.embeddable)
    if metadata.license:
        status["license"] = metadata.license
    if send_synthetic and metadata.contains_synthetic_media is not None:
        status["containsSyntheticMedia"] = bool(metadata.contains_synthetic_media)
    return {"id": youtube_video_id, "status": status}
