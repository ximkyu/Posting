# YouTube AutoPoster (V1)

Ein **lokales** Werkzeug für Windows, das fertige Episoden aus einem `READY/`-Ordner
über die **offizielle YouTube Data API v3** mit **OAuth 2.0** hochlädt – **immer als
`private`** – und sie erst dann öffentlich schaltet, wenn du im Dashboard ausdrücklich
auf **VERÖFFENTLICHEN** klickst und den Sicherheitsdialog bestätigst.

```
READY/video_001.mp4  ┐
READY/video_001.json ├─► prüfen ─► hochladen (PRIVAT) ─► UPLOADED_PRIVATE/
READY/video_001.jpg  ┘                                        │
                                          Klick auf VERÖFFENTLICHEN + Bestätigung
                                                              ▼
                                                        PUBLISHED/
```

**Sicherheitsregeln (fest eingebaut, nicht abschaltbar):**

1. Jeder Upload erfolgt mit `privacyStatus: private` – auch wenn in der JSON-Datei
   `"public"` oder `"unlisted"` steht. Der Wert wird nur als *Wunsch* protokolliert
   und im Dashboard als Warnung angezeigt.
2. Öffentlich wird ein Video **ausschließlich** über den Button **VERÖFFENTLICHEN**
   im Web-Dashboard (oder `python app.py publish <episode>`), immer mit
   Bestätigungsdialog. Der Standardfokus des Dialogs liegt auf **ABBRECHEN**.
3. `publish_at` aus der JSON wird **niemals** an YouTube gesendet → keine automatische
   Terminveröffentlichung.
4. Es wird **kein** Google-Passwort gespeichert und **kein** Access-Token im Klartext
   in `config.json` abgelegt. Das OAuth-Token liegt unter Windows per DPAPI geschützt
   in `credentials/token.json` (sonst als Datei mit Rechten `0600`).
5. Es gibt **keinen Doppel-Upload**: Deduplizierung über `episode_id` **und** SHA-256
   der Videodatei, plus Neustart-Wiederanlauf (unterbrochene Uploads werden als
   `UNTERBROCHEN` markiert und **nicht** automatisch erneut gesendet).
6. Ausschließlich offizielle API. Kein Scraping, keine Browser-Automation
   (kein Selenium/Playwright), keine inoffiziellen Endpunkte.
7. Originale werden niemals gelöscht – Dateien werden nur zwischen den
   Status-Ordnern verschoben.

---

## Inhalt

- [1. Voraussetzungen](#1-voraussetzungen)
- [2. Installation](#2-installation)
- [3. Google Cloud einrichten (einmalig, ~10 Minuten)](#3-google-cloud-einrichten-einmalig-10-minuten)
- [4. YouTube-Konto verbinden (OAuth)](#4-youtube-konto-verbinden-oauth)
- [5. Der READY-Ordner](#5-der-ready-ordner)
- [6. JSON-Format](#6-json-format)
- [7. Upload starten](#7-upload-starten)
- [8. Veröffentlichen](#8-veröffentlichen)
- [9. Dry-Run (Trockenlauf)](#9-dry-run-trockenlauf)
- [10. Ordner & Status-Ablauf](#10-ordner--status-ablauf)
- [11. Befehlszeile (CLI)](#11-befehlszeile-cli)
- [12. Konfiguration](#12-konfiguration)
- [13. Quota-Verbrauch](#13-quota-verbrauch)
- [14. Tests](#14-tests)
- [15. Fehlerbehebung](#15-fehlerbehebung)
- [16. Projektstruktur](#16-projektstruktur)
- [17. Datenbank, Ergebnisdateien & Sicherung](#17-datenbank-ergebnisdateien--sicherung)
- [18. Sicherheit & Datenschutz](#18-sicherheit--datenschutz)
- [19. Erweiterbarkeit (weitere Plattformen)](#19-erweiterbarkeit-weitere-plattformen)
- [20. Grenzen von V1](#20-grenzen-von-v1)
- [Weitere Dokumente](#weitere-dokumente)

---

## 1. Voraussetzungen

| Baustein | Anforderung | Hinweis |
|---|---|---|
| Betriebssystem | Windows 10/11 (64 Bit) | Linux/macOS funktionieren ebenfalls (`setup.sh`/`start.sh`) |
| Python | **3.11 oder neuer** (empfohlen 3.12) | <https://www.python.org/downloads/> – bei der Installation **„Add python.exe to PATH"** anhaken |
| FFmpeg/ffprobe | optional, aber empfohlen | nur für die **technische** Prüfung (Dauer, Auflösung, Codec). Ohne ffprobe wird automatisch `ffmpeg -i` ausgewertet; fehlt beides, wird die Prüfung übersprungen (konfigurierbar) |
| YouTube-Konto | mit aktiviertem Kanal | für eigene Thumbnails sollte die Telefonnummer bestätigt sein |
| Google-Cloud-Projekt | mit YouTube Data API v3 | siehe Abschnitt 3 |

FFmpeg installieren (nur falls gewünscht): <https://ffmpeg.org/download.html> →
`ffmpeg-release-essentials.zip` entpacken → den Ordner `bin` zur Umgebungsvariable
`PATH` hinzufügen **oder** in `config.json` `FFPROBE_PATH` / `FFMPEG_PATH` setzen.

## 2. Installation

### 2.1 Standardweg (Windows) – 10 Schritte

**1. Repository klonen**

```bat
git clone -b arena/01a09550-posting https://github.com/ximkyu/Posting.git C:\YouTubeAutoPoster
cd C:\YouTubeAutoPoster
```

**2. Python-Version prüfen** – benötigt wird **Python 3.11 oder neuer** (empfohlen 3.12),
64 Bit. Bei der Installation „**Add python.exe to PATH**" anhaken.

```bat
py -3 --version
```

**3. Virtuelle Umgebung erstellen**

```bat
py -3 -m venv .venv
```

**4. `requirements.txt` installieren**

```bat
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
REM optional für die Tests:
pip install -r requirements-dev.txt
```

**5. `credentials.json` aus der Google Cloud ablegen** →
`credentials\credentials.json` (Anleitung: [Abschnitt 3](#3-google-cloud-einrichten-einmalig-10-minuten)).
Ohne diese Datei startet die Anwendung trotzdem – sie bleibt dann sicher im Status
**PAUSIERT** und lädt nichts hoch.

**6. Initialisieren** (Ordner + Datenbank + `config.json`)

```bat
python app.py init
```

**7. Starten**

```bat
start.bat
REM oder:
python app.py run --host 127.0.0.1 --port 8765
```

**8. Setup-Seite öffnen** → <http://127.0.0.1:8765/setup>

**9. Google-/YouTube-Konto verbinden** → Button
**„Google-/YouTube-Konto einmalig verbinden"** → Konto wählen →
*„Google hat diese App nicht überprüft"* → **Erweitert** → **Weiter zu YouTube AutoPoster
(nicht sicher)** → **Zulassen** → danach **„Verbindung testen"**.

**10. Privaten Testupload durchführen**

```bat
python app.py init --demo        &:: Demo-Episode nach folders\READY legen (einmalig)
python app.py scan               &:: erkennen
python app.py status             &:: kontrollieren
```

Das Demo-Video wird automatisch **privat** hochgeladen (`UPLOADED_PRIVATE`).
Veröffentlicht wird es erst, wenn du im Dashboard auf **VERÖFFENTLICHEN** klickst.
Kontrolle: <http://127.0.0.1:8765/health> → `"youtube_connected": true`.

### 2.2 Kurzform (Doppelklick)

| Datei | Wirkung |
|---|---|
| `setup.bat` | `.venv` anlegen, Abhängigkeiten installieren, Ordner + Datenbank + `config.json` erzeugen (= Schritte 3, 4, 6) |
| `start.bat` | starten und Browser öffnen (= Schritt 7) |
| `start_hidden.bat` | dasselbe minimiert im Hintergrund |
| `start_minimized.vbs` | Start ohne Konsolenfenster im Vordergrund |
| `run_tests.bat` | alle Tests ausführen |

Beenden: im Konsolenfenster **STRG+C** (oder Fenster schließen bzw. Task-Manager →
`python.exe` beenden).

### 2.3 Linux / macOS

```bash
git clone -b arena/01a09550-posting https://github.com/ximkyu/Posting.git ~/YouTubeAutoPoster
cd ~/YouTubeAutoPoster
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip && pip install -r requirements.txt
python app.py init --demo
./start.sh                       # bzw. python app.py run
```

### 2.4 Demo-/Testdateien (reproduzierbar, ohne große Binärdateien)

```bat
python app.py init --demo                      &:: Struktur + Demo-Episode video_001
python app.py demo                             &:: nur die Demo-Episode
python app.py demo --name folge_007            &:: eigener Name
python app.py demo --generate --seconds 8      &:: echtes Testvideo mit ffmpeg erzeugen
python app.py demo --overwrite                 &:: vorhandene Demo-Dateien ersetzen
python tools\create_sample_episode.py --name video_025   &:: dasselbe als Skript
```

Die Demo-Episode besteht immer aus `mp4` + `json` + `jpg` und wird in dieser Reihenfolge
erzeugt: (1) Kopie aus `examples\ready_example\` (im Repository, zusammen < 150 KB),
(2) sonst ein mit **ffmpeg** erzeugtes Testvideo, (3) sonst eine synthetisierte
Platzhalterdatei (MP4-Header + 1280×720-JPEG über Pillow). Vorhandene Dateien werden
nie überschrieben (außer mit `--overwrite`) und nie gelöscht.

### 2.5 Health-Endpunkt

```
GET http://127.0.0.1:8765/health
```

```json
{
  "app": "YouTube AutoPoster",
  "version": "1.0.0",
  "status": "ok",
  "dry_run": false,
  "youtube_connected": false,
  "database": { "videos": 1, "uploads": 0, "logs": 12, "settings": 4 }
}
```

`youtube_connected` ist **ohne** OAuth erwartungsgemäß `false`. Der Endpunkt liest nur
die lokale Datenbank – er kostet **keine** YouTube-Quota. Weitere Prüf-Endpunkte:
`/api/status` (ausführlich, inkl. Auth-Zustand, Quota, Konfiguration), `/api/videos`,
`/version`.

### 2.6 Installierte Pakete

`requirements.txt`: `flask`, `waitress`, `watchdog`, `google-api-python-client`,
`google-auth`, `google-auth-oauthlib`, `google-auth-httplib2`, `httplib2`, `Pillow`.
Unter Windows zusätzlich `pywin32` (DPAPI-Verschlüsselung des Tokens) – auf Linux/macOS
entfällt es automatisch. Entwicklung/Tests: `requirements-dev.txt` (`pytest`,
`pytest-cov`). **FFmpeg ist keine Python-Abhängigkeit** und bleibt optional
(Abschnitt 1 und `docs\FEHLERBEHEBUNG.md`, Punkt 8).

## 3. Google Cloud einrichten (einmalig, ~10 Minuten)

> Derselbe Ablauf ist im Dashboard unter **Einrichtung** (`http://127.0.0.1:8765/setup`)
> mit aktuellem Status hinterlegt.

1. **Projekt erstellen** – <https://console.cloud.google.com/projectcreate>
   → Name z. B. `youtube-autoposter` → **Erstellen**. Danach das Projekt oben in der
   Leiste auswählen.
2. **YouTube Data API v3 aktivieren** – Menü → **APIs & Dienste → Bibliothek** →
   „YouTube Data API v3" → **Aktivieren**.
3. **OAuth-Zustimmungsbildschirm** – Menü → **APIs & Dienste → Google Auth Platform**
   (in älteren Konsolen: *OAuth-Zustimmungsbildschirm*). Beim ersten Mal **Get started**
   / **Loslegen**:
   - **App-Name:** `YouTube AutoPoster`, Support- und Entwickler-E-Mail: deine Adresse.
   - **User type:** `External` (normales Google-Konto).
   - **Scopes:** nichts zwingend nötig – die App fordert den Scope beim Login an.
   - **Audience → Publishing status:** `Testing` → **Add users** → **deine eigene
     Google-Adresse** eintragen. Ohne diesen Testnutzer erscheint beim Login
     „Zugriff verweigert (Error 403: access_denied)".
4. **OAuth-Client erstellen – Typ: Desktop-App** – **Google Auth Platform → Clients**
   (bzw. *APIs & Dienste → Anmeldedaten*) → **Client erstellen** /
   *Create credentials → OAuth client ID*:
   - **Anwendungstyp:** **Desktop-App** (englisch *Desktop app*).
     Dieser Typ erlaubt den lokalen Redirect auf `http://127.0.0.1:8765/auth/callback`,
     **ohne** dass du ihn eintragen musst.
   - Falls du stattdessen **Webanwendung** wählst, musst du unter
     *Autorisierte Redirect-URIs* **exakt** `http://127.0.0.1:8765/auth/callback`
     hinterlegen (und bei geändertem Port entsprechend anpassen).
5. **credentials.json herunterladen und ablegen** – beim neuen Client
   **JSON herunterladen** → Datei umbenennen in `credentials.json` → ablegen als

   ```
   <Projektordner>\credentials\credentials.json
   ```

   Diese Datei ist durch `.gitignore` geschützt und wird niemals versioniert.
   Sie enthält nur die *öffentlichen* Client-Daten (Client-ID/-Secret), **kein** Passwort.
6. **Wichtig – private Sperre (API-Audit):** Projekte, die nach dem **28.07.2020**
   erstellt und **nicht** von YouTube auditiert wurden, dürfen Videos nur als
   `private`/`unlisted` hochladen. Ein Versuch, per API auf `public` zu stellen, schlägt
   dann mit `forbiddenPrivacySetting` fehl. Für die Freischaltung:
   <https://support.google.com/youtube/contact/yt_api_form>
   Die App meldet diesen Fall verständlich und lässt das Video privat (kein Datenverlust).
7. **Quota:** Standardmäßig 10.000 Einheiten/Tag plus ein separates
   **Upload-Kontingent von 100 Videos/Tag** (siehe Abschnitt 13).

## 4. YouTube-Konto verbinden (OAuth)

Es gibt zwei Wege – beide nutzen den offiziellen OAuth-2.0-Flow für Desktop-Apps
(Autorisierungs-URL → Google-Consent → Redirect auf `127.0.0.1` → Token wird lokal gespeichert).

**A) Über das Dashboard (empfohlen)**

1. `start.bat` starten → <http://127.0.0.1:8765>
2. Oben rechts auf **„Google-/YouTube-Konto einmalig verbinden"** klicken
   (Seite **Einrichtung**). Der Browser öffnet die Google-Anmeldung.
3. Google-Konto auswählen → bei *„Google hat diese App nicht überprüft"* auf
   **Erweitert** → **Weiter zu YouTube AutoPoster (nicht sicher)** → **Zulassen**.
4. Das Browserfenster meldet „Verbindung hergestellt" – im Dashboard erscheint
   **YouTube: CONNECTED** mit Kanalname.
5. Optional: **„Verbindung testen"** (1 Quota-Einheit) prüft Kanal und Rechte.

**B) Über die Befehlszeile**

```bat
python app.py auth login     &:: lokaler Loopback-Server + Browser
python app.py auth status    &:: Zustand anzeigen (ohne API-Aufruf)
python app.py auth test      &:: Verbindung + Kanal prüfen (1 Einheit)
python app.py auth logout    &:: Token lokal löschen
```

**Minimale Rechte:** Es wird genau **ein** Scope angefordert:

```
https://www.googleapis.com/auth/youtube
```

(Dieser Scope deckt Upload, Thumbnail und Statuswechsel ab. `youtube.force-ssl`,
`youtubepartner` o. Ä. werden bewusst **nicht** verlangt.) Ein Refresh-Token wird
nur mit `access_type=offline` + `prompt=consent` erteilt – deshalb siehst du den
Consent-Bildschirm jedes Mal erneut.

**Nie gespeichert:** Google-Passwort, 2FA-Codes, Access-Token in `config.json`.

**Kein API-Key als Ersatz:** Ein reiner API-Key reicht für Uploads **nicht** aus und wird
von dieser Anwendung bewusst nicht verwendet. Erforderlich ist ausschließlich
**OAuth 2.0** mit einem **Desktop-App**-Client (`credentials\credentials.json`) und dem
Scope `https://www.googleapis.com/auth/youtube`. Benötigte Google-API:
**YouTube Data API v3** (aktiviert im Cloud-Projekt).

## 5. Der READY-Ordner

Ablageort: `folders\READY\` (im Projektordner). Pro Episode **ein Dateiname**
(= `episode_id`) mit:

| Datei | Pflicht | Bedeutung |
|---|---|---|
| `<episode_id>.mp4` | **ja** | das Video (Standard: nur `.mp4`, weitere Formate per Konfiguration) |
| `<episode_id>.json` | **ja** | Titel, Beschreibung, Tags, Kategorie … |
| `<episode_id>.jpg` / `.jpeg` / `.png` | optional | Thumbnail (max. 2 MB, empfohlen 1280×720) |

Beispiel:

```
folders\READY\video_001.mp4
folders\READY\video_001.json
folders\READY\video_001.jpg
```

Regeln:

- Der **Basisname** ist die Episode-ID (`video_001`). Groß-/Kleinschreibung ist egal.
- Dateien dürfen **kopiert** werden, während die App läuft: Der Watcher wartet, bis die
  Dateigröße `FILE_STABILITY_SECONDS` (Standard 10 s) unverändert bleibt. Unfertige
  Kopien (`.part`, `.crdownload`, `~$…`, `Thumbs.db`, `.DS_Store`) werden ignoriert.
- Fehlt die `.json`, wird die Episode als **FEHLER** markiert und nach `FAILED/` gelegt
  (`REQUIRE_METADATA_FILE=false` erlaubt Uploads auch ohne JSON – dann mit den
  Standardwerten aus `config.json`).
- Ein komplettes, lauffähiges Beispiel liegt in `examples\ready_example\`
  (`example_video.mp4`, `.json`, `.jpg`). Schnell in den READY-Ordner kopieren:

  ```bat
  python tools\create_sample_episode.py --name video_001
  ```

## 6. JSON-Format

Vollständiges Beispiel (alle Felder, deutsche Kommentare sind in JSON nicht erlaubt):

```json
{
  "episode_id": "video_001",
  "title": "Die verborgene Lehre des Thot",
  "description": "Eine Reise in die Welt der alten aegyptischen Symbolik.\n\nKapitel:\n00:00 Einleitung\n03:00 Fazit\n\n#Thot #Aegypten",
  "tags": ["Thot", "Aegypten", "Hermetik", "Bewusstsein", "Mystik"],
  "category_id": "27",
  "language": "de",
  "privacy_status": "private",
  "thumbnail": "video_001.jpg",
  "publish_at": null,
  "made_for_kids": false,
  "self_declared_made_for_kids": false,
  "playlist_id": null,
  "channel_identifier": null,
  "license": "youtube",
  "embeddable": true,
  "notify_subscribers": false
}
```

| Feld | Pflicht | YouTube-Limit / Hinweis |
|---|---|---|
| `title` | **ja** | max. **100 Zeichen** |
| `description` | **ja** | max. **5000 Zeichen** |
| `tags` | nein | Array oder kommagetrennter String; max. **100 Zeichen pro Tag**, **500 Zeichen gesamt** |
| `category_id` | nein | Zahl **als Text**, z. B. `"27"` (Education), `"22"` (People & Blogs), `"24"` (Entertainment). Fehlt sie, greift `DEFAULT_CATEGORY_ID` aus `config.json` (Standard `22` = People & Blogs) |
| `language` | nein | z. B. `"de"` → `snippet.defaultLanguage` |
| `audio_language` | nein | z. B. `"de"` → `snippet.defaultAudioLanguage` |
| `privacy_status` | nein | **wird ignoriert**: Upload ist immer `private`. `public`/`unlisted` erzeugen nur eine Warnung im Dashboard |
| `thumbnail` | nein | Dateiname **relativ zum READY-Ordner**; Fehler hier blockieren den Upload nie |
| `publish_at` | nein | wird **nicht** gesendet (keine automatische Veröffentlichung); dient nur als Hinweis |
| `made_for_kids` / `self_declared_made_for_kids` | nein | COPPA-Kennzeichnung; Standard `false` (`DEFAULT_MADE_FOR_KIDS`) |
| `license` | nein | `"youtube"` oder `"creativeCommon"` |
| `embeddable` | nein | `true`/`false` → `status.embeddable` |
| `contains_synthetic_media` | nein | KI-/Synthetik-Kennzeichnung; wird nur gesendet, wenn `SEND_SYNTHETIC_MEDIA_FLAG: true` gesetzt ist |
| `notify_subscribers` | nein | Standard **false** (private Uploads sollen niemanden benachrichtigen) |
| `playlist_id`, `channel_identifier` | nein | werden gespeichert, in V1 nicht ausgewertet |
| `episode_id` | nein | optional; sonst wird der Dateiname verwendet |

**Deutsche Alternativen (Aliase)** werden automatisch erkannt:
`titel`, `beschreibung`, `schlagwoerter`, `kategorie`, `sprache`, `vorschaubild`,
`sichtbarkeit`, `veroeffentlichen_am`, `lizenz`, `kinder`, `einbettbar`,
`abonnenten_benachrichtigen` u. a. Unbekannte Felder bleiben gespeichert, werden aber
nicht an YouTube gesendet (Hinweis im Dashboard).

Ungültiges JSON, fehlende Pflichtfelder oder überschrittene Limits → Status **FEHLER**,
Dateien landen in `FAILED/`, Meldung steht im Dashboard und in `data/logs/app.log`.

## 7. Upload starten

**Automatisch (Normalfall):** `start.bat` starten. Der Watcher erkennt neue Dateien,
der Worker lädt **eine Episode nach der anderen** hoch (`MAX_CONCURRENT_UPLOADS = 1`).

**Manuell im Dashboard:**

- **JETZT SCANNEN** – READY-Ordner sofort prüfen und in die Datenbank übernehmen.
- **UPLOAD AUSFÜHREN** – die Warteschlange abarbeiten (immer privat).
- **PAUSIEREN** / **FORTSETZEN** – automatisches Hochladen vorübergehend stoppen.
  (Eine Pause blockiert auch den Button **UPLOAD AUSFÜHREN** – mit Hinweis im Dashboard.)

**Manuell per CLI:**

```bat
python app.py scan        &:: nur erkennen, nichts hochladen
python app.py validate    &:: Metadaten + Video prüfen (Details)
python app.py upload      &:: Queue abarbeiten (seriell, mit Retries)
python app.py status      &:: Statistik, Verbindung, Quota
```

Was pro Episode passiert:

1. **Stabilitätsprüfung** (Datei wächst nicht mehr)
2. **SHA-256** berechnen → Dublettenprüfung (Episode-ID + Dateiinhalt)
3. **JSON parsen** und gegen die YouTube-Limits prüfen
4. Dateien nach `PROCESSING/` verschieben
5. **Technische Prüfung** per ffprobe/ffmpeg (Dauer ≤ 12 h, Auflösung, Audio, Größe ≤ 256 GB)
6. **Resumable Upload** in 8-MB-Chunks mit Fortschritt, bei `5xx`/Netzwerkfehlern
   automatischer Retry mit exponentiellem Backoff (Standard 3 Versuche, 5 s → 10 s → 20 s)
7. **Thumbnail setzen** (nach dem Upload; ein Fehler hier ist unkritisch)
8. Dateien nach `UPLOADED_PRIVATE/` archivieren, YouTube-ID/-URL in die Datenbank schreiben

## 8. Veröffentlichen

1. Dashboard → Episode mit Status **PRIVAT HOCHGELADEN** (`UPLOADED_PRIVATE`).
2. In der Karten-Ansicht oder auf der Detailseite `/videos/<id>` auf
   **VERÖFFENTLICHEN** klicken.
3. Es erscheint der Sicherheitsdialog
   *„Video öffentlich machen?"* mit den Buttons **ABBRECHEN** und **VERÖFFENTLICHEN**.
   - Der **Fokus liegt auf ABBRECHEN**; `ESC` und ein Klick außerhalb schließen den Dialog.
   - Erst ein **zweiter, gezielter Klick** auf VERÖFFENTLICHEN löst den API-Aufruf aus.
4. Die App prüft zusätzlich serverseitig: CSRF-Token, `confirm=yes` und den erwarteten
   Status (hat sich in der Zwischenzeit etwas geändert, wird abgebrochen).
5. Ergebnis: Status **VERÖFFENTLICHT**, `published_at` wird gesetzt, die Dateien wandern
   nach `PUBLISHED/`.

**Alternative „nicht gelistet"** (nur per CLI; der Dialog stellt ausschließlich öffentlich):
`python app.py publish video_001 --unlisted`

**Per CLI mit Rückfrage:**

```bat
python app.py publish video_001            &:: fragt die Episode-ID als Bestätigung ab
python app.py publish video_001 --yes      &:: ohne Rückfrage (z. B. für Skripte)
```

Ein zweites Veröffentlichen derselben Episode wird abgelehnt
(`StateError: bereits veröffentlicht`) – auch nach einem Neustart.

## 9. Dry-Run (Trockenlauf)

```bat
python app.py --dry-run                 &:: vollständiger Prüfbericht
python app.py dry-run                   &:: gleichbedeutend
python app.py doctor                    &:: Alias für denselben Check
python app.py --dry-run --serve         &:: Bericht + Dashboard im Dry-Run-Modus
python app.py --dry-run --online        &:: zusätzlich echte Verbindung prüfen (1 Einheit)
```

Der Bericht prüft Python-Version, installierte Module, `config.json`, Ordner,
Datenbank, `credentials.json`, OAuth-Status, ffprobe/ffmpeg, freien Port und
**jede Episode im READY-Ordner** (Metadaten, Videodatei, Thumbnail, technische Daten).
Am Ende steht: `Upload versucht: NEIN (Dry-Run führt niemals echte Uploads aus)`.

Im Dry-Run sind zusätzlich **alle** Upload-/Publish-Pfade blockiert
(Pipeline, Worker und Web-Buttons) – das Dashboard zeigt oben das Kennzeichen **DRY RUN**.
Exit-Code `0` = alles bereit, `1` = Probleme gefunden.

## 10. Ordner & Status-Ablauf

```
folders\READY\               hier legst du neue Episoden ab
folders\PROCESSING\          wird gerade geprüft/hochgeladen
folders\UPLOADED_PRIVATE\    fertig hochgeladen (privat)
folders\PUBLISHED\           veröffentlicht
folders\FAILED\              Fehler (Dateien bleiben erhalten, Retry möglich)
folders\SKIPPED\             Dublette (gleiche Datei/Episode schon hochgeladen)
```

Ab dem Archiv bekommt **jede Episode einen eigenen Unterordner**, damit Video, JSON,
Thumbnail und Ergebnisdatei zusammenbleiben:

```
folders\UPLOADED_PRIVATE\episode_001\episode_001.mp4
folders\UPLOADED_PRIVATE\episode_001\episode_001.json
folders\UPLOADED_PRIVATE\episode_001\episode_001.jpg
folders\UPLOADED_PRIVATE\episode_001\upload_result.json
```

(`ARCHIVE_IN_SUBFOLDER: false` legt alles flach in den Statusordner ab.)

**`upload_result.json`** – zusätzliche, menschenlesbare Ergebnissicherung pro Episode
(ersetzt die Datenbank nicht, ergänzt sie):

```json
{
  "episode_id": "episode_001",
  "platform": "youtube",
  "title": "Die verborgene Lehre des Thot",
  "youtube_video_id": "dQw4w9WgXcQ",
  "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "upload_status": "UPLOADED_PRIVATE",
  "privacy_status_requested": "private",
  "effective_privacy_status": "private",
  "uploaded_at": "2026-09-12T18:04:11+00:00",
  "published_at": null,
  "thumbnail": { "set": true, "url": "https://i.ytimg.com/vi/.../hqdefault.jpg", "error": null },
  "files": { "video": "...mp4", "metadata": "...json", "thumbnail": "...jpg", "folder": "..." },
  "file_size": 82865,
  "file_hash": "cee815bf6b8f720b...",
  "retry_count": 0,
  "error_code": null,
  "error_message": null,
  "needs_attention": false,
  "updated_at": "2026-09-12T18:04:12+00:00"
}
```

Nach dem Veröffentlichen wird dieselbe Datei nach `PUBLISHED\episode_001\` übernommen
und zeigt `"upload_status": "PUBLISHED"` sowie `"published_at": "…"`.
Abschaltbar mit `WRITE_RESULT_JSON: false`, eigener Name mit `RESULT_JSON_FILENAME`.

Datenbank-Status (`videos.status`): `NEW`, `READY`, `VALIDATING`, `UPLOADING`,
`UPLOADED_PRIVATE`, `PUBLISH_READY`, `PUBLISHING`, `PUBLISHED`, `FAILED`, `PAUSED`,
`INTERRUPTED`, `SKIPPED_DUPLICATE`.

**Nach einem Neustart/Absturz** (automatisch beim Start, oder `python app.py recover <episode>`):

| Zustand vorher | Aktion |
|---|---|
| `UPLOADING` | → `INTERRUPTED` (**kein** automatischer Re-Upload; Buttons „Auf YouTube prüfen" / „Erneut hochladen") |
| `PUBLISHING` | → `UPLOADED_PRIVATE` (Video bleibt privat, Veröffentlichung erneut starten) |
| `VALIDATING` | → `READY` (es war noch nichts hochgeladen, gefahrlos neu einreihen) |
| Datei fehlt | → `FAILED` mit klarer Meldung |
| Dateien ohne DB-Bezug in `PROCESSING/` | werden **gemeldet**, aber nie gelöscht |

„**Auf YouTube prüfen**" sucht über `channels.list` + `playlistItems.list` +
`videos.list` (3 Einheiten) nach dem bereits hochgeladenen Video und übernimmt die
YouTube-ID, statt doppelt hochzuladen.

## 11. Befehlszeile (CLI)

```
python app.py [globale Optionen] <befehl> [argumente]
```

| Befehl | Wirkung |
|---|---|
| `run` / `serve` (Standard) | Dashboard + Watcher + Worker starten |
| `dry-run` / `doctor` | Prüfbericht, keine Uploads |
| `scan` | READY-Ordner erkennen und in die DB übernehmen |
| `upload [--limit N]` | Warteschlange seriell abarbeiten |
| `validate [pfad]` | Metadaten/Video/Thumbnail im Detail prüfen |
| `status` | Statistik, Verbindung, Quota, letzte Episoden |
| `auth status\|login\|test\|logout` | OAuth-Zustand, Login, Verbindungstest, Trennen |
| `publish <episode> [--unlisted] [--yes]` | veröffentlichen (mit Bestätigung) |
| `retry <episode> [--yes]` | fehlgeschlagene Episode neu einreihen |
| `recover <episode>` | unterbrochenen Upload auf YouTube suchen |
| `archive <episode> [--target ORDNER]` | Dateien manuell verschieben |
| `logs [--limit N] [--follow]` | Protokoll anzeigen |
| `init [--demo]` | Ordner, Datenbank, `config.json` anlegen (mit `--demo` zusätzlich eine Demo-Episode) |
| `demo [--name N] [--generate] [--seconds S] [--overwrite] [--target PFAD]` | Demo-Episode (mp4+json+jpg) im READY-Ordner anlegen |
| `config [--write]` | Konfiguration anzeigen/schreiben |
| `reset [--yes]` | Fehler/Unterbrochen zurück in die Queue |

Globale Optionen: `--config PFAD`, `--host`, `--port`, `--log-level`,
`--dry-run`, `--no-browser`, `--no-watch`, `--no-worker`, `--quiet`.
Hilfe zu jedem Befehl: `python app.py --help`.

## 12. Konfiguration

Alle Werte stehen in `config.json` (Projektordner). Pfade dürfen **relativ** sein
(sie beziehen sich auf den Ordner der `config.json`) – das Projekt kann also auf ein
anderes Laufwerk verschoben werden. Anzeige: `python app.py config`,
Schreiben: `python app.py config --write`.

Wichtige Schlüssel:

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8765` | **nur lokal** erreichbar; `0.0.0.0` öffnet das Dashboard im Netz (Warnung beim Start) |
| `DEFAULT_PRIVACY_STATUS` | `private` | wird intern **immer** auf `private` erzwungen |
| `ENFORCE_PRIVATE_UPLOAD` | `true` | nicht abschaltbar |
| `SEND_PUBLISH_AT` | `false` | keine Terminveröffentlichung |
| `REQUIRE_METADATA_FILE` | `true` | JSON ist Pflicht |
| `ALLOWED_VIDEO_EXTENSIONS` | `[".mp4"]` | mit `ALLOW_EXTRA_VIDEO_EXTENSIONS: true` auch `.mov/.mkv/.webm/…` |
| `FILE_STABILITY_SECONDS` / `STABILITY_CHECK_INTERVAL` / `STABILITY_REQUIRED_CHECKS` | `10` / `2` / `2` | Schutz vor unfertigen Kopien |
| `MAX_RETRIES` / `RETRY_DELAY` / `RETRY_BACKOFF_FACTOR` / `RETRY_MAX_DELAY` | `3` / `5.0` / `2.0` / `120` | Wiederholungen mit Backoff |
| `UPLOAD_CHUNK_SIZE` | `8388608` | Chunk-Größe des resumable Uploads |
| `AUTO_UPLOAD` | `true` | automatisch hochladen, sobald eine Episode bereit ist |
| `MAX_CONCURRENT_UPLOADS` | `1` | seriell (von YouTube empfohlen) |
| `SCAN_INTERVAL_SECONDS` / `WATCH_ENABLED` / `USE_WATCHDOG` | `10` / `true` / `true` | Watch-Verhalten |
| `FFPROBE_PATH` / `FFMPEG_PATH` | `""` | eigener Pfad zu den Werkzeugen |
| `REQUIRE_FFPROBE` | `false` | `true` = ohne Prüfung kein Upload |
| `DEFAULT_CATEGORY_ID` / `DEFAULT_LANGUAGE` / `DEFAULT_MADE_FOR_KIDS` | `22` / `de` / `false` | Fallbacks, wenn die JSON nichts angibt |
| `CLIENT_SECRETS_FILE` / `TOKEN_FILE` / `TOKEN_STORAGE` | `credentials/credentials.json` / `token.json` / `auto` | `auto` = DPAPI unter Windows, sonst Datei `0600` |
| `OAUTH_SCOPES` | `["https://www.googleapis.com/auth/youtube"]` | minimal |
| `OAUTH_REDIRECT_URI` / `OAUTH_SUCCESS_REDIRECT` | `""` / `/` | leer = `http://127.0.0.1:<PORT>/auth/callback`; Seite nach erfolgreichem Login |
| `USE_WAITRESS` / `DEBUG` | `true` / `false` | produktiver WSGI-Server (Waitress) statt Flask-Dev-Server |
| `AUTO_REFRESH_SECONDS` | `5` | Dashboard aktualisiert sich selbst (lokal, **ohne** API-Aufruf) |
| `PAUSE_WHEN_DISCONNECTED` | `true` | ohne gültige Autorisierung wird pausiert statt wiederholt zu scheitern |
| `WORKER_POLL_SECONDS` | `2` | Takt des Upload-Workers |
| `UPLOAD_TIMEOUT_SECONDS` / `PROGRESS_LOG_SECONDS` | `300` / `20` | Timeout je Versuch / Fortschrittsmeldung im Log |
| `STABILITY_WAIT_BEFORE_UPLOAD` / `SCAN_RECURSIVE` | `true` / `false` | vor dem Upload erneut auf Stabilität warten / Unterordner durchsuchen |
| `VALIDATE_CATEGORY_ONLINE` / `CATEGORY_CACHE_HOURS` | `true` / `168` | Kategorie per API prüfen (1 Einheit, 7 Tage gecacht) |
| `LOG_TO_DATABASE` / `LOG_KEEP_ROWS` | `true` / `20000` | Logzeilen zusätzlich in der DB (Dashboard-Seite **Protokoll**) |
| `FETCH_CHANNEL_INFO` | `true` | Kanalname nach dem Login anzeigen (1 Einheit, 30 min gecacht) |
| `ARCHIVE_AFTER_UPLOAD` / `ARCHIVE_AFTER_PUBLISH` | `true` / `true` | Dateien in Archiv-Ordner verschieben (nie löschen) |
| `ARCHIVE_IN_SUBFOLDER` | `true` | pro Episode ein Unterordner im Archiv |
| `WRITE_RESULT_JSON` / `RESULT_JSON_FILENAME` | `true` / `upload_result.json` | Ergebnisdatei pro Episode |
| `MARK_INTERRUPTED_ON_STARTUP` | `true` | Wiederanlauf-Schutz |
| `VERIFY_HASH_AFTER_MOVE` | `true` | Integritätsprüfung beim Verschieben |
| `LOG_LEVEL` / `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` | `INFO` / `5242880` / `5` | Protokollierung nach `data/logs/app.log` |

**Umgebungsvariablen** überschreiben `config.json` (Präfix `YAP_`), z. B.:

```bat
set YAP_PORT=9000
set YAP_FILE_STABILITY_SECONDS=30
set YAP_AUTO_UPLOAD=false
python app.py
```

## 13. Quota-Verbrauch

Die App ruft die API **nur** ab, wenn es nötig ist – das Dashboard verbraucht **keine**
Quota (alle Anzeigen kommen aus der lokalen SQLite-Datenbank).

| Aktion | API-Aufruf | Kosten |
|---|---|---|
| Upload | `videos.insert` | **1 Einheit** im separaten Kontingent **„Video Uploads"**, Limit **100 Uploads/Tag** |
| Thumbnail | `thumbnails.set` | 50 Einheiten |
| Veröffentlichen | `videos.update` | 50 Einheiten |
| Status prüfen (Button) | `videos.list` | 1 Einheit |
| Kanal/Verbindung testen | `channels.list` | 1 Einheit |
| Unterbrochenen Upload suchen | `channels.list` + `playlistItems.list` + `videos.list` | 3 Einheiten |
| Kategorie-Prüfung (optional) | `videoCategories.list` | 1 Einheit, **7 Tage gecacht** |

Standard-Pool: **10.000 Einheiten/Tag** (Reset Mitternacht Pacific Time). Der Verbrauch
wird pro Tag in der Datenbank mitgezählt und im Dashboard sowie mit `python app.py status`
angezeigt. Bei `quotaExceeded` geht die Episode in den Status **PAUSIERT** (nicht
FEHLER) und wird am nächsten Tag automatisch erneut versucht – es wird **nicht**
sofort wiederholt, um keine weiteren Einheiten zu verbrauchen.

## 14. Tests

```bat
run_tests.bat            &:: Windows
./run_tests.sh           &:: Linux/macOS
.venv\Scripts\activate.bat && python -m pytest -q
```

381 Tests, **alle ohne Netzwerk** und ohne echtes YouTube-Konto:

- JSON-Parser (Pflichtfelder, Limits, Aliase, ungültige Werte, `privacy_status`-Override)
- SHA-256/Dubletten, atomares Verschieben, Integritätsprüfung, Stabilitätsprüfung
- ffprobe-Auswertung **und** `ffmpeg -i`-Fallback (mit Test-Stub), Thumbnail-Prüfung
- READY-Erkennung (Paare, fehlende JSON, Fremddateien, `.part`, Groß-/Kleinschreibung)
- SQLite: Schema, Migration, Status-Übergänge, atomares „Claim", Statistik, Log-Rotation
- Pipeline: kompletter Ablauf, Fehler/Retries mit Backoff, Quota, Pause, Dry-Run
- **Sicherheitsregeln:** Upload immer `private`, Veröffentlichen nur mit Bestätigung,
  kein doppeltes Veröffentlichen, kein Doppel-Upload nach Neustart
- YouTube-API gemockt: resumable Upload in Chunks, `503`-Retries, neue Session nach
  Transportfehler, Fehlerklassifikation (`quotaExceeded`, `forbiddenPrivacySetting`, …)
- OAuth/Token-Verwaltung (DPAPI/Datei), kein Passwort im Klartext
- Web-UI: Dashboard, Detailseite, Sicherheitsdialog (Fokus auf ABBRECHEN), CSRF-Schutz,
  alle Buttons, API-Endpunkte, Dry-Run-Sperren
- CLI: `init`, `scan`, `status`, `validate`, `dry-run`, `upload`, `auth`, `publish`,
  `retry`, `recover`, `logs`, `config`
- Beispiel-Episode in `examples/ready_example/` und Portabilität der Konfiguration
- Archiv-Unterordner, `upload_result.json`, Dubletten, Wiederanlauf, Idempotenz des
  Veröffentlichen-Buttons
- Demo-Episode (`init --demo`, `demo`, `tools\create_sample_episode.py`): gültige
  Metadaten, idempotent, Fallback ohne `examples/` und ohne ffmpeg, kompletter
  Upload-Durchlauf bis `UPLOADED_PRIVATE`

**End-to-End-Durchlauf mit simulierter YouTube-API** (zeigt den kompletten Ablauf
inklusive Dashboard, Absturz-Wiederanlauf, Dublette und Veröffentlichen – ohne Netzwerk,
ohne Google-Konto, ohne echte Uploads):

```bat
python tools\e2e_mock_demo.py            &:: in einem temporären Projekt
python tools\e2e_mock_demo.py --keep     &:: Projektordner behalten und inspizieren
```

Erwartete Ausgabe: 55 Prüfungen, `Probleme: 0`, Exit-Code `0`.

## 15. Fehlerbehebung

| Symptom | Ursache / Lösung |
|---|---|
| `credentials.json fehlt` | Google-Cloud-JSON als `credentials\credentials.json` ablegen (Abschnitt 3, Schritt 5) |
| `credentials.json enthält weder 'installed' noch 'web'` | Falscher Client-Typ – **Desktop-App** wählen und die JSON erneut herunterladen |
| `Error 403: access_denied` beim Login | OAuth-Status ist *Testing* und dein Konto fehlt bei **Audience → Test users** |
| `redirect_uri_mismatch` | Du nutzt einen **Web**-Client: `http://127.0.0.1:8765/auth/callback` exakt eintragen (oder Desktop-Client verwenden) |
| `invalid_grant` | Token widerrufen/abgelaufen → **Erneut verbinden** bzw. `python app.py auth login` |
| `YouTube-Autorisierung ist ungültig oder abgelaufen` | Refresh fehlgeschlagen → neu verbinden; bei Markenkonten das **richtige** Konto wählen |
| `quotaExceeded` / `dailyLimitExceeded` | Tageskontingent erschöpft → Episode bleibt **PAUSIERT**, läuft am nächsten Tag weiter |
| Uploads bleiben privat, `forbiddenPrivacySetting` beim Veröffentlichen | Nicht auditiertes Projekt (nach 28.07.2020) → Audit-Formular: <https://support.google.com/youtube/contact/yt_api_form> |
| Thumbnail-Fehler `403 forbidden` | YouTube-Konto nicht verifiziert (Telefonnummer bestätigen) oder Bild > 2 MB / anderes Format |
| `ffprobe/ffmpeg nicht gefunden` | FFmpeg installieren oder `FFPROBE_PATH` setzen. Ohne Werkzeug wird die technische Prüfung übersprungen (Warnung) – mit `REQUIRE_FFPROBE=true` wird sie zur Pflicht |
| Episode bleibt „wartet (Datei wird noch geschrieben)" | Kopie läuft noch; `FILE_STABILITY_SECONDS` abwarten (bei sehr großen Dateien erhöhen) |
| `Port 8765 ist bereits belegt` | `python app.py --port 9000` oder `PORT` in `config.json` ändern |
| Dashboard im Browser nicht erreichbar | Läuft `start.bat` noch? Steht `HOST` auf `127.0.0.1`? Firewall-/Proxy-Erweiterungen prüfen |
| `Metadaten-Fehler: 'description' fehlt` | Pflichtfelder `title` und `description` in der JSON ergänzen |
| Status **UNTERBROCHEN** nach Absturz | Bewusst kein Auto-Re-Upload: **„Auf YouTube prüfen"** (übernimmt die ID) oder **„Erneut hochladen"** |
| Status **DUBLETTE** | Gleiche Datei (SHA-256) oder gleiche Episode-ID wurde schon hochgeladen – Dateien liegen in `SKIPPED/` |
| Fehlerdetails suchen | `data\logs\app.log` (rollierend) bzw. Dashboard → **Protokoll**, oder `python app.py logs --follow` |

Diagnose in einem Schritt:

```bat
python app.py --dry-run
```

## 16. Projektstruktur

```
Posting\
├─ app.py                     Einstieg (leitet an youtube_autoposter.cli weiter)
├─ config.py                  Komfort-Import der Konfiguration
├─ config.json                deine Einstellungen (portabel, relative Pfade)
├─ setup.bat / start.bat      Einrichtung / Start (Windows)
├─ start_hidden.bat           Start im Hintergrund (minimiertes Fenster)
├─ start_minimized.vbs        dasselbe ohne Konsolenfenster im Vordergrund
├─ setup.sh  / start.sh       dasselbe für Linux/macOS
├─ run_tests.bat / .sh        Testlauf
├─ pytest.ini                 Test-Konfiguration (offline, testpaths=tests)
├─ requirements.txt           Laufzeit-Abhängigkeiten
├─ requirements-dev.txt       pytest (Entwicklung)
├─ credentials\               .gitkeep + README.txt; hier liegen NUR lokal
│                             credentials.json / token.json (nie in Git)
├─ data\                      youtube_autoposter.db, logs\app.log
├─ folders\                   READY, PROCESSING, UPLOADED_PRIVATE, PUBLISHED, FAILED, SKIPPED
├─ examples\ready_example\    lauffähige Beispiel-Episode (mp4 + json + jpg)
├─ tools\create_sample_episode.py   Demo-Episode anlegen (nutzt utils\demo_episode.py)
├─ tools\e2e_mock_demo.py     kompletter Durchlauf mit simulierter YouTube-API
├─ docs\                      JSON-Referenz, Google-Cloud-Anleitung, Architektur, Fehlerbehebung
├─ tests\                     362 Offline-Tests
└─ youtube_autoposter\
   ├─ cli.py                  Befehlszeile
   ├─ app.py                  Flask-App + Server (waitress)
   ├─ config.py               Einstellungen, harte Sicherheitsregeln
   ├─ constants.py            Status, Grenzwerte, Quota-Tabelle
   ├─ errors.py               Fehler-Hierarchie
   ├─ auth\youtube_auth.py    OAuth-Flow, Token, Status
   ├─ core\                   pipeline.py, dry_run.py, recovery.py, context.py, models.py
   ├─ database\               db.py (Schema/Migration), repository.py (Zugriff)
   ├─ metadata\parser.py      JSON → Upload-Body (erzwingt private)
   ├─ platforms\              base.py (Adapter-Interface), registry.py
   │  └─ youtube\             api.py, uploader.py, publisher.py, __init__.py
   ├─ scanner\folder_scanner.py   READY-Erkennung + Stabilität
   ├─ scheduler\watcher.py    Watcher (watchdog) + serieller Upload-Worker
   ├─ ui\                     routes.py, templates\, static\ (Dashboard)
   └─ utils\                  files.py, media.py, secure_store.py, validation.py,
                             logging_utils.py, demo_episode.py (Demo-Episode)
```

## 17. Datenbank, Ergebnisdateien & Sicherung

**Datenbank** – `data\youtube_autoposter.db` (SQLite, WAL-Modus). Vier Tabellen:

| Tabelle | Inhalt |
|---|---|
| `videos` | eine Zeile pro Episode: Status, Pfade, SHA-256, Titel/Beschreibung/Tags, YouTube-ID/-URL, `effective_privacy_status`, Zeitstempel, Fehler, `needs_attention` |
| `uploads` | jeder Upload-Versuch: Phase, gesendete Bytes, HTTP-Status, Quota-Einheiten, Fehler |
| `logs` | Protokollzeilen für das Dashboard (begrenzt durch `LOG_KEEP_ROWS`) |
| `settings` | Laufzeit-Zustand: Pause, Quota-Zähler je Tag, Kategorie-Cache, letzter Wiederanlauf |

Die Datenbank ist die **einzige Statusquelle für das Dashboard** – deshalb kostet ein
Seitenaufruf keine YouTube-Quota. Schema-Änderungen werden beim Start automatisch
migriert.

Ansehen ohne Browser:

```bat
python app.py status                 &:: Statistik, Verbindung, Quota
python app.py logs --limit 100       &:: letzte Protokollzeilen
```

**Ergebnisdateien** – pro Episode `upload_result.json` im Archiv-Unterordner
(siehe Abschnitt 10). Sie ist eine zusätzliche, menschenlesbare Sicherung und wird bei
jedem Statuswechsel neu geschrieben (Upload, Veröffentlichung, Fehler, Dublette).

**Sicherung (Backup)**

1. App beenden (`STRG+C` im Fenster von `start.bat`).
2. Diese Dateien/Ordner kopieren:
   - `data\youtube_autoposter.db` (Status, YouTube-IDs)
   - `config.json` (Einstellungen – Pfade sind relativ, also portabel)
   - `credentials\` (OAuth-Client und Token; **vertraulich**, gehört in kein Git und
     nicht in eine Cloud-Freigabe)
   - `folders\` (Originale und Archive)
3. Wiederherstellen: alles zurückkopieren und `start.bat` ausführen. Liegt das Projekt
   in einem anderen Ordner/Laufwerk, passen sich die Pfade automatisch an
   (`BASE_DIR` wird aus dem Ort der `config.json` abgeleitet).

Ein Verlust der Datenbank ist kein Datenverlust: `python app.py init` legt sie neu an und
`JETZT SCANNEN` übernimmt alle Dateien erneut. Dann fehlen allerdings die bereits
vergebenen YouTube-IDs – die Dublettenerkennung über den Datei-Hash bleibt erhalten,
solange die archivierten Dateien vorhanden sind.

**Start & Stopp**

| Aktion | Weg |
|---|---|
| Starten | `start.bat` (Fenster sichtbar) bzw. `start_hidden.bat` / `start_minimized.vbs` (minimiert) |
| Beenden | `STRG+C` im Konsolenfenster oder Fenster schließen |
| Ohne Browser-Start | `python app.py --no-browser` |
| Nur prüfen | `python app.py --dry-run` |
| Nur Queue abarbeiten | `python app.py upload` |
| Port prüfen | `python app.py status` (zeigt Host/Port aus `config.json`) |

## 18. Sicherheit & Datenschutz

- **Nur offizielle API** (`google-api-python-client`), keine inoffiziellen Endpunkte,
  kein Scraping, keine Browser-Automation von YouTube Studio.
- **Kein Passwort**, keine 2FA-Codes: ausschließlicher OAuth-2.0-Autorisierungscode-Flow
  mit Refresh-Token. Der Redirect läuft über `127.0.0.1` (Loopback) bzw. einen lokalen
  Loopback-Server bei `auth login`.
- **Token-Schutz:** Windows DPAPI (`credentials/token.dpapi`) oder Datei mit `0600`;
  `credentials/.gitignore` blockiert zusätzlich jeden Commit.
- **Kein Klartext-Token** in `config.json` oder der Datenbank.
- **Bindung an localhost:** Standard `HOST=127.0.0.1`; bei `0.0.0.0` warnt die App
  beim Start deutlich.
- **CSRF-Schutz** für alle schreibenden Aktionen der Web-Oberfläche.
- **Zerstörungsfrei:** Dateien werden verschoben, nie gelöscht; beim Verschieben über
  Dateisystemgrenzen wird der SHA-256 verglichen.
- **Protokollierung:** `data\logs\app.log` – Access-Token/Secrets werden automatisch
  geschwärzt (`redact`).
- Alles bleibt **lokal**: keine Cloud-Datenbank, keine Telemetrie, keine Drittanbieter.

## 19. Erweiterbarkeit (weitere Plattformen)

Die Plattform ist hinter einem Adapter-Interface gekapselt
(`youtube_autoposter\platforms\base.py`):

```python
class PlatformAdapter(Protocol):
    name: str
    def info(self) -> PlatformInfo: ...
    def is_ready(self) -> tuple[bool, str]: ...
    def upload(self, job: PublicationJob) -> UploadResult: ...
    def set_thumbnail(self, platform_video_id: str, thumbnail_path: str) -> ThumbnailResult: ...
    def publish(self, job: PublicationJob, *, confirm: bool, privacy_status: str) -> PublishResult: ...
    def fetch_status(self, platform_video_id: str) -> dict: ...
    def find_existing_upload(self, *, title, since, duration_seconds, limit) -> str | None: ...
    def validate_online(self, metadata) -> list[str]: ...
```

Neue Plattform (z. B. Instagram/Meta, TikTok, Spotify) = neues Paket
`platforms\<name>\` + Registrierung in `platforms\registry.py`. Pipeline, Scanner,
Datenbank, CLI und Dashboard bleiben unverändert; die Datenbank führt bereits eine
`platform`-Spalte pro Episode.

## 20. Grenzen von V1

- **Keine Terminveröffentlichung:** `publish_at` wird gelesen, angezeigt, aber nicht
  gesendet. Veröffentlichung erfolgt ausschließlich manuell.
- **Ein Upload gleichzeitig** (seriell) – bewusst, um Quota und Bandbreite zu schonen.
- **Keine** Playlists/Kommentare/Endscreens/Kapitel-API – nur Upload, Thumbnail,
  Privacy-Status (Kapitel kannst du als Text in die Beschreibung schreiben).
- **Kein** Mehrkanal-Betrieb pro Installation (ein OAuth-Konto je Projektordner; für
  mehrere Kanäle mehrere Projektordner mit eigener `config.json` anlegen).
- **Keine** automatische Videobearbeitung/Konvertierung – die Datei muss uploadfertig sein.

---

## Weitere Dokumente

| Datei | Inhalt |
|---|---|
| `docs\GOOGLE-CLOUD-SETUP.md` | Google Cloud + OAuth im Detail, Audit, Quota, Mehrkanal-Betrieb |
| `docs\JSON-FORMAT.md` | vollständige Feldreferenz, deutsche Aliase, Limits, Fehlercodes |
| `docs\FEHLERBEHEBUNG.md` | alle Meldungen mit Ursache und Lösung, Logs, Backup, Diagnose |
| `docs\ARCHITEKTUR.md` | Module, Datenfluss, Statusmaschine, Datenbankschema, Adapter-Erweiterung |
| `examples\ready_example\` | lauffähige Beispiel-Episode (mp4 + json + jpg) |
| `tools\create_sample_episode.py` | Beispiel nach `READY\` kopieren oder Testvideo erzeugen |
| `tools\e2e_mock_demo.py` | kompletter Durchlauf mit simulierter YouTube-API (offline) |

---

**Kurzanleitung in 60 Sekunden**

1. `setup.bat` → 2. `credentials\credentials.json` ablegen (Abschnitt 3) →
3. `start.bat` → 4. im Dashboard **Mit YouTube verbinden** →
5. `video_001.mp4` + `video_001.json` (+ `.jpg`) nach `folders\READY\` →
6. warten bis **PRIVAT HOCHGELADEN**, dann bei Bedarf **VERÖFFENTLICHEN**.
