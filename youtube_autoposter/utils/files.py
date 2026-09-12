"""Datei-Hilfsfunktionen: Hashing, Stabilitaetspruefung, atomares Verschieben.

Hier steckt der Schutz gegen die zwei gefaehrlichsten Situationen:

1. Eine Videodatei wird noch kopiert, waehrend das System schon hochlaedt
   (-> :func:`wait_for_stable_file` / :class:`FileStabilityTracker`).
2. Eine Datei wird beim Archivieren beschaedigt oder ueberschrieben
   (-> :func:`atomic_move` mit Integritaetspruefung).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from ..errors import FileOperationError

#: Dateien, die niemals als Episode interpretiert werden
TEMP_SUFFIXES = (".part", ".partial", ".tmp", ".temp", ".crdownload", ".download", ".lock", ".swp")
TEMP_PREFIXES = ("~$", "._", ".")
#: System-/Cache-Dateien, die niemals zu einer Episode gehoeren
IGNORED_NAMES = ("thumbs.db", "desktop.ini", ".ds_store", "folder.jpg")

_CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# Zeit
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return to_iso(now_utc()) or ""


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_local(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.astimezone()


def format_local(value: str | datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """ISO-String (UTC) oder datetime in lokaler Zeit anzeigen."""

    moment = value if isinstance(value, datetime) else parse_iso(value if isinstance(value, str) else None)
    local = to_local(moment)
    return local.strftime(fmt) if local else "-"


# ---------------------------------------------------------------------------
# Groesse / Formatierung
# ---------------------------------------------------------------------------


def human_size(num_bytes: float | int | None) -> str:
    """1536 -> '1.5 KB', 1932735283 -> '1.8 GB'."""

    if num_bytes is None or num_bytes < 0:
        return "-"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    total = int(round(float(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def file_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return -1


def _mtime(path: str | Path) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Hashing (Dubletten-Schutz)
# ---------------------------------------------------------------------------


def sha256_file(
    path: str | Path,
    *,
    chunk_size: int = _CHUNK,
    progress_cb: Callable[[int, int], None] | None = None,
) -> str:
    """SHA-256 einer Datei.

    Liest die Datei vollstaendig - damit wird gleichzeitig geprueft, ob sie
    ueberhaupt lesbar ist. ``progress_cb(bytes_done, bytes_total)`` erlaubt
    Fortschrittsausgaben bei sehr grossen Dateien.
    """

    p = Path(path)
    if not p.exists():
        raise FileOperationError(f"Datei nicht gefunden: {p}")
    if not p.is_file():
        raise FileOperationError(f"Keine Datei: {p}")

    digest = hashlib.sha256()
    total = p.stat().st_size
    done = 0
    with p.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if progress_cb is not None:
                progress_cb(done, total)
    return digest.hexdigest()


def quick_fingerprint(path: str | Path, sample_bytes: int = 1024 * 1024) -> str:
    """Schneller Fingerabdruck (Kopf + Mitte + Ende + Groesse).

    Wird nur fuer optionale Vorab-Checks genutzt - der verbindliche
    Dubletten-Schutz verwendet :func:`sha256_file`.
    """

    p = Path(path)
    size = p.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode())
    with p.open("rb") as handle:
        digest.update(handle.read(sample_bytes))
        if size > sample_bytes * 2:
            handle.seek(size // 2)
            digest.update(handle.read(sample_bytes))
            handle.seek(max(0, size - sample_bytes))
            digest.update(handle.read(sample_bytes))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Stabilitaetspruefung
# ---------------------------------------------------------------------------


@dataclass
class StabilityResult:
    stable: bool
    path: Path
    size: int
    checks: int = 0
    elapsed_seconds: float = 0.0
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - Komfort
        return self.stable


def wait_for_stable_file(
    path: str | Path,
    *,
    stability_seconds: float = 10.0,
    interval: float = 2.0,
    required_checks: int = 2,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> StabilityResult:
    """Blockierend warten, bis eine Datei nicht mehr waechst.

    Vorgehen (konfigurierbar):
      1. Groesse messen
      2. ``interval`` Sekunden warten
      3. Groesse erneut messen
      4. Erst wenn sie ``required_checks`` mal in Folge unveraendert blieb
         UND mindestens ``stability_seconds`` vergangen sind, gilt die Datei
         als fertig kopiert.
    """

    p = Path(path)
    required_checks = max(1, int(required_checks))
    started = clock()

    if not p.exists():
        return StabilityResult(False, p, -1, reason="Datei existiert nicht")

    last_size = file_size(p)
    unchanged = 1
    checks = 1

    while True:
        elapsed = clock() - started
        if timeout is not None and elapsed >= timeout:
            return StabilityResult(
                False,
                p,
                last_size,
                checks=checks,
                elapsed_seconds=elapsed,
                reason=f"Timeout nach {elapsed:.0f}s (Datei waechst weiter?)",
            )
        if unchanged >= required_checks and elapsed >= stability_seconds:
            return StabilityResult(
                True, p, last_size, checks=checks, elapsed_seconds=elapsed, reason="Groesse stabil"
            )

        wait = interval
        if stability_seconds and elapsed < stability_seconds:
            wait = max(wait, min(interval, stability_seconds - elapsed))
        sleep(wait)

        checks += 1
        if not p.exists():
            return StabilityResult(False, p, -1, checks=checks, reason="Datei ist verschwunden")
        size = file_size(p)
        if size == last_size and size >= 0:
            unchanged += 1
        else:
            unchanged = 1
            last_size = size
        elapsed = clock() - started


@dataclass
class FileStabilityTracker:
    """Nicht-blockierende Stabilitaetspruefung fuer den Watch-Folder.

    Der Scanner-Thread darf nicht schlafen, waehrend eine grosse Datei
    kopiert wird. Deshalb merkt sich der Tracker pro Datei die letzte
    Groesse und den Zeitpunkt der letzten Aenderung.

    Zusaetzlich wird der Dateistempel (mtime) genutzt: Ist eine Datei bei der
    ersten Sichtung bereits laenger als ``stability_seconds`` unveraendert,
    gilt sie sofort als stabil. Das ist wichtig fuer
    * einmalige CLI-Scans (``python app.py scan``) und
    * den Neustart der Anwendung (Dateien, die vorher kopiert wurden).

    Waehrend eine Kopie laeuft, aendert sich die mtime staendig - die Datei
    wird dann weiterhin korrekt als "nicht stabil" behandelt.
    """

    stability_seconds: float = 10.0
    required_checks: int = 2
    trust_mtime: bool = True
    _seen: dict[str, dict[str, float]] = field(default_factory=dict)

    def observe(self, path: str | Path, *, now: float | None = None) -> StabilityResult:
        p = Path(path)
        key = str(p)
        stamp = time.time() if now is None else now
        if not p.exists():
            self._seen.pop(key, None)
            return StabilityResult(False, p, -1, reason="Datei existiert nicht")

        size = file_size(p)
        state = self._seen.get(key)
        if state is None:
            mtime = _mtime(p)
            if self.trust_mtime and self.stability_seconds and mtime and (stamp - mtime) >= self.stability_seconds:
                self._seen[key] = {
                    "size": size,
                    "first_seen": stamp,
                    "last_change": mtime,
                    "checks": max(self.required_checks, 2),
                }
                return StabilityResult(
                    True,
                    p,
                    size,
                    checks=int(max(self.required_checks, 2)),
                    elapsed_seconds=stamp - mtime,
                    reason=f"seit {stamp - mtime:.0f}s unveraendert (mtime)",
                )
            self._seen[key] = {"size": size, "first_seen": stamp, "last_change": stamp, "checks": 1}
            return StabilityResult(False, p, size, checks=1, reason="Erste Sichtung - warte auf Stabilitaet")

        checks = int(state.get("checks", 0)) + 1
        if size != state.get("size"):
            self._seen[key] = {
                "size": size,
                "first_seen": state.get("first_seen", stamp),
                "last_change": stamp,
                "checks": checks,
            }
            return StabilityResult(
                False, p, size, checks=checks, reason="Datei waechst noch (Groesse hat sich geaendert)"
            )

        state["checks"] = checks
        unchanged_for = stamp - float(state.get("last_change", stamp))
        self._seen[key] = state
        if checks >= self.required_checks and unchanged_for >= self.stability_seconds:
            return StabilityResult(True, p, size, checks=checks, elapsed_seconds=unchanged_for, reason="stabil")
        return StabilityResult(
            False,
            p,
            size,
            checks=checks,
            elapsed_seconds=unchanged_for,
            reason=f"stabil seit {unchanged_for:.0f}s (noetig: {self.stability_seconds:.0f}s)",
        )

    def forget(self, path: str | Path) -> None:
        self._seen.pop(str(Path(path)), None)

    def forget_many(self, paths: Iterable[str | Path]) -> None:
        for p in paths:
            self.forget(p)

    def prune(self, existing: Iterable[str | Path]) -> None:
        keep = {str(Path(p)) for p in existing}
        for key in list(self._seen):
            if key not in keep:
                self._seen.pop(key, None)


# ---------------------------------------------------------------------------
# Dateinamen
# ---------------------------------------------------------------------------


def is_temp_file(path: str | Path) -> bool:
    """True fuer unfertige Kopien, Sperren und System-/Cache-Dateien."""

    p = Path(path)
    name = p.name.lower()
    if name in IGNORED_NAMES:
        return True
    if name.startswith(TEMP_PREFIXES):
        return True
    return any(name.endswith(suffix) for suffix in TEMP_SUFFIXES)


def safe_stem(name: str) -> str:
    """Dateinamen auf einen sicheren, vergleichbaren Stamm reduzieren."""

    stem = Path(name).stem if "." in name else name
    return stem.strip().strip(".")


_INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_FOLDER_NAMES = {
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5",
    "com6", "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5",
    "lpt6", "lpt7", "lpt8", "lpt9",
}


def safe_folder_name(name: str, *, max_length: int = 80, fallback: str = "episode") -> str:
    """Aus einem Bezeichner (z. B. ``episode_id``) einen sicheren Ordnernamen machen.

    Windows und POSIX werden gleichermassen bedient: verbotene Zeichen werden
    ersetzt, fuehrende/endende Punkte und Leerzeichen entfernt, die Laenge
    begrenzt und reservierte Namen (``CON``, ``NUL``, ...) entschaerft.
    """

    raw = _INVALID_FOLDER_CHARS.sub("_", str(name or "").strip())
    raw = raw.replace("..", ".").strip(" .")
    if not raw:
        return fallback
    if len(raw) > max_length:
        raw = raw[:max_length].rstrip(" ._") or fallback
    if raw.split(".")[0].lower() in _RESERVED_FOLDER_NAMES:
        raw = f"_{raw}"
    return raw or fallback


def unique_path(directory: str | Path, filename: str) -> Path:
    """Liefert einen Pfad in ``directory``, der noch nicht existiert.

    Es wird niemals eine vorhandene Datei ueberschrieben - stattdessen wird
    ein Zaehler angehaengt (``name (2).mp4``).
    """

    directory = Path(directory)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Atomares / sicheres Verschieben
# ---------------------------------------------------------------------------


def _same_filesystem(src: Path, dst_dir: Path) -> bool:
    try:
        return os.stat(src).st_dev == os.stat(dst_dir).st_dev
    except OSError:
        return False


def atomic_move(
    src: str | Path,
    dst_dir: str | Path,
    *,
    verify_hash: bool = True,
    expected_hash: str | None = None,
    keep_structure: bool = False,
) -> Path:
    """Datei sicher in einen Zielordner verschieben.

    * Ueberschreibt niemals eine bestehende Datei (eindeutiger Zielname).
    * Gleiches Dateisystem -> ``os.replace`` (atomar).
    * Anderes Dateisystem -> Kopieren in eine Temporaerdatei im Zielordner,
      Groessen-/Hash-Vergleich und erst danach ``os.replace`` + Loeschen der
      Quelle. Damit kann eine unterbrochene Kopie die Zieldatei nicht
      beschaedigen.
    """

    source = Path(src)
    target_dir = Path(dst_dir)
    if not source.exists():
        raise FileOperationError(f"Quelldatei fehlt: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)

    relative = ""
    if keep_structure:
        relative = source.parent.name if source.parent != target_dir else ""
    final_dir = target_dir / relative if relative else target_dir
    final_dir.mkdir(parents=True, exist_ok=True)

    destination = unique_path(final_dir, source.name)
    source_size = source.stat().st_size

    if _same_filesystem(source, final_dir):
        try:
            os.replace(source, destination)
        except OSError:
            # Fallback, falls os.replace z. B. wegen Rechten scheitert
            shutil.move(str(source), str(destination))
    else:
        handle, tmp_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.", suffix=destination.suffix, dir=str(final_dir)
        )
        os.close(handle)
        tmp_path = Path(tmp_name)
        try:
            shutil.copy2(source, tmp_path)
            moved_size = tmp_path.stat().st_size
            if moved_size != source_size:
                raise FileOperationError(
                    f"Groesse nach dem Kopieren unterschiedlich: {source_size} -> {moved_size} ({source.name})"
                )
            if verify_hash:
                moved_hash = sha256_file(tmp_path)
                reference = expected_hash or sha256_file(source)
                if moved_hash != reference:
                    raise FileOperationError(
                        f"Hash nach dem Kopieren unterschiedlich - Datei beschaedigt: {source.name}"
                    )
            os.replace(tmp_path, destination)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        source.unlink(missing_ok=True)

    if destination.stat().st_size != source_size:
        raise FileOperationError(
            f"Integritaetspruefung fehlgeschlagen: {destination} hat unerwartete Groesse"
        )
    return destination


def move_episode_files(
    paths: Iterable[str | Path | None],
    dst_dir: str | Path,
    *,
    verify_hash: bool = True,
) -> list[tuple[str, str]]:
    """Mehrere Dateien (Video, JSON, Thumbnail) in einen Ordner verschieben.

    Gibt ``[(alter Pfad, neuer Pfad), ...]`` zurueck. Ein Fehler bei einer
    einzelnen Datei bricht die restlichen Dateien nicht ab; Fehler werden
    gesammelt und nur dann geworfen, wenn gar nichts verschoben werden konnte.
    """

    moved: list[tuple[str, str]] = []
    errors: list[str] = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.exists():
            continue
        try:
            target = atomic_move(path, dst_dir, verify_hash=verify_hash)
            moved.append((str(path), str(target)))
        except FileOperationError as exc:
            errors.append(str(exc))
    if errors and not moved:
        raise FileOperationError("; ".join(errors))
    return moved
