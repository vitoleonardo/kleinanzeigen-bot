# Kleinanzeigen-Bot „Zu verschenken“

Der Bot prüft alle 5 Minuten die neuesten Zu-verschenken-Anzeigen in deiner Stadt. Claude bewertet jede neue Anzeige anhand deines Interessenprofils, des Fotos und deines 👍/👎-Feedbacks. Gute Treffer kommen sofort per Telegram, alle anderen findest du auf einer Karte im Browser.

Inoffizielles Projekt ohne Verbindung zu Kleinanzeigen. Du bist selbst dafür verantwortlich, dass deine Nutzung mit den Nutzungsbedingungen von Kleinanzeigen vereinbar ist.

## So funktioniert es

1. **Abruf:** Alle `KA_INTERVAL` Sekunden (Standard 5 Minuten) liest der Bot die neueste Seite der Kategorie „Zu verschenken“ für deinen Ort und Umkreis. Beim ersten Start merkt er sich die vorhandenen Anzeigen, ohne sie zu melden.
2. **Bewertung:** Für jede neue Anzeige holt er Beschreibung, Foto und Koordinaten. Claude vergibt einen Score von 0 bis 10 und begründet ihn in einem Satz. Maßstab sind dein Interessenprofil (`interests.md`) und deine bisherigen 👍/👎. Anzeigen mit Wörtern aus `KA_EXCLUDE` bekommen ohne KI-Aufruf Score 0.
3. **Meldung:** Ab `KA_MIN_SCORE` (Standard 7) schickt der Bot ein Foto mit drei Zeilen und Buttons an Telegram. Um 19 Uhr folgt eine Zusammenfassung der knapp verpassten Anzeigen (Score 5 bis `KA_MIN_SCORE`−1).
4. **Karte:** Alle bewerteten Anzeigen der letzten drei Tage stehen zusätzlich auf einer Karte im Browser (siehe unten).

So sieht eine Meldung aus:

```text
Stehlampe aus Messing · ⭐ 8
12345 Musterstadt · ca. 3 km
Wertige Vintage-Lampe, passt zu deinem Interesse an Designmöbeln.
[Anzeige ↗] [🗺 Karte] [✍️ Anfrage]
[👍] [👎]
```

## Schnellstart

Du brauchst:

- Linux oder macOS mit Python 3.9+ und `curl`. Getestet ist nur Linux; unter Windows fehlt eventuell `tzdata` (`pip install tzdata`).
- Einen Telegram-Account für den eigenen Bot.
- Für die Bewertung entweder [Claude Code](https://claude.com/claude-code) (`claude` im `PATH` und angemeldet; nutzt dein Abo) oder einen Anthropic-API-Key (Abrechnung pro Token).

```bash
git clone https://github.com/vitoleonardo/kleinanzeigen-bot
cd kleinanzeigen-bot
cp .env.example .env
```

1. Bei [@BotFather](https://t.me/BotFather) mit `/newbot` einen Bot anlegen und das Token als `TELEGRAM_BOT_TOKEN` in die `.env` eintragen.
2. In der `.env` deine Stadt festlegen:
   ```ini
   KA_LOCATION=Köln
   KA_RADIUS_KM=10
   ```
3. Bot starten und ihm in Telegram schreiben. Er antwortet mit deiner Chat-ID:
   ```bash
   python3 ka_bot.py
   ```
4. Die Chat-ID als `TELEGRAM_CHAT_ID` eintragen und den Bot neu starten.
5. `interests.md` anpassen oder das Profil in Telegram mit `/interessen` ersetzen. Die Datei entsteht beim ersten Start aus `interests.example.md`.

Beim ersten Durchlauf merkt sich der Bot die aktuellen Anzeigen, ohne sie zu melden. Danach meldet er nur noch neue.

Für den API-Modus zusätzlich `KA_JUDGE_BACKEND=api` und `ANTHROPIC_API_KEY` setzen und das SDK installieren:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python ka_bot.py
```

## Konfiguration

Alle Einstellungen stehen in der `.env`; `.env.example` beschreibt jede einzelne.

| Variable | Bedeutung | Standard |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Zugangsdaten für Telegram | Pflicht |
| `KA_LOCATION` | Ort oder PLZ | Pflicht |
| `KA_RADIUS_KM` | Umkreis: 5, 10, 20, 30, 50, 100, 150 oder 200 | `5` |
| `KA_JUDGE_BACKEND` | `cli` (Claude Code) oder `api` (Anthropic API) | `cli` |
| `ANTHROPIC_API_KEY` | nur für `api` | – |
| `KA_EXCLUDE` | Wörter, die ohne KI-Aufruf aussortieren, z. B. `baby*,kinderwagen` | – |
| `KA_MIN_SCORE` | Meldung ab diesem Score | `7` |
| `KA_BUDGET_EUR`, `KA_USD_EUR` | Monatsbudget und Umrechnungskurs | `5`, `0.92` |
| `KA_INTERVAL` | Abrufintervall in Sekunden | `300` |
| `KA_WEB_HOST`, `KA_WEB_PORT`, `KA_MAP_URL` | Karte; ohne Port ausgeschaltet | siehe `.env.example` |

### Ort wählen

Der Bot löst `KA_LOCATION` über die Ortssuche von Kleinanzeigen auf. Ist ein Name mehrdeutig, startet er nicht und nennt die passenden Schreibweisen, zum Beispiel:

```text
KA_LOCATION: „Neustadt“ ist nicht eindeutig. Trag eine dieser Zeilen ein:
  KA_LOCATION=Neustadt - Bremen
  KA_LOCATION=Neustadt - Hessen
  ...
```

Es gelten nur Orte in Deutschland, so wie Kleinanzeigen sie in der Suche anbietet. Ort oder Umkreis lassen sich jederzeit ändern. Nach dem Neustart gilt die neue Suche: Der Bot merkt sich die dortigen Anzeigen wieder ohne Meldung, und die Karte zeigt nur noch die neue Region. Dein Feedback bleibt erhalten.

## Feste Vorgaben

Diese Werte sind im Code festgelegt und nicht über die `.env` einstellbar:

- Ruhezeit 23–7 Uhr: Meldungen kommen stumm, ab Score 9 trotzdem mit Ton.
- Zusammenfassung der knapp verpassten Anzeigen um 19 Uhr.
- Zeitzone `Europe/Berlin` für Ruhezeit, Zusammenfassung und Tageszähler.
- Die Karte zeigt Anzeigen der letzten drei Tage. Anzeigen, die der Bot nicht innerhalb von 24 Stunden bewerten konnte, verfallen.
- Bewertungsprompt, Telegram-Texte und Karte sind deutsch. Gesucht wird ausschließlich auf kleinanzeigen.de.

## Betrieb als Dienst

`systemd/ka-bot.service` erwartet das Repository in `~/kleinanzeigen-bot` und ein venv in `.venv`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/systemd/user
cp systemd/ka-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ka-bot
loginctl enable-linger "$USER"          # Dienst läuft auch ohne Anmeldung
journalctl --user -u ka-bot -f          # Log inkl. Scores und Tokens je Aufruf
```

Der Zustand liegt in `ka.db` (SQLite).

## Karte und Liste

Die Karte läuft auf Port 8672. Für die Karten-Buttons in Telegram `KA_MAP_URL` auf eine aus dem Heimnetz oder VPN erreichbare Adresse setzen.

**Sicherheit:** Die Karte hat keine Anmeldung. Der Bot weist direkte Zugriffe von öffentlichen IPs ab, sieht hinter einem Reverse Proxy aber nur dessen Adresse. Dann muss der Proxy den Zugriff beschränken; `nginx/kleinanzeigen-bot.conf.example` zeigt das. Karte nicht ungeschützt ins Internet stellen.

- Suche in Titel, vollständiger Beschreibung und Ort; mehrere Suchwörter werden kombiniert.
- 25 Anzeigen pro Listenseite. Die Karte zeigt alle passenden Anzeigen mit Standort, unabhängig von der Listenseite. Anzeigen ohne Koordinaten bleiben in der Liste sichtbar.
- Titel, Bilder und Anzeigenlinks öffnen einen neuen Tab. „Auf Karte“ zeigt den Standort.
- 👍 „Gut“ und 👎 „Nein“ in Liste und Karten-Popup speichern dein Urteil, auch bei niedrigem KI-Score. Es fließt wie Telegram-Feedback in kommende Bewertungen ein. Der andere Button ändert dein Urteil; ein erneuter Klick auf den gewählten Button nimmt es zurück. Der KI-Score bleibt erhalten. Um schwächer bewertete Angebote zu sehen, den Filter „Score ≥ 7“ ausschalten.
- Suche, Score-Filter, Sortierung, Seite, Scrollposition, Kartenposition und geöffnete Anzeige bleiben beim Zurückkehren und Neuladen im selben Tab erhalten. Telegram-Direktlinks haben einen eigenen Zustand.
- Hintergrundabfragen alle fünf Minuten kündigen Änderungen über „Aktualisieren“ an; erst ein Klick übernimmt sie. Beim Übernehmen bleibt die oberste sichtbare Anzeige möglichst an derselben Stelle.
- Die Übersicht umfasst die letzten drei Tage. Standorte sind ungefähr.

## Telegram

- Buttons unter jeder Meldung:
  - 👍/👎 sind Feedback. Die letzten 30 Bewertungen fließen als Beispiele in die KI-Bewertung ein.
  - ✍️ erzeugt einen Vorschlag für eine Abhol-Nachricht.
  - „Anzeige ↗“ öffnet die Anzeige; „🗺 Karte“ öffnet sie in der Übersicht.
- Pro Anzeige kommt nur eine Foto-Nachricht mit drei kurzen Textzeilen; falls das Bild nicht geladen werden kann, eine Textnachricht.
- Ruhezeit 23–7 Uhr: Meldungen kommen stumm, Score 9–10 trotzdem mit Ton.
- 19 Uhr: Zusammenfassung der knapp verpassten Anzeigen (Score 5–6).
- Befehle (erscheinen in Telegram beim Tippen von `/`, Übersicht mit `/hilfe`):
  - `/status` zeigt den Stand.
  - `/tokens` zeigt den Verbrauch heute und im laufenden Monat.
  - `/interessen [text]` zeigt das Profil an bzw. ersetzt es durch den Text.
  - `/mehr <text>` ergänzt das Profil.
  - `/karte` schickt den Link zur Karte.
  - `/pause` und `/weiter` halten den Bot an bzw. lassen ihn weiterlaufen.
  - `/test` schickt eine Testnachricht.

## KI-Verbrauch

Der CLI-Modus speichert Bewertungen sieben Tage lang. Eine Wiederverwendung erfordert identischen Bewertungstext, vollständige Beschreibung, Foto-Inhalt, Interessenprofil, Feedback-Kontext, Modell und Bewertungsanweisungen. Kurze Standardbeschreibungen und fehlgeschlagene Fotoabrufe werden nicht zwischengespeichert. Der API-Modus nutzt diesen lokalen Cache nicht.

`/tokens` zeigt auch die durchschnittlichen Tokens pro Bewertung und die Anzahl wiederverwendeter Bewertungen. Das ist eine Messgröße, keine Anzeige des verbleibenden Claude-Abo-Kontingents.

## Datenschutz

Der Bot läuft auf deinem Rechner und speichert alles in `ka.db`. Dabei werden diese Dienste kontaktiert:

- **kleinanzeigen.de** für Suche und Detailseiten.
- **Anthropic** (bei `cli` über Claude Code, bei `api` direkt): Titel, Beschreibung, Ort und das Foto jeder bewerteten Anzeige sowie dein Interessenprofil und die letzten 30 Feedback-Titel.
- **Telegram** für Meldungen und Befehle. Antworten auf andere Chats ignoriert der Bot.
- **nominatim.openstreetmap.org** (OpenStreetMap), um den Mittelpunkt deines Orts und Anzeigen ohne Koordinaten zu verorten.
- **tiles.openfreemap.org** und **unpkg.com**: Dein Browser lädt von dort Kartenkacheln und MapLibre, sobald du die Karte öffnest.

## Fehlersuche

| Meldung oder Symptom | Ursache und Abhilfe |
| --- | --- |
| `KA_LOCATION fehlt` | In der `.env` einen Ort oder eine PLZ eintragen. |
| `„…“ ist nicht eindeutig` | Eine der genannten Schreibweisen als `KA_LOCATION` eintragen. |
| `„…“ nicht gefunden` | Schreibweise prüfen oder stattdessen die PLZ verwenden. |
| `KA_RADIUS_KM muss einer dieser Werte sein` | Nur 5, 10, 20, 30, 50, 100, 150 oder 200 sind erlaubt. |
| `claude CLI nicht gefunden` | Claude Code installieren und anmelden oder `KA_JUDGE_BACKEND=api` setzen. Unter systemd steht der `PATH` in `ka-bot.service`. |
| `TELEGRAM_BOT_TOKEN fehlt` | Token von @BotFather in die `.env` eintragen. |
| Bot antwortet mit der Chat-ID | `TELEGRAM_CHAT_ID` in die `.env` eintragen und neu starten. |
| Alarm „HTTP 403/429“ | Kleinanzeigen blockt die Abfragen. `KA_INTERVAL` erhöhen und eine Weile warten. |
| Alarm „0 Anzeigen gefunden“ | Kleinanzeigen hat sein Layout geändert, `ka_scrape.parse_list` braucht eine Anpassung. |
| Karten-Button in Telegram fehlt oder die Meldung kommt nicht an | `KA_MAP_URL` darf nicht `localhost` sein. Telegram lehnt solche Buttons ab. |
| Karte zeigt „forbidden“ | Zugriff von einer öffentlichen IP. Nur Heimnetz, VPN oder ein beschränkter Proxy sind erlaubt. |
| Keine Meldungen | `/status` in Telegram zeigt Pause, Budget und letzten Abruf. Mit `--dry-run --limit 5` lässt sich die Bewertung ohne Senden testen. |

## Entwicklung

```bash
uv run --no-project --with pytest python -m pytest -q tests
python3 ka_bot.py --dry-run --limit 5      # echte Seite, 5 Bewertungen, nichts senden
python3 ka_bot.py --send-top 1             # beste aktuelle Anzeige an Telegram schicken

# Optionale Browser-Tests mit echtem Chromium und MapLibre (CDN-Zugriff erforderlich):
uv run --no-project --with playwright python -m playwright install chromium
KA_BROWSER_TESTS=1 uv run --no-project --with pytest --with playwright python -m pytest -q tests
```

Für ein vorhandenes Chromium kann `KA_CHROMIUM=/pfad/zu/chrome` gesetzt werden. Browser-Tests verwenden synthetische Anzeigen und einen lokalen Testserver; Telegram und KI werden nicht aufgerufen.

Ändert Kleinanzeigen das Layout (0 Anzeigen bei HTTP 200), schickt der Bot einen Alarm. Dann `ka_scrape.parse_list` und die Fixtures in `tests/fixtures` anpassen. Die Fixtures enthalten bewusst erfundene Anzeigen: Bitte keine echten Seiten einchecken, sie enthalten Daten fremder Personen.

## Mitmachen

Fehler und Wünsche gern als Issue, Änderungen als Pull Request. Bitte vorher `pytest` laufen lassen und keine echten Kleinanzeigen-Seiten oder persönlichen Daten in die Fixtures legen. Da der Bot eine Webseite ohne offizielle Schnittstelle liest, kann ein Layoutwechsel ihn jederzeit brechen. Es gibt keine Gewährleistung.

## Lizenz

[MIT](LICENSE)
