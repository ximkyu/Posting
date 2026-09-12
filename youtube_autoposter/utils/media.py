"""Technische Video-Analyse mit ffprobe (bzw. ffmpeg als Fallback).

FFmpeg wird NUR fuer die lokale technische Pruefung benutzt - niemals fuer
einen Upload-Trick. Die YouTube-Uploads laufen ausschliesslich ueber die
offizielle YouTube Data API v3.

Ergebnis-Beispiel fuer die Oberflaeche::

    VIDEO
    1920 x 1080
    29:41
    H.264 (video) / AAC (audio)
    1.8 GB
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .files import file_size, format_duration, human_size

#: Haeufige Installationsorte unter Windows (werden zusaetzlich zu PATH
#: und zu den Konfigurationswerten geprueft).
WINDOWS_CANDIDATES = (
    r"C:\ffmpeg\bin\ffprobe.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffprobe.exe",
    r"C:\Tools\ffmpeg\bin\ffprobe.exe",
)


@dataclass
class MediaInfo:
    """Technische Eckdaten einer Videodatei."""

    path: str
    size_bytes: int = 0
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    has_audio: bool = False
    fps: float | None = None
    container_format: str | None = None
    bit_rate: int | None = None
    source: str = "unknown"            # ffprobe | ffmpeg | none
    probe_ok: bool = False
    probe_error: str | None = None

    # --- Anzeige ------------------------------------------------------
    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width} x {self.height}"
        return "unbekannt"

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration_seconds)

    @property
    def size_text(self) -> str:
        return human_size(self.size_bytes)

    @property
    def codec_text(self) -> str:
        video = self.video_codec or "kein Video-Stream"
        audio = self.audio_codec if self.has_audio else "kein Audio"
        return f"{video} / {audio}"

    def summary_lines(self) -> list[str]:
        return [
            "VIDEO",
            self.resolution,
            self.duration_text,
            self.codec_text,
            self.size_text,
        ]

    def summary_text(self) -> str:
        return "\n".join(self.summary_lines())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_executable(configured: str | None, name: str) -> str | None:
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    for candidate in WINDOWS_CANDIDATES:
        if Path(candidate).name.lower().startswith(name) and Path(candidate).exists():
            return candidate
    return None


def find_ffprobe(settings: Any = None) -> str | None:
    configured = getattr(settings, "FFPROBE_PATH", "") if settings is not None else ""
    return _resolve_executable(configured, "ffprobe")


def find_ffmpeg(settings: Any = None) -> str | None:
    configured = getattr(settings, "FFMPEG_PATH", "") if settings is not None else ""
    return _resolve_executable(configured, "ffmpeg")


def probe_available(settings: Any = None) -> tuple[bool, str]:
    """Ist eine technische Analyse moeglich? (ffprobe bevorzugt, ffmpeg Fallback)"""

    if find_ffprobe(settings):
        return True, "ffprobe"
    if find_ffmpeg(settings):
        return True, "ffmpeg (Fallback)"
    return False, "weder ffprobe noch ffmpeg gefunden"


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def _parse_fps(text: Any) -> float | None:
    """'30000/1001' -> 29.97, '25' -> 25.0"""

    if text in (None, "", "0/0", "N/A"):
        return None
    raw = str(text)
    if "/" in raw:
        numerator, _, denominator = raw.partition("/")
        num = _to_float(numerator)
        den = _to_float(denominator)
        if num is None or not den:
            return None
        return round(num / den, 3)
    return _to_float(raw)


def probe_video(path: str | Path, settings: Any = None) -> MediaInfo:
    """Videodatei technisch analysieren.

    Wirft bewusst KEINE Exception: Bei Problemen ist ``probe_ok`` False und
    ``probe_error`` gefuellt. Die Validierung entscheidet dann, ob das ein
    Fehler (``REQUIRE_FFPROBE=True``) oder nur eine Warnung ist.
    """

    p = Path(path)
    info = MediaInfo(path=str(p), size_bytes=max(0, file_size(p)))
    if not p.exists():
        info.probe_error = "Datei existiert nicht"
        return info

    timeout = int(getattr(settings, "MEDIA_PROBE_TIMEOUT_SECONDS", 120) or 120)
    ffprobe = find_ffprobe(settings)
    if ffprobe:
        try:
            data = _run_ffprobe(ffprobe, p, timeout)
        except Exception as exc:  # noqa: BLE001 - robuste Pipeline
            info.probe_error = f"ffprobe fehlgeschlagen: {exc}"
        else:
            _fill_from_ffprobe(info, data)
            info.source = "ffprobe"
            info.probe_ok = True
            return info

    ffmpeg = find_ffmpeg(settings)
    if ffmpeg:
        try:
            stderr = _run_ffmpeg_info(ffmpeg, p, timeout)
        except Exception as exc:  # noqa: BLE001 - robuste Pipeline
            info.probe_error = f"ffmpeg fehlgeschlagen: {exc}"
        else:
            _fill_from_ffmpeg_stderr(info, stderr)
            info.source = "ffmpeg"
            info.probe_ok = bool(info.duration_seconds or info.video_codec)
            if not info.probe_ok and not info.probe_error:
                info.probe_error = "ffmpeg konnte keine Stream-Informationen liefern"
            return info

    info.source = "none"
    info.probe_error = (
        "ffprobe/ffmpeg nicht gefunden - technische Pruefung uebersprungen. "
        "Installation: https://ffmpeg.org/download.html (Windows: ffmpeg.zip "
        "entpacken und den bin-Ordner zum PATH hinzufuegen)."
    )
    return info


def _run_ffprobe(executable: str, path: Path, timeout: int) -> dict[str, Any]:
    command = [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = subprocess.run(  # noqa: S603 - fester Programmaufruf
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise RuntimeError(message[-1] if message else f"Exit-Code {completed.returncode}")
    payload = json.loads(completed.stdout or "{}")
    if not isinstance(payload, dict):
        raise RuntimeError("unerwartetes ffprobe-Format")
    return payload


def _fill_from_ffprobe(info: MediaInfo, data: dict[str, Any]) -> None:
    fmt = data.get("format") or {}
    info.duration_seconds = _to_float(fmt.get("duration"))
    info.size_bytes = _to_int(fmt.get("size")) or info.size_bytes
    info.container_format = fmt.get("format_name")
    info.bit_rate = _to_int(fmt.get("bit_rate"))

    streams = data.get("streams") or []
    video_stream: dict[str, Any] | None = None
    audio_stream: dict[str, Any] | None = None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        kind = stream.get("codec_type")
        if kind == "video" and video_stream is None and not stream.get("attached_pic"):
            video_stream = stream
        elif kind == "audio" and audio_stream is None:
            audio_stream = stream

    if video_stream:
        info.video_codec = video_stream.get("codec_name") or video_stream.get("codec_long_name")
        info.width = _to_int(video_stream.get("width"))
        info.height = _to_int(video_stream.get("height"))
        info.fps = _parse_fps(video_stream.get("avg_frame_rate")) or _parse_fps(
            video_stream.get("r_frame_rate")
        )
    if audio_stream:
        info.audio_codec = audio_stream.get("codec_name")
        info.has_audio = True
    if info.duration_seconds is None and video_stream:
        info.duration_seconds = _to_float(video_stream.get("duration"))


def _run_ffmpeg_info(executable: str, path: Path, timeout: int) -> str:
    # "ffmpeg -i datei" bricht ohne Ausgabeziel mit Exit-Code 1 ab und
    # schreibt alle Stream-Informationen nach stderr. Das ist der uebliche
    # Fallback, wenn kein ffprobe installiert ist.
    command = [executable, "-hide_banner", "-nostdin", "-i", str(path)]
    try:
        completed = subprocess.run(  # noqa: S603 - fester Programmaufruf
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Zeitueberschreitung bei der ffmpeg-Analyse") from None
    output = (completed.stderr or "") + (completed.stdout or "")
    if not output.strip():
        raise RuntimeError(f"ffmpeg lieferte keine Ausgabe (Exit-Code {completed.returncode})")
    return output


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", re.IGNORECASE)
_INPUT_RE = re.compile(r"Input\s*#\d+,\s*([^,]+),", re.IGNORECASE)
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s*kb/s", re.IGNORECASE)
_VIDEO_CODEC_RE = re.compile(r"\bVideo:\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)
_AUDIO_CODEC_RE = re.compile(r"\bAudio:\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)
_DIMENSIONS_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_FPS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*fps\b", re.IGNORECASE)


def _fill_from_ffmpeg_stderr(info: MediaInfo, text: str) -> None:
    duration = _DURATION_RE.search(text)
    if duration:
        hours = int(duration.group(1))
        minutes = int(duration.group(2))
        seconds = float(duration.group(3))
        info.duration_seconds = hours * 3600 + minutes * 60 + seconds

    container = _INPUT_RE.search(text)
    if container:
        info.container_format = container.group(1).strip()

    bitrate = _BITRATE_RE.search(text)
    if bitrate:
        info.bit_rate = int(bitrate.group(1)) * 1000

    # Stream-Zeilen einzeln auswerten (ein grosser Regex ueber mehrere
    # optionale Gruppen wuerde bei lazy-Quantifiern leer matchen).
    for line in text.splitlines():
        if "Stream #" not in line:
            continue
        if not info.video_codec:
            codec = _VIDEO_CODEC_RE.search(line)
            if codec:
                info.video_codec = codec.group(1)
                dimensions = _DIMENSIONS_RE.search(line)
                if dimensions:
                    info.width = int(dimensions.group(1))
                    info.height = int(dimensions.group(2))
                fps = _FPS_RE.search(line)
                if fps:
                    info.fps = float(fps.group(1))
                continue
        if not info.has_audio:
            codec = _AUDIO_CODEC_RE.search(line)
            if codec:
                info.audio_codec = codec.group(1)
                info.has_audio = True


# ---------------------------------------------------------------------------
# Thumbnail-Analyse
# ---------------------------------------------------------------------------


@dataclass
class ThumbnailInfo:
    path: str
    exists: bool = False
    size_bytes: int = 0
    width: int | None = None
    height: int | None = None
    format: str | None = None
    readable: bool = False
    error: str | None = None

    @property
    def size_text(self) -> str:
        return human_size(self.size_bytes)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_thumbnail(path: str | Path) -> ThumbnailInfo:
    """Thumbnail pruefen: vorhanden, Format, Groesse (max. 2 MB), lesbar."""

    p = Path(path)
    info = ThumbnailInfo(path=str(p))
    if not p.exists():
        info.error = "Thumbnail-Datei nicht gefunden"
        return info
    info.exists = True
    info.size_bytes = max(0, file_size(p))
    info.format = p.suffix.lower().lstrip(".")

    try:
        from PIL import Image, UnidentifiedImageError  # type: ignore
    except Exception:  # pragma: no cover - Pillow fehlt
        # Fallback: Magische Bytes pruefen (JPEG/PNG)
        try:
            with p.open("rb") as handle:
                head = handle.read(12)
        except OSError as exc:
            info.error = f"Thumbnail nicht lesbar: {exc}"
            return info
        if head.startswith(b"\xff\xd8\xff"):
            info.format = "jpg"
            info.readable = True
        elif head.startswith(b"\x89PNG\r\n\x1a\n"):
            info.format = "png"
            info.readable = True
        else:
            info.error = "Unbekanntes Bildformat (weder JPEG noch PNG erkannt)"
        return info

    try:
        with Image.open(p) as image:
            image.verify()
        with Image.open(p) as image:
            info.width, info.height = image.size
            info.format = (image.format or info.format or "").lower()
        info.readable = True
    except UnidentifiedImageError:
        info.error = "Datei ist kein gueltiges Bild (JPEG/PNG erwartet)"
    except Exception as exc:  # noqa: BLE001 - robuste Pipeline
        info.error = f"Bild konnte nicht gelesen werden: {exc}"
    return info
