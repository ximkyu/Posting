"""Sichere lokale Ablage fuer Secrets (OAuth-Token).

Anforderungen aus dem Lastenheft:

* Kein Passwort, kein Client-Secret und kein Access-Token im Quellcode,
  in ``config.json`` oder in Logdateien.
* ``credentials/`` ist per ``.gitignore`` hart aus dem Repository
  ausgeschlossen (zusaetzlich liegt dort eine eigene ``.gitignore``).
* Unter Windows wird das Token mit **DPAPI** (``win32crypt``) verschluesselt
  gespeichert - es kann nur vom selben Windows-Benutzer auf demselben
  Rechner wieder gelesen werden.
* Ohne DPAPI (Linux/macOS oder ohne pywin32) faellt der Store auf eine
  JSON-Datei mit beschraenkten Zugriffsrechten (0600) zurueck; unter Windows
  wird zusaetzlich die ACL per ``icacls`` auf den aktuellen Benutzer
  eingeschraenkt (best effort).

Ein Zugriffstoken ist damit niemals "Klartext in einer oeffentlich
sichtbaren Konfigurationsdatei".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .files import now_iso

_DESCRIPTION = "YouTube AutoPoster"


def _dpapi_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32crypt  # type: ignore  # noqa: F401
    except Exception:  # pragma: no cover - nur Windows
        return False
    return True


def _dpapi_encrypt(payload: bytes) -> bytes:
    import win32crypt  # type: ignore

    return win32crypt.CryptProtectData(payload, _DESCRIPTION, None, None, None, 0)


def _dpapi_decrypt(blob: bytes) -> bytes:
    import win32crypt  # type: ignore

    _description, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    return data


def _restrict_permissions(path: Path) -> None:
    """Datei so weit moeglich gegen andere lokale Benutzer abschotten."""

    try:
        if sys.platform == "win32":
            user = os.environ.get("USERNAME") or os.environ.get("USER")
            if user:
                subprocess.run(  # noqa: S603 - fester Programmaufruf, best effort
                    [
                        "icacls",
                        str(path),
                        "/inheritance:r",
                        "/grant:r",
                        f"{user}:F",
                    ],
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
        else:
            os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001 - Sicherheit darf nie den Betrieb stoppen
        pass


class SecretStore:
    """Kleiner, plattformuebergreifender Secret-Speicher."""

    def __init__(self, directory: str | Path, mode: str = "auto") -> None:
        self.directory = Path(directory).expanduser()
        self.mode = (mode or "auto").lower()
        if self.mode not in {"auto", "dpapi", "file"}:
            self.mode = "auto"

    # ------------------------------------------------------------------

    @property
    def effective_mode(self) -> str:
        if self.mode == "dpapi":
            return "dpapi" if _dpapi_available() else "file"
        if self.mode == "file":
            return "file"
        return "dpapi" if _dpapi_available() else "file"

    def path_for(self, name: str) -> Path:
        suffix = ".dpapi" if self.effective_mode == "dpapi" else ".json"
        return self.directory / f"{name}{suffix}"

    def _all_candidates(self, name: str) -> list[Path]:
        return [self.directory / f"{name}.dpapi", self.directory / f"{name}.json"]

    def exists(self, name: str) -> bool:
        return any(p.exists() for p in self._all_candidates(name))

    # ------------------------------------------------------------------

    def save(self, name: str, payload: dict[str, Any] | str) -> Path:
        """Secret atomar schreiben und Zugriffsrechte einschraenken."""

        self.directory.mkdir(parents=True, exist_ok=True)
        # Strings werden ebenfalls als JSON abgelegt (mit Anfuehrungszeichen),
        # damit ``load()``/``load_text()`` sie eindeutig zuruecklesen koennen.
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        raw = data.encode("utf-8")

        target = self.path_for(name)
        if self.effective_mode == "dpapi":
            blob = _dpapi_encrypt(raw)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(blob)
            _restrict_permissions(tmp)
            os.replace(tmp, target)
        else:
            tmp = target.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            _restrict_permissions(tmp)
            os.replace(tmp, target)
            _restrict_permissions(target)

        # Alte Variante entfernen (z. B. Modus-Wechsel dpapi <-> file)
        for candidate in self._all_candidates(name):
            if candidate != target and candidate.exists():
                try:
                    candidate.unlink()
                except OSError:
                    pass
        return target

    def load(self, name: str) -> dict[str, Any] | None:
        """Secret lesen - liefert ``None``, wenn nichts (Lesbares) vorhanden ist."""

        candidates = self._all_candidates(name)
        # Bevorzugt den zum aktuellen Modus passenden Speicher
        candidates.sort(key=lambda p: 0 if p == self.path_for(name) else 1)
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                if candidate.suffix == ".dpapi":
                    if not _dpapi_available():
                        continue
                    raw = _dpapi_decrypt(candidate.read_bytes()).decode("utf-8")
                else:
                    raw = candidate.read_text(encoding="utf-8")
                loaded = json.loads(raw)
                return loaded if isinstance(loaded, dict) else {"value": loaded}
            except Exception:  # noqa: BLE001 - defekte Datei != Absturz
                continue
        return None

    def load_text(self, name: str) -> str | None:
        data = self.load(name)
        if data is None:
            return None
        if set(data.keys()) == {"value"}:
            return str(data["value"])
        return json.dumps(data, ensure_ascii=False)

    def delete(self, name: str) -> list[Path]:
        removed: list[Path] = []
        for candidate in self._all_candidates(name):
            if candidate.exists():
                try:
                    candidate.unlink()
                    removed.append(candidate)
                except OSError:
                    pass
        return removed

    def info(self, name: str) -> dict[str, Any]:
        path = self.path_for(name)
        found = next((p for p in self._all_candidates(name) if p.exists()), None)
        return {
            "mode": self.effective_mode,
            "directory": str(self.directory),
            "expected_path": str(path),
            "found_path": str(found) if found else None,
            "exists": found is not None,
            "stored_at": now_iso() if found else None,
        }


def ensure_gitignore(directory: str | Path) -> None:
    """Legt in ``credentials/`` eine lokale .gitignore an (Schutz in Tiefe)."""

    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ".gitignore"
    content = "# Niemals Secrets committen\n*\n!.gitignore\n!README.txt\n"
    if not target.exists() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
