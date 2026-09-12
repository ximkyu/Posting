"""SQLite-Zugriffsschicht: Schema, Verbindungen, Migrationen.

Design-Entscheidungen:

* **WAL-Modus** und ``foreign_keys=ON`` - damit Lesezugriffe der Web-UI nicht
  blockieren, waehrend der Upload-Thread schreibt.
* **Verbindung pro Vorgang** + ``threading.RLock``: SQLite-Verbindungen sind
  nicht thread-safe; kurze, gekapselte Transaktionen sind hier robuster als
  eine geteilte Verbindung.
* **Additive Migration**: fehlende Spalten werden automatisch ergaenzt, damit
  eine bestehende Datenbank nach einem Update weiter funktioniert.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..utils.files import now_iso

SCHEMA_VERSION = 3

#: Spalten der ``videos``-Tabelle (Name, SQLite-Typ, Default-Ausdruck)
VIDEO_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT", ""),
    ("episode_id", "TEXT NOT NULL UNIQUE", ""),
    ("platform", "TEXT NOT NULL DEFAULT 'youtube'", ""),
    # --- Dateien -------------------------------------------------------
    ("video_filename", "TEXT", ""),
    ("video_path", "TEXT", ""),
    ("metadata_filename", "TEXT", ""),
    ("metadata_path", "TEXT", ""),
    ("thumbnail_filename", "TEXT", ""),
    ("thumbnail_path", "TEXT", ""),
    ("file_size", "INTEGER DEFAULT 0", ""),
    ("file_hash", "TEXT", ""),
    ("folder_state", "TEXT", ""),          # READY|PROCESSING|UPLOADED_PRIVATE|PUBLISHED|FAILED|SKIPPED
    ("archived_path", "TEXT", ""),
    # --- Metadaten ------------------------------------------------------
    ("title", "TEXT", ""),
    ("description", "TEXT", ""),
    ("tags_json", "TEXT", ""),
    ("category_id", "TEXT", ""),
    ("language", "TEXT", ""),
    ("audio_language", "TEXT", ""),
    ("playlist_id", "TEXT", ""),
    ("channel_identifier", "TEXT", ""),
    ("made_for_kids", "INTEGER", ""),
    ("self_declared_made_for_kids", "INTEGER", ""),
    ("contains_synthetic_media", "INTEGER", ""),
    ("publish_at_requested", "TEXT", ""),
    ("privacy_status_requested", "TEXT", ""),
    ("effective_privacy_status", "TEXT DEFAULT 'private'", ""),
    ("metadata_json", "TEXT", ""),
    # --- technische Pruefung -------------------------------------------
    ("duration_seconds", "REAL", ""),
    ("width", "INTEGER", ""),
    ("height", "INTEGER", ""),
    ("video_codec", "TEXT", ""),
    ("audio_codec", "TEXT", ""),
    ("fps", "REAL", ""),
    ("has_audio", "INTEGER", ""),
    ("media_info_json", "TEXT", ""),
    ("validation_json", "TEXT", ""),
    ("thumbnail_info_json", "TEXT", ""),
    # --- Status / Ergebnis ---------------------------------------------
    ("status", "TEXT NOT NULL DEFAULT 'NEW'", ""),
    ("youtube_video_id", "TEXT", ""),
    ("youtube_url", "TEXT", ""),
    ("thumbnail_url", "TEXT", ""),
    ("thumbnail_set", "INTEGER DEFAULT 0", ""),
    ("thumbnail_error", "TEXT", ""),
    ("channel_id", "TEXT", ""),
    ("channel_title", "TEXT", ""),
    ("upload_started_at", "TEXT", ""),
    ("upload_completed_at", "TEXT", ""),
    ("published_at", "TEXT", ""),
    ("published_privacy_status", "TEXT", ""),
    ("progress_percent", "INTEGER DEFAULT 0", ""),
    ("retry_count", "INTEGER DEFAULT 0", ""),
    ("error_message", "TEXT", ""),
    ("error_code", "TEXT", ""),
    ("needs_attention", "INTEGER DEFAULT 0", ""),
    ("publish_locked_at", "TEXT", ""),
    ("created_at", "TEXT NOT NULL", ""),
    ("updated_at", "TEXT NOT NULL", ""),
)

#: Reihenfolge fuer die Anzeige (Dashboard)
STATUS_ORDER = (
    "UPLOADING",
    "VALIDATING",
    "PUBLISHING",
    "INTERRUPTED",
    "FAILED",
    "READY",
    "NEW",
    "PAUSED",
    "UPLOADED_PRIVATE",
    "PUBLISH_READY",
    "SKIPPED_DUPLICATE",
    "PUBLISHED",
)


class Database:
    """Duenn gekapselte SQLite-Verbindung mit Sperre fuer Threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Transaktion mit Rollback im Fehlerfall (thread-sicher)."""

        with self._lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Connection]:
        """Einzelanweisung ohne explizite Transaktion (autocommit)."""

        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Tabellen anlegen bzw. migrieren (idempotent)."""

        with self.transaction() as connection:
            self._create_tables(connection)
            self._migrate(connection)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                " key TEXT PRIMARY KEY,"
                " value TEXT,"
                " updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS logs ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts TEXT NOT NULL,"
                " level TEXT NOT NULL,"
                " episode_id TEXT,"
                " action TEXT,"
                " message TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS uploads ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,"
                " platform TEXT NOT NULL DEFAULT 'youtube',"
                " attempt INTEGER NOT NULL DEFAULT 1,"
                " phase TEXT NOT NULL,"
                " status TEXT NOT NULL,"
                " youtube_video_id TEXT,"
                " bytes_sent INTEGER DEFAULT 0,"
                " quota_units INTEGER DEFAULT 0,"
                " http_status INTEGER,"
                " error_message TEXT,"
                " started_at TEXT,"
                " finished_at TEXT)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_logs_episode ON logs(episode_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_uploads_video ON uploads(video_id)")
            self._set_setting(connection, "schema_version", str(SCHEMA_VERSION))

    def _create_tables(self, connection: sqlite3.Connection) -> None:
        columns = ",\n  ".join(
            f"{name} {definition}" for name, definition, _default in VIDEO_COLUMNS
        )
        connection.execute(f"CREATE TABLE IF NOT EXISTS videos (\n  {columns}\n)")
        for index, column in (
            ("idx_videos_status", "status"),
            ("idx_videos_file_hash", "file_hash"),
            ("idx_videos_youtube_id", "youtube_video_id"),
            ("idx_videos_updated", "updated_at"),
        ):
            connection.execute(f"CREATE INDEX IF NOT EXISTS {index} ON videos({column})")

    def _migrate(self, connection: sqlite3.Connection) -> None:
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(videos)")}
        for name, definition, _default in VIDEO_COLUMNS:
            if name in existing or "PRIMARY KEY" in definition.upper():
                continue
            try:
                connection.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                # Spalte existiert bereits (z. B. nach Race) - ignorieren
                pass

    # ------------------------------------------------------------------

    def close(self) -> None:
        """Aufraeumen: Die Klasse haelt keine dauerhafte Verbindung.

        Es wird lediglich das WAL-Journal zurueckgeschrieben, damit nach dem
        Beenden keine ``-wal``/``-shm``-Dateien uebrig bleiben.
        """

        try:
            with self.cursor() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def vacuum(self) -> None:
        with self.transaction() as connection:
            connection.execute("VACUUM")

    def integrity_check(self) -> str:
        with self.cursor() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def table_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.cursor() as connection:
            for table in ("videos", "uploads", "logs", "settings"):
                row = connection.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                counts[table] = int(row["c"]) if row else 0
        return counts

    @staticmethod
    def _set_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now_iso()),
        )

    def set_setting(self, key: str, value: Any) -> None:
        with self.transaction() as connection:
            self._set_setting(connection, key, "" if value is None else str(value))

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.cursor() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


__all__ = ["Database", "SCHEMA_VERSION", "STATUS_ORDER", "VIDEO_COLUMNS", "row_to_dict"]
