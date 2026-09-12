"""Wiederanlauf nach einem Neustart/Absturz.

Ziel (Anforderung 12/23): Nach einem Neustart macht das System genau dort
weiter, wo es aufgehoert hat - ohne dass ein Video zweimal hochgeladen wird.

* ``UPLOADING``  -> ``INTERRUPTED``  (bewusst KEIN automatischer Re-Upload;
  der Benutzer waehlt "Auf YouTube pruefen" oder "Erneut hochladen")
* ``PUBLISHING`` -> ``UPLOADED_PRIVATE`` (Veroeffentlichen ist idempotent und
  kann gefahrlos erneut ausgeloest werden)
* ``READY``/``NEW``/``PAUSED`` bleiben in der Queue und werden weiter
  abgearbeitet
* Datensaetze, deren Dateien verschwunden sind, werden klar markiert
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import VideoStatus
from ..utils.files import now_iso


@dataclass
class RecoveryReport:
    interrupted: list[str] = field(default_factory=list)
    republished_pending: list[str] = field(default_factory=list)
    requeued: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    ran_at: str = field(default_factory=now_iso)

    @property
    def has_findings(self) -> bool:
        return bool(
            self.interrupted
            or self.missing_files
            or self.orphans
            or self.republished_pending
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "interrupted": list(self.interrupted),
            "republished_pending": list(self.republished_pending),
            "requeued": list(self.requeued),
            "missing_files": list(self.missing_files),
            "orphans": list(self.orphans),
            "messages": list(self.messages),
            "ran_at": self.ran_at,
        }


def startup_recovery(ctx: Any, *, logger: logging.Logger | None = None) -> RecoveryReport:
    """Beim Start der Anwendung einmal ausfuehren."""

    log = logger or ctx.logger
    repo = ctx.repository
    settings = ctx.settings
    report = RecoveryReport()

    if not getattr(settings, "MARK_INTERRUPTED_ON_STARTUP", True):
        report.messages.append("Wiederanlauf-Pruefung ist deaktiviert (MARK_INTERRUPTED_ON_STARTUP=false)")
        return report

    # 1. Laufende Vorgaenge aus der vorherigen Sitzung bereinigen
    affected = repo.reset_running_to_interrupted()
    for row in affected:
        episode = str(row.get("episode_id"))
        if row.get("new_status") == VideoStatus.INTERRUPTED.value:
            report.interrupted.append(episode)
        else:
            report.republished_pending.append(episode)
    if report.interrupted:
        message = (
            f"{len(report.interrupted)} Upload(s) wurden beim letzten Lauf unterbrochen: "
            f"{', '.join(report.interrupted)}. Kein automatischer Re-Upload - bitte im "
            "Dashboard 'Auf YouTube pruefen' oder 'Erneut hochladen' waehlen."
        )
        log.warning(message)
        report.messages.append(message)
        repo.add_log("WARNING", message, action="RECOVERY")
    if report.republished_pending:
        message = (
            f"{len(report.republished_pending)} Veroeffentlichung(en) wurden unterbrochen und "
            f"zurueckgesetzt: {', '.join(report.republished_pending)}"
        )
        log.warning(message)
        report.messages.append(message)

    # 2. Queue-Datensaetze pruefen: liegen die Dateien noch?
    queue_statuses = [
        VideoStatus.NEW.value,
        VideoStatus.READY.value,
        VideoStatus.PAUSED.value,
        VideoStatus.VALIDATING.value,
    ]
    for row in repo.list_videos(statuses=queue_statuses):
        video_path = row.get("video_path")
        episode = str(row.get("episode_id"))
        if not video_path:
            continue
        if not Path(video_path).exists():
            repo.update(
                int(row["id"]),
                status=VideoStatus.FAILED.value,
                error_message=(
                    f"Videodatei nicht mehr vorhanden: {video_path}. "
                    "Bitte die Dateien erneut in READY/ ablegen."
                ),
                error_code="file_missing",
                needs_attention=1,
            )
            report.missing_files.append(episode)
            log.warning("[%s] Videodatei fehlt - als FEHLER markiert", episode)
        else:
            # VALIDATING darf nach einem Absturz nicht haengen bleiben
            if row.get("status") == VideoStatus.VALIDATING.value:
                repo.update(int(row["id"]), status=VideoStatus.READY.value, needs_attention=0)
                report.requeued.append(episode)
            elif row.get("status") in (VideoStatus.NEW.value, VideoStatus.READY.value):
                report.requeued.append(episode)

    # 3. Verwaiste Dateien im PROCESSING-Ordner melden (ohne sie zu loeschen)
    processing = Path(settings.PROCESSING_FOLDER)
    if processing.exists():
        known_paths: set[str] = set()
        for row in repo.list_videos(limit=5000):
            for spalte in ("video_path", "metadata_path", "thumbnail_path"):
                wert = row.get(spalte)
                if wert:
                    known_paths.add(str(wert))
        for candidate in sorted(processing.iterdir()):
            if not candidate.is_file():
                continue
            if candidate.name.startswith("."):
                continue
            if str(candidate) not in known_paths:
                report.orphans.append(str(candidate))
        if report.orphans:
            message = (
                f"{len(report.orphans)} Datei(en) liegen ohne Zuordnung in PROCESSING/: "
                f"{', '.join(Path(p).name for p in report.orphans[:5])}. "
                "Sie werden nicht geloescht - bitte bei Bedarf manuell nach READY/ verschieben."
            )
            log.warning(message)
            report.messages.append(message)
            repo.add_log("WARNING", message, action="RECOVERY")

    repo.set_setting("recovery.last_run_at", report.ran_at)
    return report


__all__ = ["RecoveryReport", "startup_recovery"]
