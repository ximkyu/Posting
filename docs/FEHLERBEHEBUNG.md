# Fehlerbehebung & Betrieb

Alles, was im Alltag schiefgehen kann – mit Ursache und Lösung. Die App schreibt
jede Meldung zusätzlich nach `data\logs\app.log` (rollierend, 5 × 5 MB) und in die
Datenbank (Dashboard-Seite **Protokoll**).

**Schnelldiagnose in einem Befehl:**

```bat
python app.py --dry-run
```

Der Bericht prüft Python, Module, `config.json`, Ordner, Datenbank, `credentials.json`,
OAuth-Status, ffprobe/ffmpeg, freien Port und jede Episode im READY-Ordner – und lädt
**nichts** hoch.

---

## 1. Wo steht was?

| Ort | Inhalt |
|---|---|
| `data\logs\app.log` | ausführliches Protokoll (Tokens/Secrets werden automatisch geschwärzt) |
| Dashboard → **Protokoll** | dieselben Zeilen, filterbar nach Level/Episode |
| `data\youtube_autoposter.db` | Status, YouTube-IDs, Upload-Versuche, Quota-Zähler |
| `folders\<STATUS>\<episode_id>\upload_result.json` | Ergebnis pro Episode (menschenlesbar) |
| `folders\FAILED\<episode_id>\` | isolierte Dateien fehlerhafter Episoden (nichts gelöscht) |
| Konsole von `start.bat` | Live-Ausgabe, Beenden mit `STRG+C` |

Protokoll live mitlesen:

```bat
python app.py logs --follow
python app.py logs --limit 200
```

## 2. Installation & Start

| Symptom | Ursache | Lösung |
|---|---|---|
| `python` wird nicht gefunden | Python fehlt oder nicht im `PATH` | Python 3.11+ installieren, **„Add python.exe to PATH"** anhaken; alternativ `py -3` statt `python` verwenden |
| `setup.bat` bricht ab | kein `py`/`python` im PATH | Python installieren, Fenster neu öffnen |
| `pip install` schlägt fehl | Proxy/Firewall oder veraltetes pip | `python -m pip install --upgrade pip`; Firmennetz: `pip install --proxy http://…` |
| `ModuleNotFoundError: youtube_autoposter` | im falschen Ordner gestartet | immer aus dem Projektordner starten (`cd C:\YouTubeAutoPoster`) oder `start.bat` nutzen |
| `Web-Server konnte nicht starten … Port 8765 belegt` | andere Anwendung nutzt den Port | `python app.py --port 9000` oder `PORT` in `config.json`; belegt oft: andere AutoPoster-Instanz → `start_hidden.bat`-Fenster schließen |
| Dashboard öffnet sich nicht automatisch | Standardbrowser blockiert | Adresse selbst öffnen: `http://127.0.0.1:8765` (steht immer in der Konsole) |
| Seite lädt, aber „Verbindung fehlt" | `credentials.json` fehlt oder OAuth nicht durchgeführt | Abschnitt 3 |
| Zugriff von einem anderen Rechner funktioniert nicht | bewusst: `HOST=127.0.0.1` | nur lokal nutzen. Für LAN: `HOST=0.0.0.0` (**Warnung**: jeder im Netz könnte veröffentlichen) |
| Antivirus blockiert `python.exe` | Signatur-/Heuristik-Problem | Projektordner und `.venv` als Ausnahme eintragen |

## 3. Google Cloud / OAuth

| Meldung | Ursache | Lösung |
|---|---|---|
| `credentials.json fehlt: …\credentials\credentials.json` | Datei nicht abgelegt | Google-Cloud-JSON herunterladen, umbenennen, ablegen (siehe `docs\GOOGLE-CLOUD-SETUP.md`) |
| `credentials.json enthält weder 'installed' noch 'web'` | falscher Client-Typ (z. B. API-Key oder Dienstkonto) | neuen OAuth-Client vom Typ **Desktop-App** erstellen |
| `Error 403: access_denied` im Browser | OAuth-Status *Testing*, Konto fehlt bei den Testnutzern | Google Auth Platform → **Audience → Test users** → eigene Adresse hinzufügen |
| `redirect_uri_mismatch` | Web-Client statt Desktop-Client | Redirect-URI `http://127.0.0.1:8765/auth/callback` exakt eintragen oder Desktop-Client verwenden |
| `accessNotConfigured` / `403 The request is missing a valid API key` | YouTube Data API v3 nicht aktiviert | APIs & Dienste → Bibliothek → **YouTube Data API v3 → Aktivieren** |
| `invalid_grant` | Token widerrufen, Passwort geändert oder Uhrzeit falsch | `python app.py auth logout` → erneut verbinden; Systemuhr prüfen |
| `YouTube-Autorisierung ist ungültig oder abgelaufen` / `NEEDS_REAUTH` | Refresh fehlgeschlagen | Dashboard → **Erneut verbinden**; bei Markenkonten das verwaltende Konto wählen |
| `Insufficient Permission` / `insufficientPermissions` | Scope fehlt im gespeicherten Token | `auth logout` → erneut verbinden (das Tool fordert nur `https://www.googleapis.com/auth/youtube`) |
| Login-Browser bleibt leer stehen | Loopback-Port blockiert (Firewall/Proxy) | `python app.py auth login` (nutzt einen eigenen lokalen Port) oder Firewall-Ausnahme für Python |
| Verbindung funktioniert, Kanalname fehlt | `FETCH_CHANNEL_INFO=false` oder Rechte fehlen | `FETCH_CHANNEL_INFO=true`; **Verbindung testen** drücken (1 Quota-Einheit) |

**Zustände im Dashboard** (`/api/status` → `auth.state`):
`MISSING_CREDENTIALS_FILE` → `INVALID_CREDENTIALS_FILE` → `NOT_CONNECTED` →
`NEEDS_REAUTH` → `EXPIRED` (Token vorhanden, Erneuerung nötig) → `CONNECTED`.

## 4. Upload

| Meldung / Verhalten | Ursache | Lösung |
|---|---|---|
| Episode bleibt `PAUSIERT`, Hinweis „credentials.json fehlt" | keine Autorisierung | Abschnitt 3 |
| `PAUSIERT` mit „Kontingent erschöpft" (`quotaExceeded`, `dailyLimitExceeded`) | 10.000 Einheiten bzw. 100 Uploads/Tag erreicht | warten bis zum Reset (00:00 Pacific Time); die Episode läuft automatisch weiter, es wird **nicht** sofort erneut versucht |
| `PAUSIERT` nach Auth-Fehler | Token ungültig | erneut verbinden; danach **FORTSETZEN** drücken |
| `FEHLER` mit `upload_failed` nach 3 Versuchen | dauerhafter API-Fehler (oft 400) | Meldung im Detail ansehen (`/videos/<id>`), JSON prüfen, dann **Erneut versuchen** |
| `forbiddenPrivacySetting` / „private viewing mode" | nicht auditiertes Projekt (nach 28.07.2020) | Audit-Formular: <https://support.google.com/youtube/contact/yt_api_form>. Upload bleibt privat, es geht nichts verloren |
| `video_too_large` (> 256 GB) / `video_too_long` (> 12 h) | YouTube-Limit | Datei kürzen/komprimieren |
| `video_unsupported_format` | z. B. `.mov`/`.mkv` | als `.mp4` exportieren oder `ALLOW_EXTRA_VIDEO_EXTENSIONS: true` setzen |
| Upload hängt lange ohne Fortschritt | sehr große Datei/langsame Leitung | `UPLOAD_TIMEOUT_SECONDS`, `UPLOAD_CHUNK_SIZE` und `PROGRESS_LOG_SECONDS` anpassen; Fortschritt steht im Protokoll |
| `FEHLER` sofort, `metadata_invalid` | JSON fehlerhaft (Syntax, Pflichtfeld, Limits) | Meldung lesen, JSON in `FAILED\<episode>\` korrigieren, dann **Erneut versuchen** |
| Episode wird nicht erkannt | falscher Ordner, falsche Endung, Datei noch nicht fertig | `python app.py scan`; `ALLOWED_VIDEO_EXTENSIONS`; Ordner `folders\READY` verwenden |
| „wartet (Datei wird noch geschrieben)" | Kopie läuft | normal; `FILE_STABILITY_SECONDS` abwarten (bei riesigen Dateien erhöhen) |

**Wiederholungen:** `MAX_RETRIES=3`, `RETRY_DELAY=5`, `RETRY_BACKOFF_FACTOR=2`,
`RETRY_MAX_DELAY=120` → Versuche nach 5 s, 10 s, 20 s. Nur als temporär eingestufte
Fehler (5xx, `backendError`, Netzwerkabbrüche) werden wiederholt; 400er-Fehler,
Quota- und Auth-Fehler nicht.

## 5. Thumbnail

| Meldung | Ursache | Lösung |
|---|---|---|
| `Thumbnail-Fehler (Upload bleibt gültig): 403 forbidden` | YouTube-Konto nicht verifiziert | Telefonnummer im Kanal bestätigen (YouTube Studio → Einstellungen → Kanal → Funktionsumfang) |
| `thumbnail_too_large` | Bild > 2 MB | als JPG/PNG unter 2 MB speichern |
| `thumbnail_unsupported_format` | z. B. `.webp`, `.gif` | in `.jpg`/`.jpeg`/`.png` umwandeln |
| Bild wird nicht angezeigt, obwohl gesetzt | YouTube verarbeitet Thumbnails verzögert | Dashboard neu laden; `USE_REMOTE_THUMBNAIL_FALLBACK=true` zeigt dann die YouTube-Variante |
| Thumbnail fehlt komplett | Datei nicht abgelegt | Upload läuft trotzdem; Datei später ergänzen und im Detail auf **Thumbnail erneut setzen** klicken |

Das Ergebnis steht sauber getrennt in der Datenbank und in `upload_result.json`:
`upload_status: UPLOADED_PRIVATE` **und** `thumbnail.set: false` + `thumbnail.error`.

## 6. Veröffentlichen

| Verhalten | Ursache | Lösung |
|---|---|---|
| Button **VERÖFFENTLICHEN** fehlt | Status nicht `UPLOADED_PRIVATE`/`PUBLISH_READY` (z. B. `PAUSIERT`, `INTERRUPTED`, `READY`) | Episode erst hochladen bzw. Fehler beheben |
| Dialog erscheint, aber nichts passiert | CSRF-Sitzung abgelaufen (Seite lange offen) | Seite neu laden (F5) und erneut klicken |
| `Status hat sich geändert` | jemand/etwas hat die Episode inzwischen verändert | Dashboard neu laden, Status prüfen |
| „bereits veröffentlicht" | doppeltes Klicken / Wiederholung | gewollt: ein Video wird nur einmal veröffentlicht |
| Veröffentlichung schlägt fehl (`forbiddenPrivacySetting`) | API-Audit fehlt | <https://support.google.com/youtube/contact/yt_api_form>; Video bleibt **privat**, `needs_attention` wird gesetzt |
| Nach Fehlversuch steht das Video auf `UPLOADED_PRIVATE` | bewusst: kein halber Zustand | Fehlermeldung lesen, dann erneut veröffentlichen |
| Dry-Run-Modus blockiert alles | `--dry-run` aktiv | ohne `--dry-run` starten |

## 7. Neustart, Absturz, Doppel-Upload

| Situation | Verhalten des Systems | Was du tun musst |
|---|---|---|
| Absturz während `UPLOADING` | Status → `INTERRUPTED`, **kein** automatischer Re-Upload | **„Auf YouTube prüfen"** (sucht das Video über den Kanal, 3 Einheiten) oder **„Erneut hochladen"**, wenn sicher nichts ankam |
| Absturz während `PUBLISHING` | Status → `UPLOADED_PRIVATE` (Video bleibt privat) | bei Bedarf erneut veröffentlichen |
| Absturz während `VALIDATING` | Status → `READY` | nichts – läuft beim nächsten Start weiter |
| Dateien fehlen (extern verschoben/gelöscht) | Status → `FAILED` mit `file_missing` | Dateien erneut nach `READY\` legen oder Episode verwerfen |
| Dateien ohne DB-Bezug in `PROCESSING\` | werden **gemeldet**, nie gelöscht | bei Bedarf manuell nach `READY\` verschieben |
| Gleiche Datei unter anderem Namen | `SKIPPED_DUPLICATE` (SHA-256), Verweis auf das Original | nichts |
| Gleiche Episode-ID erneut | `SKIPPED_DUPLICATE` | nichts |
| Wiederanlauf deaktivieren | `MARK_INTERRUPTED_ON_STARTUP=false` | nicht empfohlen |

Manuell nachholen:

```bat
python app.py recover episode_001    &:: unterbrochenen Upload auf YouTube suchen
python app.py retry episode_001      &:: fehlgeschlagene Episode neu einreihen
python app.py reset --yes            &:: alle FEHLER/UNTERBROCHEN zurück in die Queue
```

## 8. FFmpeg / ffprobe

| Meldung | Bedeutung |
|---|---|
| `ffprobe/ffmpeg nicht gefunden` | technische Prüfung wird übersprungen (Warnung). Der Upload funktioniert trotzdem |
| `Technische Prüfung nicht möglich … REQUIRE_FFPROBE=false` | dito – Metadaten wurden geprüft, Videodaten nicht |
| `video_probe_skipped` | Warncode in `validation_json`, unkritisch |

Lösung: FFmpeg installieren (<https://ffmpeg.org/download.html>) und `bin` in den `PATH`
aufnehmen **oder** in `config.json` setzen:

```json
{
  "FFPROBE_PATH": "C:\\ffmpeg\\bin\\ffprobe.exe",
  "FFMPEG_PATH": "C:\\ffmpeg\\bin\\ffmpeg.exe"
}
```

Fehlt `ffprobe`, wertet das Tool automatisch `ffmpeg -i` aus (Fallback). Nur wenn
beides fehlt, entfällt die Prüfung. Mit `REQUIRE_FFPROBE: true` wird sie zur Pflicht
(dann kein Upload ohne Prüfung). Der Upload selbst hängt **nie** von FFmpeg ab.

## 9. Datenbank & Sicherung

| Aufgabe | Vorgehen |
|---|---|
| Sicherung | App beenden, dann `data\youtube_autoposter.db` und `config.json` kopieren |
| Wiederherstellen | beide Dateien zurückkopieren (Pfade in `config.json` sind relativ → projektportabel) |
| Datenbank defekt | `python app.py init` legt sie neu an; die Ordner bleiben unverändert, ein Scan übernimmt alle Dateien erneut |
| Protokoll zu groß | `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` anpassen; `LOG_KEEP_ROWS` begrenzt die DB-Zeilen |
| Statistik zurücksetzen | `python app.py reset --yes` (nur Status, keine Dateien) |
| Neustart „bei Null" | App beenden, `data\youtube_autoposter.db` löschen, `python app.py init`, `JETZT SCANNEN` – **Achtung:** YouTube-IDs gehen verloren, Dublettenerkennung per Hash bleibt nur erhalten, wenn die Dateien noch da sind |

`python app.py status` zeigt Integrität, Tabellenstände, Verbindung, Quota und die
letzten Episoden – ohne einen einzigen YouTube-Aufruf.

## 10. Wenn nichts mehr hilft

```bat
python app.py --dry-run                 &:: 1. Umgebung + Episoden prüfen
python app.py auth status               &:: 2. OAuth-Zustand
python app.py status                    &:: 3. Datenbank + Quota
python app.py logs --limit 100          &:: 4. letzte Meldungen
python app.py validate                  &:: 5. Metadaten im Detail
```

Danach: App neu starten (`start.bat`), bei OAuth-Problemen `auth logout` + neu
verbinden, und im Zweifel die betroffene Episode mit `retry` neu einreihen.
Dateien gehen dabei nie verloren – sie werden ausschließlich verschoben.
