"""Anwendungs-Kontext: verbindet Konfiguration, Datenbank, Auth, Plattform,
Scanner, Pipeline und Hintergrund-Threads (Dependency Injection).

``bootstrap()`` ist der einzige Einstiegspunkt, den CLI, Web-UI und Tests
benutzen - dadurch gibt es genau einen Ort, an dem die Reihenfolge der
Initialisierung definiert ist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..auth.youtube_auth import AuthStatus, YouTubeAuthManager
from ..config import Settings, load_settings
from ..constants import APP_NAME, __version__
from ..database.db import Database
from ..database.repository import VideoRepository
from ..platforms.base import PlatformAdapter
from ..platforms.registry import PlatformRegistry, build_registry
from ..scanner.folder_scanner import FolderScanner
from ..scheduler.watcher import FolderWatcher, UploadWorker
from ..utils.files import now_iso
from ..utils.logging_utils import get_logger, setup_logging
from ..utils.media import probe_available
from ..utils.secure_store import SecretStore, ensure_gitignore
from .pipeline import UploadPipeline


@dataclass
class AppContext:
    """Alles, was die Anwendung zum Arbeiten braucht."""

    settings: Settings
    db: Database
    repository: VideoRepository
    logger: logging.Logger
    store: SecretStore
    auth: YouTubeAuthManager
    registry: PlatformRegistry
    scanner: FolderScanner
    pipeline: UploadPipeline
    worker: UploadWorker | None = None
    watcher: FolderWatcher | None = None
    dry_run: bool = False
    started_at: str = field(default_factory=now_iso)
    version: str = __version__

    # ------------------------------------------------------------------

    def platform(self, name: str | None = None) -> PlatformAdapter:
        return self.registry.get(name)

    @property
    def youtube(self) -> PlatformAdapter:
        return self.registry.get("youtube")

    def auth_status(self) -> AuthStatus:
        return self.auth.status()

    def start_background(self) -> None:
        """Watcher + Worker starten (nicht im Dry-Run)."""

        if self.dry_run:
            self.logger.info("Dry-Run: Hintergrund-Dienste bleiben deaktiviert")
            return
        if self.worker is None:
            self.worker = UploadWorker(self)
        if self.watcher is None:
            self.watcher = FolderWatcher(self)
            # Der Watcher braucht den Worker zum Anstossen der Queue
            self.watcher.worker = self.worker
        self.worker.start()
        if self.settings.WATCH_ENABLED:
            self.watcher.start()
        else:
            self.logger.info("Watch-Folder ist deaktiviert (WATCH_ENABLED=false)")

    def stop_background(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
        if self.worker is not None:
            self.worker.stop()

    def trigger_scan(self) -> dict[str, Any]:
        """Manueller Scan ('JETZT SCANNEN')."""

        if self.watcher is not None:
            report = self.watcher.scan_now()
            if self.worker is not None:
                self.worker.wake()
            return report
        result, report = self.scanner.scan_and_intake()
        return {**report.to_dict(), "found": len(result.candidates), "ready": len(result.ready)}

    def to_dict(self) -> dict[str, Any]:
        """Kompakter Status fuer ``/api/status`` (ohne YouTube-API-Aufrufe)."""

        auth = self.auth.status()
        probe_ok, probe_source = probe_available(self.settings)
        return {
            "app": APP_NAME,
            "version": self.version,
            "started_at": self.started_at,
            "dry_run": self.dry_run,
            "config": self.settings.describe(),
            "database": {
                "path": str(self.settings.DATABASE_PATH),
                "counts": self.db.table_counts(),
            },
            "auth": auth.to_dict(),
            "platforms": [info.to_dict() for info in self.registry.infos()],
            "media_probe": {"available": probe_ok, "source": probe_source},
            "stats": self.repository.stats(),
            "worker": self.worker.state.to_dict() if self.worker else {"running": False},
            "watcher": self.watcher.state() if self.watcher else {"running": False},
            "paused": bool(self.repository.get_bool_setting("worker.paused", False)),
            "auto_upload": bool(self.settings.AUTO_UPLOAD),
        }


def bootstrap(
    settings: Settings | None = None,
    *,
    dry_run: bool = False,
    console_logging: bool = True,
    initialize_database: bool = True,
    run_recovery: bool = True,
) -> AppContext:
    """Anwendung initialisieren (Ordner, DB, Logging, Auth, Plattform, Pipeline)."""

    cfg = settings or load_settings()
    created = cfg.ensure_directories()
    ensure_gitignore(cfg.CREDENTIALS_DIR)

    db = Database(cfg.DATABASE_PATH)
    if initialize_database:
        db.initialize()
    repository = VideoRepository(db)

    logger = setup_logging(cfg, repository=repository, console=console_logging, force=True)
    if created:
        logger.info("Ordner angelegt: %s", ", ".join(str(p) for p in created))
    logger.info(
        "%s v%s bereit (Datenbank: %s, Dashboard: %s)",
        APP_NAME,
        __version__,
        Path(cfg.DATABASE_PATH).name,
        cfg.dashboard_url,
    )

    store = SecretStore(cfg.CREDENTIALS_DIR, mode=cfg.TOKEN_STORAGE)
    auth = YouTubeAuthManager(cfg, store=store, repository=repository, logger=get_logger("auth"))
    registry = build_registry(settings=cfg, auth=auth, repository=repository, logger=get_logger("platforms"))
    scanner = FolderScanner(cfg, repository, logger=get_logger("scanner"))
    pipeline = UploadPipeline(
        settings=cfg,
        repository=repository,
        registry=registry,
        auth=auth,
        logger=get_logger("pipeline"),
        dry_run=dry_run,
    )

    ctx = AppContext(
        settings=cfg,
        db=db,
        repository=repository,
        logger=logger,
        store=store,
        auth=auth,
        registry=registry,
        scanner=scanner,
        pipeline=pipeline,
        dry_run=dry_run,
    )

    if run_recovery and not dry_run:
        from .recovery import startup_recovery

        report = startup_recovery(ctx)
        if report.has_findings:
            repository.set_setting("recovery.last_findings", str(report.to_dict()))

    repository.set_setting("app.version", __version__)
    repository.set_setting("app.started_at", ctx.started_at)
    return ctx


__all__ = ["AppContext", "bootstrap"]
