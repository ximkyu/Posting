# Google Cloud & OAuth – Schritt für Schritt

Einmalige Einrichtung (~10 Minuten). Danach ist nur noch der Login nötig.
Der gleiche Ablauf ist live im Dashboard unter **Einrichtung** (`http://127.0.0.1:8765/setup`)
hinterlegt – dort siehst du zusätzlich, welcher Schritt schon erledigt ist.

---

## 0. Was am Ende vorhanden sein muss

| Datei | Inhalt | Woher |
|---|---|---|
| `credentials\credentials.json` | Client-ID + Client-Secret deines **Desktop-App**-OAuth-Clients | Google Cloud Console → „JSON herunterladen" |
| `credentials\token.json` bzw. `token.dpapi` | OAuth-Refresh-/Access-Token (wird **automatisch** erzeugt) | erster Login mit dem Tool |

Beide Dateien stehen in `.gitignore` und werden nie versioniert.
Es wird **kein** Passwort gespeichert.

---

## 1. Google-Cloud-Projekt erstellen

1. <https://console.cloud.google.com/projectcreate> öffnen (mit dem Google-Konto,
   zu dem der YouTube-Kanal gehört).
2. **Projektname:** z. B. `youtube-autoposter` → **Erstellen**.
3. Oben in der Leiste dieses Projekt auswählen (wichtig: alle folgenden Schritte
   beziehen sich auf *dieses* Projekt).
4. Falls nach Organisation/Rechnungsdaten gefragt wird: Für die YouTube Data API v3
   ist **keine Kreditkarte** nötig – die API hat ein kostenloses Tageskontingent.

## 2. YouTube Data API v3 aktivieren

1. Menü ☰ → **APIs & Dienste → Bibliothek** (*APIs & Services → Library*).
2. Suchen nach **„YouTube Data API v3"**.
3. **Aktivieren** (*Enable*) klicken.
4. Kontrolle: **APIs & Dienste → Aktivierte APIs & Dienste** muss
   „YouTube Data API v3" auflisten.

> Ohne diesen Schritt liefert jeder Aufruf `403 accessNotConfigured`.

## 3. OAuth-Zustimmungsbildschirm (Google Auth Platform)

1. Menü ☰ → **APIs & Dienste → Google Auth Platform**
   (in älteren Konsolen: *OAuth-Zustimmungsbildschirm* / *OAuth consent screen*).
2. Beim ersten Mal: **Get started** / **Loslegen**.
3. Angaben:
   - **App-Name:** `YouTube AutoPoster`
   - **Support-E-Mail** und **Entwickler-Kontakt:** deine eigene E-Mail-Adresse
   - **User type:** `External` (für ein normales Google-Konto; `Internal` gibt es nur
     bei Google-Workspace-Domains)
4. **Scopes:** nichts eintragen nötig. Das Tool fordert beim Login genau einen Scope an:
   `https://www.googleapis.com/auth/youtube`
5. **Audience → Publishing status:** `Testing`
   → **Add users** → **deine eigene Google-Adresse** eintragen (und jedes weitere Konto,
   das den Kanal verwalten darf).
   Ohne Testnutzer: `Error 403: access_denied` beim Login.
6. Speichern.

> **Kein „Verifizieren" nötig.** Ein unverifizierter Status erzeugt beim Login nur den
> Hinweis *„Google hat diese App nicht überprüft"* → **Erweitert** →
> **Weiter zu YouTube AutoPoster (nicht sicher)** → **Zulassen**.
> Wichtig ist aber der Punkt im nächsten Abschnitt (API-Audit).

## 4. OAuth-Client erstellen – Typ: Desktop-App

1. **Google Auth Platform → Clients** (bzw. *APIs & Dienste → Anmeldedaten*).
2. **Client erstellen** / *Create credentials → OAuth client ID*.
3. **Anwendungstyp:** **Desktop-App** (englisch *Desktop app*).
4. Name z. B. `autoposter-desktop` → **Erstellen**.
5. Es erscheinen **Client-ID** und **Client-Secret** → **JSON herunterladen**.

**Warum „Desktop-App" und nicht „Webanwendung"?**
Desktop-Clients erlauben die lokale Loopback-Umleitung
(`http://127.0.0.1:8765/auth/callback` bzw. ein zufälliger lokaler Port bei
`python app.py auth login`) **ohne** Eintragung. Bei einem Web-Client musst du
unter *Autorisierte Redirect-URIs* exakt hinterlegen:

```
http://127.0.0.1:8765/auth/callback
```

(Änderst du den Port in `config.json`, musst du die URI dort anpassen oder
`OAUTH_REDIRECT_URI` setzen.)

## 5. credentials.json ablegen

1. Die heruntergeladene Datei (z. B. `client_secret_1234-abc.apps.googleusercontent.com.json`)
   **umbenennen** in `credentials.json`.
2. Ablegen als

   ```
   <Projektordner>\credentials\credentials.json
   ```

   (Linux/macOS: `<Projektordner>/credentials/credentials.json`)
3. Kontrolle im Dashboard (**Einrichtung**) oder per CLI:

   ```bat
   python app.py auth status
   python app.py --dry-run
   ```

   Erwartet: `credentials.json: OK` und `Zustand: NOT CONNECTED`.

## 6. Konto verbinden

**Dashboard:** Button **„Google-/YouTube-Konto einmalig verbinden"** → Browser →
Konto wählen → *Erweitert* → *Weiter zu YouTube AutoPoster (nicht sicher)* → **Zulassen**
→ „Verbindung hergestellt".

**CLI:** `python app.py auth login` (startet einen lokalen Loopback-Server und öffnet
den Browser), danach `python app.py auth test`.

Danach steht oben im Dashboard **YouTube: CONNECTED** mit Kanalname, und der
Upload-Worker nimmt die Arbeit auf.

---

## 7. Private Sperre & API-Audit (wichtig!)

Projekte, die **nach dem 28.07.2020** erstellt und **nicht** von YouTube auditiert
wurden, dürfen Videos nur als `private`/`unlisted` hochladen. Ein Wechsel auf `public`
schlägt dann fehl mit:

```
403 forbiddenPrivacySetting – The video that you are trying to update is private-locked …
```

Freischaltung (dauert meist wenige Werktage):
**<https://support.google.com/youtube/contact/yt_api_form>**

Das Tool behandelt diesen Fall ausdrücklich:

- Uploads sind ohnehin **immer privat** – es geht also nichts verloren.
- Beim Veröffentlichen wird die Sperre erkannt, verständlich gemeldet und das Video
  bleibt privat (Status `needs_attention`), inkl. Link zum Audit-Formular.

## 8. Quota & Limits

| Größe | Wert |
|---|---|
| Standard-Kontingent | **10.000 Einheiten/Tag** (Reset 00:00 Uhr Pacific Time) |
| Uploads | separates Kontingent **„Video Uploads"**, **100 Videos/Tag** |
| `videos.insert` (Upload) | 1 Einheit aus dem Upload-Kontingent |
| `videos.update` (veröffentlichen) | 50 Einheiten |
| `thumbnails.set` | 50 Einheiten |
| `videos.list` / `channels.list` / `playlistItems.list` | je 1 Einheit |
| Maximale Dateigröße | 256 GB |
| Maximale Länge | 12 Stunden |

Bei `quotaExceeded` / `dailyLimitExceeded` setzt das Tool die Episode auf **PAUSIERT**
und versucht sie am nächsten Tag erneut – es wird **nicht** sofort wiederholt.
Der Verbrauch wird lokal mitgezählt (`python app.py status`, Dashboard).

## 9. Mehrere Kanäle / Markenkonten

- Pro **Projektordner** wird genau **ein** Google-Konto verbunden
  (eine `credentials.json` + ein Token).
- Für mehrere Kanäle: mehrere Kopien des Projektordners mit eigener `config.json`
  und anderem `PORT` anlegen (z. B. 8765, 8766).
- Beim Login das Konto wählen, das den Kanal **verwalten** darf. Bei Markenkonten
  erscheint nach der Kontowahl die Auswahl des Kanals/der Marke.

## 10. Verbindung zurücksetzen

```bat
python app.py auth logout        &:: Token lokal löschen
```

oder im Dashboard **Verbindung trennen (Token löschen)** → anschließend erneut verbinden.
Nötig z. B. bei `invalid_grant`, nach einem Passwortwechsel oder wenn du dem Projekt
weitere Rechte gegeben hast.
