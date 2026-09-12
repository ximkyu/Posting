"""Hintergrund-Ablauf: Watch-Folder ueberwachen + Upload-Queue abarbeiten.

Beide Teile laufen als Daemon-Threads:

* :class:`FolderWatcher` - erkennt neue, stabile Dateien in READY/
* :class:`UploadWorker`  - verarbeitet die Queue **strikt seriell**
  (Standard: 1 Upload gleichzeitig)

Die Queue lebt in der Datenbank (Spalte ``status``), nicht im Speicher.
Dadurch ist sie nach einem Neustart unveraendert gueltig.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..constants import VideoStatus
from ..utils.files import now_iso
from ..utils.logging_utils import get_logger, redact

try:  # watchdog ist optional - Polling funktioniert immer
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except Exception:  # pragma: no cover - abhaengig von der Installation
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]
    WATCHDOG_AVAILABLE = False


@dataclass
class WorkerState:
    running: bool = False
    paused: bool = False
    current_episode: str | None = None
    current_video_id: int | None = None
    processed_total: int = 0
    success_total: int = 0
    failed_total: int = 0
    last_run_at: str | None = None
    last_message: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "paused": self.paused,
            "current_episode": self.current_episode,
            "current_video_id": self.current_video_id,
            "processed_total": self.processed_total,
            "success_total": self.success_total,
            "failed_total": self.failed_total,
            "last_run_at": self.last_run_at,
            "last_message": self.last_message,
            "history": list(self.history[-10:]),
        }


class UploadWorker:
    """Abarbeitung der Upload-Queue (seriell)."""

    def __init__(self, ctx: Any, *, logger: logging.Logger | None = None) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.repository = ctx.repository
        self.pipeline = ctx.pipeline
        self.logger = logger or get_logger("worker")
        self.state = WorkerState()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._manual_lock = threading.Lock()
        self._manual_running = False

    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        if self.repository.get_bool_setting("worker.paused", False):
            return True
        return not bool(self.settings.AUTO_UPLOAD) and not self.repository.get_bool_setting(
            "worker.manual_run", False
        )

    def set_paused(self, paused: bool) -> None:
        self.repository.set_setting("worker.paused", "1" if paused else "0")
        self.state.paused = paused
        self.logger.info("Warteschlange %s", "pausiert" if paused else "fortgesetzt")
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.state.running = True
        self._thread = threading.Thread(target=self._loop, name="upload-worker", daemon=True)
        self._thread.start()
        self.logger.info(
            "Upload-Worker gestartet (max. %d gleichzeitige Uploads, Auto-Upload: %s)",
            self.settings.MAX_CONCURRENT_UPLOADS,
            "an" if self.settings.AUTO_UPLOAD else "aus (nur manuell)",
        )

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        self.pipeline.request_stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.state.running = False
        self.logger.info("Upload-Worker gestoppt")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.is_paused:
                    self.state.paused = True
                    self._wake.wait(timeout=float(self.settings.WORKER_POLL_SECONDS))
                    self._wake.clear()
                    continue

                self.state.paused = False
                processed = self.process_next()
                if processed is None:
                    self._wake.wait(timeout=float(self.settings.WORKER_POLL_SECONDS))
                    self._wake.clear()
            except Exception as exc:  # noqa: BLE001 - Worker darf nie sterben
                message = redact(str(exc))
                self.logger.exception("Unerwarteter Fehler im Upload-Worker: %s", message)
                self.state.last_message = f"Fehler: {message}"
                self._wake.wait(timeout=5.0)
                self._wake.clear()
        self.state.running = False

    # ------------------------------------------------------------------

    def process_next(self) -> Any | None:
        """Ein einzelnes Queue-Element verarbeiten (auch manuell nutzbar)."""

        if bool(getattr(self.ctx, "dry_run", False)):
            self.logger.info("Dry-Run aktiv: Upload-Queue bleibt stehen")
            return None

        if self.is_paused:
            # Gilt auch fuer "UPLOAD AUSFUEHREN": Wenn der Nutzer pausiert hat,
            # darf nichts hochgeladen werden.
            self.state.paused = True
            self.logger.info("Uploads sind pausiert - es wird nichts hochgeladen")
            return None

        row = self.repository.next_queued((VideoStatus.NEW, VideoStatus.READY))
        if row is None:
            return None
        video_id = int(row["id"])
        episode = str(row.get("episode_id"))
        self.state.current_episode = episode
        self.state.current_video_id = video_id
        self.repository.set_setting("worker.current_episode", episode)
        try:
            outcome = self.pipeline.process(video_id)
        finally:
            self.state.current_episode = None
            self.state.current_video_id = None
            self.repository.set_setting("worker.current_episode", "")

        self.state.processed_total += 1
        if outcome.success:
            self.state.success_total += 1
        elif not outcome.skipped:
            self.state.failed_total += 1
        self.state.last_run_at = now_iso()
        self.state.last_message = outcome.message
        self.state.history.append(
            {
                "episode_id": outcome.episode_id,
                "status": outcome.status,
                "success": outcome.success,
                "message": outcome.message,
                "at": self.state.last_run_at,
            }
        )
        self.state.history = self.state.history[-50:]
        self.repository.set_setting("worker.last_run_at", self.state.last_run_at)
        self.repository.set_setting("worker.last_message", outcome.message[:500])
        return outcome

    def process_all(self, *, max_items: int | None = None, scan_first: bool = True) -> list[Any]:
        """Queue komplett abarbeiten (fuer 'UPLOAD AUSFUEHREN' und die CLI)."""

        outcomes: list[Any] = []
        if bool(getattr(self.ctx, "dry_run", False)):
            self.logger.info("Dry-Run aktiv: Es wird nichts hochgeladen")
            return outcomes
        if scan_first:
            try:
                self.ctx.scanner.scan_and_intake()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Scan vor dem Upload fehlgeschlagen: %s", redact(str(exc)))

        self.repository.set_setting("worker.manual_run", "1")
        self._manual_running = True
        try:
            while True:
                if max_items is not None and len(outcomes) >= max_items:
                    break
                outcome = self.process_next()
                if outcome is None:
                    break
                outcomes.append(outcome)
                if self._stop.is_set():
                    break
        finally:
            self.repository.set_setting("worker.manual_run", "0")
            self._manual_running = False
        return outcomes

    def process_all_async(self) -> bool:
        """'UPLOAD AUSFUEHREN' aus der Oberflaeche: einmaligen Lauf anstossen.

        Liefert ``False``, wenn bereits ein manueller Lauf aktiv ist
        (verhindert Doppel-Verarbeitung durch mehrfaches Klicken).
        """

        with self._manual_lock:
            if self._manual_running:
                return False
            self._manual_running = True
        thread = threading.Thread(
            target=self._safe_process_all, name="upload-manual", daemon=True
        )
        thread.start()
        return True

    def _safe_process_all(self) -> None:
        try:
            self.process_all()
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Manueller Upload-Lauf fehlgeschlagen: %s", redact(str(exc)))
        finally:
            with self._manual_lock:
                self._manual_running = False


class _ReadyFolderHandler(FileSystemEventHandler):  # type: ignore[misc,valid-type]
    """Meldet Dateisystem-Events an den Watcher (sofort statt erst beim Polling)."""

    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self.callback = callback

    def on_created(self, event: Any) -> None:
        self._notify(event)

    def on_modified(self, event: Any) -> None:
        self._notify(event)

    def on_moved(self, event: Any) -> None:
        self._notify(event)

    def _notify(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return
        try:
            self.callback()
        except Exception:  # noqa: BLE001 - Events duerfen nie crashen
            pass


class FolderWatcher:
    """Ueberwacht READY/ und stoesst Scan + Queue an."""

    def __init__(self, ctx: Any, *, logger: logging.Logger | None = None) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.scanner = ctx.scanner
        self.worker: UploadWorker | None = getattr(ctx, "worker", None)
        self.logger = logger or get_logger("watcher")
        self._thread: threading.Thread | None = None
        self._observer: Any | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.running = False
        self.last_scan_at: str | None = None
        self.last_report: dict[str, Any] = {}
        self.scan_count = 0

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        folder = Path(self.settings.READY_FOLDER)
        folder.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self.running = True
        self._start_observer(folder)
        self._thread = threading.Thread(target=self._loop, name="folder-watcher", daemon=True)
        self._thread.start()
        self.logger.info(
            "Watch-Folder aktiv: %s (Intervall %ss, Stabilitaet %ss, watchdog: %s)",
            folder,
            self.settings.SCAN_INTERVAL_SECONDS,
            self.settings.FILE_STABILITY_SECONDS,
            "ja" if self._observer else "nein (Polling)",
        )

    def _start_observer(self, folder: Path) -> None:
        if not (self.settings.USE_WATCHDOG and WATCHDOG_AVAILABLE):
            return
        try:
            observer = Observer()
            observer.schedule(_ReadyFolderHandler(self.notify), str(folder), recursive=bool(self.settings.SCAN_RECURSIVE))
            observer.daemon = True
            observer.start()
            self._observer = observer
        except Exception as exc:  # noqa: BLE001 - Polling reicht ebenfalls
            self.logger.warning("watchdog konnte nicht gestartet werden (%s) - nutze Polling", redact(str(exc)))
            self._observer = None

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.running = False
        self.logger.info("Watch-Folder deaktiviert")

    def notify(self) -> None:
        """Sofort einen Scan anstossen (Dateisystem-Event oder Button)."""

        self._wake.set()

    # ------------------------------------------------------------------

    def _loop(self) -> None:
        # Erster Scan direkt nach dem Start (Wiederanlauf nach Neustart)
        self.scan_now()
        while not self._stop.is_set():
            triggered = self._wake.wait(timeout=float(self.settings.SCAN_INTERVAL_SECONDS))
            self._wake.clear()
            if self._stop.is_set():
                break
            if triggered:
                # Kurz warten, damit mehrere Kopier-Events gebuendelt werden
                time.sleep(0.5)
            self.scan_now()

    def scan_now(self) -> dict[str, Any]:
        """Ein Scan-Durchlauf inkl. Aufnahme in die Datenbank."""

        try:
            result, report = self.scanner.scan_and_intake()
        except Exception as exc:  # noqa: BLE001 - ein Scan-Fehler stoppt nichts
            message = redact(str(exc))
            self.logger.exception("Scan fehlgeschlagen: %s", message)
            self.last_report = {"error": message, "scanned_at": now_iso()}
            return self.last_report

        self.scan_count += 1
        self.last_scan_at = result.scanned_at
        self.last_report = {
            **report.to_dict(),
            "found": len(result.candidates),
            "ready": len(result.ready),
            "unstable": [
                {"episode_id": c.episode_id, "reason": c.stability_reason} for c in result.unstable
            ],
            "errors": list(result.errors),
            "scanned_at": result.scanned_at,
        }
        if report.created:
            self.logger.info("Neue Episoden erkannt: %s", ", ".join(report.created))
        if report.failed:
            self.logger.warning("Episoden mit Fehlern: %s", ", ".join(report.failed))
        if report.blocked:
            self.logger.info("Bereits verarbeitet (uebersprungen): %s", ", ".join(report.blocked))

        if self.worker is not None and (report.total_new or report.waiting):
            self.worker.wake()
        return self.last_report

    def state(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "watchdog": self._observer is not None,
            "watchdog_available": WATCHDOG_AVAILABLE,
            "folder": str(self.settings.READY_FOLDER),
            "scan_interval_seconds": self.settings.SCAN_INTERVAL_SECONDS,
            "file_stability_seconds": self.settings.FILE_STABILITY_SECONDS,
            "last_scan_at": self.last_scan_at,
            "scan_count": self.scan_count,
            "last_report": self.last_report,
        }


__all__ = ["FolderWatcher", "UploadWorker", "WorkerState", "WATCHDOG_AVAILABLE"]
