"""Logging: Datei (rotierend), Konsole und Datenbank - mit Secret-Redaktion.

Beispielzeilen, wie sie in ``data/logs/app.log`` landen::

    2026-09-12 14:03:22 INFO  [video_001] Found video episode video_001
    2026-09-12 14:03:25 INFO  [video_001] Validation passed
    2026-09-12 14:03:26 INFO  [video_001] Upload started
    2026-09-12 14:14:09 INFO  [video_001] Upload completed
    2026-09-12 14:14:10 INFO  [video_001] YouTube ID: abc123
    2026-09-12 14:14:10 INFO  [video_001] Status: PRIVATE
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

LOGGER_NAME = "youtube_autoposter"

#: Muster, die niemals in eine Logdatei gelangen duerfen.
REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"ya29\.[A-Za-z0-9_\-\.]+"), "[REDACTED_ACCESS_TOKEN]"),
    (re.compile(r"\b1//?[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(refresh_token\"?\s*[:=]\s*\"?)([^\",}\s]+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(access_token\"?\s*[:=]\s*\"?)([^\",}\s]+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(client_secret\"?\s*[:=]\s*\"?)([^\",}\s]+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(id_token\"?\s*[:=]\s*\"?)([^\",}\s]+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s]+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password\"?\s*[:=]\s*\"?)([^\",}\s]+)"), r"\1[REDACTED]"),
    (re.compile(r"GOCSPX-[A-Za-z0-9_\-]+"), "[REDACTED_CLIENT_SECRET]"),
)


def redact(text: str) -> str:
    """Token/Secrets in einem String unkenntlich machen."""

    if not text:
        return text
    result = str(text)
    for pattern, replacement in REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        key: redact(value) if isinstance(value, str) else value
                        for key, value in record.args.items()
                    }
                else:
                    record.args = tuple(
                        redact(value) if isinstance(value, str) else value for value in record.args
                    )
        except Exception:  # noqa: BLE001 - Logging darf nie crashen
            pass
        return True


class EpisodeLogRecord:
    """Marker-Interface: Records koennen ``episode_id``/``action`` tragen."""


class DatabaseLogHandler(logging.Handler):
    """Schreibt Logzeilen zusaetzlich in die SQLite-Tabelle ``logs``."""

    def __init__(self, repository: Any, *, min_level: int = logging.INFO) -> None:
        super().__init__(level=min_level)
        self.repository = repository

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.repository.add_log(
                level=record.levelname,
                message=self.format(record) if record.exc_info is None else f"{record.getMessage()} | {record.exc_text or ''}",
                episode_id=getattr(record, "episode_id", None),
                action=getattr(record, "action", None),
            )
        except Exception:  # noqa: BLE001 - Logging darf nie crashen
            pass


class EpisodeLoggerAdapter(logging.LoggerAdapter):
    """Logger, der Episode-Kontext an jede Zeile anhaengt."""

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        extra = dict(kwargs.get("extra") or {})
        extra.setdefault("episode_id", self.extra.get("episode_id") if self.extra else None)
        extra.setdefault("action", self.extra.get("action") if self.extra else None)
        kwargs["extra"] = extra
        prefix = f"[{extra['episode_id']}] " if extra.get("episode_id") else ""
        return f"{prefix}{msg}", kwargs


class _EpisodeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        episode = getattr(record, "episode_id", None)
        base = super().format(record)
        if episode and f"[{episode}]" not in base:
            return f"{base} [episode={episode}]"
        return base


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def episode_logger(logger: logging.Logger | None = None, episode_id: str | None = None, action: str | None = None) -> EpisodeLoggerAdapter:
    base = logger or get_logger()
    return EpisodeLoggerAdapter(base, {"episode_id": episode_id, "action": action})


def setup_logging(
    settings: Any,
    *,
    repository: Any = None,
    console: bool = True,
    force: bool = False,
) -> logging.Logger:
    """Logger mit Datei-, Konsolen- und (optional) Datenbank-Handler einrichten."""

    logger = get_logger()
    logger.setLevel(getattr(logging, str(settings.LOG_LEVEL).upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers and not force:
        return logger
    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass

    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / settings.LOG_FILE

    formatter = _EpisodeFormatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    redactor = RedactingFilter()

    try:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=int(settings.LOG_MAX_BYTES),
            backupCount=int(settings.LOG_BACKUP_COUNT),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)
    except OSError:
        # Ohne Schreibrechte auf dem Log-Ordner bleibt die Konsole aktiv
        pass

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        logger.addHandler(stream)

    if repository is not None and getattr(settings, "LOG_TO_DATABASE", True):
        db_handler = DatabaseLogHandler(repository)
        db_handler.setFormatter(formatter)
        db_handler.addFilter(redactor)
        logger.addHandler(db_handler)

    return logger


def recent_log_lines(path: str | Path, lines: int = 200) -> list[str]:
    """Letzte Zeilen der Logdatei (fuer die Oberflaeche/CLI)."""

    file = Path(path)
    if not file.exists():
        return []
    try:
        with file.open("r", encoding="utf-8", errors="replace") as handle:
            content: Iterable[str] = handle.readlines()
        return [line.rstrip("\n") for line in list(content)[-max(1, lines) :]]
    except OSError:
        return []
