"""Tests fuer Datei-Hilfsfunktionen, Hashing, Stabilitaet und den Secret-Store."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from youtube_autoposter.errors import FileOperationError
from youtube_autoposter.utils.files import (
    FileStabilityTracker,
    atomic_move,
    file_size,
    format_duration,
    human_size,
    is_temp_file,
    move_episode_files,
    now_iso,
    parse_iso,
    quick_fingerprint,
    safe_stem,
    sha256_file,
    unique_path,
    wait_for_stable_file,
)
from youtube_autoposter.utils.secure_store import SecretStore, ensure_gitignore


# ---------------------------------------------------------------------------
# Hashing (Grundlage der Dubletten-Erkennung)
# ---------------------------------------------------------------------------


def test_sha256_ist_stabil_und_bekannter_wert(tmp_path: Path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"youtube-autoposter")
    first = sha256_file(path)
    second = sha256_file(path)
    assert first == second
    assert len(first) == 64
    # Referenzwert (mit Python berechnet, unabhaengig von der Implementierung)
    import hashlib

    assert first == hashlib.sha256(b"youtube-autoposter").hexdigest()


def test_sha256_unterscheidet_inhalte(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"x" * 2048)
    b.write_bytes(b"x" * 2047 + b"y")
    assert sha256_file(a) != sha256_file(b)


def test_sha256_ignoriert_dateiname_und_zeitstempel(tmp_path: Path) -> None:
    a = tmp_path / "folge_01.mp4"
    b = tmp_path / "anderer_name.mp4"
    a.write_bytes(b"identischer-inhalt")
    b.write_bytes(b"identischer-inhalt")
    os.utime(b, (time.time() + 3600, time.time() + 3600))
    assert sha256_file(a) == sha256_file(b)


def test_sha256_meldet_fortschritt(tmp_path: Path) -> None:
    path = tmp_path / "gross.bin"
    path.write_bytes(os.urandom(512 * 1024))
    seen: list[tuple[int, int]] = []
    sha256_file(path, chunk_size=64 * 1024, progress_cb=lambda sent, total: seen.append((sent, total)))
    assert seen, "kein Fortschritt gemeldet"
    assert seen[-1][0] == seen[-1][1] == path.stat().st_size


def test_quick_fingerprint_ist_schnell_und_unterscheidbar(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"KOPF" + os.urandom(4096))
    b.write_bytes(b"KOP2" + os.urandom(4096))
    fa = quick_fingerprint(a)
    fb = quick_fingerprint(b)
    assert fa != fb
    assert quick_fingerprint(a) == fa


def test_fehlende_datei_wirft_dateifehler(tmp_path: Path) -> None:
    with pytest.raises(FileOperationError):
        sha256_file(tmp_path / "gibt_es_nicht.mp4")


# ---------------------------------------------------------------------------
# Anzeige-Helfer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wert,erwartet",
    [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1536, "1.5 KB"), (5 * 1024 * 1024, "5.0 MB")],
)
def test_human_size(wert: int, erwartet: str) -> None:
    assert human_size(wert) == erwartet


def test_human_size_ohne_wert() -> None:
    assert human_size(None) in {"-", "0 B", ""}


@pytest.mark.parametrize(
    "sekunden,erwartet",
    [(0, "0:00"), (4, "0:04"), (65, "1:05"), (3600, "1:00:00"), (3661, "1:01:01")],
)
def test_format_duration(sekunden: float, erwartet: str) -> None:
    assert format_duration(sekunden) == erwartet


def test_format_duration_ohne_wert() -> None:
    assert format_duration(None) in {"-", "0:00", ""}


def test_iso_roundtrip() -> None:
    text = now_iso()
    moment = parse_iso(text)
    assert moment is not None
    assert parse_iso("kein-datum") is None


def test_file_size_fehlerfall(tmp_path: Path) -> None:
    assert file_size(tmp_path / "nix") == -1


# ---------------------------------------------------------------------------
# Stabilitaetspruefung (Schutz vor Upload waehrend des Kopierens)
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministische Uhr fuer Stabilitaetstests."""

    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def fake_sleep(clock: FakeClock, seconds: float) -> None:
    clock.advance(seconds)


def test_wachsende_datei_gilt_nicht_als_stabil(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "kopie.mp4"
    path.write_bytes(b"x" * 1000)

    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        clock.advance(seconds)
        # Datei waechst bei jedem "Schlaf" -> Kopier-Vorgang
        calls["n"] += 1
        path.write_bytes(b"x" * (1000 + calls["n"] * 100))

    result = wait_for_stable_file(
        path,
        stability_seconds=1,
        interval=1,
        required_checks=2,
        timeout=3,
        sleep=sleep,
        clock=clock,
    )
    assert result.stable is False
    assert not result.reason.startswith("stabil")


def test_stehende_datei_wird_stabil(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "fertig.mp4"
    path.write_bytes(b"x" * 5000)

    result = wait_for_stable_file(
        path,
        stability_seconds=1,
        interval=1,
        required_checks=2,
        timeout=20,
        sleep=lambda s: clock.advance(s),
        clock=clock,
    )
    assert result.stable is True
    assert result.size == 5000


def test_tracker_erste_sichtung_nicht_stabil(tmp_path: Path) -> None:
    path = tmp_path / "neu.mp4"
    path.write_bytes(b"12345")
    # mtime bewusst auf "jetzt" setzen
    tracker = FileStabilityTracker(stability_seconds=30, required_checks=2)
    now = time.time()
    result = tracker.observe(path, now=now)
    assert result.stable is False


def test_tracker_alte_mtime_ist_sofort_stabil(tmp_path: Path) -> None:
    """Wichtig fuer CLI-Scans und Neustarts: alte Dateien nicht blockieren."""

    path = tmp_path / "alt.mp4"
    path.write_bytes(b"12345")
    alt = time.time() - 120
    os.utime(path, (alt, alt))
    tracker = FileStabilityTracker(stability_seconds=10, required_checks=2)
    result = tracker.observe(path, now=time.time())
    assert result.stable is True
    assert "mtime" in result.reason


def test_tracker_groessenwechsel_setzt_zaehler_zurueck(tmp_path: Path) -> None:
    path = tmp_path / "wachsend.mp4"
    alt = time.time() - 3600
    os.utime(path.parent, (alt, alt))
    path.write_bytes(b"1000")
    os.utime(path, (time.time(), time.time()))  # frische mtime -> nicht stabil
    tracker = FileStabilityTracker(stability_seconds=2, required_checks=3, trust_mtime=False)
    now = time.time()
    assert tracker.observe(path, now=now).stable is False
    assert tracker.observe(path, now=now + 1).stable is False
    dritte = tracker.observe(path, now=now + 2)
    assert dritte.stable is True
    assert dritte.checks == 3
    # Die Datei waechst weiter -> darf nicht als stabil gelten
    path.write_bytes(b"20000")  # 5 statt 4 Bytes
    assert tracker.observe(path, now=now + 3).stable is False
    assert tracker.observe(path, now=now + 4).stable is False
    assert tracker.observe(path, now=now + 5).stable is True


def test_tracker_vergisst_geloeschte_dateien(tmp_path: Path) -> None:
    path = tmp_path / "temporaer.mp4"
    path.write_bytes(b"x")
    tracker = FileStabilityTracker(stability_seconds=1, required_checks=1, trust_mtime=False)
    tracker.observe(path)
    path.unlink()
    result = tracker.observe(path)
    assert result.stable is False
    assert not tracker._seen


def test_is_temp_file() -> None:
    assert is_temp_file("folge.mp4.part") is True
    assert is_temp_file("folge.mp4.crdownload") is True
    assert is_temp_file("~$folge.mp4") is True
    assert is_temp_file("._folge.mp4") is True
    assert is_temp_file(".DS_Store") is True
    assert is_temp_file("Thumbs.db") is True
    assert is_temp_file("desktop.ini") is True
    assert is_temp_file("folge_01.mp4") is False


def test_safe_stem() -> None:
    assert safe_stem("folge_01.mp4") == "folge_01"
    assert safe_stem("folge_01") == "folge_01"
    assert safe_stem("  folge 01 .mp4 ") == "folge 01"


# ---------------------------------------------------------------------------
# Atomares Verschieben + Integritaet
# ---------------------------------------------------------------------------


def test_atomic_move_verschiebt_datei(tmp_path: Path) -> None:
    src_dir = tmp_path / "READY"
    dst_dir = tmp_path / "PROCESSING"
    src_dir.mkdir()
    dst_dir.mkdir()
    src = src_dir / "folge.mp4"
    src.write_bytes(b"inhalt-der-datei")
    erwartet = sha256_file(src)

    ziel = atomic_move(src, dst_dir, verify_hash=True)

    assert ziel.parent == dst_dir
    assert not src.exists()
    assert ziel.read_bytes() == b"inhalt-der-datei"
    assert sha256_file(ziel) == erwartet


def test_atomic_move_verhindert_ueberschreiben(tmp_path: Path) -> None:
    src_dir = tmp_path / "READY"
    dst_dir = tmp_path / "UPLOADED_PRIVATE"
    src_dir.mkdir()
    dst_dir.mkdir()
    (dst_dir / "folge.mp4").write_bytes(b"alte-datei")
    src = src_dir / "folge.mp4"
    src.write_bytes(b"neue-datei")

    ziel = atomic_move(src, dst_dir)

    assert ziel.name != "folge.mp4"
    assert (dst_dir / "folge.mp4").read_bytes() == b"alte-datei"
    assert ziel.read_bytes() == b"neue-datei"


def test_atomic_move_bricht_bei_beschaedigter_kopie_ab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kopiervorgang ueber "Dateisystemgrenzen" mit beschaedigtem Ergebnis.

    Simuliert den Fall, dass die kopierte Datei nicht der Quelle entspricht
    (z. B. weil parallel weitergeschrieben wurde). Die Quelldatei muss dabei
    erhalten bleiben - es darf nichts halbfertig im Ziel liegen.
    """

    src_dir = tmp_path / "READY"
    dst_dir = tmp_path / "PROCESSING"
    src_dir.mkdir()
    dst_dir.mkdir()
    src = src_dir / "folge.mp4"
    src.write_bytes(b"original")

    monkeypatch.setattr("youtube_autoposter.utils.files._same_filesystem", lambda *a, **k: False)

    real = sha256_file

    def beschaedigt(path, **kwargs):
        Path(path).write_bytes(b"veraendert")
        return real(path, **kwargs)

    monkeypatch.setattr("youtube_autoposter.utils.files.sha256_file", beschaedigt)

    with pytest.raises(FileOperationError):
        atomic_move(src, dst_dir, verify_hash=True, expected_hash=real(src))

    # Quelle bleibt, Zielordner enthaelt keine Restdatei
    assert src.exists()
    assert list(dst_dir.iterdir()) == []


def test_atomic_move_mit_bekanntem_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("youtube_autoposter.utils.files._same_filesystem", lambda *a, **k: False)
    src_dir = tmp_path / "READY"
    dst_dir = tmp_path / "PROCESSING"
    src_dir.mkdir()
    dst_dir.mkdir()
    src = src_dir / "folge.mp4"
    src.write_bytes(b"inhalt")
    guter_hash = sha256_file(src)

    ziel = atomic_move(src, dst_dir, expected_hash=guter_hash)
    assert ziel.exists()
    assert not src.exists()

    src2 = src_dir / "folge2.mp4"
    src2.write_bytes(b"inhalt")
    with pytest.raises(FileOperationError):
        atomic_move(src2, dst_dir, expected_hash="0" * 64)
    assert src2.exists()


def test_move_episode_files_verschiebt_alle_drei(tmp_path: Path) -> None:
    src_dir = tmp_path / "READY"
    dst_dir = tmp_path / "UPLOADED_PRIVATE"
    src_dir.mkdir()
    dst_dir.mkdir()
    video = src_dir / "folge_09.mp4"
    meta = src_dir / "folge_09.json"
    thumb = src_dir / "folge_09.jpg"
    video.write_bytes(b"v")
    meta.write_text("{}", encoding="utf-8")
    thumb.write_bytes(b"t")

    moved = move_episode_files([video, meta, thumb], dst_dir)

    assert len(moved) == 3
    assert not list(src_dir.iterdir())
    assert (dst_dir / "folge_09.mp4").exists()
    assert (dst_dir / "folge_09.json").exists()
    assert (dst_dir / "folge_09.jpg").exists()


def test_move_episode_files_ignoriert_none_und_fehlendes(tmp_path: Path) -> None:
    dst = tmp_path / "FAILED"
    dst.mkdir()
    src = tmp_path / "folge.mp4"
    src.write_bytes(b"x")
    moved = move_episode_files([src, None, tmp_path / "gibt_es_nicht.jpg"], dst)
    assert len(moved) == 1


def test_unique_path(tmp_path: Path) -> None:
    target = tmp_path / "archiv"
    target.mkdir()
    erste = unique_path(target, "folge.mp4")
    assert erste.name == "folge.mp4"
    erste.write_bytes(b"x")
    zweite = unique_path(target, "folge.mp4")
    assert zweite.name != "folge.mp4"
    assert zweite.suffix == ".mp4"
    assert not zweite.exists()


# ---------------------------------------------------------------------------
# Secret-Store (Token-Sicherheit)
# ---------------------------------------------------------------------------


def test_secret_store_datei_modus(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "credentials", mode="file")
    assert store.effective_mode == "file"
    payload = {"token": "ya29.geheim", "refresh_token": "1//geheim"}
    path = store.save("token", payload)
    assert path.exists()
    if sys.platform != "win32":
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600
    geladen = store.load("token")
    assert geladen == payload
    assert store.exists("token") is True
    geloescht = store.delete("token")
    assert geloescht
    assert store.load("token") is None
    assert store.exists("token") is False


def test_secret_store_auto_modus_fallback(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "credentials", mode="auto")
    store.save("token", {"token": "wert"})
    assert store.load("token") == {"token": "wert"}
    assert store.effective_mode in {"dpapi", "file", "keyring", "plaintext"}


def test_secret_store_ignoriert_kaputten_inhalt(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "credentials", mode="file")
    store.path_for("token").parent.mkdir(parents=True, exist_ok=True)
    store.path_for("token").write_text("{kein json", encoding="utf-8")
    assert store.load("token") is None


def test_secret_store_textmodus(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "credentials", mode="file")
    store.save("notiz", "einfacher text")
    assert store.load_text("notiz") == "einfacher text"


def test_ensure_gitignore_schuetzt_credentials(tmp_path: Path) -> None:
    verzeichnis = tmp_path / "credentials"
    ensure_gitignore(verzeichnis)
    inhalt = (verzeichnis / ".gitignore").read_text(encoding="utf-8")
    assert "*" in inhalt
    # README darf versioniert bleiben
    assert "!README" in inhalt or "!*.md" in inhalt or ".gitignore" in inhalt
    ensure_gitignore(verzeichnis)  # idempotent
    assert (verzeichnis / ".gitignore").read_text(encoding="utf-8").count("*\n") >= 1


def test_info_zeigt_speicherart(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "credentials", mode="file")
    store.save("token", {"a": 1})
    info = store.info("token")
    assert info["exists"] is True
    assert info["mode"] in {"file", "dpapi", "keyring", "plaintext"}
