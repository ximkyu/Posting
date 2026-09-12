"""Watch-Folder: READY/ durchsuchen, Episoden erkennen, in die DB aufnehmen.

Erkennungslogik: Der gemeinsame Basisname (Dateiname ohne Endung) ist die
Episode-ID::

    READY/video_001.mp4
    READY/video_001.json      -> Episode "video_001"
    READY/video_001.jpg       -> Thumbnail (optional)

Bevor eine Episode aufgenommen wird, muss die Datei **stabil** sein
(Groesse ueber ``FILE_STABILITY_SECONDS`` unveraendert) - sonst wuerde das
System eine noch laufende Kopie hochladen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..constants import METADATA_EXTENSION, VideoStatus
from ..database.repository import VideoRepository
from ..utils.files import (
    FileStabilityTracker,
    file_size,
    is_temp_file,
    now_iso,
)
from ..utils.logging_utils import get_logger

#: Prioritaet, falls mehrere Videodateien zum selben Stamm existieren
VIDEO_EXTENSION_PRIORITY = (".mp4", ".mov", ".mkv", ".webm")

#: Dateiendungen, die ignoriert werden
IGNORED_EXTENSIONS = (".txt", ".md", ".log", ".nfo", ".srt", ".vtt", ".ass")


@dataclass
class EpisodeCandidate:
    """Ein potenzieller Upload aus dem READY-Ordner."""

    episode_id: str
    video_path: Path | None = None
    metadata_path: Path | None = None
    thumbnail_path: Path | None = None
    folder: Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stable: bool = False
    stability_reason: str = ""
    size: int = 0
    extra_files: list[Path] = field(default_factory=list)

    @property
    def files(self) -> list[Path]:
        return [p for p in (self.video_path, self.metadata_path, self.thumbnail_path) if p]

    @property
    def ok(self) -> bool:
        return bool(self.video_path) and self.stable and not self.errors

    @property
    def is_complete(self) -> bool:
        return bool(self.video_path and self.metadata_path)

    def summary(self) -> str:
        parts = [f"Episode '{self.episode_id}'"]
        parts.append("Video: " + (self.video_path.name if self.video_path else "FEHLT"))
        parts.append("JSON: " + (self.metadata_path.name if self.metadata_path else "fehlt"))
        parts.append("Thumbnail: " + (self.thumbnail_path.name if self.thumbnail_path else "keins"))
        if self.errors:
            parts.append("Fehler: " + "; ".join(self.errors))
        if self.warnings:
            parts.append("Hinweise: " + "; ".join(self.warnings))
        if not self.stable:
            parts.append(f"Stabilitaet: {self.stability_reason or 'nicht stabil'}")
        return " | ".join(parts)


@dataclass
class ScanResult:
    candidates: list[EpisodeCandidate] = field(default_factory=list)
    ignored_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    scanned_at: str = field(default_factory=now_iso)
    root: str = ""

    @property
    def ready(self) -> list[EpisodeCandidate]:
        return [c for c in self.candidates if c.ok]

    @property
    def unstable(self) -> list[EpisodeCandidate]:
        return [c for c in self.candidates if not c.stable and not c.errors]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "scanned_at": self.scanned_at,
            "total": len(self.candidates),
            "ready": len(self.ready),
            "unstable": len(self.unstable),
            "with_errors": len([c for c in self.candidates if c.errors]),
            "ignored_files": list(self.ignored_files),
            "errors": list(self.errors),
            "candidates": [
                {
                    "episode_id": c.episode_id,
                    "video": str(c.video_path) if c.video_path else None,
                    "metadata": str(c.metadata_path) if c.metadata_path else None,
                    "thumbnail": str(c.thumbnail_path) if c.thumbnail_path else None,
                    "stable": c.stable,
                    "stability_reason": c.stability_reason,
                    "size": c.size,
                    "errors": list(c.errors),
                    "warnings": list(c.warnings),
                }
                for c in self.candidates
            ],
        }


@dataclass
class IntakeReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped_duplicate: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)       # bereits hochgeladen
    failed: list[str] = field(default_factory=list)        # Fehler (z. B. JSON fehlt)
    waiting: list[str] = field(default_factory=list)       # noch nicht stabil
    scanned_at: str = field(default_factory=now_iso)

    @property
    def total_new(self) -> int:
        return len(self.created) + len(self.updated)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": list(self.created),
            "updated": list(self.updated),
            "skipped_duplicate": list(self.skipped_duplicate),
            "blocked": list(self.blocked),
            "failed": list(self.failed),
            "waiting": list(self.waiting),
            "scanned_at": self.scanned_at,
        }


class FolderScanner:
    """Ueberwacht den READY-Ordner und pflegt die Episoden-Datenbank."""

    def __init__(
        self,
        settings: Any,
        repository: VideoRepository,
        logger: logging.Logger | None = None,
        tracker: FileStabilityTracker | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.logger = logger or get_logger("scanner")
        self.tracker = tracker or FileStabilityTracker(
            stability_seconds=float(settings.FILE_STABILITY_SECONDS),
            required_checks=int(settings.STABILITY_REQUIRED_CHECKS),
        )

    # ------------------------------------------------------------------
    # Dateisystem
    # ------------------------------------------------------------------

    def _iter_files(self, root: Path, recursive: bool) -> Iterable[Path]:
        if not root.exists():
            return []
        iterator = root.rglob("*") if recursive else root.glob("*")
        return [p for p in iterator if p.is_file()]

    def discover_candidates(self, root: str | Path | None = None) -> ScanResult:
        """READY-Ordner lesen und Dateien zu Episoden gruppieren."""

        folder = Path(root or self.settings.READY_FOLDER)
        result = ScanResult(root=str(folder))
        if not folder.exists():
            result.errors.append(f"READY-Ordner existiert nicht: {folder}")
            return result

        allowed_videos = tuple(str(e).lower() for e in self.settings.ALLOWED_VIDEO_EXTENSIONS)
        allowed_thumbs = tuple(str(e).lower() for e in self.settings.ALLOWED_THUMBNAIL_EXTENSIONS)

        groups: dict[str, dict[str, list[Path]]] = {}
        unsupported: dict[str, list[Path]] = {}

        for path in sorted(self._iter_files(folder, bool(self.settings.SCAN_RECURSIVE))):
            name = path.name
            if is_temp_file(path) or name.startswith("."):
                result.ignored_files.append(str(path))
                continue
            suffix = path.suffix.lower()
            key = path.stem.casefold()
            bucket = groups.setdefault(key, {"video": [], "metadata": [], "thumbnail": [], "other": []})

            if suffix in allowed_videos:
                bucket["video"].append(path)
            elif suffix == METADATA_EXTENSION:
                bucket["metadata"].append(path)
            elif suffix in allowed_thumbs:
                bucket["thumbnail"].append(path)
            elif suffix in {".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".m4v", ".ts", ".mov", ".mkv", ".webm"}:
                unsupported.setdefault(key, []).append(path)
                bucket["other"].append(path)
            else:
                bucket["other"].append(path)
                if suffix not in IGNORED_EXTENSIONS:
                    result.ignored_files.append(str(path))

        for key, bucket in groups.items():
            if not (bucket["video"] or bucket["metadata"] or bucket["thumbnail"]):
                # Nur Fremd-/Systemdateien (readme.txt, Thumbs.db, ...) -> keine Episode
                continue
            candidate = self._build_candidate(key, bucket, allowed_videos)
            for path in unsupported.get(key, []):
                candidate.errors.append(
                    f"Nicht unterstuetztes Videoformat '{path.suffix}' ({path.name}). "
                    f"Erlaubt: {', '.join(allowed_videos)}"
                )
            self._check_stability(candidate)
            result.candidates.append(candidate)

        result.candidates.sort(key=lambda c: c.episode_id)
        return result

    def _build_candidate(
        self,
        key: str,
        bucket: dict[str, list[Path]],
        allowed_videos: tuple[str, ...],
    ) -> EpisodeCandidate:
        videos = sorted(
            bucket["video"],
            key=lambda p: (
                VIDEO_EXTENSION_PRIORITY.index(p.suffix.lower())
                if p.suffix.lower() in VIDEO_EXTENSION_PRIORITY
                else len(VIDEO_EXTENSION_PRIORITY),
                -file_size(p),
            ),
        )
        metadata_files = sorted(bucket["metadata"], key=lambda p: p.name)
        thumbnails = sorted(bucket["thumbnail"], key=lambda p: (-file_size(p), p.name))

        video = videos[0] if videos else None
        metadata = metadata_files[0] if metadata_files else None
        thumbnail = thumbnails[0] if thumbnails else None

        episode_id = video.stem if video else (metadata.stem if metadata else key)
        candidate = EpisodeCandidate(
            episode_id=episode_id,
            video_path=video,
            metadata_path=metadata,
            thumbnail_path=thumbnail,
            folder=(video or metadata or thumbnail).parent if (video or metadata or thumbnail) else None,
            size=file_size(video) if video else 0,
            extra_files=[*videos[1:], *metadata_files[1:], *thumbnails[1:], *bucket["other"]],
        )
        if len(videos) > 1:
            candidate.warnings.append(
                f"Mehrere Videodateien mit demselben Namen gefunden - verwendet wird {video.name}"
                if video
                else "Mehrere Videodateien gefunden"
            )
        if len(metadata_files) > 1:
            candidate.warnings.append("Mehrere JSON-Dateien gefunden - verwendet wird die erste")

        if not video:
            if metadata:
                # Nur JSON, kein Video: vermutlich laeuft die Kopie noch -> warten
                candidate.warnings.append("Videodatei fehlt noch (JSON ist bereits vorhanden) - warte")
        else:
            if not metadata and bool(self.settings.REQUIRE_METADATA_FILE):
                candidate.errors.append(
                    f"Metadaten-Datei fehlt: erwartet wurde '{video.stem}{METADATA_EXTENSION}'"
                )
            if file_size(video) <= 0:
                candidate.errors.append(f"Videodatei ist leer (0 Byte): {video.name}")

        if candidate.extra_files and len(candidate.extra_files) > 3:
            candidate.warnings.append(
                f"{len(candidate.extra_files)} zusaetzliche Datei(en) im Ordner werden ignoriert"
            )
        return candidate

    def _check_stability(self, candidate: EpisodeCandidate) -> None:
        """Alle Dateien einer Episode auf Stabilitaet pruefen."""

        if not candidate.files:
            candidate.stable = False
            candidate.stability_reason = "keine Dateien"
            return

        results = [self.tracker.observe(path) for path in candidate.files]
        candidate.stable = all(r.stable for r in results)
        if not candidate.stable:
            unstable = next((r for r in results if not r.stable), None)
            candidate.stability_reason = unstable.reason if unstable else "Datei wird noch geschrieben"
        else:
            candidate.stability_reason = "stabil"

    # ------------------------------------------------------------------
    # Aufnahme in die Datenbank
    # ------------------------------------------------------------------

    def intake_candidate(self, candidate: EpisodeCandidate) -> tuple[int, str] | None:
        """Eine erkannte Episode in die Datenbank uebernehmen.

        Liefert ``(video_id, aktion)`` oder ``None``, wenn nichts zu tun ist.
        Aktionen: ``created`` | ``updated`` | ``blocked`` | ``failed`` | ``waiting``
        """

        if candidate.errors and not candidate.video_path:
            return None

        # Noch nicht stabil (Datei wird kopiert) -> bewusst nicht aufnehmen
        if not candidate.stable and candidate.video_path is not None and not candidate.errors:
            self.logger.debug(
                "[%s] warte auf stabile Datei (%s)", candidate.episode_id, candidate.stability_reason
            )
            return None

        existing = self.repository.get_by_episode(candidate.episode_id)
        if existing:
            status = str(existing.get("status"))
            if status in {s.value for s in (
                VideoStatus.UPLOADED_PRIVATE,
                VideoStatus.PUBLISH_READY,
                VideoStatus.PUBLISHING,
                VideoStatus.PUBLISHED,
                VideoStatus.SKIPPED_DUPLICATE,
                VideoStatus.UPLOADING,
                VideoStatus.VALIDATING,
            )}:
                self.logger.info(
                    "[%s] Episode ist bereits im Status %s - kein erneuter Upload (Duplikatschutz)",
                    candidate.episode_id,
                    status,
                )
                return int(existing["id"]), "blocked"

        fields: dict[str, Any] = {
            "platform": "youtube",
            "status": VideoStatus.READY.value,
            "folder_state": "READY",
            "needs_attention": 0,
            "error_message": None,
            "error_code": None,
            "retry_count": 0,
            "progress_percent": 0,
        }
        if candidate.video_path:
            fields.update(
                {
                    "video_filename": candidate.video_path.name,
                    "video_path": str(candidate.video_path),
                    "file_size": candidate.size,
                }
            )
        if candidate.metadata_path:
            fields.update(
                {
                    "metadata_filename": candidate.metadata_path.name,
                    "metadata_path": str(candidate.metadata_path),
                }
            )
        if candidate.thumbnail_path:
            fields.update(
                {
                    "thumbnail_filename": candidate.thumbnail_path.name,
                    "thumbnail_path": str(candidate.thumbnail_path),
                }
            )

        # Titel/Tags schon beim Erkennen uebernehmen, damit das Dashboard
        # etwas anzeigen kann, bevor die Pipeline gelaufen ist. Fehler im JSON
        # werden hier bewusst ignoriert - die Pipeline meldet sie sauber.
        fields.update(self._peek_metadata(candidate))

        action = "updated" if existing else "created"
        if candidate.errors:
            fields["status"] = VideoStatus.FAILED.value
            fields["error_message"] = "; ".join(candidate.errors)
            fields["error_code"] = "intake_error"
            fields["needs_attention"] = 1
            action = "failed"

        video_id, was_new = self.repository.upsert_episode(candidate.episode_id, **fields)
        if was_new:
            action = "created" if action != "failed" else "failed"
        if candidate.warnings:
            self.repository.add_log(
                "INFO",
                "; ".join(candidate.warnings),
                episode_id=candidate.episode_id,
                action="INTAKE",
            )
        self.logger.info(
            "[%s] Episode erkannt (%s): %s",
            candidate.episode_id,
            action,
            candidate.video_path.name if candidate.video_path else "ohne Videodatei",
        )
        return int(video_id), action

    def _peek_metadata(self, candidate: EpisodeCandidate) -> dict[str, Any]:
        """JSON-Datei vorsichtig anlesen (nur fuer die Anzeige)."""

        if not candidate.metadata_path:
            return {}
        try:
            from ..metadata.parser import parse_metadata_file

            parsed = parse_metadata_file(candidate.metadata_path)
        except Exception:  # noqa: BLE001 - Fehler meldet die Pipeline
            return {}
        metadata = parsed.metadata
        values: dict[str, Any] = {
            "title": metadata.title or None,
            "description": metadata.description or None,
            "tags_json": list(metadata.tags),
            "category_id": metadata.category_id or None,
            "language": metadata.language or None,
            "privacy_status_requested": metadata.privacy_status_requested or None,
            "publish_at_requested": metadata.publish_at,
            "metadata_json": metadata.to_dict(),
        }
        if metadata.thumbnail and candidate.folder:
            thumb = Path(candidate.folder) / metadata.thumbnail
            if thumb.exists():
                values["thumbnail_filename"] = thumb.name
                values["thumbnail_path"] = str(thumb)
        return {key: value for key, value in values.items() if value is not None}

    def scan(self, root: str | Path | None = None) -> ScanResult:
        """Nur scannen (ohne Datenbank-Aenderung) - z. B. fuer --dry-run."""

        result = self.discover_candidates(root)
        self.repository.set_setting("scanner.last_scan_at", result.scanned_at)
        return result

    def scan_and_intake(self, root: str | Path | None = None) -> tuple[ScanResult, IntakeReport]:
        """Scannen und neue/veraenderte Episoden in die Datenbank aufnehmen."""

        result = self.discover_candidates(root)
        report = IntakeReport(scanned_at=result.scanned_at)

        for candidate in result.candidates:
            try:
                outcome = self.intake_candidate(candidate)
            except Exception as exc:  # noqa: BLE001 - eine Episode darf den Scan nicht stoppen
                message = f"Aufnahme fehlgeschlagen: {exc}"
                result.errors.append(f"{candidate.episode_id}: {message}")
                self.logger.exception("[%s] %s", candidate.episode_id, message)
                report.failed.append(candidate.episode_id)
                continue
            if outcome is None:
                report.waiting.append(candidate.episode_id)
                continue
            _video_id, action = outcome
            bucket = {
                "created": report.created,
                "updated": report.updated,
                "blocked": report.blocked,
                "failed": report.failed,
            }.get(action)
            if bucket is not None:
                bucket.append(candidate.episode_id)

        self.repository.set_setting("scanner.last_scan_at", result.scanned_at)
        self.repository.set_setting("scanner.last_scan_found", str(len(result.candidates)))
        self.repository.set_setting("scanner.last_scan_new", str(report.total_new))
        self.tracker.prune([p for c in result.candidates for p in c.files])

        if report.created or report.failed:
            self.logger.info(
                "Scan abgeschlossen: %d neu, %d aktualisiert, %d fehlerhaft, %d wartend, %d blockiert",
                len(report.created),
                len(report.updated),
                len(report.failed),
                len(report.waiting),
                len(report.blocked),
            )
        return result, report


__all__ = ["EpisodeCandidate", "FolderScanner", "IntakeReport", "ScanResult"]
