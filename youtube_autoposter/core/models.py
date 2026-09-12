"""Datenmodell fuer die Anzeige: eine Episode inkl. abgeleiteter Eigenschaften."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..constants import STATUS_LABELS, UPLOADED_STATUSES, VideoStatus
from ..database.repository import load_json
from ..utils.files import format_duration, format_local, human_size

#: Stati, in denen der Button "VEROEFFENTLICHEN" sichtbar und aktiv ist.
PUBLISHABLE_STATUSES = (VideoStatus.UPLOADED_PRIVATE.value, VideoStatus.PUBLISH_READY.value)

#: Stati, in denen "ERNEUT VERSUCHEN" angeboten wird.
RETRYABLE_STATUSES = (
    VideoStatus.FAILED.value,
    VideoStatus.INTERRUPTED.value,
    VideoStatus.PAUSED.value,
)

MAIN_FIELDS = (
    "id",
    "episode_id",
    "platform",
    "status",
    "title",
    "description",
    "video_filename",
    "video_path",
    "metadata_filename",
    "metadata_path",
    "thumbnail_filename",
    "thumbnail_path",
    "file_size",
    "file_hash",
    "folder_state",
    "archived_path",
    "category_id",
    "language",
    "youtube_video_id",
    "youtube_url",
    "thumbnail_url",
    "thumbnail_set",
    "thumbnail_error",
    "channel_id",
    "channel_title",
    "duration_seconds",
    "width",
    "height",
    "video_codec",
    "audio_codec",
    "fps",
    "has_audio",
    "progress_percent",
    "retry_count",
    "error_message",
    "error_code",
    "needs_attention",
    "privacy_status_requested",
    "effective_privacy_status",
    "published_privacy_status",
    "publish_at_requested",
    "upload_started_at",
    "upload_completed_at",
    "published_at",
    "created_at",
    "updated_at",
)


@dataclass
class VideoRecord:
    """Eine Zeile aus der ``videos``-Tabelle, aufbereitet fuer UI/Logik."""

    id: int = 0
    episode_id: str = ""
    platform: str = "youtube"
    status: str = VideoStatus.NEW.value
    title: str | None = None
    description: str | None = None
    video_filename: str | None = None
    video_path: str | None = None
    metadata_filename: str | None = None
    metadata_path: str | None = None
    thumbnail_filename: str | None = None
    thumbnail_path: str | None = None
    file_size: int | None = 0
    file_hash: str | None = None
    folder_state: str | None = None
    archived_path: str | None = None
    category_id: str | None = None
    language: str | None = None
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    thumbnail_url: str | None = None
    thumbnail_set: int | None = 0
    thumbnail_error: str | None = None
    channel_id: str | None = None
    channel_title: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    fps: float | None = None
    has_audio: int | None = None
    progress_percent: int | None = 0
    retry_count: int | None = 0
    error_message: str | None = None
    error_code: str | None = None
    needs_attention: int | None = 0
    privacy_status_requested: str | None = None
    effective_privacy_status: str | None = None
    published_privacy_status: str | None = None
    publish_at_requested: str | None = None
    upload_started_at: str | None = None
    upload_completed_at: str | None = None
    published_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    tags: list[str] = field(default_factory=list)
    media_info: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    thumbnail_info: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> "VideoRecord | None":
        if not row:
            return None
        kwargs: dict[str, Any] = {name: row.get(name) for name in MAIN_FIELDS if name in row}
        kwargs["tags"] = load_json(row.get("tags_json"), []) or []
        kwargs["media_info"] = load_json(row.get("media_info_json"), {}) or {}
        kwargs["validation"] = load_json(row.get("validation_json"), {}) or {}
        kwargs["thumbnail_info"] = load_json(row.get("thumbnail_info_json"), {}) or {}
        kwargs["metadata"] = load_json(row.get("metadata_json"), {}) or {}
        kwargs["raw"] = dict(row)
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {name: getattr(self, name, None) for name in MAIN_FIELDS}
        data.update(
            {
                "tags": list(self.tags),
                "media_info": dict(self.media_info),
                "validation": dict(self.validation),
                "thumbnail_info": dict(self.thumbnail_info),
                "metadata": dict(self.metadata),
                "status_label": self.status_label,
                "display_title": self.display_title,
                "is_uploaded": self.is_uploaded,
                "is_published": self.is_published,
                "can_publish": self.can_publish,
                "can_retry": self.can_retry,
                "size_text": self.size_text,
                "duration_text": self.duration_text,
                "resolution_text": self.resolution_text,
                "codec_text": self.codec_text,
                "created_at_local": self.created_at_local,
                "updated_at_local": self.updated_at_local,
                "upload_completed_at_local": self.upload_completed_at_local,
                "published_at_local": self.published_at_local,
                "youtube_url": self.youtube_url,
            }
        )
        return data

    # --- abgeleitete Anzeige-Werte --------------------------------------

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def display_title(self) -> str:
        return (self.title or "").strip() or self.episode_id or f"Episode #{self.id}"

    @property
    def is_uploaded(self) -> bool:
        return self.status in tuple(s.value for s in UPLOADED_STATUSES)

    @property
    def is_published(self) -> bool:
        return self.status == VideoStatus.PUBLISHED.value

    @property
    def is_failed(self) -> bool:
        return self.status in (VideoStatus.FAILED.value, VideoStatus.INTERRUPTED.value)

    @property
    def can_publish(self) -> bool:
        return self.status in PUBLISHABLE_STATUSES and bool(self.youtube_video_id)

    @property
    def can_retry(self) -> bool:
        return self.status in RETRYABLE_STATUSES

    @property
    def size_text(self) -> str:
        return human_size(self.file_size)

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration_seconds)

    @property
    def resolution_text(self) -> str:
        if self.width and self.height:
            return f"{self.width} x {self.height}"
        return "-"

    @property
    def codec_text(self) -> str:
        video = self.video_codec or "-"
        audio = self.audio_codec if self.has_audio else "kein Audio"
        return f"{video} / {audio or '-'}"

    @property
    def created_at_local(self) -> str:
        return format_local(self.created_at)

    @property
    def updated_at_local(self) -> str:
        return format_local(self.updated_at)

    @property
    def upload_completed_at_local(self) -> str:
        return format_local(self.upload_completed_at)

    @property
    def published_at_local(self) -> str:
        return format_local(self.published_at)

    @property
    def youtube_watch_url(self) -> str:
        if self.youtube_url:
            return str(self.youtube_url)
        if self.youtube_video_id:
            return f"https://www.youtube.com/watch?v={self.youtube_video_id}"
        return ""

    def validation_issues(self, level: str | None = None) -> list[dict[str, Any]]:
        issues = self.validation.get("issues") or []
        if level is None:
            return list(issues)
        return [issue for issue in issues if issue.get("level") == level]
