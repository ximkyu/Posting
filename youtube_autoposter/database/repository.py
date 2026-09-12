"""Alle Datenbank-Abfragen an einer Stelle (Repository).

Wichtig fuer die Zuverlaessigkeit:

* Statuswechsel laufen **atomar** (``UPDATE ... WHERE status IN (...)``).
  Dadurch kann dieselbe Episode nicht von zwei Threads gleichzeitig
  verarbeitet und schon gar nicht doppelt hochgeladen werden.
* Dubletten werden ueber ``episode_id`` (UNIQUE) UND ``file_hash`` erkannt.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Sequence

from ..constants import QUEUEABLE_STATUSES, UPLOADED_STATUSES, VideoStatus
from ..utils.files import now_iso
from .db import Database

_UPLOADED_STATUS_VALUES = tuple(s.value for s in UPLOADED_STATUSES)


def _encode_value(value: Any) -> Any:
    """Python-Werte in SQLite-taugliche Werte umwandeln.

    Listen/Woerterbuecher werden als JSON-Text gespeichert, Boolswerte als
    0/1. Ohne diese Umwandlung wuerde ein INSERT mit ``tags_json=[...]``
    fehlschlagen (sqlite3 unterstuetzt keine Listen als Parameter).
    """

    if isinstance(value, (dict, list, tuple)):
        return _json_dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class VideoRepository:
    """Zugriff auf ``videos``, ``uploads``, ``settings`` und ``logs``."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # videos
    # ------------------------------------------------------------------

    def upsert_episode(self, episode_id: str, platform: str = "youtube", **fields: Any) -> tuple[int, bool]:
        """Episode anlegen oder aktualisieren.

        Liefert ``(video_id, neu_angelegt)``. Ein bereits erfolgreich
        hochgeladener Datensatz wird dabei niemals zurueckgesetzt - das ist
        die erste Stufe des Duplikatschutzes.
        """

        payload = dict(fields)
        payload["episode_id"] = episode_id
        payload["platform"] = platform
        payload.setdefault("created_at", now_iso())
        payload["updated_at"] = now_iso()

        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT id, status FROM videos WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            if row:
                video_id = int(row["id"])
                current = str(row["status"])
                # Bereits hochgeladene/veroeffentlichte Episoden nicht antasten
                protected = _UPLOADED_STATUS_VALUES + (VideoStatus.SKIPPED_DUPLICATE.value,)
                updates = {
                    key: value for key, value in payload.items() if key not in {"episode_id", "created_at"}
                }
                if current in protected:
                    updates.pop("status", None)
                    updates.pop("error_message", None)
                    updates.pop("needs_attention", None)
                if updates:
                    self._apply_update(connection, video_id, updates)
                return video_id, False

            columns = [key for key, value in payload.items() if value is not None or key in {"status"}]
            values = [_encode_value(payload[key]) for key in columns]
            placeholders = ", ".join("?" for _ in columns)
            cursor = connection.execute(
                f"INSERT INTO videos ({', '.join(columns)}) VALUES ({placeholders})", values
            )
            return int(cursor.lastrowid or 0), True

    def _apply_update(self, connection: sqlite3.Connection, video_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        assignments = []
        values: list[Any] = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(_encode_value(value))
        assignments.append("updated_at = ?")
        values.append(now_iso())
        values.append(video_id)
        connection.execute(
            f"UPDATE videos SET {', '.join(assignments)} WHERE id = ?", values
        )

    def update(self, video_id: int, **fields: Any) -> None:
        if not fields:
            return
        with self.db.transaction() as connection:
            self._apply_update(connection, int(video_id), fields)

    def get(self, video_id: int) -> dict[str, Any] | None:
        with self.db.cursor() as connection:
            row = connection.execute("SELECT * FROM videos WHERE id = ?", (int(video_id),)).fetchone()
        return dict(row) if row else None

    def get_by_episode(self, episode_id: str) -> dict[str, Any] | None:
        with self.db.cursor() as connection:
            row = connection.execute("SELECT * FROM videos WHERE episode_id = ?", (episode_id,)).fetchone()
        return dict(row) if row else None

    def list_videos(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        search: str | None = None,
        needs_attention: bool | None = None,
        order: str = "newest",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            values = [s.value if isinstance(s, VideoStatus) else str(s) for s in statuses]
            if values:
                clauses.append(f"status IN ({', '.join('?' for _ in values)})")
                params.extend(values)
        if search:
            clauses.append("(episode_id LIKE ? OR title LIKE ? OR youtube_video_id LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        if needs_attention is not None:
            clauses.append("needs_attention = ?")
            params.append(1 if needs_attention else 0)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_sql = {
            "newest": "ORDER BY datetime(updated_at) DESC, id DESC",
            "oldest": "ORDER BY datetime(created_at) ASC, id ASC",
            "status": "ORDER BY status ASC, datetime(updated_at) DESC",
            "title": "ORDER BY COALESCE(title, episode_id) ASC",
        }.get(order, "ORDER BY datetime(updated_at) DESC, id DESC")
        sql = f"SELECT * FROM videos {where} {order_sql}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        with self.db.cursor() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        with self.db.cursor() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS c FROM videos GROUP BY status").fetchall()
        counts = {status.value: 0 for status in VideoStatus}
        for row in rows:
            counts[str(row["status"])] = int(row["c"])
        return counts

    def stats(self) -> dict[str, Any]:
        counts = self.count_by_status()
        with self.db.cursor() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(file_size), 0) AS bytes_total FROM videos"
            ).fetchone()
        queued = self.list_videos(statuses=[s.value for s in QUEUEABLE_STATUSES], limit=1000)
        return {
            "total": int(row["total"]) if row else 0,
            "bytes_total": int(row["bytes_total"]) if row else 0,
            "counts": counts,
            "ready": counts.get(VideoStatus.NEW.value, 0)
            + counts.get(VideoStatus.READY.value, 0)
            + counts.get(VideoStatus.PAUSED.value, 0),
            "uploading": counts.get(VideoStatus.UPLOADING.value, 0)
            + counts.get(VideoStatus.VALIDATING.value, 0)
            + counts.get(VideoStatus.PUBLISHING.value, 0),
            "private": counts.get(VideoStatus.UPLOADED_PRIVATE.value, 0)
            + counts.get(VideoStatus.PUBLISH_READY.value, 0),
            "published": counts.get(VideoStatus.PUBLISHED.value, 0),
            "failed": counts.get(VideoStatus.FAILED.value, 0)
            + counts.get(VideoStatus.INTERRUPTED.value, 0),
            "skipped": counts.get(VideoStatus.SKIPPED_DUPLICATE.value, 0),
            "queued": len(queued),
            "queue": [
                {"id": item["id"], "episode_id": item["episode_id"], "title": item["title"], "status": item["status"]}
                for item in queued[:10]
            ],
        }

    def set_status(
        self,
        video_id: int,
        status: str | VideoStatus,
        *,
        expected: Sequence[str | VideoStatus] | None = None,
        **fields: Any,
    ) -> bool:
        """Status setzen - optional nur, wenn der aktuelle Status passt."""

        target = status.value if isinstance(status, VideoStatus) else str(status)
        params: list[Any] = []
        where = "id = ?"
        params.append(int(video_id))
        if expected:
            values = [s.value if isinstance(s, VideoStatus) else str(s) for s in expected]
            where += f" AND status IN ({', '.join('?' for _ in values)})"
            params.extend(values)

        assignments = ["status = ?", "updated_at = ?"]
        payload: list[Any] = [target, now_iso()]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            payload.append(_encode_value(value))
        payload.extend(params)

        with self.db.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE videos SET {', '.join(assignments)} WHERE {where}", payload
            )
            return cursor.rowcount > 0

    def claim(self, video_id: int, from_statuses: Sequence[str | VideoStatus], to_status: str | VideoStatus) -> bool:
        """Atomar "reservieren": nur EIN Thread/Prozess bekommt True."""

        return self.set_status(video_id, to_status, expected=from_statuses)

    def next_queued(self, statuses: Sequence[str | VideoStatus] | None = None) -> dict[str, Any] | None:
        wanted = statuses or QUEUEABLE_STATUSES
        values = [s.value if isinstance(s, VideoStatus) else str(s) for s in wanted]
        if not values:
            return None
        sql = (
            "SELECT * FROM videos WHERE status IN "
            f"({', '.join('?' for _ in values)}) "
            "ORDER BY datetime(created_at) ASC, id ASC LIMIT 1"
        )
        with self.db.cursor() as connection:
            row = connection.execute(sql, values).fetchone()
        return dict(row) if row else None

    def find_by_hash(self, file_hash: str, *, exclude_id: int | None = None) -> list[dict[str, Any]]:
        if not file_hash:
            return []
        sql = "SELECT * FROM videos WHERE file_hash = ?"
        params: list[Any] = [file_hash]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(int(exclude_id))
        with self.db.cursor() as connection:
            rows = connection.execute(sql + " ORDER BY id ASC", params).fetchall()
        return [dict(row) for row in rows]

    def find_uploaded_by_hash(self, file_hash: str, *, exclude_id: int | None = None) -> dict[str, Any] | None:
        for row in self.find_by_hash(file_hash, exclude_id=exclude_id):
            if row.get("status") in _UPLOADED_STATUS_VALUES:
                return row
        return None

    def find_uploaded_by_episode(self, episode_id: str) -> dict[str, Any] | None:
        row = self.get_by_episode(episode_id)
        if row and row.get("status") in _UPLOADED_STATUS_VALUES:
            return row
        return None

    def find_by_youtube_id(self, youtube_video_id: str) -> dict[str, Any] | None:
        if not youtube_video_id:
            return None
        with self.db.cursor() as connection:
            row = connection.execute(
                "SELECT * FROM videos WHERE youtube_video_id = ? ORDER BY id DESC LIMIT 1",
                (youtube_video_id,),
            ).fetchone()
        return dict(row) if row else None

    def interrupted(self) -> list[dict[str, Any]]:
        return self.list_videos(statuses=[VideoStatus.INTERRUPTED.value])

    def reset_running_to_interrupted(self) -> list[dict[str, Any]]:
        """Nach einem Neustart: laufende Uploads als UNTERBROCHEN markieren.

        Es wird bewusst NICHT automatisch erneut hochgeladen - das verhindert
        Doppeluploads nach einem Crash waehrend des Uploads.
        """

        affected: list[dict[str, Any]] = []
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM videos WHERE status IN (?, ?, ?)",
                (
                    VideoStatus.UPLOADING.value,
                    VideoStatus.PUBLISHING.value,
                    VideoStatus.VALIDATING.value,
                ),
            ).fetchall()
            for row in rows:
                record = dict(row)
                previous = record["status"]
                if previous == VideoStatus.PUBLISHING.value:
                    # Veroeffentlichen ist idempotent: zurueck auf privat-Bereit
                    connection.execute(
                        "UPDATE videos SET status = ?, error_message = ?, needs_attention = 1, updated_at = ?"
                        " WHERE id = ?",
                        (
                            VideoStatus.UPLOADED_PRIVATE.value,
                            "Veroeffentlichung wurde unterbrochen - bitte erneut pruefen/starten",
                            now_iso(),
                            record["id"],
                        ),
                    )
                elif previous == VideoStatus.VALIDATING.value:
                    # Die Pruefung laeuft VOR dem Upload: Es ist noch nichts an
                    # YouTube gegangen, deshalb darf die Episode gefahrlos
                    # erneut eingereiht werden.
                    connection.execute(
                        "UPDATE videos SET status = ?, error_message = NULL, needs_attention = 0, updated_at = ?"
                        " WHERE id = ?",
                        (
                            VideoStatus.READY.value,
                            now_iso(),
                            record["id"],
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE videos SET status = ?, error_message = ?, needs_attention = 1, updated_at = ?"
                        " WHERE id = ?",
                        (
                            VideoStatus.INTERRUPTED.value,
                            "Upload wurde unterbrochen (Neustart/Absturz). Kein automatischer Re-Upload "
                            "moeglich - bitte 'Auf YouTube pruefen' oder 'Erneut hochladen' waehlen.",
                            now_iso(),
                            record["id"],
                        ),
                    )
                    connection.execute(
                        "UPDATE uploads SET status = 'FAILED', finished_at = ?, error_message = ?"
                        " WHERE video_id = ? AND status IN ('STARTED', 'IN_PROGRESS')",
                        (
                            now_iso(),
                            "Unterbrochen durch Neustart",
                            record["id"],
                        ),
                    )
                if previous == VideoStatus.PUBLISHING.value:
                    record["new_status"] = VideoStatus.UPLOADED_PRIVATE.value
                elif previous == VideoStatus.VALIDATING.value:
                    record["new_status"] = VideoStatus.READY.value
                else:
                    record["new_status"] = VideoStatus.INTERRUPTED.value
                affected.append(record)
        return affected

    # ------------------------------------------------------------------
    # uploads (Versuche)
    # ------------------------------------------------------------------

    def start_attempt(
        self,
        video_id: int,
        phase: str,
        *,
        platform: str = "youtube",
        attempt: int | None = None,
    ) -> int:
        if attempt is None:
            with self.db.cursor() as connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(attempt), 0) AS m FROM uploads WHERE video_id = ?", (int(video_id),)
                ).fetchone()
            attempt = int(row["m"]) + 1 if row else 1
        with self.db.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO uploads (video_id, platform, attempt, phase, status, started_at)"
                " VALUES (?, ?, ?, ?, 'STARTED', ?)",
                (int(video_id), platform, int(attempt), phase, now_iso()),
            )
            return int(cursor.lastrowid or 0)

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        error: str | None = None,
        youtube_video_id: str | None = None,
        bytes_sent: int | None = None,
        quota_units: int | None = None,
        http_status: int | None = None,
    ) -> None:
        assignments = ["status = ?", "finished_at = ?"]
        values: list[Any] = [status, now_iso()]
        for key, value in (
            ("error_message", error),
            ("youtube_video_id", youtube_video_id),
            ("bytes_sent", bytes_sent),
            ("quota_units", quota_units),
            ("http_status", http_status),
        ):
            if value is not None:
                assignments.append(f"{key} = ?")
                values.append(value)
        values.append(int(attempt_id))
        with self.db.transaction() as connection:
            connection.execute(
                f"UPDATE uploads SET {', '.join(assignments)} WHERE id = ?", values
            )

    def list_attempts(self, video_id: int) -> list[dict[str, Any]]:
        with self.db.cursor() as connection:
            rows = connection.execute(
                "SELECT * FROM uploads WHERE video_id = ? ORDER BY id DESC", (int(video_id),)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.db.get_setting(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.db.set_setting(key, value)

    def get_bool_setting(self, key: str, default: bool = False) -> bool:
        raw = self.get_setting(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on", "ja"}

    def all_settings(self) -> dict[str, str]:
        with self.db.cursor() as connection:
            rows = connection.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"] or "") for row in rows}

    def delete_setting(self, key: str) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM settings WHERE key = ?", (key,))

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    def add_log(
        self,
        level: str,
        message: str,
        *,
        episode_id: str | None = None,
        action: str | None = None,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO logs (ts, level, episode_id, action, message) VALUES (?, ?, ?, ?, ?)",
                (now_iso(), str(level).upper(), episode_id, action, str(message)[:4000]),
            )

    def list_logs(
        self,
        *,
        limit: int = 200,
        episode_id: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)
        if level:
            clauses.append("level = ?")
            params.append(str(level).upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self.db.cursor() as connection:
            rows = connection.execute(
                f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_logs(self, keep_rows: int = 20000) -> int:
        keep_rows = max(1000, int(keep_rows))
        with self.db.transaction() as connection:
            row = connection.execute("SELECT COUNT(*) AS c FROM logs").fetchone()
            total = int(row["c"]) if row else 0
            excess = total - keep_rows
            if excess <= 0:
                return 0
            connection.execute(
                "DELETE FROM logs WHERE id IN (SELECT id FROM logs ORDER BY id ASC LIMIT ?)", (excess,)
            )
            return excess


def load_json(value: Any, default: Any = None) -> Any:
    """JSON-Spalte sicher zurueckverwandeln."""

    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


__all__ = ["VideoRepository", "load_json"]
