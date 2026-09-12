"""Tests fuer die YouTube-API-Schicht: resumable Upload, Retries, Thumbnail,
Veroeffentlichen, Quota-Buchhaltung - alles mit gemocktem googleapiclient.

Es wird niemals ein echter API-Aufruf gemacht (Netzwerk ist in den Tests
zusaetzlich gesperrt).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from youtube_autoposter.constants import ENFORCED_UPLOAD_PRIVACY, PRIVACY_PRIVATE, PRIVACY_PUBLIC, PRIVACY_UNLISTED
from youtube_autoposter.errors import (
    AuthError,
    PlatformError,
    PrivateLockError,
    PublishError,
    QuotaExceededError,
    StateError,
    UploadError,
)
from youtube_autoposter.metadata.parser import EpisodeMetadata
from youtube_autoposter.platforms.base import PublicationJob
from youtube_autoposter.platforms.youtube.api import (
    YouTubeApi,
    classify_error,
    is_retriable_exception,
    parse_http_error,
)
from youtube_autoposter.platforms.youtube.publisher import YouTubePublisher
from youtube_autoposter.platforms.youtube.uploader import YouTubeUploader, guess_mimetype, watch_url

from tests.fakes import (
    FakeProgress,
    FakeYouTubeService,
    make_http_error,
)


class FakeAuth:
    """Ersatz fuer YouTubeAuthManager: liefert einen gemockten Service."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.build_calls = 0

    def build_service(self) -> Any:
        self.build_calls += 1
        return self.service


def _job(tmp_path: Path, *, metadata: EpisodeMetadata | None = None, video_id: str | None = None, size: int = 4096) -> PublicationJob:
    video = tmp_path / "folge.mp4"
    if not video.exists():
        video.write_bytes(b"videodaten" * (size // 10))
    thumb = tmp_path / "folge.jpg"
    if not thumb.exists():
        from PIL import Image

        Image.new("RGB", (1280, 720), (10, 20, 30)).save(thumb, "JPEG")
    return PublicationJob(
        video_id=1,
        episode_id="folge_01",
        platform="youtube",
        video_path=str(video),
        metadata=metadata
        or EpisodeMetadata(
            title="Folge 1",
            description="Beschreibung",
            tags=["a", "b"],
            category_id="27",
            language="de",
            privacy_status_requested=PRIVACY_PUBLIC,  # muss ignoriert werden
        ),
        thumbnail_path=str(thumb),
        file_size=video.stat().st_size,
        file_hash="0" * 64,
        platform_video_id=video_id,
        options={},
    )


def _api(service: Any, settings: Any, repository: Any = None) -> YouTubeApi:
    return YouTubeApi(FakeAuth(service), settings=settings, repository=repository)


UPLOAD_RESPONSE = {
    "kind": "youtube#video",
    "id": "dQw4w9WgXcQ",
    "snippet": {
        "title": "Folge 1",
        "categoryId": "27",
        "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/default.jpg"}},
    },
    "status": {"privacyStatus": "private", "uploadStatus": "processed", "selfDeclaredMadeForKids": False},
}


# ===========================================================================
# Resumable Upload
# ===========================================================================


def test_upload_laeuft_in_chunks_durch(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService(
        {
            "videos": [
                (FakeProgress(0.25), None),
                (FakeProgress(0.5), None),
                (FakeProgress(1.0), None),
                (None, UPLOAD_RESPONSE),
            ]
        }
    )
    uploader = YouTubeUploader(_api(service, settings), settings=settings)
    fortschritt: list[tuple[int, int]] = []
    job = _job(tmp_path)
    job.progress_cb = lambda sent, total: fortschritt.append((sent, total))

    result = uploader.upload(job)

    assert result.platform_video_id == "dQw4w9WgXcQ"
    assert result.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert result.privacy_status == PRIVACY_PRIVATE
    assert result.resumable is True
    assert result.bytes_sent == Path(job.video_path).stat().st_size
    assert result.thumbnail_url.endswith("default.jpg")
    assert result.quota_units == 1  # 1 Einheit im Upload-Kontingent

    # Request wurde korrekt aufgebaut
    request = service.videos_resource.requests[0]
    assert request.kwargs["part"] == "snippet,status"
    assert request.kwargs["notifySubscribers"] is False
    body = request.kwargs["body"]
    assert body["status"]["privacyStatus"] == PRIVACY_PRIVATE
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["snippet"]["title"] == "Folge 1"
    assert body["snippet"]["categoryId"] == "27"
    assert request.calls == 4  # 3 Fortschritte + finale Antwort

    # Fortschritt wurde gemeldet und steigt an
    assert fortschritt
    gesendet = [sent for sent, _total in fortschritt]
    assert gesendet == sorted(gesendet)
    assert gesendet[-1] == fortschritt[-1][1]  # letzter Stand = 100 %


def test_upload_body_ist_aus_dem_json_nie_public(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService({"videos": [(None, UPLOAD_RESPONSE)]})
    uploader = YouTubeUploader(_api(service, settings), settings=settings)
    metadata = EpisodeMetadata(
        title="T", description="D", category_id="27", privacy_status_requested=PRIVACY_PUBLIC
    )
    job = _job(tmp_path, metadata=metadata)

    body = uploader.build_body(job)
    assert body["status"]["privacyStatus"] == ENFORCED_UPLOAD_PRIVACY

    uploader.upload(job)
    gesendet = service.videos_resource.requests[0].kwargs["body"]
    assert gesendet["status"]["privacyStatus"] == ENFORCED_UPLOAD_PRIVACY


def test_upload_wiederholt_bei_503_mit_backoff(tmp_path: Path, settings) -> None:
    settings.MAX_RETRIES = 3
    settings.RETRY_DELAY = 5.0
    settings.RETRY_BACKOFF_FACTOR = 2.0
    service = FakeYouTubeService(
        {
            "videos": [
                make_http_error(503, "backendError", "Dienst voruebergehend nicht verfuegbar"),
                make_http_error(503, "backendError", "erneut"),
                (FakeProgress(1.0), None),
                (None, UPLOAD_RESPONSE),
            ]
        },
        resumable_progress=2048,  # es wurden schon Bytes gesendet
    )
    uploader = YouTubeUploader(_api(service, settings), settings=settings)
    gewartet: list[float] = []

    result = uploader.upload(_job(tmp_path), sleep=gewartet.append)

    assert result.platform_video_id == "dQw4w9WgXcQ"
    assert gewartet == [5.0, 10.0]  # exponentieller Backoff
    # dieselbe Session wird fortgesetzt (resumable!), keine neue Session
    assert len(service.videos_resource.requests) == 1


def test_upload_bricht_bei_dauerhaftem_fehler_ab(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService(
        {"videos": [make_http_error(400, "invalidTitle", "Titel enthaelt ungueltige Zeichen")]}
    )
    uploader = YouTubeUploader(_api(service, settings), settings=settings)
    gewartet: list[float] = []

    with pytest.raises(UploadError) as info:
        uploader.upload(_job(tmp_path), sleep=gewartet.append)

    assert info.value.status_code == 400
    assert info.value.reason == "invalidTitle"
    assert info.value.retriable is False
    assert gewartet == []  # kein sinnloser Wiederholungsversuch
    assert service.videos_resource.requests[0].calls == 1


def test_upload_gibt_nach_maximalen_retries_auf(tmp_path: Path, settings) -> None:
    settings.MAX_RETRIES = 2
    settings.RETRY_DELAY = 0.01
    fehler = make_http_error(500, "internalError", "Interner Fehler")
    service = FakeYouTubeService({"videos": [fehler, fehler, fehler, fehler]}, resumable_progress=1024)
    uploader = YouTubeUploader(_api(service, settings), settings=settings)

    with pytest.raises(UploadError):
        uploader.upload(_job(tmp_path), sleep=lambda s: None)

    # 1 Versuch + 2 Wiederholungen
    assert service.videos_resource.requests[0].calls == 3


def test_upload_baut_session_nach_transportfehler_neu(tmp_path: Path, settings) -> None:
    settings.MAX_RETRIES = 3
    settings.RETRY_DELAY = 0.01
    service = FakeYouTubeService(
        {
            "videos": [
                ConnectionResetError("Verbindung zurueckgesetzt"),
                (FakeProgress(1.0), None),
                (None, UPLOAD_RESPONSE),
            ]
        }
    )
    uploader = YouTubeUploader(_api(service, settings), settings=settings)

    result = uploader.upload(_job(tmp_path), sleep=lambda s: None)

    assert result.platform_video_id == "dQw4w9WgXcQ"
    assert len(service.videos_resource.requests) == 2  # neue Session aufgebaut


def test_quota_erschopft_wird_erkannt(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService(
        {"videos": [make_http_error(403, "quotaExceeded", "Die Zuweisung fuer dieses Projekt wurde ueberschritten")]}
    )
    uploader = YouTubeUploader(_api(service, settings), settings=settings)

    with pytest.raises(QuotaExceededError) as info:
        uploader.upload(_job(tmp_path), sleep=lambda s: None)
    assert info.value.retriable is False


def test_upload_ohne_antwort_id_scheitert(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService({"videos": [(None, {"kind": "youtube#video", "status": {}})]})
    uploader = YouTubeUploader(_api(service, settings), settings=settings)

    with pytest.raises(UploadError) as info:
        uploader.upload(_job(tmp_path), sleep=lambda s: None)
    assert "Video-ID" in str(info.value)


def test_upload_verweigert_fehlende_oder_leere_datei(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService({"videos": [(None, UPLOAD_RESPONSE)]})
    uploader = YouTubeUploader(_api(service, settings), settings=settings)

    job = _job(tmp_path)
    Path(job.video_path).unlink()
    with pytest.raises(UploadError):
        uploader.upload(job)

    job2 = _job(tmp_path)
    Path(job2.video_path).write_bytes(b"")
    with pytest.raises(UploadError):
        uploader.upload(job2)
    assert service.videos_resource.requests == []


def test_mimetype_und_url_helfer(tmp_path: Path) -> None:
    assert guess_mimetype(tmp_path / "video.mp4") == "video/mp4"
    assert guess_mimetype(tmp_path / "video.mov") == "video/quicktime"
    assert guess_mimetype(tmp_path / "video.mkv") == "video/x-matroska"
    assert guess_mimetype(tmp_path / "unbekannt.xyz") == "video/*"  # von YouTube akzeptiert
    assert watch_url("abc123") == "https://www.youtube.com/watch?v=abc123"


# ===========================================================================
# Thumbnail
# ===========================================================================


def test_thumbnail_setzen_erfolgreich(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService(
        {
            "thumbnails": [
                {
                    "kind": "youtube#thumbnailDetails",
                    "items": [
                        {
                            "default": {"url": "https://i.ytimg.com/vi/x/default.jpg", "width": 120},
                            "high": {"url": "https://i.ytimg.com/vi/x/hqdefault.jpg", "width": 480},
                            "maxres": {"url": "https://i.ytimg.com/vi/x/maxres.jpg", "width": 1280},
                        }
                    ],
                }
            ]
        }
    )
    publisher = YouTubePublisher(_api(service, settings), settings=settings)
    job = _job(tmp_path)

    result = publisher.set_thumbnail("x", job.thumbnail_path)

    assert result.ok is True
    assert result.url.endswith("maxres.jpg")
    assert result.quota_units == 50
    assert service.thumbnails_resource.requests[0].kwargs["videoId"] == "x"


def test_thumbnail_fehler_bricht_nichts_ab(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService(
        {"thumbnails": [make_http_error(403, "forbidden", "Nutzer hat das Thumbnail nicht verifiziert")]}
    )
    publisher = YouTubePublisher(_api(service, settings), settings=settings)
    job = _job(tmp_path)

    result = publisher.set_thumbnail("x", job.thumbnail_path)

    assert result.ok is False
    assert "verifiziert" in result.message.lower() or "thumbnail" in result.message.lower()


def test_thumbnail_zu_gross_wird_nicht_gesendet(tmp_path: Path, settings) -> None:
    import os

    service = FakeYouTubeService({"thumbnails": [{"items": []}]})
    publisher = YouTubePublisher(_api(service, settings), settings=settings)
    gross = tmp_path / "gross.jpg"
    gross.write_bytes(os.urandom(3 * 1024 * 1024))

    result = publisher.set_thumbnail("x", gross)

    assert result.ok is False
    assert "2 MB" in result.message
    assert service.thumbnails_resource.requests == []


def test_thumbnail_datei_fehlt(tmp_path: Path, settings) -> None:
    publisher = YouTubePublisher(_api(FakeYouTubeService(), settings), settings=settings)
    result = publisher.set_thumbnail("x", tmp_path / "gibt_es_nicht.jpg")
    assert result.ok is False


def test_thumbnail_falsches_format(tmp_path: Path, settings) -> None:
    publisher = YouTubePublisher(_api(FakeYouTubeService(), settings), settings=settings)
    bmp = tmp_path / "bild.bmp"
    bmp.write_bytes(b"xx" * 100)
    result = publisher.set_thumbnail("x", bmp)
    assert result.ok is False
    assert "Format" in result.message


# ===========================================================================
# Veroeffentlichen
# ===========================================================================


def test_publish_ohne_confirm_wird_verweigert(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService({"videos": [{"id": "x", "status": {"privacyStatus": "public"}}]})
    publisher = YouTubePublisher(_api(service, settings), settings=settings)
    job = _job(tmp_path, video_id="x")

    with pytest.raises(StateError):
        publisher.publish(job, confirm=False)
    assert service.videos_resource.requests == []


def test_publish_sendet_nur_status_part(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService(
        {
            "videos": [
                {
                    "id": "x",
                    "status": {"privacyStatus": "public"},
                    "snippet": {"publishedAt": "2026-09-12T18:00:00Z"},
                }
            ]
        }
    )
    publisher = YouTubePublisher(_api(service, settings), settings=settings)
    job = _job(tmp_path, video_id="x")

    result = publisher.publish(job, confirm=True, privacy_status=PRIVACY_PUBLIC)

    assert result.privacy_status == PRIVACY_PUBLIC
    assert result.published_at == "2026-09-12T18:00:00Z"
    assert result.quota_units == 50
    request = service.videos_resource.execute_requests[0]
    assert request.kwargs["part"] == "status"
    body = request.kwargs["body"]
    assert body == {
        "id": "x",
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }


def test_publish_nur_public_oder_unlisted(tmp_path: Path, settings) -> None:
    publisher = YouTubePublisher(_api(FakeYouTubeService(), settings), settings=settings)
    job = _job(tmp_path, video_id="x")

    with pytest.raises(StateError):
        publisher.publish(job, confirm=True, privacy_status=PRIVACY_PRIVATE)
    with pytest.raises(StateError):
        publisher.publish(job, confirm=True, privacy_status="geheim")


def test_publish_ohne_video_id(tmp_path: Path, settings) -> None:
    publisher = YouTubePublisher(_api(FakeYouTubeService(), settings), settings=settings)
    job = _job(tmp_path, video_id=None)
    job.platform_video_id = None

    with pytest.raises(PublishError):
        publisher.publish(job, confirm=True)


def test_private_lock_wird_erkannt(tmp_path: Path, settings) -> None:
    """Nicht auditiertes Projekt: YouTube antwortet 'ok', bleibt aber privat."""

    service = FakeYouTubeService(
        {"videos": [{"id": "x", "status": {"privacyStatus": "private", "rejectionReason": "forbiddenPrivacySetting"}}]}
    )
    publisher = YouTubePublisher(_api(service, settings), settings=settings)
    job = _job(tmp_path, video_id="x")

    with pytest.raises(PublishError) as info:
        publisher.publish(job, confirm=True)
    assert "auditiert" in str(info.value) or "privat" in str(info.value).lower()


def test_publish_api_fehler_wird_klassifiziert(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService(
        {"videos": [make_http_error(403, "forbiddenPrivacySetting", "Video ist als privat gesperrt")]}
    )
    api = _api(service, settings)
    settings.MAX_RETRIES = 0
    publisher = YouTubePublisher(api, settings=settings)
    job = _job(tmp_path, video_id="x")

    with pytest.raises(PrivateLockError):
        publisher.publish(job, confirm=True, privacy_status=PRIVACY_PUBLIC)


def test_publish_unlisted(tmp_path: Path, settings) -> None:
    service = FakeYouTubeService({"videos": [{"id": "x", "status": {"privacyStatus": "unlisted"}}]})
    publisher = YouTubePublisher(_api(service, settings), settings=settings)
    job = _job(tmp_path, video_id="x")

    result = publisher.publish(job, confirm=True, privacy_status=PRIVACY_UNLISTED)
    assert result.privacy_status == PRIVACY_UNLISTED


# ===========================================================================
# API-Helfer, Quota, Fehlerklassifikation
# ===========================================================================


def test_execute_wiederholt_bei_5xx(settings) -> None:
    service = FakeYouTubeService(
        {"videos": [make_http_error(503, "backendError"), {"id": "x", "status": {"privacyStatus": "private"}}]}
    )
    settings.MAX_RETRIES = 2
    settings.RETRY_DELAY = 0.01
    api = _api(service, settings, repository=None)
    gewartet: list[float] = []

    request = service.videos().list(part="status", id="x")
    # Script liegt auf der videos-Liste, deshalb direkt ausfuehren
    result = api.execute(request, method="videos.list", sleep=gewartet.append)
    assert result["id"] == "x"
    assert len(gewartet) == 1


def test_quota_wird_in_der_datenbank_gebucht(ctx, settings) -> None:
    service = FakeYouTubeService({"channels": [{"items": []}]})
    api = _api(service, settings, repository=ctx.repository)

    api.track_quota("videos.insert")
    api.track_quota("thumbnails.set")
    api.track_quota("videos.list")

    quota = api.quota_today()
    assert quota["uploads"] == 1
    assert quota["units"] == 1 + 50 + 1
    assert quota["calls"]["videos.insert"] == 1
    assert quota["upload_limit_per_day"] == 100
    assert quota["default_bucket_limit"] == 10000
    assert "videos.insert" in quota["documented_costs"]

    # In der Datenbank gespeichert (ueberlebt Neustart)
    gespeichert = json.loads(ctx.repository.get_setting(f"quota.{__import__('datetime').date.today().isoformat()}"))
    assert gespeichert["units"] == 52


def test_fetch_channel_info(ctx, settings) -> None:
    antwort = {
        "items": [
            {
                "id": "UC123",
                "snippet": {"title": "Mein Kanal", "country": "DE"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}},
            }
        ]
    }
    service = FakeYouTubeService({"channels": [antwort]})
    api = _api(service, settings, repository=ctx.repository)

    info = api.fetch_channel_info()
    assert info["id"] == "UC123"
    assert info["title"] == "Mein Kanal"
    assert info["uploads_playlist"] == "UU123"


def test_fetch_channel_info_ohne_kanal(settings) -> None:
    service = FakeYouTubeService({"channels": [{"items": []}]})
    api = _api(service, settings)
    with pytest.raises(AuthError):
        api.fetch_channel_info()


def test_get_video_status(settings) -> None:
    service = FakeYouTubeService(
        {
            "videos": [
                {
                    "items": [
                        {
                            "id": "abc",
                            "status": {"privacyStatus": "private", "uploadStatus": "processed"},
                            "snippet": {"title": "Titel", "thumbnails": {"default": {"url": "u"}}},
                        }
                    ]
                }
            ]
        }
    )
    api = _api(service, settings)
    status = api.get_video_status("abc")
    assert status["found"] is True
    assert status["privacy_status"] == "private"
    assert status["upload_status"] == "processed"


def test_get_video_status_nicht_gefunden(settings) -> None:
    service = FakeYouTubeService({"videos": [{"items": []}]})
    api = _api(service, settings)
    assert api.get_video_status("gibt_es_nicht")["found"] is False


def test_find_upload_by_title(settings) -> None:
    service = FakeYouTubeService(
        {
            "channels": [
                {
                    "items": [
                        {
                            "id": "UC1",
                            "snippet": {"title": "Kanal"},
                            "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}},
                        }
                    ]
                }
            ],
            "playlistItems": [
                {"items": [{"contentDetails": {"videoId": "v1"}, "snippet": {"title": "Folge 1"}}]}
            ],
            "videos": [
                {
                    "items": [
                        {
                            "id": "v1",
                            "snippet": {"title": "Folge 1", "publishedAt": "2026-09-01T10:00:00Z"},
                            "status": {"privacyStatus": "private"},
                        }
                    ]
                }
            ],
        }
    )
    api = _api(service, settings)
    gefunden = api.find_upload_by_title("Folge 1")
    assert gefunden == "v1"


@pytest.mark.parametrize(
    "status,reason,typ,retriable",
    [
        (503, "backendError", UploadError, True),
        (500, "internalError", UploadError, True),
        (403, "quotaExceeded", QuotaExceededError, False),
        (403, "dailyLimitExceeded", QuotaExceededError, False),
        (403, "forbiddenPrivacySetting", PrivateLockError, False),
        (403, "insufficientPermissions", UploadError, False),
        (401, "authError", AuthError, False),
        (403, "invalidCredentials", AuthError, False),
        (400, "invalidTitle", UploadError, False),
    ],
)
def test_fehlerklassifikation(status: int, reason: str, typ: type, retriable: bool) -> None:
    exc = make_http_error(status, reason, f"Meldung {reason}")
    klassifiziert = classify_error(exc, action="upload")
    assert isinstance(klassifiziert, typ)
    assert klassifiziert.retriable is retriable
    assert klassifiziert.status_code == status
    assert is_retriable_exception(klassifiziert) is retriable


def test_parse_http_error_ohne_json() -> None:
    from tests.fakes import FakeResponse

    class NacktFehler(Exception):
        resp = FakeResponse(status=502, reason="Bad Gateway")
        content = b"<html>Fehler</html>"

        def __str__(self) -> str:
            return "502 Bad Gateway"

    status, reason, message = parse_http_error(NacktFehler())
    assert status == 502
    assert "Bad Gateway" in message


def test_parse_http_error_unbekannte_exception() -> None:
    status, reason, message = parse_http_error(RuntimeError("irgendwas"))
    assert status is None
    assert reason is None
    assert "irgendwas" in message


def test_auth_fehler_bei_tokenverlust(tmp_path: Path, settings) -> None:
    from google.auth.exceptions import RefreshError

    service = FakeYouTubeService({"videos": [RefreshError("Token abgelaufen")]})
    uploader = YouTubeUploader(_api(service, settings), settings=settings)

    with pytest.raises(AuthError):
        uploader.upload(_job(tmp_path), sleep=lambda s: None)


def test_redaktion_von_geheimnissen() -> None:
    from youtube_autoposter.utils.logging_utils import redact

    text = "access_token=ya29.geheim&client_secret=GOCSPX-1234567890"
    sauber = redact(text)
    assert "ya29.geheim" not in sauber
    assert "GOCSPX-1234567890" not in sauber
