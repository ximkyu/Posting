"""Gemeinsame pytest-Fixtures.

Alle Tests arbeiten in einem temporaeren Projektordner (tmp_path) und ohne
Netzwerk. Ein falscher ``ffprobe``-Stub liefert die Videodaten, ``FakePlatform``
ersetzt den YouTube-Adapter.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fakes import (  # noqa: E402
    FakePlatform,
    SAMPLE_FFPROBE_JSON,
    sample_client_secrets,
    write_fake_ffmpeg,
    write_fake_ffprobe,
)

from youtube_autoposter.config import Settings, load_settings  # noqa: E402
from youtube_autoposter.constants import PRIVACY_PRIVATE  # noqa: E402
from youtube_autoposter.core.context import bootstrap  # noqa: E402


# ---------------------------------------------------------------------------
# Grundausstattung
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sicherheitsnetz: echte HTTP-Zugriffe in Tests verhindern."""

    import http.client

    def _blocked(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - Schutzmechanismus
        raise AssertionError("Test hat versucht, eine Netzwerkverbindung zu oeffnen")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", _blocked)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", _blocked)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Temporaeres Projektverzeichnis mit Unterordnern."""

    for name in ("READY", "PROCESSING", "UPLOADED_PRIVATE", "PUBLISHED", "FAILED", "SKIPPED"):
        (tmp_path / "folders" / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "credentials").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def fake_ffprobe(project: Path) -> Path:
    path = write_fake_ffprobe(project / "bin")
    (project / "bin").mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def settings(project: Path, fake_ffprobe: Path) -> Settings:
    """Konfiguration im temporaeren Projekt (ohne Umgebungsvariablen)."""

    base = load_settings(base_dir=project, use_env=False, create_if_missing=True)
    base.FFPROBE_PATH = str(fake_ffprobe)
    base.FFMPEG_PATH = ""
    base.FILE_STABILITY_SECONDS = 1
    base.STABILITY_CHECK_INTERVAL = 1
    base.STABILITY_REQUIRED_CHECKS = 1
    base.STABILITY_WAIT_BEFORE_UPLOAD = False
    base.RETRY_DELAY = 0.01
    base.MAX_RETRIES = 3
    base.DEFAULT_PRIVACY_STATUS = PRIVACY_PRIVATE
    base.ENFORCE_PRIVATE_UPLOAD = True
    base.USE_SYSTEM_KEYRING = False
    base.TOKEN_STORAGE = "file"
    base.CLIENT_SECRETS_FILE = "credentials/credentials.json"
    base.WATCH_ENABLED = False
    base.AUTO_UPLOAD = False
    base.OPEN_BROWSER = False
    base.VALIDATE_CATEGORY_ONLINE = False
    base.LOG_TO_DATABASE = False
    return base


@pytest.fixture
def ctx(settings: Settings) -> Iterator[Any]:
    context = bootstrap(settings, run_recovery=False, console_logging=False)
    try:
        yield context
    finally:
        context.stop_background()
        context.db.close()


@pytest.fixture
def fake_platform(ctx: Any) -> FakePlatform:
    """Registriert den Mock als YouTube-Plattform im Testkontext."""

    adapter = FakePlatform()
    ctx.registry.register(adapter, default=True)
    return adapter


# ---------------------------------------------------------------------------
# Episoden-Helfer
# ---------------------------------------------------------------------------


def _tiny_jpeg(path: Path, *, width: int = 64, height: int = 36) -> Path:
    from PIL import Image

    image = Image.new("RGB", (width, height), (17, 42, 88))
    image.save(path, "JPEG", quality=88)
    return path


def _tiny_mp4(path: Path, *, size: int = 4096, marker: bytes = b"") -> Path:
    """Erzeugt eine kleine Datei mit MP4-Header (kein echtes Video).

    Fuer die Tests reicht das: ffprobe wird durch einen Stub ersetzt, der die
    technischen Daten liefert.
    """

    header = bytes.fromhex("0000002066747970") + b"isom" + b"\x00" * 4 + b"mp41"
    payload = marker or os.urandom(max(0, size - len(header)))
    path.write_bytes(header + payload)
    return path


@pytest.fixture
def make_episode(project: Path) -> Callable[..., dict[str, Any]]:
    """Erzeugt eine Episode im READY-Ordner und liefert ihre Pfade."""

    ready = project / "folders" / "READY"

    def _make(
        episode_id: str = "video_001",
        *,
        title: str = "Folge 01 - Die verborgene Lehre",
        description: str = "Erste Folge der Testreihe.\n\n00:00 Intro",
        tags: list[str] | None = None,
        category_id: str = "27",
        privacy_status: str = "private",
        language: str = "de",
        with_thumbnail: bool = True,
        video_size: int = 4096,
        stale: bool = True,
        folder: Path | None = None,
        probe: dict[str, Any] | None = None,
        extra_metadata: dict[str, Any] | None = None,
        raw_json: str | None = None,
    ) -> dict[str, Any]:
        target = Path(folder) if folder is not None else ready
        target.mkdir(parents=True, exist_ok=True)
        video_path = target / f"{episode_id}.mp4"
        metadata_path = target / f"{episode_id}.json"
        _tiny_mp4(video_path, size=video_size)

        if raw_json is not None:
            metadata_path.write_text(raw_json, encoding="utf-8")
        else:
            payload: dict[str, Any] = {
                "episode_id": episode_id,
                "title": title,
                "description": description,
                "tags": tags if tags is not None else ["test", "dokumentation", "geschichte"],
                "category_id": category_id,
                "privacy_status": privacy_status,
                "language": language,
                "made_for_kids": False,
                "thumbnail": f"{episode_id}.jpg" if with_thumbnail else None,
                "publish_at": None,
            }
            if extra_metadata:
                payload.update(extra_metadata)
            metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        thumbnail_path = _tiny_jpeg(target / f"{episode_id}.jpg") if with_thumbnail else None

        if probe is not None:
            (project / "bin").mkdir(parents=True, exist_ok=True)
            write_fake_ffprobe(project / "bin", probe)

        paths = [video_path, metadata_path] + ([thumbnail_path] if thumbnail_path else [])
        if stale:
            # "Kopiervorgang" ist lange abgeschlossen -> sofort stabil
            alt = time.time() - 3600
            for item in paths:
                os.utime(item, (alt, alt))
        else:
            # frisch geschrieben -> darf als "wird noch kopiert" gelten
            jetzt = time.time()
            for item in paths:
                os.utime(item, (jetzt, jetzt))

        return {
            "episode_id": episode_id,
            "folder": target,
            "video_path": video_path,
            "metadata_path": metadata_path,
            "thumbnail_path": thumbnail_path,
        }

    return _make


@pytest.fixture
def episode(make_episode: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    return make_episode()


@pytest.fixture
def fake_ffmpeg(project: Path) -> Path:
    """ffmpeg-Stub (fuer den Fallback-Pfad ohne ffprobe)."""

    (project / "bin").mkdir(parents=True, exist_ok=True)
    return write_fake_ffmpeg(project / "bin")


@pytest.fixture
def client(ctx: Any) -> Any:
    """Flask-Testclient mit aktiviertem CSRF-Schutz."""

    from youtube_autoposter.app import create_app

    app = create_app(ctx)
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def credentials_file(project: Path) -> Path:
    """Gueltige (aber wertlose) OAuth-Client-Datei ablegen."""

    return sample_client_secrets(project / "credentials" / "credentials.json")


@pytest.fixture
def connected_ctx(ctx: Any, credentials_file: Path, project: Path) -> Any:
    """Kontext mit Credentials-Datei UND gespeichertem Token."""

    from tests.fakes import sample_token_payload

    token_path = project / "data" / "token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(sample_token_payload()), encoding="utf-8")
    token_path.chmod(0o600)
    return ctx


def ffprobe_payload(*, duration: str = "1781.320000", size: str = "1932735283", **kwargs: Any) -> dict[str, Any]:
    """Variante des ffprobe-Stubs (z. B. fuer Fehlerfaelle)."""

    payload = json.loads(json.dumps(SAMPLE_FFPROBE_JSON))
    payload["format"]["duration"] = duration
    payload["format"]["size"] = size
    for key, value in kwargs.items():
        if key in payload["format"]:
            payload["format"][key] = value
    return payload


@pytest.fixture
def probe_factory() -> Callable[..., dict[str, Any]]:
    return ffprobe_payload
