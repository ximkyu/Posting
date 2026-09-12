"""Test-Hilfsmittel.

GANZ WICHTIG: Alle Tests laufen ohne Netzwerk und ohne echtes YouTube-Konto.
``FakePlatform`` ersetzt den YouTube-Adapter, ``FakeYouTubeService`` ersetzt
die googleapiclient-Objekte. Es kann dadurch niemals ein reales Video
hochgeladen oder veroeffentlicht werden.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from youtube_autoposter.constants import ENFORCED_UPLOAD_PRIVACY, PRIVACY_PUBLIC
from youtube_autoposter.errors import PlatformError, PublishError, StateError, UploadError
from youtube_autoposter.platforms.base import (
    PlatformAdapter,
    PlatformInfo,
    PublicationJob,
    PublishResult,
    ThumbnailResult,
    UploadResult,
)


class FakePlatform(PlatformAdapter):
    """YouTube-Adapter-Double fuer Pipeline-/UI-Tests."""

    name = "youtube"
    label = "YouTube (Test-Mock)"

    def __init__(
        self,
        *,
        ready: bool = True,
        reason: str = "Test-Mock bereit",
        fail_upload: Exception | None = None,
        fail_uploads: int = 0,
        fail_publish: Exception | None = None,
        fail_thumbnail: str | None = None,
        video_id_prefix: str = "ytid_",
        find_existing: str | None = None,
        online_warnings: list[str] | None = None,
    ) -> None:
        self.ready = ready
        self.reason = reason
        self.fail_upload = fail_upload
        self.fail_uploads = fail_uploads
        self.fail_publish = fail_publish
        self.fail_thumbnail = fail_thumbnail
        self.video_id_prefix = video_id_prefix
        self.find_existing = find_existing
        self.online_warnings = list(online_warnings or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.uploaded: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        self.thumbnails: list[dict[str, Any]] = []
        self._counter = 0

    # ------------------------------------------------------------------

    def _record(self, action: str, **payload: Any) -> None:
        self.calls.append((action, payload))

    def call_count(self, action: str) -> int:
        return len([call for call in self.calls if call[0] == action])

    def reset(self) -> None:
        self.calls.clear()
        self.uploaded.clear()
        self.published.clear()
        self.thumbnails.clear()

    # ------------------------------------------------------------------

    def info(self) -> PlatformInfo:
        return PlatformInfo(
            name=self.name,
            label=self.label,
            available=self.ready,
            message=self.reason,
            channel="Testkanal",
            quota_today={"units": 0, "uploads": 0, "upload_limit_per_day": 100, "calls": {}},
        )

    def is_ready(self) -> tuple[bool, str]:
        self._record("is_ready")
        return self.ready, self.reason

    def upload(self, job: PublicationJob) -> UploadResult:
        self._record("upload", episode_id=job.episode_id, video_path=job.video_path)
        self.uploaded.append(
            {
                "episode_id": job.episode_id,
                "video_path": job.video_path,
                "title": job.metadata.title,
                "privacy": ENFORCED_UPLOAD_PRIVACY,
                "has_thumbnail": bool(job.thumbnail_path),
            }
        )
        # Sicherheitsregel des Mocks: es darf nie etwas anderes als private
        # verlangt werden.
        if job.options.get("privacy_status", ENFORCED_UPLOAD_PRIVACY) != ENFORCED_UPLOAD_PRIVACY:
            raise AssertionError("Test-Mock: Upload mit nicht-privatem Status angefordert")

        if self.fail_uploads and self.call_count("upload") <= self.fail_uploads:
            raise self.fail_upload or UploadError("Test-Fehler beim Upload", reason="backendError", retriable=True)
        if self.fail_upload is not None and self.call_count("upload") > self.fail_uploads:
            raise self.fail_upload

        self._counter += 1
        video_id = f"{self.video_id_prefix}{self._counter:03d}"
        if job.progress_cb:
            total = job.file_size or 1024
            job.progress_cb(total // 2, total)
            job.progress_cb(total, total)
        return UploadResult(
            platform_video_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            privacy_status=ENFORCED_UPLOAD_PRIVACY,
            thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            quota_units=1,
            raw={"id": video_id, "status": {"privacyStatus": ENFORCED_UPLOAD_PRIVACY}},
            bytes_sent=job.file_size or 0,
        )

    def set_thumbnail(self, platform_video_id: str, thumbnail_path: str) -> ThumbnailResult:
        self._record("set_thumbnail", video_id=platform_video_id, path=thumbnail_path)
        self.thumbnails.append({"video_id": platform_video_id, "path": thumbnail_path})
        if self.fail_thumbnail:
            return ThumbnailResult(False, message=self.fail_thumbnail)
        return ThumbnailResult(
            True,
            url=f"https://i.ytimg.com/vi/{platform_video_id}/maxres.jpg",
            message="Thumbnail erfolgreich gesetzt (Mock)",
            quota_units=50,
        )

    def publish(
        self, job: PublicationJob, *, confirm: bool = False, privacy_status: str = PRIVACY_PUBLIC
    ) -> PublishResult:
        self._record(
            "publish",
            episode_id=job.episode_id,
            video_id=job.platform_video_id,
            confirm=confirm,
            privacy_status=privacy_status,
        )
        if not confirm:
            raise StateError("Mock: Veroeffentlichen ohne Bestaetigung verweigert")
        if self.fail_publish is not None:
            raise self.fail_publish
        self.published.append(
            {
                "episode_id": job.episode_id,
                "video_id": job.platform_video_id,
                "privacy_status": privacy_status,
            }
        )
        return PublishResult(
            platform_video_id=job.platform_video_id or "",
            privacy_status=privacy_status,
            url=f"https://www.youtube.com/watch?v={job.platform_video_id}",
            published_at="2026-09-12T14:20:00+00:00",
            quota_units=50,
            raw={"id": job.platform_video_id, "status": {"privacyStatus": privacy_status}},
        )

    def fetch_status(self, platform_video_id: str) -> dict[str, Any]:
        self._record("fetch_status", video_id=platform_video_id)
        return {
            "found": True,
            "id": platform_video_id,
            "privacy_status": "private",
            "upload_status": "processed",
            "title": "Mock-Titel",
            "thumbnail_url": f"https://i.ytimg.com/vi/{platform_video_id}/hqdefault.jpg",
        }

    def find_existing_upload(
        self, *, title: str, since: str | None = None, duration_seconds: float | None = None, limit: int = 25
    ) -> str | None:
        self._record("find_existing_upload", title=title, since=since)
        return self.find_existing

    def validate_online(self, metadata: Any) -> list[str]:
        self._record("validate_online", title=getattr(metadata, "title", ""))
        return list(self.online_warnings)


# ---------------------------------------------------------------------------
# googleapiclient-Double fuer echte Uploader-Tests
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
    status: int = 200
    reason: str = "OK"

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def make_http_error(status: int, reason: str = "backendError", message: str = "Temporaerer Fehler") -> Any:
    """Baut ein echtes ``googleapiclient.errors.HttpError``-Objekt (offline)."""

    from googleapiclient.errors import HttpError

    payload = {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"reason": reason, "message": message, "domain": "youtube.api"}],
            "status": reason.upper(),
        }
    }
    response = FakeResponse(status=status, reason=reason)
    return HttpError(response, json.dumps(payload).encode("utf-8"))


class FakeRequest:
    """Simuliert einen resumable Upload-Request (``next_chunk``)."""

    def __init__(
        self, script: list[Any], *, kwargs: dict[str, Any] | None = None, resumable_progress: int = 0
    ) -> None:
        self.script = script
        self.kwargs = dict(kwargs or {})
        self.resumable_progress = resumable_progress
        self.calls = 0

    def next_chunk(self) -> tuple[Any, Any]:
        self.calls += 1
        if not self.script:
            raise AssertionError("FakeRequest: next_chunk() oefters aufgerufen als geplant")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        progress, response = item
        if progress is not None:
            self.resumable_progress = int(getattr(progress, "resumable_progress", 0) or 0)
        return progress, response


class FakeProgress:
    def __init__(self, fraction: float, total: int = 1024) -> None:
        self._fraction = fraction
        self.resumable_progress = int(total * fraction)
        self.total_size = total

    def progress(self) -> float:
        return self._fraction


class FakeVideosResource:
    def __init__(self, script: list[Any], resumable_progress: int = 0) -> None:
        self.script = script
        self.requests: list[FakeRequest] = []
        self.execute_requests: list[FakeExecuteRequest] = []
        #: bereits gesendete Bytes - entscheidet, ob die Session fortgesetzt wird
        self.resumable_progress = resumable_progress

    def insert(self, **kwargs: Any) -> FakeRequest:
        request = FakeRequest(self.script, kwargs=kwargs, resumable_progress=self.resumable_progress)
        self.requests.append(request)
        return request

    def update(self, **kwargs: Any) -> FakeExecuteRequest:
        request = FakeExecuteRequest(kwargs, self.script)
        self.execute_requests.append(request)
        return request

    def list(self, **kwargs: Any) -> FakeExecuteRequest:
        request = FakeExecuteRequest(kwargs, self.script)
        self.execute_requests.append(request)
        return request


class FakeExecuteRequest:
    """Request-Objekt mit ``execute()`` (fuer update/list/thumbnails)."""

    def __init__(self, kwargs: dict[str, Any], script: list[Any]) -> None:
        self.kwargs = kwargs
        self.script = script
        self.execute_calls = 0

    def execute(self) -> Any:
        self.execute_calls += 1
        if not self.script:
            return {}
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(self.kwargs)
        return item


class FakeThumbnailsResource:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.requests: list[Any] = []

    def set(self, **kwargs: Any) -> FakeExecuteRequest:
        request = FakeExecuteRequest(kwargs, self.script)
        self.requests.append(request)
        return request


class FakeChannelsResource:
    def __init__(self, script: list[Any]) -> None:
        self.script = script

    def list(self, **kwargs: Any) -> FakeExecuteRequest:
        return FakeExecuteRequest(kwargs, self.script)


class FakePlaylistItemsResource:
    def __init__(self, script: list[Any]) -> None:
        self.script = script

    def list(self, **kwargs: Any) -> FakeExecuteRequest:
        return FakeExecuteRequest(kwargs, self.script)


class FakeVideoCategoriesResource:
    def __init__(self, script: list[Any]) -> None:
        self.script = script

    def list(self, **kwargs: Any) -> FakeExecuteRequest:
        return FakeExecuteRequest(kwargs, self.script)


class FakeYouTubeService:
    """Ersatz fuer ``googleapiclient.discovery.build('youtube', 'v3', ...)``."""

    def __init__(self, scripts: dict[str, list[Any]] | None = None, *, resumable_progress: int = 0) -> None:
        scripts = scripts or {}
        self.videos_resource = FakeVideosResource(
            scripts.get("videos", []), resumable_progress=resumable_progress
        )
        self.thumbnails_resource = FakeThumbnailsResource(scripts.get("thumbnails", []))
        self.channels_resource = FakeChannelsResource(scripts.get("channels", []))
        self.playlist_items_resource = FakePlaylistItemsResource(scripts.get("playlistItems", []))
        self.video_categories_resource = FakeVideoCategoriesResource(scripts.get("videoCategories", []))

    def videos(self) -> FakeVideosResource:
        return self.videos_resource

    def thumbnails(self) -> FakeThumbnailsResource:
        return self.thumbnails_resource

    def channels(self) -> FakeChannelsResource:
        return self.channels_resource

    def playlistItems(self) -> FakePlaylistItemsResource:
        return self.playlist_items_resource

    def videoCategories(self) -> FakeVideoCategoriesResource:
        return self.video_categories_resource


@dataclass
class RecordedCall:
    name: str
    kwargs: dict[str, Any] = field(default_factory=dict)


SAMPLE_FFMPEG_STDERR = """ffmpeg version 6.0 Copyright (c) 2000-2023 the FFmpeg developers
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'video_001.mp4':
  Metadata:
    major_brand     : isom
    encoder         : Lavf60.3.100
  Duration: 00:29:41.32, start: 0.000000, bitrate: 8412 kb/s
  Stream #0:0(und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 1920x1080 [SAR 1:1 DAR 16:9], 8280 kb/s, 25 fps, 25 tbr, 12800 tbn (default)
  Stream #0:1(und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 128 kb/s (default)
At least one output file must be specified
"""

SAMPLE_FFPROBE_JSON = {
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "r_frame_rate": "30000/1001",
            "duration": "1781.320000",
        },
        {
            "index": 1,
            "codec_name": "aac",
            "codec_type": "audio",
            "sample_rate": "48000",
            "channels": 2,
        },
    ],
    "format": {
        "filename": "video_001.mp4",
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "1781.320000",
        "size": "1932735283",
        "bit_rate": "8412000",
    },
}


def write_fake_ffprobe(directory: Path, payload: dict[str, Any] | None = None, *, exit_code: int = 0) -> Path:
    """Erzeugt ein ausfuehrbares ffprobe-Skript mit fester JSON-Ausgabe."""

    data = json.dumps(payload or SAMPLE_FFPROBE_JSON)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "ffprobe"
    script.write_text(
        "#!/bin/sh\n"
        f"cat <<'JSON_EOF'\n{data}\nJSON_EOF\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def write_fake_ffmpeg(directory: Path, stderr_text: str = SAMPLE_FFMPEG_STDERR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "ffmpeg"
    script.write_text(
        "#!/bin/sh\n"
        "cat >&2 <<'TXT_EOF'\n"
        f"{stderr_text}\n"
        "TXT_EOF\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def sample_client_secrets(path: Path, *, client_type: str = "installed") -> Path:
    """Erzeugt eine (wertlose) OAuth-Client-Datei fuer Tests."""

    payload = {
        client_type: {
            "client_id": "1234567890-test.apps.googleusercontent.com",
            "project_id": "youtube-autoposter-test",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": "GOCSPX-TESTVALUE-ONLY",
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def sample_token_payload(
    *,
    scopes: list[str] | None = None,
    refresh_token: str | None = "1//test-refresh",
    expiry: str | None = None,
) -> dict[str, Any]:
    """Token-Inhalt fuer Tests (Standard: gueltig fuer eine Stunde)."""

    from datetime import datetime, timedelta, timezone

    payload: dict[str, Any] = {
        "token": "ya29.TEST-ACCESS-TOKEN",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "1234567890-test.apps.googleusercontent.com",
        "client_secret": "GOCSPX-TESTVALUE-ONLY",
        "scopes": scopes or ["https://www.googleapis.com/auth/youtube"],
        "expiry": expiry or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    if refresh_token:
        payload["refresh_token"] = refresh_token
    return payload


__all__ = [
    "FakePlatform",
    "FakeProgress",
    "FakeYouTubeService",
    "RecordedCall",
    "SAMPLE_FFMPEG_STDERR",
    "SAMPLE_FFPROBE_JSON",
    "make_http_error",
    "sample_client_secrets",
    "sample_token_payload",
    "write_fake_ffmpeg",
    "write_fake_ffprobe",
    "PlatformError",
    "PublishError",
    "UploadError",
    "Path",
]
