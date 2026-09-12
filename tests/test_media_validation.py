"""Tests fuer ffprobe/ffmpeg-Auswertung und die Episoden-Validierung."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from youtube_autoposter.utils.media import (
    ThumbnailInfo,
    probe_available,
    probe_thumbnail,
    probe_video,
)
from youtube_autoposter.utils.validation import (
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARNING,
    validate_episode,
    validate_metadata,
    validate_thumbnail,
    validate_video_file,
)

from tests.fakes import (
    SAMPLE_FFMPEG_STDERR,
    SAMPLE_FFPROBE_JSON,
    write_fake_ffmpeg,
    write_fake_ffprobe,
)
from youtube_autoposter.metadata.parser import EpisodeMetadata, parse_metadata_dict


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------


def test_probe_mit_ffprobe_stub(project: Path, fake_ffprobe: Path, settings, make_episode) -> None:
    episode = make_episode("probe_01")
    info = probe_video(episode["video_path"], settings)

    assert info.probe_ok is True
    assert info.source == "ffprobe"
    assert info.probe_error is None
    assert info.width == 1920 and info.height == 1080
    assert info.resolution == "1920 x 1080"
    assert info.duration_seconds == pytest.approx(1781.32, abs=0.01)
    assert info.duration_text == "29:41"
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.has_audio is True
    # 30000/1001 -> 29.97 fps
    assert info.fps == pytest.approx(29.97, abs=0.01)
    assert info.size_bytes == int(SAMPLE_FFPROBE_JSON["format"]["size"])
    assert "1920 x 1080" in info.summary_text()


def test_probe_ohne_audio_warnt(project: Path, settings, make_episode) -> None:
    payload = {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "25/1",
                "duration": "60.0",
            }
        ],
        "format": {"duration": "60.0", "size": "1000000"},
    }
    write_fake_ffprobe(project / "bin", payload)
    episode = make_episode("kein_audio")
    info = probe_video(episode["video_path"], settings)
    assert info.has_audio is False
    assert info.audio_codec is None
    assert info.fps == pytest.approx(25.0)

    validation = validate_video_file(episode["video_path"], settings)
    assert "video_no_audio" in _codes(validation)


def test_probe_kaputtes_ffprobe_meldet_fehler(project: Path, settings, make_episode) -> None:
    write_fake_ffprobe(project / "bin", SAMPLE_FFPROBE_JSON, exit_code=1)
    episode = make_episode("kaputt_probe")
    info = probe_video(episode["video_path"], settings)
    assert info.probe_ok is False
    assert info.probe_error
    assert info.source != "ffprobe" or info.probe_error

    # Ohne REQUIRE_FFPROBE -> Warnung, kein Fehler
    validation = validate_video_file(episode["video_path"], settings)
    assert "video_probe_skipped" in _codes(validation)
    assert validation.ok is True

    # Mit REQUIRE_FFPROBE -> Fehler
    settings.REQUIRE_FFPROBE = True
    strict = validate_video_file(episode["video_path"], settings)
    assert "video_unreadable" in _codes(strict)
    assert strict.ok is False
    settings.REQUIRE_FFPROBE = False


def test_probe_fehlende_datei(project: Path, settings) -> None:
    info = probe_video(project / "gibt_es_nicht.mp4", settings)
    assert info.probe_ok is False
    assert info.probe_error == "Datei existiert nicht"


def test_probe_ffmpeg_fallback(project: Path, settings, fake_ffmpeg: Path, make_episode) -> None:
    """Ohne ffprobe wird ffmpeg -i ausgewertet (Wichtig fuer Windows-Nutzer)."""

    settings.FFPROBE_PATH = ""
    settings.FFMPEG_PATH = str(fake_ffmpeg)
    episode = make_episode("fallback")

    ok, source = probe_available(settings)
    assert ok is True
    assert "ffmpeg" in source

    info = probe_video(episode["video_path"], settings)
    assert info.source == "ffmpeg"
    assert info.probe_ok is True
    assert info.duration_seconds == pytest.approx(29 * 60 + 41.32, abs=0.05)
    assert info.width == 1920
    assert info.height == 1080
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.has_audio is True
    assert info.fps == pytest.approx(25.0)


def test_probe_ganz_ohne_werkzeuge(project: Path, settings, make_episode) -> None:
    settings.FFPROBE_PATH = ""
    settings.FFMPEG_PATH = ""
    os.environ.pop("YAP_FFMPEG_PATH", None)
    episode = make_episode("ohne_tools")
    info = probe_video(episode["video_path"], settings)
    assert info.probe_ok is False
    assert info.source == "none"
    assert "ffprobe" in (info.probe_error or "").lower() or "ffmpeg" in (info.probe_error or "").lower()


def test_ffmpeg_stderr_parser_findet_alle_werte() -> None:
    from youtube_autoposter.utils.media import MediaInfo, _fill_from_ffmpeg_stderr

    info = MediaInfo(path="test.mp4")
    _fill_from_ffmpeg_stderr(info, SAMPLE_FFMPEG_STDERR)
    assert info.width == 1920 and info.height == 1080
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.has_audio is True
    assert info.duration_seconds == pytest.approx(29 * 60 + 41.32, abs=0.05)
    assert info.fps == pytest.approx(25.0)
    assert info.bit_rate == 8412000
    assert info.container_format == "mov" or "mp4" in (info.container_format or "")


def test_ffmpeg_stderr_parser_ohne_streams() -> None:
    from youtube_autoposter.utils.media import MediaInfo, _fill_from_ffmpeg_stderr

    info = MediaInfo(path="test.mp4")
    _fill_from_ffmpeg_stderr(info, "irgendein Text ohne Informationen")
    assert info.width is None
    assert info.duration_seconds is None


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------


def _jpeg(path: Path, width: int = 1280, height: int = 720, quality: int = 85) -> Path:
    image = Image.new("RGB", (width, height), (10, 90, 160))
    image.save(path, "JPEG", quality=quality)
    return path


def test_probe_thumbnail_1280x720(tmp_path: Path) -> None:
    path = _jpeg(tmp_path / "thumb.jpg", 1280, 720)
    info = probe_thumbnail(path)
    assert info.readable is True
    assert (info.width, info.height) == (1280, 720)
    assert info.format == "jpeg"
    assert info.size_bytes == path.stat().st_size
    assert info.error is None


def test_probe_thumbnail_png(tmp_path: Path) -> None:
    path = tmp_path / "thumb.png"
    Image.new("RGB", (1280, 720), (200, 30, 30)).save(path, "PNG")
    info = probe_thumbnail(path)
    assert info.readable is True
    assert info.format == "png"


def test_probe_thumbnail_kaputt(tmp_path: Path) -> None:
    path = tmp_path / "kaputt.jpg"
    path.write_bytes(b"das ist kein bild")
    info = probe_thumbnail(path)
    assert info.readable is False
    assert info.error


def test_probe_thumbnail_fehlt(tmp_path: Path) -> None:
    info = probe_thumbnail(tmp_path / "nix.jpg")
    assert info.readable is False
    assert isinstance(info, ThumbnailInfo)


def test_validate_thumbnail_ok(tmp_path: Path, settings) -> None:
    path = _jpeg(tmp_path / "thumb.jpg", 1280, 720)
    result = validate_thumbnail(path, settings)
    assert result.ok is True
    assert "thumbnail_ok" in _codes(result)


def test_validate_thumbnail_zu_gross(tmp_path: Path, settings) -> None:
    path = tmp_path / "gross.jpg"
    path.write_bytes(os.urandom(3 * 1024 * 1024))  # 3 MB > 2 MB Limit
    result = validate_thumbnail(path, settings)
    assert "thumbnail_too_large" in _codes(result)
    assert result.ok is False


def test_validate_thumbnail_falsches_format(tmp_path: Path, settings) -> None:
    path = tmp_path / "bild.bmp"
    path.write_bytes(os.urandom(1024))
    result = validate_thumbnail(path, settings)
    assert "thumbnail_unsupported_format" in _codes(result)


def test_validate_thumbnail_fehlt_ist_nur_info(settings) -> None:
    result = validate_thumbnail(None, settings)
    assert result.ok is True
    assert "thumbnail_missing" in _codes(result)


def test_validate_thumbnail_falsches_seitenverhaeltnis(tmp_path: Path, settings) -> None:
    path = _jpeg(tmp_path / "quadrat.jpg", 640, 640)
    result = validate_thumbnail(path, settings)
    codes = _codes(result)
    assert "thumbnail_aspect_ratio" in codes
    assert "thumbnail_small" in codes


# ---------------------------------------------------------------------------
# Gesamt-Validierung
# ---------------------------------------------------------------------------


def test_validate_episode_ok(make_episode, settings) -> None:
    episode = make_episode("valide_01")
    parsed = parse_metadata_dict(
        {
            "title": "Folge 1",
            "description": "Beschreibung mit Inhalt",
            "tags": ["a", "b"],
            "category_id": "27",
        }
    )
    _jpeg(Path(episode["thumbnail_path"]), 1280, 720)

    result = validate_episode(
        metadata=parsed.metadata,
        video_path=episode["video_path"],
        thumbnail_path=episode["thumbnail_path"],
        settings=settings,
    )
    assert result.ok is True, [str(i) for i in result.errors]
    assert result.media_info is not None
    assert result.thumbnail_info is not None
    assert result.checked_files["video"] == str(episode["video_path"])


def test_validate_episode_metadata_fehlt(make_episode, settings) -> None:
    episode = make_episode("ohne_meta")
    result = validate_episode(metadata=None, video_path=episode["video_path"], settings=settings)
    assert result.ok is False
    assert "metadata_missing" in _codes(result)


def test_validate_episode_video_fehlt(settings) -> None:
    metadata = EpisodeMetadata(title="T", description="D", category_id="27")
    result = validate_episode(metadata=metadata, video_path=None, settings=settings)
    assert result.ok is False
    assert "video_missing" in _codes(result)


def test_validate_episode_unterstuetztes_format(settings, tmp_path: Path) -> None:
    video = tmp_path / "folge.avi"
    video.write_bytes(b"x" * 2048)
    metadata = EpisodeMetadata(title="T", description="D", category_id="27")
    result = validate_episode(
        metadata=metadata, video_path=video, settings=settings, run_probe=False
    )
    assert result.ok is False
    assert "video_unsupported_format" in _codes(result)


def test_validate_episode_leere_datei(settings, tmp_path: Path) -> None:
    video = tmp_path / "folge.mp4"
    video.write_bytes(b"")
    metadata = EpisodeMetadata(title="T", description="D", category_id="27")
    result = validate_episode(metadata=metadata, video_path=video, settings=settings)
    assert "video_empty" in _codes(result)
    assert result.ok is False


def test_validate_episode_zu_langes_video(project: Path, settings, make_episode) -> None:
    payload = {
        "streams": [
            {
                "codec_name": "h264",
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "25/1",
            },
            {"codec_name": "aac", "codec_type": "audio", "sample_rate": "48000", "channels": 2},
        ],
        "format": {"duration": str(13 * 3600), "size": "1000000"},
    }
    write_fake_ffprobe(project / "bin", payload)
    episode = make_episode("zu_lang")
    metadata = EpisodeMetadata(title="T", description="D", category_id="27")
    result = validate_episode(
        metadata=metadata, video_path=episode["video_path"], settings=settings
    )
    assert "video_too_long" in _codes(result)
    assert result.ok is False


def test_thumbnail_fehler_blockiert_upload_nicht(make_episode, settings, tmp_path: Path) -> None:
    """Ein kaputtes Thumbnail darf den Upload nie verhindern (Anforderung 13)."""

    episode = make_episode("thumb_kaputt")
    Path(episode["thumbnail_path"]).write_bytes(b"kein bild")
    metadata = EpisodeMetadata(title="T", description="D", category_id="27")

    result = validate_episode(
        metadata=metadata,
        video_path=episode["video_path"],
        thumbnail_path=episode["thumbnail_path"],
        settings=settings,
    )
    codes = _codes(result)
    assert "thumbnail_unreadable" in codes
    # herabgestuft zur Warnung -> Upload bleibt moeglich
    assert result.ok is True
    assert not [i for i in result.issues if i.code == "thumbnail_unreadable" and i.level == LEVEL_ERROR]
    assert [i for i in result.issues if i.code == "thumbnail_unreadable" and i.level == LEVEL_WARNING]


def test_validate_metadata_privacy_public_nur_warnung(settings) -> None:
    parsed = parse_metadata_dict(
        {"title": "T", "description": "D", "category_id": "27", "privacy_status": "public"}
    )
    result = validate_metadata(parsed.metadata, settings)
    assert result.ok is True
    assert "privacy_overridden" in _codes(result)


def test_validate_metadata_kategorie_standard(settings) -> None:
    parsed = parse_metadata_dict({"title": "T", "description": "D"})
    result = validate_metadata(parsed.metadata, settings)
    assert "category_default" in _codes(result)
    assert result.ok is True


def test_validierungs_stufen_sind_getrennt() -> None:
    parsed = parse_metadata_dict({"title": "", "description": "D", "category_id": "27"})
    result = validate_metadata(parsed.metadata, None)
    assert result.errors and all(i.level == LEVEL_ERROR for i in result.errors)
    assert all(i.level != LEVEL_ERROR for i in result.warnings)
    assert isinstance(result.summary, str)
    assert result.error_messages()


def test_ohne_probe_laeuft_validierung_schnell(make_episode, settings) -> None:
    episode = make_episode("ohne_probe")
    metadata = EpisodeMetadata(title="T", description="D", category_id="27")
    result = validate_episode(
        metadata=metadata, video_path=episode["video_path"], settings=settings, run_probe=False
    )
    assert result.media_info is None
    assert result.ok is True
    assert all(i.level != LEVEL_INFO or "probe" not in i.code for i in result.issues)
