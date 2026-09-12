CREDENTIALS-ORDNER
==================

Hier liegen ausschliesslich deine persoenlichen Zugangsdaten:

  credentials.json   OAuth-Client aus der Google Cloud Console
                     (Typ: "Desktop-App"). Herunterladen, umbenennen und
                     hier ablegen.

  token.json         wird nach der einmaligen Autorisierung automatisch
                     erzeugt (bzw. token.dpapi unter Windows, wenn pywin32
                     installiert ist - dann DPAPI-verschluesselt).

WICHTIG
-------
* Beide Dateien stehen in .gitignore und werden NIEMALS committet.
* Nicht per E-Mail/Chat verschicken und nicht in Cloud-Ordner synchronisieren.
* Kein Google-Passwort wird gespeichert - nur OAuth-Token.
* Backup = diesen Ordner zusammen mit data/youtube_autoposter.db sichern.

Woher bekomme ich credentials.json?
-----------------------------------
1. https://console.cloud.google.com  -> Projekt erstellen
2. APIs & Dienste -> Bibliothek -> "YouTube Data API v3" aktivieren
3. Google Auth Platform (bzw. OAuth-Zustimmungsbildschirm):
   App-Name + E-Mail, User type "External", Publishing-Status "Testing",
   eigene Google-Adresse als Testnutzer eintragen
4. Clients -> Client erstellen -> Anwendungstyp: "Desktop-App"
5. JSON herunterladen -> als credentials/credentials.json speichern

Ausfuehrliche Anleitung: README.md im Projektordner oder im Dashboard
unter http://127.0.0.1:8765/setup
