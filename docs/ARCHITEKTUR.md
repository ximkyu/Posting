# Architektur & Erweiterbarkeit

Ziel der Struktur: **einfach halten, aber austauschbar**. Alles, was YouTube-spezifisch
ist, steckt hinter einem Adapter-Interface; Scanner, Datenbank, Pipeline, CLI und
Dashboard kennen die Plattform nur über dieses Interface.

---

## 1. Schichten

```
┌───────────────────────────────────────────────────────────────────┐
│  Einstieg: app.py (Shim) → youtube_autoposter/cli.py              │
│            setup.bat / start.bat / start.sh / run_tests.bat       │
├───────────────────────────────────────────────────────────────────┤
│  Oberfläche                                                       │
│   ui/routes.py  + templates/ + static/  (Flask, 127.0.0.1:8765)   │
│   cli.py        (alle Funktionen auch ohne Browser)               │
├───────────────────────────────────────────────────────────────────┤
│  Steuerung                                                        │
│   scheduler/watcher.py  FolderWatcher (watchdog/Polling)          │
│   scheduler/watcher.py  UploadWorker (seriell, 1 Upload)          │
│   core/pipeline.py      UploadPipeline (der eigentliche Ablauf)   │
│   core/recovery.py      Wiederanlauf nach Absturz/Neustart        │
│   core/dry_run.py       Trockenlauf-Bericht (keine API-Aufrufe)   │
├───────────────────────────────────────────────────────────────────┤
│  Fachlogik                                                        │
│   scanner/folder_scanner.py  READY-Erkennung + Stabilität         │
│   metadata/parser.py         JSON → Upload-Body (erzwingt private)│
│   utils/validation.py        Limits (Titel, Tags, Dauer, Größe)   │
│   utils/media.py             ffprobe / ffmpeg -i                  │
│   utils/files.py             SHA-256, atomares Verschieben        │
│   auth/youtube_auth.py       OAuth-Flow, Token, Status            │
├───────────────────────────────────────────────────────────────────┤
│  Plattform-Adapter                                                │
│   platforms/base.py      PlatformAdapter (Protocol) + Resultate   │
│   platforms/registry.py  Registrierung, Default-Plattform         │
│   platforms/youtube/     api.py (HTTP/Quota/Retries),             │
│                          uploader.py (resumable), publisher.py    │
├───────────────────────────────────────────────────────────────────┤
│  Persistenz & Querschnitt                                         │
│   database/db.py          Schema, Migration, WAL                  │
│   database/repository.py  alle Zugriffe (VideoRepository)         │
│   config.py               Settings + harte Sicherheitsregeln      │
│   constants.py            Status, Limits, Quota-Tabelle           │
│   errors.py               Fehler-Hierarchie                       │
│   utils/secure_store.py   DPAPI / Datei 0600 für das Token        │
│   utils/logging_utils.py  rotierende Logs + Schwärzung            │
└───────────────────────────────────────────────────────────────────┘
```

## 2. Datenfluss einer Episode

```
READY/*.mp4+*.json+*.jpg
   │  FolderWatcher (watchdog oder SCAN_INTERVAL_SECONDS)
   ▼
FolderScanner.scan()          Stabilität, Paarbildung, Dubletten-Vorfilter
   │  upsert_episode()
   ▼
SQLite (videos.status = NEW/READY)
   │  UploadWorker.process_next()   ← seriell, MAX_CONCURRENT_UPLOADS = 1
   ▼
UploadPipeline.process(record)
   1. claim() atomar → VALIDATING       (kein zweiter Worker kommt dazwischen)
   2. Adapter.is_ready()                sonst PAUSED
   3. Stabilitätsprüfung (vor dem Upload)
   4. SHA-256 + Dublettensuche          → SKIPPED_DUPLICATE, Dateien nach SKIPPED/
   5. Metadaten parsen + validieren     → FAILED bei Fehlern
   6. Dateien → PROCESSING/
   7. Adapter.upload(job)               resumable, Chunks, Backoff, Quota-Tracking
   8. Adapter.set_thumbnail(...)        unkritisch bei Fehlern
   9. status = UPLOADED_PRIVATE, effective_privacy_status = private
  10. Dateien → UPLOADED_PRIVATE/<episode_id>/ + upload_result.json
   ▼
Mensch: Button VERÖFFENTLICHEN + Bestätigung (CSRF, erwarteter Status)
   ▼
UploadPipeline.publish(record.id, confirm=True, privacy_status="public")
   → videos.update(part=status) → Prüfen der Antwort → PUBLISHED
   → Dateien nach PUBLISHED/<episode_id>/, upload_result.json wird aktualisiert
```

**Fehlerpfade:** `PlatformError`-Klassifizierung in `platforms/youtube/api.py`
(`classify_error`):

| Fehler | Klasse | Reaktion der Pipeline |
|---|---|---|
| `quotaExceeded`, `dailyLimitExceeded`, `userRateLimitExceeded` | `QuotaExceededError` | **PAUSIERT**, `needs_attention`, Dateien zurück nach `READY/`, kein sofortiger Retry |
| 401/403 mit Auth-Grund, `RefreshError` | `AuthError` | **PAUSIERT** (erneut verbinden), kein sinnloses Wiederholen |
| `insufficientPermissions` + Scope-Hinweis | `InsufficientScopeError` | **PAUSIERT** mit Hinweis auf die Rechte |
| `forbiddenPrivacySetting`, private Sperre | `PrivateLockError` | **FEHLER**/`needs_attention` mit Audit-Link, Video bleibt privat |
| 500/503, `backendError`, Netzwerkabbruch | `UploadError(retriable=True)` | Retry mit Backoff, solange `MAX_RETRIES` reicht; sonst **FEHLER** |
| 400 (`invalidParameter`, `missingRequiredParameter`) | `UploadError(retriable=False)` | sofort **FEHLER** |

## 3. Status-Übergänge

```
NEW ──► READY ──► VALIDATING ──► UPLOADING ──► UPLOADED_PRIVATE ──► PUBLISHING ──► PUBLISHED
              │        │              │                ▲                   │
              │        │              │                └───────────────────┘ (Fehler beim Publizieren)
              │        └──────────────┴──► FAILED  (Dateien nach FAILED/, Retry möglich)
              │        └──► READY      (retriable-Fehler: neuer Versuch mit Backoff)
              └──► SKIPPED_DUPLICATE (Dateien nach SKIPPED/)
   PAUSED ◄── Quota / Autorisierung / explizite Pause
   INTERRUPTED ◄── Neustart während UPLOADING (bewusst kein Auto-Re-Upload)
```

Reihenfolge der Regeln: **kein Doppel-Upload** > **nie ungewollt öffentlich** >
**Zuverlässigkeit** > Einfachheit.

## 4. Datenbank (`data/youtube_autoposter.db`, SQLite + WAL)

| Tabelle | Zweck | Wichtigste Spalten |
|---|---|---|
| `videos` | eine Zeile pro Episode | `episode_id` (UNIQUE), `platform`, `video_path`, `file_hash`, `file_size`, `status`, `youtube_video_id`, `youtube_url`, `effective_privacy_status`, `privacy_status_requested`, `publish_at_requested`, `retry_count`, `progress_percent`, `error_code`, `error_message`, `needs_attention`, Zeitstempel |
| `uploads` | ein Versuch pro Upload | `video_id`, `platform`, `attempt`, `phase`, `status`, `bytes_sent`, `quota_units`, `http_status`, `error_message` |
| `logs` | Protokoll für das Dashboard | `ts`, `level`, `episode_id`, `action`, `message` (begrenzt durch `LOG_KEEP_ROWS`) |
| `settings` | Laufzeit-Zustand | `worker.paused`, `recovery.last_run_at`, `quota.<datum>`, Kategorie-Cache |

Schema-Änderungen laufen über `database/db.py` (Migration bei vorhandener Datenbank,
keine manuelle Pflege nötig). Alle Schreibzugriffe laufen über
`database/repository.py` – dort sitzen auch `claim()` (atomares Reservieren) und
`find_by_hash()` / `find_uploaded_by_episode()` (Dublettenschutz).

## 5. Sicherheitsregeln im Code (mehrfach abgesichert)

1. `metadata/parser.py::build_upload_body(force_private=True)` setzt
   `status.privacyStatus = "private"` – unabhängig von der JSON.
2. `platforms/youtube/uploader.py` prüft den fertig gebauten Body **erneut** kurz vor
   dem Absenden und überschreibt `status.privacyStatus` mit `ENFORCED_UPLOAD_PRIVACY`
   (`"private"`), falls dort etwas anderes stünde.
3. `core/pipeline.py::publish()` verlangt `confirm=True`, den korrekten Ausgangsstatus
   und `dry_run == False`; sonst `StateError`.
4. `ui/routes.py` verlangt CSRF-Token, `confirm=yes` und `expected_status`; der
   Bestätigungsdialog hat den Fokus auf **ABBRECHEN**.
5. `config.py` erzwingt `DEFAULT_PRIVACY_STATUS = private` und
   `ENFORCE_PRIVATE_UPLOAD = true`, selbst wenn in `config.json` etwas anderes steht.
6. `SEND_PUBLISH_AT = false` **und** die `force_private`-Bedingung im Parser: ein
   `publishAt` wird in V1 unter keinen Umständen gesendet.

## 6. Plattform-Adapter (`platforms/base.py`)

```python
class PlatformAdapter(Protocol):
    name: str

    def info(self) -> PlatformInfo: ...
    def is_ready(self) -> tuple[bool, str]: ...
    def upload(self, job: PublicationJob) -> UploadResult: ...
    def set_thumbnail(self, platform_video_id: str, thumbnail_path: str) -> ThumbnailResult: ...
    def publish(self, job: PublicationJob, *, confirm: bool, privacy_status: str) -> PublishResult: ...
    def fetch_status(self, platform_video_id: str) -> dict: ...
    def find_existing_upload(self, *, title: str, since: datetime | None,
                             duration_seconds: float | None, limit: int = 10) -> str | None: ...
    def validate_online(self, metadata: EpisodeMetadata) -> list[str]: ...
```

Datenaustausch über kleine Wertobjekte: `PublicationJob` (`video_id`, `episode_id`,
`platform`, `video_path`, `metadata: EpisodeMetadata`, `thumbnail_path`, `file_size`,
`file_hash`, `platform_video_id`, `progress_cb`, `options` – darin `attempt`) sowie
`UploadResult`, `PublishResult`, `ThumbnailResult` mit `success`, `message`,
`error_code` und Plattform-IDs/URLs.

**Neue Plattform anbinden (z. B. Instagram/Meta, TikTok, Spotify):**

1. Paket `youtube_autoposter/platforms/<name>/` mit `__init__.py` und `adapter.py`
   anlegen; Klasse `class <Name>Adapter:` implementiert die Methoden oben.
2. In `platforms/registry.py::build_registry()` registrieren – dort liegen bereits
   auskommentierte Erweiterungsplätze (`MetaPlatform`, `TikTokPlatform`, `SpotifyPlatform`).
   Für Tests/CLI gibt es zusätzlich `get_adapter(name, **kwargs)`.
3. Fertig – Scanner, Pipeline, DB, CLI und Dashboard brauchen **keine** Änderung,
   weil `videos.platform` die Zuordnung trägt.
4. UI: Button/Seite optional ergänzen; die Pipeline-Logik bleibt identisch.
5. Eigene Sicherheitsregeln (z. B. „immer privat/Entwurf") gehören in
   `build_*_body` bzw. `_guard_*` der neuen Plattform, analog zu YouTube.

## 7. Nebenläufigkeit

- **Ein** Upload zur Zeit (`MAX_CONCURRENT_UPLOADS = 1`): weniger Risiko, weniger
  Quota-Druck, klare Logs.
- Watcher und Worker laufen als Daemon-Threads; die Flask-App (Waitress) bleibt
  bedienbar, während hochgeladen wird.
- `claim()` + `reset_running_to_interrupted()` sorgen dafür, dass nach einem Absturz
  keine Zeile im Zustand „läuft" hängen bleibt.
- Explizite Pause (`settings.worker.paused`) blockiert auch den Button
  **UPLOAD AUSFÜHREN** – mit Hinweis im Dashboard.

## 8. Testbarkeit

Alle externen Abhängigkeiten sind austauschbar:

- `tests/fakes.py`: `FakeYouTubeService` (skriptbare Antworten, resumable Fortschritt),
  `FakeVideosResource`, `FakePlatform` (zeichnet Aufrufe auf).
- `pipeline._sleep` ist injizierbar → Backoff-Tests laufen in Millisekunden.
- ffprobe/ffmpeg werden per Stub getestet (inkl. `ffmpeg -i`-Fallback).
- `tests/conftest.py` baut je Test ein **eigenes** Projektverzeichnis mit eigener
  `config.json` (`BASE_DIR`-Verankerung), eigener Datenbank und eigenen Ordnern:
  Die Tests fassen niemals das echte `folders\` oder `data\` an.
- 362 Tests, komplett offline (`python -m pytest -q`, ca. 14 s).
- `tools/e2e_mock_demo.py` spielt denselben Ablauf als nachvollziehbares Skript durch
  (READY → Upload privat → Dashboard → Absturz-Wiederanlauf → Dublette → Veröffentlichen
  → Archiv + `upload_result.json`) und beendet sich mit Exit-Code 0, wenn alle 55
  Prüfungen bestanden sind.
