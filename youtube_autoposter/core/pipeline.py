"""Die Upload-Pipeline: erkennen -> pruefen -> hochladen -> archivieren.

Ablauf pro Episode (strikt seriell, 1 Upload gleichzeitig):

 1. Datensatz atomar reservieren (``claim``)            -> kein Doppel-Processing
 2. Plattform-Bereitschaft pruefen (OAuth vorhanden?)    -> sonst PAUSED
 3. Datei-Stabilitaet abwarten (Kopierschutz)
 4. SHA-256 berechnen
 5. Duplikatschutz (Episode-ID UND Datei-Hash)           -> SKIPPED_DUPLICATE
 6. Metadaten parsen (JSON)
 7. Dateien READY -> PROCESSING verschieben (atomar, mit Integritaetspruefung)
 8. Vollstaendige Validierung (Metadaten + ffprobe + Thumbnail)
 9. Upload ueber die offizielle YouTube Data API v3 (resumable, Retries)
10. Thumbnail setzen (nicht blockierend)
11. Dateien PROCESSING -> UPLOADED_PRIVATE archivieren
12. Status, YouTube-ID, URL, Zeitstempel in SQLite speichern

Fehler fuehren pro Episode zu FAILED (Dateien nach FAILED/) - die naechste
Episode wird trotzdem verarbeitet. Es wird niemals automatisch
veroeffentlicht.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..auth.youtube_auth import AuthState
from ..constants import (
    ENFORCED_UPLOAD_PRIVACY,
    PRIVACY_PUBLIC,
    Phase,
    VideoStatus,
)
from ..database.repository import VideoRepository
from ..errors import (
    AuthError,
    AutoPosterError,
    DuplicateError,
    FileOperationError,
    MetadataError,
    PlatformError,
    QuotaExceededError,
    StateError,
    UploadError,
    ValidationError,
)
from ..metadata.parser import EpisodeMetadata, MetadataIssue, ParseResult, parse_metadata_file
from ..platforms.base import PlatformAdapter, PublicationJob
from ..platforms.registry import PlatformRegistry
from ..utils.files import (
    atomic_move,
    file_size,
    format_duration,
    human_size,
    now_iso,
    safe_folder_name,
    sha256_file,
    wait_for_stable_file,
)
from ..utils.logging_utils import episode_logger, redact
from ..utils.validation import validate_episode
from .models import VideoRecord

PROGRESS_DB_INTERVAL_SECONDS = 5.0

#: Ordnerzustaende, die als "Archiv" gelten: pro Episode ein Unterordner und
#: eine Ergebnisdatei (upload_result.json) neben den Dateien.
ARCHIVE_FOLDER_STATES = ("UPLOADED_PRIVATE", "PUBLISHED", "FAILED", "SKIPPED")


@dataclass
class PipelineOutcome:
    """Ergebnis eines Verarbeitungslaufs (fuer UI, CLI und Tests)."""

    video_id: int = 0
    episode_id: str = ""
    status: str = ""
    success: bool = False
    skipped: bool = False
    message: str = ""
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "episode_id": self.episode_id,
            "status": self.status,
            "success": self.success,
            "skipped": self.skipped,
            "message": self.message,
            "youtube_video_id": self.youtube_video_id,
            "youtube_url": self.youtube_url,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "steps": list(self.steps),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


class UploadPipeline:
    """Verarbeitet einzelne Episoden - immer seriell, immer fehlertolerant."""

    def __init__(
        self,
        *,
        settings: Any,
        repository: VideoRepository,
        registry: PlatformRegistry,
        logger: logging.Logger | None = None,
        auth: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        dry_run: bool = False,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.registry = registry
        self.auth = auth
        self.logger = logger or logging.getLogger("youtube_autoposter.pipeline")
        self._sleep = sleep
        self._stop_requested = False
        #: Sicherheitsnetz: Im Dry-Run darf die Pipeline niemals etwas senden.
        self.dry_run = bool(dry_run)

    # ------------------------------------------------------------------
    # Steuerung
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """Signalisiert einen sauberen Abbruch (wird zwischen Schritten geprueft)."""

        self._stop_requested = True

    def reset_stop(self) -> None:
        self._stop_requested = False

    def adapter_for(self, record: VideoRecord) -> PlatformAdapter:
        return self.registry.get(record.platform or "youtube")

    def process_next(self, *, statuses: tuple[VideoStatus, ...] | None = None) -> PipelineOutcome | None:
        """Naechste Episode aus der Queue verarbeiten."""

        wanted = statuses or (VideoStatus.NEW, VideoStatus.READY)
        row = self.repository.next_queued(wanted)
        if not row:
            return None
        return self.process(int(row["id"]))

    # ------------------------------------------------------------------
    # Haupt-Pipeline
    # ------------------------------------------------------------------

    def process(self, video_id: int) -> PipelineOutcome:
        """Eine Episode vollstaendig verarbeiten. Wirft keine Exceptions."""

        started = time.monotonic()
        record_row = self.repository.get(video_id)
        record = VideoRecord.from_row(record_row)
        if record is None:
            return PipelineOutcome(
                video_id=video_id,
                status=VideoStatus.FAILED.value,
                message=f"Datensatz {video_id} nicht gefunden",
                errors=["Datensatz nicht gefunden"],
            )

        log = episode_logger(self.logger, record.episode_id)
        outcome = PipelineOutcome(
            video_id=video_id,
            episode_id=record.episode_id,
            status=record.status,
        )

        # --- 0. Dry-Run: grundsaetzlich nichts veraendern --------------
        if self.dry_run:
            outcome.skipped = True
            outcome.success = True
            outcome.message = (
                "Dry-Run aktiv: Es wurde nichts hochgeladen, nichts verschoben und "
                "kein Status geaendert."
            )
            log.info(outcome.message)
            return outcome

        # --- 1. Duplikatschutz Stufe 1: Status -------------------------
        if record.status in {s.value for s in (
            VideoStatus.UPLOADED_PRIVATE,
            VideoStatus.PUBLISH_READY,
            VideoStatus.PUBLISHING,
            VideoStatus.PUBLISHED,
            VideoStatus.SKIPPED_DUPLICATE,
        )}:
            outcome.skipped = True
            outcome.success = True
            outcome.status = record.status
            outcome.message = (
                f"Bereits verarbeitet (Status {record.status}) - es wird nichts erneut hochgeladen"
            )
            log.info(outcome.message)
            outcome.elapsed_seconds = time.monotonic() - started
            return outcome

        # --- 2. atomar reservieren -------------------------------------
        claimed = self.repository.claim(
            video_id,
            from_statuses=(
                VideoStatus.NEW.value,
                VideoStatus.READY.value,
                VideoStatus.PAUSED.value,
                VideoStatus.VALIDATING.value,
            ),
            to_status=VideoStatus.VALIDATING.value,
        )
        if not claimed:
            current = self.repository.get(video_id) or {}
            outcome.skipped = True
            outcome.status = str(current.get("status", ""))
            outcome.message = (
                f"Episode wird bereits verarbeitet (Status {outcome.status}) - uebersprungen"
            )
            log.info(outcome.message)
            outcome.elapsed_seconds = time.monotonic() - started
            return outcome

        record = VideoRecord.from_row(self.repository.get(video_id)) or record
        attempt_id = self.repository.start_attempt(video_id, Phase.VALIDATE.value)
        log.info("Verarbeitung gestartet: %s", record.video_filename or record.episode_id)
        outcome.steps.append("reserviert")

        try:
            self._run(record, outcome, log)
        except _PipelineAbort as abort:
            # Bereits behandelt (Status/Dateien gesetzt) - nur Ergebnis melden
            outcome.status = abort.status
            outcome.success = abort.success
            outcome.skipped = abort.skipped
            outcome.message = abort.message
            if abort.error:
                outcome.errors.append(abort.error)
            outcome.warnings.extend(abort.warnings)
        except Exception as exc:  # noqa: BLE001 - letzte Sicherheitsstufe
            message = redact(str(exc))
            log.exception("Unerwarteter Fehler: %s", message)
            self._fail(record, message, code=type(exc).__name__, attempt_id=attempt_id)
            outcome.status = VideoStatus.FAILED.value
            outcome.success = False
            outcome.message = f"Unerwarteter Fehler: {message}"
            outcome.errors.append(message)
        else:
            self.repository.finish_attempt(attempt_id, status="SUCCEEDED")

        final = self.repository.get(video_id) or {}
        outcome.status = str(final.get("status", outcome.status))
        outcome.youtube_video_id = final.get("youtube_video_id")
        outcome.youtube_url = final.get("youtube_url")
        outcome.elapsed_seconds = time.monotonic() - started
        if not outcome.message:
            outcome.message = f"Status: {outcome.status}"
        return outcome

    # ------------------------------------------------------------------

    def _run(self, record: VideoRecord, outcome: PipelineOutcome, log: Any) -> None:
        """Die eigentliche Verarbeitungskette (darf _PipelineAbort werfen)."""

        adapter = self.adapter_for(record)

        # --- 2. Plattform/OAuth bereit? --------------------------------
        ready, reason = adapter.is_ready()
        if not ready:
            self._pause(record, reason)
            raise _PipelineAbort(
                VideoStatus.PAUSED.value,
                message=f"Warte auf YouTube-Verbindung: {reason}",
                skipped=True,
                success=False,
            )

        video_path = Path(record.video_path) if record.video_path else None
        if video_path is None or not video_path.exists():
            message = f"Videodatei nicht gefunden: {record.video_path or '(kein Pfad gespeichert)'}"
            self._fail(record, message, code="video_missing")
            raise _PipelineAbort(VideoStatus.FAILED.value, message=message, error=message)
        outcome.steps.append("videodatei vorhanden")

        # --- 3. Stabilitaet (Schutz gegen laufende Kopien) -------------
        if self.settings.STABILITY_WAIT_BEFORE_UPLOAD and self.settings.FILE_STABILITY_SECONDS:
            log.info(
                "Stabilitaetspruefung: %s (%s) muss %.0fs unveraendert bleiben",
                video_path.name,
                human_size(file_size(video_path)),
                self.settings.FILE_STABILITY_SECONDS,
            )
            stability = wait_for_stable_file(
                video_path,
                stability_seconds=float(self.settings.FILE_STABILITY_SECONDS),
                interval=float(self.settings.STABILITY_CHECK_INTERVAL),
                required_checks=int(self.settings.STABILITY_REQUIRED_CHECKS),
                sleep=self._sleep,
            )
            if not stability.stable:
                message = f"Datei ist nicht stabil ({stability.reason})"
                self._pause(record, message)
                raise _PipelineAbort(
                    VideoStatus.PAUSED.value,
                    message=message,
                    skipped=True,
                )
            outcome.steps.append(f"stabil ({stability.checks} Pruefungen)")

        # --- 4. Hash ----------------------------------------------------
        current_size = file_size(video_path)
        need_hash = not record.file_hash or int(record.file_size or 0) != current_size
        if need_hash:
            log.info("Berechne SHA-256 fuer %s (%s)", video_path.name, human_size(current_size))
            digest = sha256_file(video_path)
            self.repository.update(
                record.id,
                file_hash=digest,
                file_size=current_size,
            )
            record.file_hash = digest
            record.file_size = current_size
        outcome.steps.append("sha256 ermittelt")

        # --- 5. Duplikatschutz -----------------------------------------
        duplicate = self._find_duplicate(record)
        if duplicate:
            message, existing = duplicate
            self._mark_duplicate(record, message, existing)
            raise _PipelineAbort(
                VideoStatus.SKIPPED_DUPLICATE.value,
                message=message,
                skipped=True,
                success=True,
            )
        outcome.steps.append("keine Dublette")

        # --- 6. Metadaten parsen ---------------------------------------
        parse_result = self._parse_metadata(record)
        metadata = parse_result.metadata
        outcome.warnings.extend(str(w) for w in parse_result.warnings)
        if not parse_result.ok:
            message = "; ".join(parse_result.error_messages()) or "Metadaten ungueltig"
            self._fail(record, f"Metadaten-Fehler: {message}", code="metadata_invalid")
            raise _PipelineAbort(VideoStatus.FAILED.value, message=message, error=message)
        outcome.steps.append("metadaten ok")

        # --- 7. nach PROCESSING verschieben -----------------------------
        self._move_files(record, self.settings.PROCESSING_FOLDER, folder_state="PROCESSING")
        record = VideoRecord.from_row(self.repository.get(record.id)) or record
        outcome.steps.append("nach PROCESSING verschoben")

        # --- 8. Validierung --------------------------------------------
        validation = validate_episode(
            metadata=metadata,
            video_path=record.video_path,
            thumbnail_path=record.thumbnail_path,
            settings=self.settings,
        )
        self._store_validation(record, metadata, validation)
        for issue in validation.warnings:
            outcome.warnings.append(issue.message)
            log.warning("%s", issue.message)
        if not validation.ok:
            message = "; ".join(validation.error_messages())
            self._fail(record, f"Validierung fehlgeschlagen: {message}", code="validation_failed")
            raise _PipelineAbort(VideoStatus.FAILED.value, message=message, error=message)
        if validation.media_info and validation.media_info.probe_ok:
            log.info(
                "Technische Pruefung: %s | %s | %s | %s",
                validation.media_info.resolution,
                validation.media_info.duration_text,
                validation.media_info.codec_text,
                validation.media_info.size_text,
            )
        outcome.steps.append("validierung ok")

        # --- optionale Online-Pruefung der Kategorie -------------------
        for warning in adapter.validate_online(metadata):
            outcome.warnings.append(warning)
            log.warning("%s", warning)

        if self._stop_requested:
            message = "Abbruch durch Benutzer vor dem Upload (Dateien liegen in PROCESSING)"
            self._pause(record, message)
            raise _PipelineAbort(VideoStatus.PAUSED.value, message=message, skipped=True)

        # --- 9. Upload --------------------------------------------------
        job = self._build_job(record, metadata)
        upload_attempt = self.repository.start_attempt(record.id, Phase.UPLOAD.value)
        self.repository.update(
            record.id,
            status=VideoStatus.UPLOADING.value,
            upload_started_at=now_iso(),
            progress_percent=0,
            error_message=None,
            error_code=None,
            needs_attention=0,
        )
        record.status = VideoStatus.UPLOADING.value
        log.info("Upload gestartet (%s)", human_size(record.file_size or 0))

        try:
            result = adapter.upload(job)
        except PlatformError as exc:
            self._handle_platform_error(record, exc, upload_attempt, outcome, log)
            raise _PipelineAbort(
                outcome.status or VideoStatus.FAILED.value,
                message=outcome.message,
                error=str(exc),
            ) from exc
        except AuthError as exc:
            self.repository.finish_attempt(upload_attempt, status="FAILED", error=str(exc))
            self._pause(record, f"YouTube-Autorisierung fehlt: {exc}")
            raise _PipelineAbort(
                VideoStatus.PAUSED.value, message=f"Warte auf Autorisierung: {exc}", skipped=True
            ) from exc

        youtube_id = result.platform_video_id
        self.repository.finish_attempt(
            upload_attempt,
            status="SUCCEEDED",
            youtube_video_id=youtube_id,
            bytes_sent=result.bytes_sent,
            quota_units=result.quota_units,
        )
        self.repository.update(
            record.id,
            status=VideoStatus.UPLOADED_PRIVATE.value,
            youtube_video_id=youtube_id,
            youtube_url=result.url,
            thumbnail_url=result.thumbnail_url,
            effective_privacy_status=result.privacy_status or ENFORCED_UPLOAD_PRIVACY,
            upload_completed_at=now_iso(),
            progress_percent=100,
            error_message=None,
            error_code=None,
            needs_attention=0,
        )
        record.youtube_video_id = youtube_id
        record.youtube_url = result.url
        record.status = VideoStatus.UPLOADED_PRIVATE.value
        log.info("Upload abgeschlossen - YouTube-ID: %s", youtube_id)
        log.info("YouTube-URL: %s", result.url)
        log.info("Status: %s (PRIVAT - Veroeffentlichen nur per Button)", result.privacy_status.upper())
        outcome.youtube_video_id = youtube_id
        outcome.youtube_url = result.url
        outcome.success = True
        outcome.message = f"Privat hochgeladen: {result.url}"
        outcome.steps.append(f"upload ok ({youtube_id})")
        self.repository.add_log(
            "INFO",
            f"Upload abgeschlossen: {youtube_id} ({result.privacy_status})",
            episode_id=record.episode_id,
            action="UPLOAD",
        )

        # --- 10. Thumbnail ---------------------------------------------
        self._apply_thumbnail(record, adapter, outcome, log)

        # --- 11. Archivieren -------------------------------------------
        if self.settings.ARCHIVE_AFTER_UPLOAD:
            moved = self._move_files(
                record, self.settings.UPLOADED_PRIVATE_FOLDER, folder_state="UPLOADED_PRIVATE"
            )
            if moved:
                outcome.steps.append("archiviert nach UPLOADED_PRIVATE")
        self.repository.update(record.id, status=VideoStatus.UPLOADED_PRIVATE.value)

    # ------------------------------------------------------------------
    # Einzel-Schritte
    # ------------------------------------------------------------------

    def _find_duplicate(self, record: VideoRecord) -> tuple[str, dict[str, Any]] | None:
        by_episode = self.repository.find_uploaded_by_episode(record.episode_id)
        if by_episode and int(by_episode["id"]) != record.id:
            return (
                f"Episode-ID '{record.episode_id}' wurde bereits hochgeladen "
                f"(YouTube-ID: {by_episode.get('youtube_video_id')})",
                by_episode,
            )
        if record.file_hash:
            by_hash = self.repository.find_uploaded_by_hash(record.file_hash, exclude_id=record.id)
            if by_hash:
                return (
                    f"Identische Videodatei (SHA-256 {record.file_hash[:12]}...) wurde bereits als "
                    f"Episode '{by_hash.get('episode_id')}' hochgeladen "
                    f"(YouTube-ID: {by_hash.get('youtube_video_id')})",
                    by_hash,
                )
        return None

    def _mark_duplicate(self, record: VideoRecord, message: str, existing: dict[str, Any]) -> None:
        self.repository.update(
            record.id,
            status=VideoStatus.SKIPPED_DUPLICATE.value,
            error_message=message,
            error_code="duplicate",
            needs_attention=1,
            youtube_video_id=existing.get("youtube_video_id"),
            youtube_url=existing.get("youtube_url"),
        )
        self.repository.add_log("WARNING", message, episode_id=record.episode_id, action="DUPLICATE")
        episode_logger(self.logger, record.episode_id).warning(message)
        self._move_files(record, self.settings.SKIPPED_FOLDER, folder_state="SKIPPED", required=False)

    def _parse_metadata(self, record: VideoRecord) -> ParseResult:
        path = record.metadata_path
        if not path:
            raise MetadataError("Keine Metadaten-Datei zugeordnet")
        result = parse_metadata_file(path)

        # Ein in der JSON angegebenes Thumbnail hat Vorrang vor der
        # Namens-Konvention (z. B. video_001.jpg).
        wanted = result.metadata.thumbnail
        if wanted:
            candidate = Path(path).parent / wanted
            if candidate.exists():
                self.repository.update(
                    record.id,
                    thumbnail_filename=candidate.name,
                    thumbnail_path=str(candidate),
                )
                record.thumbnail_path = str(candidate)
                record.thumbnail_filename = candidate.name
            else:
                result.warnings.append(
                    MetadataIssue(
                        "warning",
                        "thumbnail_not_found",
                        f"In der JSON angegebenes Thumbnail '{wanted}' wurde nicht gefunden",
                        "thumbnail",
                    )
                )
        return result

    def _store_validation(self, record: VideoRecord, metadata: EpisodeMetadata, validation: Any) -> None:
        media = validation.media_info
        fields: dict[str, Any] = {
            "title": metadata.title,
            "description": metadata.description,
            "tags_json": list(metadata.tags),
            "category_id": metadata.category_id or self.settings.DEFAULT_CATEGORY_ID,
            "language": metadata.language or self.settings.DEFAULT_LANGUAGE,
            "audio_language": metadata.audio_language,
            "playlist_id": metadata.playlist_id,
            "channel_identifier": metadata.channel_identifier,
            "made_for_kids": None if metadata.made_for_kids is None else int(metadata.made_for_kids),
            "self_declared_made_for_kids": (
                None if metadata.self_declared_made_for_kids is None else int(metadata.self_declared_made_for_kids)
            ),
            "contains_synthetic_media": (
                None if metadata.contains_synthetic_media is None else int(metadata.contains_synthetic_media)
            ),
            "publish_at_requested": metadata.publish_at,
            "privacy_status_requested": metadata.privacy_status_requested or ENFORCED_UPLOAD_PRIVACY,
            "effective_privacy_status": ENFORCED_UPLOAD_PRIVACY,
            "metadata_json": metadata.to_dict(),
            "validation_json": validation.to_dict(),
            "thumbnail_info_json": validation.thumbnail_info.to_dict() if validation.thumbnail_info else None,
            "media_info_json": media.to_dict() if media else None,
        }
        if media:
            fields.update(
                {
                    "duration_seconds": media.duration_seconds,
                    "width": media.width,
                    "height": media.height,
                    "video_codec": media.video_codec,
                    "audio_codec": media.audio_codec,
                    "fps": media.fps,
                    "has_audio": int(bool(media.has_audio)),
                }
            )
        self.repository.update(record.id, **fields)
        record.title = metadata.title
        record.description = metadata.description
        record.tags = list(metadata.tags)

    def _build_job(self, record: VideoRecord, metadata: EpisodeMetadata) -> PublicationJob:
        job = PublicationJob(
            video_id=record.id,
            episode_id=record.episode_id,
            platform=record.platform or "youtube",
            video_path=str(record.video_path),
            metadata=metadata,
            thumbnail_path=str(record.thumbnail_path) if record.thumbnail_path else None,
            file_size=int(record.file_size or 0),
            file_hash=record.file_hash,
            platform_video_id=record.youtube_video_id,
            progress_cb=self._make_progress_callback(record.id),
        )
        return job

    def _make_progress_callback(self, video_id: int) -> Callable[[int, int], None]:
        state = {"last_write": 0.0, "last_percent": -1}

        def callback(sent: int, total: int) -> None:
            now = time.monotonic()
            percent = int((sent / total) * 100) if total else 0
            if percent == state["last_percent"] and now - state["last_write"] < PROGRESS_DB_INTERVAL_SECONDS:
                return
            if now - state["last_write"] < PROGRESS_DB_INTERVAL_SECONDS and percent != 100:
                return
            state["last_write"] = now
            state["last_percent"] = percent
            try:
                self.repository.update(video_id, progress_percent=min(100, max(0, percent)))
            except Exception:  # noqa: BLE001 - Fortschritt darf den Upload nie stoppen
                pass

        return callback

    def _apply_thumbnail(
        self, record: VideoRecord, adapter: PlatformAdapter, outcome: PipelineOutcome, log: Any
    ) -> None:
        if not record.thumbnail_path or not record.youtube_video_id:
            log.info("Kein Thumbnail angegeben - Schritt uebersprungen")
            self.repository.update(record.id, thumbnail_error=None, thumbnail_set=0)
            outcome.steps.append("kein thumbnail")
            return

        attempt_id = self.repository.start_attempt(record.id, Phase.THUMBNAIL.value)
        result = adapter.set_thumbnail(record.youtube_video_id, record.thumbnail_path)
        self.repository.finish_attempt(
            attempt_id,
            status="SUCCEEDED" if result.ok else "FAILED",
            error=None if result.ok else result.message,
            quota_units=result.quota_units,
        )
        self.repository.update(
            record.id,
            thumbnail_set=1 if result.ok else 0,
            thumbnail_url=result.url or record.thumbnail_url,
            thumbnail_error=None if result.ok else result.message,
        )
        if result.ok:
            log.info("Thumbnail gesetzt: %s", Path(record.thumbnail_path).name)
            outcome.steps.append("thumbnail gesetzt")
        else:
            log.warning("Thumbnail-Fehler (Upload bleibt gueltig): %s", result.message)
            outcome.warnings.append(f"Thumbnail: {result.message}")
            outcome.steps.append("thumbnail fehlgeschlagen")
            self.repository.update(record.id, needs_attention=0)

    def _archive_folder(self, record: VideoRecord, target_folder: str | Path, folder_state: str) -> Path:
        """Zielordner bestimmen: Archiv-Ordner bekommen pro Episode einen Unterordner."""

        folder = Path(target_folder)
        if folder_state not in ARCHIVE_FOLDER_STATES:
            return folder
        if not bool(getattr(self.settings, "ARCHIVE_IN_SUBFOLDER", True)):
            return folder
        name = safe_folder_name(record.episode_id or "", fallback=f"episode_{record.id}")
        return folder / name

    def write_result_json(
        self,
        record: VideoRecord,
        folder: str | Path,
        *,
        status: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path | None:
        """``upload_result.json`` in den Archiv-Ordner der Episode schreiben.

        Zusaetzliche, menschenlesbare Ergebnissicherung neben der Datenbank.
        Ein Fehler hier darf niemals den Upload/die Veroeffentlichung kippen.
        """

        if not bool(getattr(self.settings, "WRITE_RESULT_JSON", True)):
            return None
        filename = str(getattr(self.settings, "RESULT_JSON_FILENAME", "") or "upload_result.json")
        log = episode_logger(self.logger, record.episode_id)
        try:
            row = self.repository.get(record.id) or {}
            payload: dict[str, Any] = {
                "episode_id": record.episode_id,
                "platform": row.get("platform") or "youtube",
                "title": row.get("title") or record.title,
                "youtube_video_id": row.get("youtube_video_id"),
                "youtube_url": row.get("youtube_url"),
                "upload_status": status or row.get("status") or record.status,
                "privacy_status_requested": row.get("privacy_status_requested"),
                "effective_privacy_status": row.get("effective_privacy_status") or "private",
                "uploaded_at": row.get("upload_completed_at"),
                "published_at": row.get("published_at"),
                "thumbnail": {
                    "set": bool(row.get("thumbnail_set")),
                    "url": row.get("thumbnail_url"),
                    "error": row.get("thumbnail_error"),
                },
                "files": {
                    "video": row.get("video_path") or record.video_path,
                    "metadata": row.get("metadata_path") or record.metadata_path,
                    "thumbnail": row.get("thumbnail_path") or record.thumbnail_path,
                    "folder": str(folder),
                },
                "file_size": row.get("file_size") or record.file_size,
                "file_hash": row.get("file_hash") or record.file_hash,
                "duration_seconds": row.get("duration_seconds"),
                "resolution": (
                    f"{row.get('width')}x{row.get('height')}"
                    if row.get("width") and row.get("height")
                    else None
                ),
                "retry_count": row.get("retry_count") or 0,
                "error_code": row.get("error_code"),
                "error_message": row.get("error_message"),
                "needs_attention": bool(row.get("needs_attention")),
                "updated_at": now_iso(),
            }
            if extra:
                payload.update(extra)
            target_dir = Path(folder)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / filename
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, target)
            return target
        except Exception as exc:  # pragma: no cover - darf nie kippen
            log.warning("Ergebnisdatei konnte nicht geschrieben werden: %s", exc)
            return None

    def _move_files(
        self,
        record: VideoRecord,
        target_folder: str | Path,
        *,
        folder_state: str,
        required: bool = True,
    ) -> list[tuple[str, str]]:
        """Video, JSON und Thumbnail gemeinsam verschieben und Pfade aktualisieren."""

        folder = self._archive_folder(record, target_folder, folder_state)
        folder.mkdir(parents=True, exist_ok=True)
        moved: list[tuple[str, str]] = []
        errors: list[str] = []
        source_dirs: set[Path] = set()
        mapping = (
            ("video_path", record.video_path),
            ("metadata_path", record.metadata_path),
            ("thumbnail_path", record.thumbnail_path),
        )
        updates: dict[str, Any] = {}
        for field_name, raw in mapping:
            if not raw:
                continue
            source = Path(raw)
            if not source.exists():
                continue
            try:
                target = atomic_move(source, folder, verify_hash=self.settings.VERIFY_HASH_AFTER_MOVE)
            except FileOperationError as exc:
                errors.append(f"{source.name}: {exc}")
                continue
            moved.append((str(source), str(target)))
            updates[field_name] = str(target)
            source_dirs.add(source.parent)

        if errors and required:
            raise FileOperationError("; ".join(errors))
        if errors:
            episode_logger(self.logger, record.episode_id).warning(
                "Dateien konnten nicht vollstaendig verschoben werden: %s", "; ".join(errors)
            )

        if updates:
            updates["folder_state"] = folder_state
            if folder_state in ARCHIVE_FOLDER_STATES:
                updates["archived_path"] = str(folder)
            self.repository.update(record.id, **updates)
            for key, value in updates.items():
                if hasattr(record, key):
                    setattr(record, key, value)

        if folder_state in ARCHIVE_FOLDER_STATES:
            self.write_result_json(record, folder)

        # Leere Episode-Unterordner aufraeumen. ``rmdir`` entfernt ausschliesslich
        # leere Ordner - es kann also niemals eine Datei verloren gehen. Die
        # Hauptordner (READY, PROCESSING, ...) bleiben immer erhalten.
        if moved and bool(getattr(self.settings, "ARCHIVE_IN_SUBFOLDER", True)):
            geschuetzt = {
                Path(p).resolve()
                for p in (
                    self.settings.READY_FOLDER,
                    self.settings.PROCESSING_FOLDER,
                    self.settings.UPLOADED_PRIVATE_FOLDER,
                    self.settings.PUBLISHED_FOLDER,
                    self.settings.FAILED_FOLDER,
                    self.settings.SKIPPED_FOLDER,
                )
            }
            ergebnis_name = str(
                getattr(self.settings, "RESULT_JSON_FILENAME", "") or "upload_result.json"
            )
            for source_dir in sorted(source_dirs, key=lambda d: len(str(d)), reverse=True):
                try:
                    if source_dir.resolve() in geschuetzt:
                        continue
                    # Die Ergebnisdatei ist generiert (kein Original) und wurde
                    # soeben in den neuen Ordner geschrieben - die alte Kopie
                    # darf deshalb weichen, sonst bleibt ein leerer Ordner stehen.
                    alte_ergebnisdatei = source_dir / ergebnis_name
                    if folder != source_dir and alte_ergebnisdatei.is_file():
                        alte_ergebnisdatei.unlink(missing_ok=True)
                    if source_dir.is_dir() and not any(source_dir.iterdir()):
                        source_dir.rmdir()
                except OSError:
                    continue
        return moved

    def _pause(self, record: VideoRecord, reason: str) -> None:
        """Wartezustand (z. B. keine OAuth-Verbindung) - Dateien bleiben liegen."""

        self.repository.update(
            record.id,
            status=VideoStatus.PAUSED.value,
            error_message=reason,
            error_code="paused",
            needs_attention=1,
            progress_percent=0,
        )
        self.repository.add_log("WARNING", f"Pausiert: {reason}", episode_id=record.episode_id, action="PAUSE")
        episode_logger(self.logger, record.episode_id).warning("Pausiert: %s", reason)
        # Falls die Dateien bereits in PROCESSING liegen: zurueck nach READY,
        # damit der Watcher sie nach der Verbindung erneut aufnimmt.
        if record.folder_state == "PROCESSING" and record.video_path:
            try:
                self._move_files(record, self.settings.READY_FOLDER, folder_state="READY", required=False)
            except FileOperationError as exc:
                episode_logger(self.logger, record.episode_id).warning(
                    "Zurueckverschieben nach READY nicht moeglich: %s", exc
                )

    def _handle_platform_error(
        self,
        record: VideoRecord,
        exc: PlatformError,
        attempt_id: int,
        outcome: PipelineOutcome,
        log: Any,
    ) -> None:
        """Upload-Fehler: Retry (Backoff) oder FAILED mit Archivierung."""

        message = redact(str(exc))
        self.repository.finish_attempt(
            attempt_id,
            status="FAILED",
            error=message,
            http_status=exc.status_code,
        )
        retry_count = int(record.retry_count or 0) + 1
        self.repository.update(record.id, retry_count=retry_count)

        if isinstance(exc, QuotaExceededError):
            # Quota ist am naechsten Tag wieder da: Episode wartet, statt als
            # Fehler zu gelten. Es wird bewusst NICHT sofort erneut versucht
            # (das wuerde nur weitere Einheiten kosten).
            pause_text = (
                "YouTube-Tageskontingent erschoepft: "
                f"{message} - Die Episode wartet und wird am naechsten Tag erneut versucht."
            )
            self._pause(record, pause_text)
            outcome.status = VideoStatus.PAUSED.value
            outcome.skipped = True
            outcome.message = pause_text
            outcome.warnings.append(pause_text)
            log.warning("%s", pause_text)
            return

        transient = bool(exc.retriable)
        if transient and retry_count <= self.settings.MAX_RETRIES:
            delay = min(
                float(self.settings.RETRY_MAX_DELAY),
                float(self.settings.RETRY_DELAY) * (float(self.settings.RETRY_BACKOFF_FACTOR) ** (retry_count - 1)),
            )
            log.warning(
                "Temporaerer Fehler (%s) - Versuch %d/%d, neuer Versuch in %.0fs",
                exc.reason or exc.status_code or type(exc).__name__,
                retry_count,
                self.settings.MAX_RETRIES + 1,
                delay,
            )
            self.repository.update(
                record.id,
                status=VideoStatus.READY.value,
                error_message=f"Temporaerer Fehler (Versuch {retry_count}): {message}",
                error_code=exc.reason or "transient",
                needs_attention=0,
                progress_percent=0,
            )
            self._move_files(record, self.settings.READY_FOLDER, folder_state="READY", required=False)
            outcome.status = VideoStatus.READY.value
            outcome.message = f"Temporaerer Fehler, erneuter Versuch geplant: {message}"
            outcome.warnings.append(message)
            self._sleep(min(delay, 1.0))
            return

        self._fail(record, message, code=exc.reason or "upload_failed", http_status=exc.status_code)
        outcome.status = VideoStatus.FAILED.value
        outcome.success = False
        outcome.message = f"Upload fehlgeschlagen: {message}"
        outcome.errors.append(message)

    def _fail(
        self,
        record: VideoRecord,
        message: str,
        *,
        code: str = "error",
        attempt_id: int | None = None,
        http_status: int | None = None,
    ) -> None:
        """Episode als FEHLER markieren und Dateien nach FAILED/ verschieben."""

        if attempt_id:
            self.repository.finish_attempt(
                attempt_id, status="FAILED", error=message, http_status=http_status
            )
        self.repository.update(
            record.id,
            status=VideoStatus.FAILED.value,
            error_message=message[:2000],
            error_code=code,
            needs_attention=1,
            progress_percent=0,
        )
        self.repository.add_log(
            "ERROR", f"FEHLER: {message}", episode_id=record.episode_id, action="FAILED"
        )
        episode_logger(self.logger, record.episode_id).error("FEHLER: %s", message)
        try:
            self._move_files(record, self.settings.FAILED_FOLDER, folder_state="FAILED", required=False)
        except FileOperationError as exc:
            episode_logger(self.logger, record.episode_id).warning(
                "Dateien konnten nicht nach FAILED verschoben werden: %s", exc
            )

    # ------------------------------------------------------------------
    # Veroeffentlichen (nur per Button)
    # ------------------------------------------------------------------

    def publish(self, video_id: int, *, confirm: bool = False, privacy_status: str = PRIVACY_PUBLIC) -> PipelineOutcome:
        """Ein privat hochgeladenes Video oeffentlich schalten."""

        started = time.monotonic()
        record = VideoRecord.from_row(self.repository.get(video_id))
        if record is None:
            raise StateError(f"Datensatz {video_id} nicht gefunden")
        log = episode_logger(self.logger, record.episode_id, action="PUBLISH")

        if self.dry_run:
            raise StateError(
                "Dry-Run aktiv: Veroeffentlichen ist abgeschaltet. Bitte die Anwendung "
                "ohne --dry-run starten."
            )

        if record.status == VideoStatus.PUBLISHED.value:
            raise StateError(
                f"Episode '{record.episode_id}' ist bereits veroeffentlicht "
                f"({record.youtube_url or record.youtube_video_id})"
            )
        if record.status not in (VideoStatus.UPLOADED_PRIVATE.value, VideoStatus.PUBLISH_READY.value):
            raise StateError(
                f"Veroeffentlichen ist nur im Status PRIVAT moeglich (aktuell: {record.status})"
            )
        if not record.youtube_video_id:
            raise StateError("Keine YouTube-Video-ID gespeichert - Veroeffentlichen nicht moeglich")
        if not confirm:
            raise StateError(
                "Veroeffentlichen erfordert eine ausdrueckliche Bestaetigung "
                "(Sicherheitsregel: keine automatische Veroeffentlichung)"
            )

        claimed = self.repository.claim(
            video_id,
            from_statuses=(VideoStatus.UPLOADED_PRIVATE.value, VideoStatus.PUBLISH_READY.value),
            to_status=VideoStatus.PUBLISHING.value,
        )
        if not claimed:
            raise StateError(
                "Veroeffentlichung laeuft bereits oder der Status hat sich geaendert - "
                "bitte das Dashboard aktualisieren"
            )

        metadata = EpisodeMetadata()
        stored = record.metadata or {}
        if stored:
            metadata = EpisodeMetadata(
                title=stored.get("title") or record.title or "",
                description=stored.get("description") or record.description or "",
                tags=list(record.tags or []),
                category_id=record.category_id or "",
                language=record.language or "",
                audio_language=stored.get("audio_language") or "",
                made_for_kids=stored.get("made_for_kids"),
                self_declared_made_for_kids=stored.get("self_declared_made_for_kids"),
                contains_synthetic_media=stored.get("contains_synthetic_media"),
                embeddable=stored.get("embeddable"),
                license=stored.get("license"),
                publish_at=stored.get("publish_at"),
            )
        else:
            metadata = EpisodeMetadata(
                title=record.title or "",
                description=record.description or "",
                tags=list(record.tags or []),
                category_id=record.category_id or "",
                language=record.language or "",
                self_declared_made_for_kids=bool(record.raw.get("self_declared_made_for_kids")),
            )

        adapter = self.adapter_for(record)
        job = PublicationJob(
            video_id=record.id,
            episode_id=record.episode_id,
            platform=record.platform or "youtube",
            video_path=str(record.video_path or ""),
            metadata=metadata,
            thumbnail_path=str(record.thumbnail_path) if record.thumbnail_path else None,
            platform_video_id=record.youtube_video_id,
            file_size=int(record.file_size or 0),
            file_hash=record.file_hash,
        )
        attempt_id = self.repository.start_attempt(record.id, Phase.PUBLISH.value)
        log.info("VEROEFFENTLICHEN gestartet (bestaetigt) fuer %s", record.youtube_video_id)
        outcome = PipelineOutcome(video_id=record.id, episode_id=record.episode_id)

        try:
            result = adapter.publish(job, confirm=True, privacy_status=privacy_status)
        except PlatformError as exc:
            message = redact(str(exc))
            self.repository.finish_attempt(
                attempt_id, status="FAILED", error=message, http_status=exc.status_code
            )
            self.repository.update(
                record.id,
                status=VideoStatus.UPLOADED_PRIVATE.value,
                error_message=f"Veroeffentlichen fehlgeschlagen: {message}"[:2000],
                error_code=exc.reason or "publish_failed",
                needs_attention=1,
            )
            log.error("Veroeffentlichen fehlgeschlagen: %s", message)
            outcome.status = VideoStatus.UPLOADED_PRIVATE.value
            outcome.message = message
            outcome.errors.append(message)
            outcome.elapsed_seconds = time.monotonic() - started
            return outcome
        except Exception as exc:  # noqa: BLE001
            message = redact(str(exc))
            self.repository.finish_attempt(attempt_id, status="FAILED", error=message)
            self.repository.update(
                record.id,
                status=VideoStatus.UPLOADED_PRIVATE.value,
                error_message=f"Veroeffentlichen fehlgeschlagen: {message}"[:2000],
                error_code=type(exc).__name__,
                needs_attention=1,
            )
            log.exception("Veroeffentlichen fehlgeschlagen: %s", message)
            outcome.status = VideoStatus.UPLOADED_PRIVATE.value
            outcome.message = message
            outcome.errors.append(message)
            outcome.elapsed_seconds = time.monotonic() - started
            return outcome

        self.repository.finish_attempt(
            attempt_id,
            status="SUCCEEDED",
            youtube_video_id=result.platform_video_id,
            quota_units=result.quota_units,
        )
        self.repository.update(
            record.id,
            status=VideoStatus.PUBLISHED.value,
            published_at=result.published_at or now_iso(),
            published_privacy_status=result.privacy_status,
            youtube_url=result.url or record.youtube_url,
            error_message=None,
            error_code=None,
            needs_attention=0,
        )
        log.info("VEROEFFENTLICHT: %s (%s)", result.url, result.privacy_status)
        self.repository.add_log(
            "INFO",
            f"Veroeffentlicht: {result.url}",
            episode_id=record.episode_id,
            action="PUBLISH",
        )

        if self.settings.ARCHIVE_AFTER_PUBLISH:
            try:
                self._move_files(record, self.settings.PUBLISHED_FOLDER, folder_state="PUBLISHED", required=False)
            except FileOperationError as exc:
                log.warning("Archivierung nach PUBLISHED fehlgeschlagen: %s", exc)

        outcome.status = VideoStatus.PUBLISHED.value
        outcome.success = True
        outcome.youtube_video_id = result.platform_video_id
        outcome.youtube_url = result.url
        outcome.message = f"Veroeffentlicht: {result.url}"
        outcome.steps.append("publish ok")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    # ------------------------------------------------------------------
    # Wiederanlauf / manuelle Aktionen
    # ------------------------------------------------------------------

    def retry(self, video_id: int) -> PipelineOutcome:
        """Fehlgeschlagene/unterbrochene Episode erneut in die Queue stellen."""

        record = VideoRecord.from_row(self.repository.get(video_id))
        if record is None:
            raise StateError(f"Datensatz {video_id} nicht gefunden")
        log = episode_logger(self.logger, record.episode_id)

        if record.status in {s.value for s in (
            VideoStatus.UPLOADED_PRIVATE,
            VideoStatus.PUBLISH_READY,
            VideoStatus.PUBLISHING,
            VideoStatus.PUBLISHED,
        )}:
            raise StateError(
                f"Episode ist bereits im Status {record.status} - erneuter Upload wurde aus "
                "Sicherheitsgruenden verweigert (Duplikatschutz)"
            )

        # Dateien zurueck nach READY holen (falls sie in FAILED/PROCESSING liegen)
        source = Path(record.video_path) if record.video_path else None
        if source and source.exists() and source.parent != Path(self.settings.READY_FOLDER):
            try:
                self._move_files(record, self.settings.READY_FOLDER, folder_state="READY", required=False)
                record = VideoRecord.from_row(self.repository.get(video_id)) or record
            except FileOperationError as exc:
                log.warning("Zurueckverschieben nach READY fehlgeschlagen: %s", exc)

        if not record.video_path or not Path(record.video_path).exists():
            raise StateError(
                "Videodatei liegt nicht mehr vor. Bitte die Dateien erneut in READY/ ablegen "
                f"(erwartet: {record.video_filename or record.episode_id + '.mp4'})"
            )

        self.repository.update(
            video_id,
            status=VideoStatus.READY.value,
            folder_state="READY",
            error_message=None,
            error_code=None,
            needs_attention=0,
            progress_percent=0,
            retry_count=int(record.retry_count or 0) + 1,
        )
        log.info("Erneut in die Warteschlange gestellt (Versuch %d)", int(record.retry_count or 0) + 2)
        self.repository.add_log(
            "INFO", "Erneuter Versuch angestossen", episode_id=record.episode_id, action="RETRY"
        )
        return PipelineOutcome(
            video_id=video_id,
            episode_id=record.episode_id,
            status=VideoStatus.READY.value,
            success=True,
            message="Episode wurde erneut in die Warteschlange gestellt",
        )

    def check_on_platform(self, video_id: int) -> PipelineOutcome:
        """Nach einem unterbrochenen Upload auf YouTube nachsehen (Crash-Recovery).

        Verwendet ausschliesslich offizielle API-Aufrufe (channels.list +
        playlistItems.list + videos.list = 3 Einheiten) und nur auf
        ausdruecklichen Klick.
        """

        record = VideoRecord.from_row(self.repository.get(video_id))
        if record is None:
            raise StateError(f"Datensatz {video_id} nicht gefunden")
        log = episode_logger(self.logger, record.episode_id, action="RECOVER")
        outcome = PipelineOutcome(video_id=video_id, episode_id=record.episode_id, status=record.status)

        if record.status != VideoStatus.INTERRUPTED.value:
            outcome.message = f"Pruefung nur im Status UNTERBROCHEN sinnvoll (aktuell: {record.status})"
            outcome.skipped = True
            return outcome

        adapter = self.adapter_for(record)
        attempt_id = self.repository.start_attempt(record.id, Phase.RECOVER.value)
        title = record.title or ""
        try:
            found = adapter.find_existing_upload(
                title=title,
                since=record.upload_started_at,
                duration_seconds=record.duration_seconds,
            )
        except PlatformError as exc:
            message = redact(str(exc))
            self.repository.finish_attempt(attempt_id, status="FAILED", error=message)
            outcome.message = f"Pruefung fehlgeschlagen: {message}"
            outcome.errors.append(message)
            log.error(outcome.message)
            return outcome
        except AuthError as exc:
            message = str(exc)
            self.repository.finish_attempt(attempt_id, status="FAILED", error=message)
            outcome.message = f"Keine Verbindung: {message}"
            outcome.errors.append(message)
            return outcome

        if not found:
            self.repository.finish_attempt(attempt_id, status="SUCCEEDED", error=None)
            outcome.message = (
                "Auf YouTube wurde kein passendes Video gefunden. Der Upload ist "
                "vermutlich wirklich abgebrochen - 'Erneut hochladen' waehlen."
            )
            log.info(outcome.message)
            return outcome

        self.repository.finish_attempt(attempt_id, status="SUCCEEDED", youtube_video_id=found)
        url = f"https://www.youtube.com/watch?v={found}"
        self.repository.update(
            record.id,
            youtube_video_id=found,
            youtube_url=url,
            status=VideoStatus.UPLOADED_PRIVATE.value,
            upload_completed_at=now_iso(),
            error_message=None,
            error_code=None,
            needs_attention=0,
            effective_privacy_status=ENFORCED_UPLOAD_PRIVACY,
        )
        log.info("Unterbrochener Upload wiedergefunden: %s -> %s", found, url)
        self.repository.add_log(
            "INFO", f"Upload wiedergefunden: {found}", episode_id=record.episode_id, action="RECOVER"
        )
        if self.settings.ARCHIVE_AFTER_UPLOAD:
            self._move_files(record, self.settings.UPLOADED_PRIVATE_FOLDER, folder_state="UPLOADED_PRIVATE", required=False)
        outcome.status = VideoStatus.UPLOADED_PRIVATE.value
        outcome.success = True
        outcome.youtube_video_id = found
        outcome.youtube_url = url
        outcome.message = f"Upload wiedergefunden und als privat uebernommen: {url}"
        return outcome

    def archive(self, video_id: int, *, target: str | None = None) -> PipelineOutcome:
        """Dateien manuell in den zum Status passenden Archiv-Ordner verschieben."""

        record = VideoRecord.from_row(self.repository.get(video_id))
        if record is None:
            raise StateError(f"Datensatz {video_id} nicht gefunden")

        folders = {
            "READY": self.settings.READY_FOLDER,
            "PROCESSING": self.settings.PROCESSING_FOLDER,
            "UPLOADED_PRIVATE": self.settings.UPLOADED_PRIVATE_FOLDER,
            "PUBLISHED": self.settings.PUBLISHED_FOLDER,
            "FAILED": self.settings.FAILED_FOLDER,
            "SKIPPED": self.settings.SKIPPED_FOLDER,
        }
        key = (target or "").upper()
        if key and key not in folders:
            raise StateError(f"Unbekannter Zielordner '{target}' (moeglich: {', '.join(folders)})")
        if not key:
            key = {
                VideoStatus.PUBLISHED.value: "PUBLISHED",
                VideoStatus.UPLOADED_PRIVATE.value: "UPLOADED_PRIVATE",
                VideoStatus.PUBLISH_READY.value: "UPLOADED_PRIVATE",
                VideoStatus.FAILED.value: "FAILED",
                VideoStatus.INTERRUPTED.value: "FAILED",
                VideoStatus.SKIPPED_DUPLICATE.value: "SKIPPED",
            }.get(record.status, "READY")

        moved = self._move_files(record, folders[key], folder_state=key)
        message = (
            f"{len(moved)} Datei(en) nach {key}/ verschoben"
            if moved
            else f"Keine Dateien zum Verschieben gefunden (Ziel: {key}/)"
        )
        episode_logger(self.logger, record.episode_id).info(message)
        return PipelineOutcome(
            video_id=record.id,
            episode_id=record.episode_id,
            status=record.status,
            success=bool(moved),
            message=message,
        )

    # ------------------------------------------------------------------
    # Queue-Helfer
    # ------------------------------------------------------------------

    def queue_all_ready(self) -> int:
        """Alle NEW/PAUSED-Datensaetze in die Queue stellen (READY)."""

        count = 0
        for row in self.repository.list_videos(statuses=[VideoStatus.NEW.value]):
            self.repository.update(int(row["id"]), status=VideoStatus.READY.value, needs_attention=0)
            count += 1
        return count

    def queued_count(self) -> int:
        rows = self.repository.list_videos(
            statuses=[VideoStatus.NEW.value, VideoStatus.READY.value], limit=1000
        )
        return len(rows)

    def duration_text(self, record: VideoRecord) -> str:
        return format_duration(record.duration_seconds)


class _PipelineAbort(Exception):
    """Interner Kontrollfluss: Schritt hat den Lauf sauber beendet."""

    def __init__(
        self,
        status: str,
        *,
        message: str = "",
        error: str | None = None,
        skipped: bool = False,
        success: bool = False,
        warnings: list[str] | None = None,
    ) -> None:
        super().__init__(message or status)
        self.status = status
        self.message = message
        self.error = error
        self.skipped = skipped
        self.success = success
        self.warnings = list(warnings or [])


__all__ = ["PipelineOutcome", "UploadPipeline"]
