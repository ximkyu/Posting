#!/usr/bin/env python3
"""End-to-End-Durchlauf mit **simulierter** YouTube-API (kein Netzwerk).

Dieses Skript beweist den kompletten Soll-Ablauf, ohne ein Google-Konto,
ohne credentials.json und ohne echte Uploads::

    READY -> Erkennung -> Stabilitaetspruefung -> Validierung -> Hash/Dublette
          -> Upload (IMMER privat) -> YouTube-ID -> Datenbank -> Dashboard
          -> VERÖFFENTLICHEN (bestaetigt) -> PUBLISHED -> Archiv + upload_result.json

Zusaetzlich werden die beiden kritischen Schutzmechanismen durchgespielt:

* **Neustart/Absturz waehrend des Uploads** -> kein zweiter Upload
* **dieselbe Datei unter anderem Namen** -> Dublette, kein zweiter Upload

Aufruf::

    python tools/e2e_mock_demo.py             # in einem temporaeren Projekt
    python tools/e2e_mock_demo.py --keep      # Projekt behalten (Pfad wird angezeigt)
    python tools/e2e_mock_demo.py --project D:\\temp\\demo

Es wird **niemals** etwas veroeffentlicht, ausser im explizit simulierten
Publish-Schritt dieses Skripts (Mock-Adapter, kein YouTube).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from youtube_autoposter.app import create_app  # noqa: E402
from youtube_autoposter.config import load_settings  # noqa: E402
from youtube_autoposter.constants import PRIVACY_PUBLIC, VideoStatus  # noqa: E402
from youtube_autoposter.core.context import bootstrap  # noqa: E402
from youtube_autoposter.core.recovery import startup_recovery  # noqa: E402
from youtube_autoposter.errors import StateError  # noqa: E402
from youtube_autoposter.platforms.base import (  # noqa: E402
    PlatformAdapter,
    PlatformInfo,
    PublicationJob,
    PublishResult,
    ThumbnailResult,
    UploadResult,
)
from youtube_autoposter.utils.files import now_iso, sha256_file  # noqa: E402

EXAMPLE = ROOT / "examples" / "ready_example"
RESULT_JSON = "upload_result.json"

_erfolge: list[str] = []
_probleme: list[str] = []


# ---------------------------------------------------------------------------
# Ausgabe
# ---------------------------------------------------------------------------


def schritt(nummer: int, gesamt: int, titel: str) -> None:
    print()
    print("=" * 74)
    print(f"  SCHRITT {nummer}/{gesamt}: {titel}")
    print("=" * 74)


def ok(text: str) -> None:
    _erfolge.append(text)
    print(f"  [OK]   {text}")


def info(text: str) -> None:
    print(f"  [..]   {text}")


def problem(text: str) -> None:
    _probleme.append(text)
    print(f"  [!!]   {text}")


def pruefung(bedingung: bool, text: str) -> bool:
    if bedingung:
        ok(text)
    else:
        problem(text)
    return bool(bedingung)


# ---------------------------------------------------------------------------
# Simulierter YouTube-Adapter (ersetzt die echte API)
# ---------------------------------------------------------------------------


class MockYouTube(PlatformAdapter):
    """YouTube-Adapter-Double: tut alles, spricht aber mit niemandem."""

    name = "youtube"
    label = "YouTube (Simulation)"

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.publishes: list[dict[str, Any]] = []
        self.thumbnails: list[dict[str, Any]] = []

    def info(self) -> PlatformInfo:
        return PlatformInfo(
            name=self.name,
            label=self.label,
            available=True,
            message="Simulierte Verbindung (kein Netzwerk, kein echtes Konto)",
            account="demo@gmail.example",
            channel="Demo-Kanal",
            scopes=["https://www.googleapis.com/auth/youtube"],
            quota_today={"units": 51 * len(self.uploads), "uploads": len(self.uploads),
                         "upload_limit_per_day": 100, "calls": {"videos.insert": len(self.uploads)}},
        )

    def is_ready(self) -> tuple[bool, str]:
        return True, "Simulierte Autorisierung vorhanden"

    def upload(self, job: PublicationJob) -> UploadResult:
        """Upload - die Sicherheitsregel gilt auch hier: IMMER privat."""

        zaehler = len(self.uploads) + 1
        video_id = f"demoId{zaehler:05d}"
        total = job.file_size or 1024
        if job.progress_cb:
            for anteil in (0.2, 0.45, 0.7, 0.9, 1.0):
                job.progress_cb(int(total * anteil), total)
                time.sleep(0.01)
        self.uploads.append(
            {
                "episode_id": job.episode_id,
                "video_path": job.video_path,
                "privacy_status": "private",  # hart codiert, wie im echten Adapter
                "attempt": job.attempt,
                "file_hash": job.file_hash,
            }
        )
        return UploadResult(
            platform_video_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            privacy_status="private",
            thumbnail_url=None,
            quota_units=1,
            resumable=True,
            bytes_sent=total,
            raw={"id": video_id, "status": {"privacyStatus": "private"}},
        )

    def set_thumbnail(self, platform_video_id: str, thumbnail_path: str) -> ThumbnailResult:
        vorhanden = bool(thumbnail_path) and Path(thumbnail_path).is_file()
        self.thumbnails.append({"video_id": platform_video_id, "path": thumbnail_path, "ok": vorhanden})
        if not vorhanden:
            return ThumbnailResult(ok=False, message="Keine Thumbnail-Datei vorhanden")
        return ThumbnailResult(
            ok=True,
            url=f"https://i.ytimg.com/vi/{platform_video_id}/hqdefault.jpg",
            quota_units=50,
        )

    def publish(
        self, job: PublicationJob, *, confirm: bool = False, privacy_status: str = PRIVACY_PUBLIC
    ) -> PublishResult:
        if not confirm:
            raise RuntimeError("Veroeffentlichen ohne Bestaetigung ist nicht erlaubt")
        self.publishes.append(
            {"episode_id": job.episode_id, "video_id": job.platform_video_id, "privacy_status": privacy_status}
        )
        return PublishResult(
            platform_video_id=job.platform_video_id or "",
            privacy_status=privacy_status,
            url=f"https://www.youtube.com/watch?v={job.platform_video_id}",
            published_at=now_iso(),
            quota_units=50,
            raw={"id": job.platform_video_id, "status": {"privacyStatus": privacy_status}},
        )

    def fetch_status(self, platform_video_id: str) -> dict[str, Any]:
        return {
            "id": platform_video_id,
            "privacy_status": "private",
            "title": "Demo",
            "url": f"https://www.youtube.com/watch?v={platform_video_id}",
        }

    def find_existing_upload(
        self, *, title: str, since: str | None = None, duration_seconds: float | None = None, limit: int = 25
    ) -> str | None:
        return None

    def validate_online(self, metadata: Any) -> Iterable[str]:
        return []


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def projekt_vorbereiten(ziel: Path) -> Path:
    for name in ("READY", "PROCESSING", "UPLOADED_PRIVATE", "PUBLISHED", "FAILED", "SKIPPED"):
        (ziel / "folders" / name).mkdir(parents=True, exist_ok=True)
    (ziel / "credentials").mkdir(parents=True, exist_ok=True)
    (ziel / "data" / "logs").mkdir(parents=True, exist_ok=True)
    return ziel


def episode_anlegen(ready: Path, episode_id: str, *, titel: str, privacy_wunsch: str = "private") -> dict[str, str]:
    """Beispiel-Episode nach READY kopieren (mp4 + json + jpg)."""

    pfade = {}
    shutil.copy2(EXAMPLE / "example_video.mp4", ready / f"{episode_id}.mp4")
    shutil.copy2(EXAMPLE / "example_video.jpg", ready / f"{episode_id}.jpg")
    payload = json.loads((EXAMPLE / "example_video.json").read_text(encoding="utf-8"))
    payload.update(
        {
            "episode_id": episode_id,
            "title": titel,
            "privacy_status": privacy_wunsch,  # darf den Upload NICHT oeffentlich machen
        }
    )
    (ready / f"{episode_id}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    pfade["video"] = str(ready / f"{episode_id}.mp4")
    pfade["metadata"] = str(ready / f"{episode_id}.json")
    pfade["thumbnail"] = str(ready / f"{episode_id}.jpg")

    # Dateien "aeltern", damit die Stabilitaetspruefung sofort durchlaeuft
    vergangenheit = time.time() - 3600
    for pfad in pfade.values():
        os.utime(pfad, (vergangenheit, vergangenheit))
    return pfade


def einstellungen(projekt: Path):
    settings = load_settings(base_dir=projekt, use_env=False, create_if_missing=True)
    settings.FILE_STABILITY_SECONDS = 1
    settings.STABILITY_CHECK_INTERVAL = 1
    settings.STABILITY_REQUIRED_CHECKS = 1
    settings.RETRY_DELAY = 0.01
    settings.AUTO_UPLOAD = False
    settings.WATCH_ENABLED = False
    settings.OPEN_BROWSER = False
    settings.USE_SYSTEM_KEYRING = False
    settings.TOKEN_STORAGE = "file"
    settings.DEFAULT_PRIVACY_STATUS = "private"
    settings.ENFORCE_PRIVATE_UPLOAD = True
    settings.SEND_PUBLISH_AT = False
    return settings


def ergebnisdatei(archiv: Path, episode_id: str) -> dict[str, Any]:
    pfad = archiv / episode_id / RESULT_JSON
    if not pfad.is_file():
        return {}
    return json.loads(pfad.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End-to-End-Demo mit simulierter YouTube-API")
    parser.add_argument("--project", help="Projektordner (Standard: temporaer)")
    parser.add_argument("--keep", action="store_true", help="Projektordner behalten")
    parser.add_argument("--ffmpeg", help="Pfad zu ffmpeg/ffprobe fuer die technische Pruefung")
    args = parser.parse_args(argv)

    temp = None
    if args.project:
        projekt = projekt_vorbereiten(Path(args.project).expanduser().resolve())
    else:
        temp = tempfile.mkdtemp(prefix="autoposter-e2e-")
        projekt = projekt_vorbereiten(Path(temp))

    gesamt = 9
    print()
    print("#" * 74)
    print("#  YouTube AutoPoster - End-to-End-Durchlauf mit SIMULIERTER YouTube-API")
    print("#  Kein Netzwerk. Kein echtes Konto. Es wird nichts wirklich hochgeladen.")
    print("#" * 74)
    info(f"Projektordner: {projekt}")

    try:
        # ---------------- 1 ----------------
        schritt(1, gesamt, "Dateien in READY ablegen")
        settings = einstellungen(projekt)
        if args.ffmpeg:
            settings.FFMPEG_PATH = args.ffmpeg
            settings.FFPROBE_PATH = args.ffmpeg
        elif os.environ.get("YAP_FFMPEG_PATH"):
            settings.FFMPEG_PATH = os.environ["YAP_FFMPEG_PATH"]
        ready = settings.READY_FOLDER
        pfade = episode_anlegen(ready, "episode_test", titel="Episode Test – Demo-Folge", privacy_wunsch="public")
        for schluessel, pfad in pfade.items():
            pruefung(Path(pfad).is_file(), f"{schluessel}: {Path(pfad).name} liegt in READY")
        info("Hinweis: In der JSON steht bewusst \"privacy_status\": \"public\" - der Upload muss trotzdem privat bleiben.")

        # ---------------- 2 ----------------
        schritt(2, gesamt, "Anwendung starten (Kontext, Datenbank, Mock-Plattform)")
        ctx = bootstrap(settings, console_logging=False, run_recovery=False)
        mock = MockYouTube()
        ctx.registry.register(mock, default=True)
        pruefung(ctx.repository.count_by_status() is not None, "SQLite-Datenbank initialisiert")
        pruefung(mock.is_ready()[0], "Plattform bereit (simulierte Autorisierung)")

        # ---------------- 3 ----------------
        schritt(3, gesamt, "Watch-Folder: Erkennung + Stabilitaetspruefung")
        bericht = ctx.scanner.scan_and_intake()
        info(f"Scan: {bericht}")
        record = ctx.repository.get_by_episode("episode_test")
        pruefung(record is not None, "Episode in der Datenbank erkannt")
        video_id = int(record["id"])
        pruefung(
            record["status"] == VideoStatus.READY.value,
            f"Status nach Erkennung: {record['status']} (wartet auf Upload)",
        )
        info("Der SHA-256-Hash wird erst beim Upload-Versuch berechnet (Quota-/CPU-schonend).")

        # ---------------- 4 ----------------
        schritt(4, gesamt, "Validierung + Upload (muss PRIVAT bleiben)")
        outcome = ctx.pipeline.process(video_id)
        info(f"Ergebnis: {outcome.status} - {outcome.message}")
        for einzel in outcome.steps:
            info(f"Schritt: {einzel}")
        record = ctx.repository.get(video_id)
        pruefung(outcome.success is True, "Upload erfolgreich abgeschlossen")
        pruefung(
            record["status"] == VideoStatus.UPLOADED_PRIVATE.value,
            f"Datenbank-Status: {record['status']}",
        )
        pruefung(bool(record["youtube_video_id"]), f"YouTube-Video-ID: {record['youtube_video_id']}")
        pruefung(
            str(record["youtube_url"]).startswith("https://www.youtube.com/watch?v="),
            f"YouTube-URL: {record['youtube_url']}",
        )
        pruefung(
            record["effective_privacy_status"] == "private",
            f"effective_privacy_status = {record['effective_privacy_status']} (trotz \"public\" in der JSON)",
        )
        gewuenscht_public = record["privacy_status_requested"] == "public"
        pruefung(gewuenscht_public, "Der Wunsch \"public\" wurde nur protokolliert, nicht gesendet")
        pruefung(len(mock.uploads) == 1 and mock.uploads[0]["privacy_status"] == "private",
                 "Mock-Adapter erhielt genau 1 Upload mit privacyStatus=private")
        pruefung(record["progress_percent"] == 100, "Fortschritt: 100 %")
        pruefung(len(record["file_hash"] or "") == 64, f"SHA-256 berechnet: {str(record['file_hash'])[:16]}...")
        pruefung(record["thumbnail_set"] == 1, "Thumbnail gesetzt (nach dem Upload)")

        # ---------------- 5 ----------------
        schritt(5, gesamt, "Archiv + upload_result.json")
        archiv = settings.UPLOADED_PRIVATE_FOLDER / "episode_test"
        dateien = sorted(p.name for p in archiv.iterdir()) if archiv.is_dir() else []
        info(f"{archiv.name}/: {', '.join(dateien)}")
        pruefung(archiv.is_dir(), "Eigener Unterordner im Archiv UPLOADED_PRIVATE/episode_test/")
        pruefung(
            {"episode_test.mp4", "episode_test.json", "episode_test.jpg"} <= set(dateien),
            "Alle Originaldateien archiviert (nichts geloescht)",
        )
        ergebnis = ergebnisdatei(settings.UPLOADED_PRIVATE_FOLDER, "episode_test")
        pruefung(bool(ergebnis), f"{RESULT_JSON} wurde geschrieben")
        pruefung(ergebnis.get("upload_status") == "UPLOADED_PRIVATE", "upload_status = UPLOADED_PRIVATE")
        id_gleich = ergebnis.get("youtube_video_id") == record["youtube_video_id"]
        pruefung(id_gleich, f"youtube_video_id = {ergebnis.get('youtube_video_id')}")
        pruefung(bool(ergebnis.get("uploaded_at")), f"uploaded_at = {ergebnis.get('uploaded_at')}")
        pruefung(ergebnis.get("published_at") is None, "published_at = null (noch nicht veroeffentlicht)")
        pruefung(not list(settings.READY_FOLDER.iterdir()), "READY ist leer (Datei wurde verschoben)")

        # ---------------- 6 ----------------
        schritt(6, gesamt, "Dashboard: Anzeige und VERÖFFENTLICHEN-Button")
        app = create_app(ctx)
        client = app.test_client()
        antwort = client.get("/")
        html = antwort.get_data(as_text=True)
        pruefung(antwort.status_code == 200, "GET / -> HTTP 200 (Template rendert)")
        pruefung("episode_test" in html, "Episode im Dashboard sichtbar")
        knopf_vorhanden = "data-publish" in html and "FFENTLICHEN" in html
        pruefung(knopf_vorhanden, "Button VERÖFFENTLICHEN vorhanden")
        pruefung("publishCancel" in html, "Sicherheitsdialog mit ABBRECHEN vorhanden")
        api = client.get("/api/videos").get_json()
        erste = api["videos"][0]
        pruefung(api["stats"]["private"] == 1, "Statistik: 1 Video privat hochgeladen")
        pruefung(erste["status"] == VideoStatus.UPLOADED_PRIVATE.value, f"API-Status: {erste['status']}")
        pruefung(erste["youtube_video_id"] == record["youtube_video_id"], "API liefert die YouTube-ID")
        pruefung(client.get("/api/status").get_json()["platforms"][0]["available"] is True,
                 "API /api/status meldet die Plattform als bereit")
        info("Wichtig: Dashboard und API kommen ohne YouTube-Aufrufe aus (Quota bleibt unberuehrt).")

        # ---------------- 7 ----------------
        schritt(7, gesamt, "Absturz/Neustart waehrend des Uploads -> kein Doppel-Upload")
        crash_pfade = episode_anlegen(ready, "episode_crash", titel="Crash-Folge")
        ctx.scanner.scan_and_intake()
        crash_id = int(ctx.repository.get_by_episode("episode_crash")["id"])
        # Upload "abreissen lassen": Status UPLOADING, Datei liegt in PROCESSING
        ctx.repository.claim(crash_id, [VideoStatus.READY.value], VideoStatus.UPLOADING.value)
        processing = settings.PROCESSING_FOLDER
        for name, pfad in crash_pfade.items():
            shutil.move(pfad, str(processing / Path(pfad).name))
            ctx.repository.update(crash_id, **{f"{name}_path": str(processing / Path(pfad).name)})
        info(f"Simulierter Absturz: Status={ctx.repository.get(crash_id)['status']}, Dateien in PROCESSING/")

        upload_vorher = len(mock.uploads)
        ctx.stop_background()
        ctx.db.close()

        # Neustart: neuer Kontext, Wiederanlauf laeuft automatisch
        settings2 = einstellungen(projekt)
        ctx2 = bootstrap(settings2, console_logging=False, run_recovery=True)
        ctx2.registry.register(mock, default=True)
        nach = ctx2.repository.get(crash_id)
        pruefung(
            nach["status"] == VideoStatus.INTERRUPTED.value,
            f"Nach Neustart: Status = {nach['status']} (nicht automatisch erneut hochgeladen)",
        )
        pruefung(len(mock.uploads) == upload_vorher, "Kein zusaetzlicher Upload nach dem Neustart")
        pruefung(bool(nach["needs_attention"]), "Episode ist als 'Aktion noetig' markiert")
        recovery_bericht = startup_recovery(ctx2)
        info(f"Recovery-Bericht: {recovery_bericht}")

        # ---------------- 8 ----------------
        schritt(8, gesamt, "Dublette: dieselbe Datei unter anderem Namen")
        quelle = settings.UPLOADED_PRIVATE_FOLDER / "episode_test" / "episode_test.mp4"
        duplikat = ready / "episode_kopie.mp4"
        shutil.copy2(quelle, duplikat)
        # passende JSON dazu
        (ready / "episode_kopie.json").write_text(
            json.dumps({"title": "Kopie", "description": "Identischer Dateiinhalt wie episode_test."}),
            encoding="utf-8",
        )
        vergangenheit = time.time() - 3600
        for pfad in (duplikat, ready / "episode_kopie.json"):
            os.utime(pfad, (vergangenheit, vergangenheit))

        ctx2.scanner.scan_and_intake()
        kopie_id = int(ctx2.repository.get_by_episode("episode_kopie")["id"])
        kopie_hash = sha256_file(duplikat)
        info(f"SHA-256 der Kopie: {kopie_hash[:16]}... (identisch zum Original)")
        outcome2 = ctx2.pipeline.process(kopie_id)
        kopie = ctx2.repository.get(kopie_id)
        pruefung(outcome2.status == VideoStatus.SKIPPED_DUPLICATE.value, f"Status: {outcome2.status}")
        pruefung(kopie["error_code"] == "duplicate", "Fehlercode: duplicate")
        pruefung(len(mock.uploads) == upload_vorher, "Es wurde NICHT erneut hochgeladen")
        verweis_ok = kopie["youtube_video_id"] == record["youtube_video_id"]
        pruefung(verweis_ok, f"Verweis auf das Original: {kopie['youtube_video_id']}")
        pruefung((settings.SKIPPED_FOLDER / "episode_kopie").is_dir(), "Dateien liegen in SKIPPED/episode_kopie/")

        # ---------------- 9 ----------------
        schritt(9, gesamt, "VERÖFFENTLICHEN: nur mit Bestaetigung")
        try:
            ctx2.pipeline.publish(video_id)
            problem("Ohne Bestaetigung wurde veroeffentlicht - Sicherheitsregel verletzt!")
        except StateError as exc:
            ok(f"Ohne Bestaetigung abgelehnt: {exc}")
        pruefung(len(mock.publishes) == 0, "Kein API-Aufruf ohne Bestaetigung")

        outcome3 = ctx2.pipeline.publish(video_id, confirm=True, privacy_status=PRIVACY_PUBLIC)
        record3 = ctx2.repository.get(video_id)
        pruefung(outcome3.success is True, f"Mit Bestaetigung: {outcome3.message}")
        pruefung(record3["status"] == VideoStatus.PUBLISHED.value, f"Status: {record3['status']}")
        pruefung(record3["published_privacy_status"] == PRIVACY_PUBLIC, "published_privacy_status = public")
        pruefung(bool(record3["published_at"]), f"published_at = {record3['published_at']}")
        pruefung(len(mock.publishes) == 1, "Genau 1 Publish-Aufruf")

        try:
            erneut = ctx2.pipeline.publish(video_id, confirm=True, privacy_status=PRIVACY_PUBLIC)
            idempotent = erneut.success is False and len(mock.publishes) == 1
            hinweis = erneut.message
        except StateError as exc:
            idempotent = len(mock.publishes) == 1
            hinweis = str(exc)
        pruefung(idempotent, f"Zweiter Klick: abgelehnt (idempotent) - {hinweis}")
        pruefung(len(mock.publishes) == 1, "Insgesamt genau 1 Publish-Aufruf")

        veroeffentlicht = settings.PUBLISHED_FOLDER / "episode_test"
        dateien_pub = sorted(p.name for p in veroeffentlicht.iterdir()) if veroeffentlicht.is_dir() else []
        archiv_vollstaendig = {"episode_test.mp4", "episode_test.json", "episode_test.jpg", RESULT_JSON} <= set(dateien_pub)
        pruefung(archiv_vollstaendig, f"Archiv PUBLISHED/episode_test/: {', '.join(dateien_pub)}")
        ergebnis_pub = ergebnisdatei(settings.PUBLISHED_FOLDER, "episode_test")
        pruefung(ergebnis_pub.get("upload_status") == "PUBLISHED", "upload_result.json: upload_status = PUBLISHED")
        pruefung(bool(ergebnis_pub.get("published_at")), f"upload_result.json: published_at = {ergebnis_pub.get('published_at')}")
        pruefung(not list(settings.UPLOADED_PRIVATE_FOLDER.iterdir()), "UPLOADED_PRIVATE/ ist aufgeraeumt")

        client2 = create_app(ctx2).test_client()
        html2 = client2.get("/").get_data(as_text=True)
        anzeige_ok = "FFENTLICHT" in html2
        pruefung(anzeige_ok, "Dashboard zeigt VERÖFFENTLICHT ✓ und keinen VERÖFFENTLICHEN-Button mehr")

        # ---------------- Zusammenfassung ----------------
        print()
        print("#" * 74)
        print("#  ZUSAMMENFASSUNG")
        print("#" * 74)
        for text in _erfolge:
            print(f"  [OK] {text}")
        if _probleme:
            print()
            for text in _probleme:
                print(f"  [!!] {text}")
        print()
        print(f"  Pruefungen bestanden : {len(_erfolge)}")
        print(f"  Probleme             : {len(_probleme)}")
        print(f"  Simulierte Uploads   : {len(mock.uploads)} (alle privacyStatus=private)")
        print(f"  Simulierte Publishes : {len(mock.publishes)}")
        if args.keep or args.project:
            print(f"  Projektordner        : {projekt}")
        else:
            print("  Projektordner        : temporaer (wird entfernt, --keep zum Behalten)")
        print("#" * 74)

        ctx2.stop_background()
        ctx2.db.close()
        return 0 if not _probleme else 1
    finally:
        if temp and not args.keep:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
