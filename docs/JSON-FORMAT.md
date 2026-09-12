# JSON-Format der Episoden-Metadaten

Jede Episode im Ordner `folders\READY\` besteht aus einem Paar gleicher Dateinamen:

```
folders\READY\video_001.mp4      <- Video (Pflicht)
folders\READY\video_001.json     <- Metadaten (Pflicht)
folders\READY\video_001.jpg      <- Thumbnail (optional, auch .jpeg/.png)
```

Der Basisname (`video_001`) ist die **Episode-ID** und eindeutig in der Datenbank.
Ein lauffähiges Komplettbeispiel liegt in `examples\ready_example\`.

---

## 1. Minimale JSON (reicht für einen Upload)

```json
{
  "title": "Die verborgene Lehre des Thot",
  "description": "Eine Folge ueber Thot, Schrift und Weisheit."
}
```

Alles andere kommt aus `config.json`:
`DEFAULT_CATEGORY_ID` (Standard `22` = People & Blogs), `DEFAULT_LANGUAGE` (`de`),
`DEFAULT_MADE_FOR_KIDS` (`false`), `DEFAULT_PRIVACY_STATUS` (`private`, erzwungen).

## 2. Volle JSON (alle unterstützten Felder)

```json
{
  "episode_id": "video_001",
  "title": "Die verborgene Lehre des Thot",
  "description": "Eine Reise in die Welt der alten aegyptischen Symbolik.\n\nKapitel:\n00:00 Einleitung\n00:30 Wer war Thot?\n02:00 Die Smaragdtafel\n03:00 Fazit\n\n#Thot #Aegypten #Hermetik",
  "tags": ["Thot", "Aegypten", "Hermetik", "Bewusstsein", "Mystik"],
  "category_id": "27",
  "language": "de",
  "audio_language": "de",
  "privacy_status": "private",
  "thumbnail": "video_001.jpg",
  "publish_at": null,
  "made_for_kids": false,
  "self_declared_made_for_kids": false,
  "contains_synthetic_media": false,
  "license": "youtube",
  "embeddable": true,
  "notify_subscribers": false,
  "playlist_id": null,
  "channel_identifier": null
}
```

## 3. Feldreferenz

| Feld | Typ | Pflicht | Gesendet als | Limit / Hinweis |
|---|---|---|---|---|
| `title` | String | **ja** | `snippet.title` | max. **100 Zeichen** |
| `description` | String | **ja** | `snippet.description` | max. **5000 Zeichen**; `\n` für Zeilenumbruch; Kapitel als Text erlaubt |
| `tags` | Array oder String | nein | `snippet.tags` | max. **100 Zeichen je Tag**, **500 Zeichen gesamt**; String wird an Kommas getrennt |
| `category_id` | String/Zahl | nein | `snippet.categoryId` | als **Text** angeben, z. B. `"27"`; `22` People & Blogs, `24` Entertainment, `27` Education, `10` Music, `17` Sports, `28` Science & Technology |
| `language` | String | nein | `snippet.defaultLanguage` | z. B. `"de"` |
| `audio_language` | String | nein | `snippet.defaultAudioLanguage` | z. B. `"de"` |
| `thumbnail` | String | nein | `thumbnails.set` | Dateiname **relativ zum READY-Ordner** (oder absolut); max. **2 MB**, `jpg`/`jpeg`/`png`, empfohlen **1280×720** (16:9). Fehler hier brechen den Upload **nicht** ab |
| `privacy_status` | String | nein | **wird nicht gesendet** | Upload ist immer `private`. `public`/`unlisted` werden als Wunsch gespeichert (`privacy_status_requested`) und im Dashboard als Warnung markiert |
| `publish_at` | String/`null` | nein | **wird nicht gesendet** | z. B. `"2026-10-01T18:00:00Z"`; nur als Hinweis (`publish_at_requested`). Keine Terminveröffentlichung in V1 |
| `made_for_kids` | Boolean | nein | `status.selfDeclaredMadeForKids` | COPPA; Fallback `self_declared_made_for_kids` → `DEFAULT_MADE_FOR_KIDS` |
| `self_declared_made_for_kids` | Boolean | nein | `status.selfDeclaredMadeForKids` | Vorrang vor `made_for_kids` |
| `contains_synthetic_media` | Boolean | nein | `status.containsSyntheticMedia` | nur wenn `SEND_SYNTHETIC_MEDIA_FLAG: true` (Standard: false) |
| `license` | String | nein | `status.license` | `"youtube"` (Standard) oder `"creativeCommon"` |
| `embeddable` | Boolean | nein | `status.embeddable` | Einbetten erlauben |
| `notify_subscribers` | Boolean | nein | Parameter `notifySubscribers` | Standard **false** – private Uploads benachrichtigen niemanden |
| `playlist_id` | String/`null` | nein | – | wird gespeichert, in V1 nicht ausgewertet |
| `channel_identifier` | String/`null` | nein | – | wird gespeichert (für späteren Mehrkanal-Betrieb) |
| `episode_id` | String | nein | – | überschreibt den Dateinamen als ID |

**Unbekannte Felder** werden nicht verworfen: Sie bleiben vollständig in
`metadata_json` der Datenbank erhalten und erscheinen im Dashboard als Hinweis
(„unbekannte Felder"). Gesendet wird nur das, was die YouTube-API kennt.

## 4. Deutsche Aliase

Groß-/Kleinschreibung und Bindestriche/Unterstriche werden normalisiert, außerdem sind
diese Alternativnamen erlaubt:

| Kanonisches Feld | Akzeptierte Aliase |
|---|---|
| `title` | `titel`, `video_title`, `videotitle`, `name` |
| `description` | `beschreibung`, `desc`, `descr`, `video_description`, `text` |
| `tags` | `keywords`, `keyword`, `schlagwoerter`, `tag_list` |
| `category_id` | `categoryid`, `category`, `kategorie`, `youtube_category`, `catid` |
| `language` | `lang`, `default_language`, `defaultlanguage`, `sprache` |
| `audio_language` | `defaultaudiolanguage`, `audio_lang`, `tonsprache` |
| `privacy_status` | `privacy`, `privacystatus`, `sichtbarkeit`, `visibility` |
| `thumbnail` | `thumb`, `thumbnail_file`, `thumbnail_filename`, `vorschaubild`, `cover` |
| `publish_at` | `publishat`, `scheduled_at`, `schedule`, `publish_date`, `veroeffentlichen_am` |
| `playlist_id` | `playlistid`, `playlist` |
| `channel_identifier` | `channel_id`, `channelid`, `channel`, `kanal` |
| `made_for_kids` | `madeforkids`, `kinder`, `for_kids` |
| `self_declared_made_for_kids` | `selfdeclaredmadeforkids`, `coppa` |
| `contains_synthetic_media` | `containssyntheticmedia`, `synthetic_media`, `ai_generated`, `ki_inhalt` |
| `embeddable` | `einbettbar` |
| `license` | `lizenz` |
| `notify_subscribers` | `notifysubscribers`, `abonnenten_benachrichtigen` |

## 5. Prüfungen und Fehlercodes

`python app.py validate` bzw. der Dry-Run liefern die Details. Fehler führen zum Status
**FEHLER**, die Dateien werden nach `FAILED\` verschoben (nichts wird gelöscht).

| Code | Bedeutung |
|---|---|
| `metadata_missing` | JSON-Datei fehlt (`REQUIRE_METADATA_FILE=true`) |
| `title_missing` / `title_too_long` | Titel fehlt / über 100 Zeichen |
| `description_missing` / `description_too_long` | Beschreibung fehlt / über 5000 Zeichen |
| `tag_too_long` / `tags_too_long_total` / `tag_invalid_type` | Tag über 100 Zeichen / Summe über 500 Zeichen / falscher Datentyp |
| `category_missing` / `category_invalid` | Kategorie fehlt / unbekannt (Online-Prüfung, 1 Einheit, 7 Tage gecacht) |
| `video_missing` / `video_not_a_file` / `video_empty` / `video_unreadable` | Videodatei fehlt / ist ein Ordner / 0 Byte / nicht lesbar |
| `video_unsupported_format` | Endung nicht in `ALLOWED_VIDEO_EXTENSIONS` |
| `video_too_large` / `video_too_long` / `video_duration_invalid` | > 256 GB / > 12 h / Dauer nicht ermittelbar |
| `thumbnail_empty` / `thumbnail_unreadable` / `thumbnail_too_large` / `thumbnail_unsupported_format` | Thumbnail-Probleme (unkritisch, nur Warnung) |
| `metadata_invalid` | JSON syntaktisch ungültig oder Pflichtfeld fehlt (Meldung im Dashboard/Log) |
| `duplicate` | gleiche Episode-ID oder gleicher Dateiinhalt (SHA-256) schon vorhanden → `SKIPPED\` |
| `upload_failed` / `validation_failed` / `file_missing` / `paused` | Laufzeit-Zustände der Pipeline |

## 6. Praktische Hinweise

- **JSON muss UTF-8 sein** (ohne BOM). Umlaute sind erlaubt; `\n` erzeugt einen Umbruch.
- **Kommentare sind in JSON nicht erlaubt** – sonst `metadata_invalid`.
- Kapitel, Hashtags und Links einfach als Text in die `description` schreiben.
- Änderungen an einer schon erkannten Episode werden übernommen, solange sie noch
  nicht hochgeladen ist (`Neu aufgenommen` → `Aktualisiert`).
- Nach dem Hochladen bleiben die Original-JSONs in `UPLOADED_PRIVATE\` erhalten; der
  gespeicherte Stand liegt zusätzlich in der Datenbank.
