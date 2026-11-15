# Raspberry-Pi-Dashboard

Ein Touch-Dashboard für einen Raspberry Pi im Kiosk-Betrieb. Es zeigt Todoist-Aufgaben und den Exchange-/Outlook-Kalender an (Material Design 3, dunkles Theme) und bestimmt daraus einen Verfügbarkeitsstatus (**Frei / Beschäftigt / Nicht stören**). Der Status lässt sich per Touch übersteuern und wird per MQTT an ein externes Display-Gerät (z. B. ESP32 mit Statusanzeige) gesendet.

## Inhalt

1. [Überblick](#überblick)
2. [Voraussetzungen](#voraussetzungen)
3. [Installation](#installation)
4. [Entwicklung](#entwicklung)
5. [MQTT-Schnittstelle](#mqtt-schnittstelle)
6. [REST-API](#rest-api)
7. [Fehlersuche](#fehlersuche)
8. [Update](#update)

## Überblick

- **Aufgaben:** offene Todoist-Aufgaben, gruppiert nach Projekt (inkl. Unterprojekte); jeder Abschnitt eines Projekts erhält eine eigene Kachel. Mit Priorität und Fälligkeit. Unteraufgaben werden eingerückt direkt unter ihrer Elternaufgabe angezeigt. Überfällige Aufgaben sind rot, heute fällige gelb unterlegt.
- **Kalender:** Tagesansicht als Zeitleiste mit „Jetzt“-Strich und Liste der nächsten Tage (Quelle: veröffentlichte ICS-Datei).
- **Heute:** Tagesübersicht mit den heutigen Terminen als Liste und allen heute fälligen bzw. überfälligen Aufgaben, sortiert nach Priorität (innerhalb einer Priorität: überfällig zuerst, dann nach Uhrzeit).
- **Status:** automatisch aus dem Kalender (Termin läuft = Beschäftigt) oder manuell per Button, bis wieder „Auto“ gewählt wird.
- **Offline-fähig:** keine CDNs, Schriften und Icons liegen lokal; bei Ausfall einer Quelle bleiben die zuletzt geladenen Daten sichtbar.

Architektur:

```
Todoist API ──┐                         ┌── Chromium (Kiosk, Touch)
              ├─► FastAPI-Backend ◄─────┤        http://127.0.0.1:8080
Outlook ICS ──┘   (systemd-Dienst)      │
                       │                 
                       └─► Mosquitto (MQTT) ──► ESP / Display-Gerät im LAN
                              topic: dashboard/availability
```

Projektstruktur (Auszug): `backend/` (Server), `frontend/` (Oberfläche), `tests/`, `deploy/` (systemd, Kiosk, Mosquitto), `config.yaml.example`.

> **Phase 6 (ESP-Firmware) ist nicht Teil dieses Projekts.** Es ist nur die MQTT-Schnittstelle definiert (siehe unten).

## Voraussetzungen

- Raspberry Pi (3B+ oder neuer empfohlen) mit **Raspberry Pi OS Bookworm (Desktop)**, 64 Bit, mit Wayland-Compositor **labwc** (Standard seit Ende 2024) und automatischer Anmeldung am Desktop (`sudo raspi-config` → System Options → Boot / Auto Login → Desktop Autologin).
- Touchscreen (getestet für 800×480 und 1024×600).
- Netzwerkzugang (WLAN/LAN) zum Pi; für Todoist und die Kalender-ICS ein Internetzugang.
- Python 3.11 (in Bookworm enthalten).
- Todoist-Konto und ein Outlook-/Exchange-Kalender, der als ICS veröffentlicht werden darf (Administrator-Richtlinie beachten).

## Installation

Alle Befehle auf dem Pi. Als Benutzer wird `pi` und der Pfad `/home/pi/pi-dashboard` angenommen. Weicht Ihr Benutzername ab, siehe [Anderen Benutzer verwenden](#anderen-benutzer-verwenden).

### 1. Projekt auf den Pi kopieren

Vom Entwicklungsrechner aus (Beispiel; Hostname/IP anpassen):

```bash
scp -r pi-dashboard pi@raspberrypi.local:/home/pi/
```

Alternativ per `git clone` oder USB-Stick. Danach das Kiosk-Skript ausführbar machen:

```bash
chmod +x /home/pi/pi-dashboard/deploy/kiosk.sh
```

Falls die Dateien unter Windows bearbeitet wurden, sicherstellen, dass `deploy/kiosk.sh` Unix-Zeilenenden (LF) hat: `sed -i 's/\r$//' deploy/kiosk.sh`.

### 2. Pakete installieren

```bash
sudo apt update
sudo apt install -y python3-venv mosquitto mosquitto-clients fonts-roboto curl chromium
```

(Auf älteren Systemen heißt das Browser-Paket `chromium-browser`; `kiosk.sh` erkennt beide Varianten.)

### 3. Python-Umgebung

```bash
cd /home/pi/pi-dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4. Konfiguration (`config.yaml`)

```bash
cp config.yaml.example config.yaml
nano config.yaml
```

Der Pfad kann mit der Umgebungsvariablen `DASHBOARD_CONFIG` überschrieben werden (Standard: `config.yaml` im Projektordner). Die Datei enthält Zugangsdaten: `chmod 600 config.yaml`.

**`server`**

| Schlüssel | Bedeutung |
|---|---|
| `host`, `port` | Standard `127.0.0.1:8080`. Nur lokal erreichbar, da der Browser auf dem Pi läuft. |
| `state_file` | Datei für den gespeicherten Modus (Auto/manuell), überlebt Neustarts. |
| `mock` | `true` = Beispieldaten ohne Todoist/Kalender/MQTT (nur für Entwicklung). Im Betrieb `false`. |

**`todoist`**

- `token`: persönlicher API-Token. Zu finden in Todoist unter **Einstellungen → Integrationen → Entwickler** (Web-Oberfläche). Token kopieren und in `config.yaml` eintragen. Den Token niemals weitergeben oder einchecken.
- `filter` (optional): Todoist-Filterausdruck, z. B. `today | overdue`. Leer = alle offenen Aufgaben.
- `project` (optional): nur ein Projekt (Name) samt aller Unterprojekte.
- `refresh_seconds`, `timeout_seconds`: Abrufintervall und Zeitlimit.

**`calendar`**

- `ics_url`: Link zur veröffentlichten ICS-Datei. Erzeugen in **Outlook im Web → Einstellungen → Kalender → Freigegebene Kalender → Kalender veröffentlichen**:
  1. Kalender wählen, Berechtigung wählen, **Veröffentlichen**.
  2. Den **ICS-Link** kopieren (nicht den HTML-Link).
  - **Wichtig:** Die Berechtigung bestimmt den Inhalt. Bei **„Kann Titel, Ort sehen“** enthält die ICS Titel und Ort der Termine. Bei **„Nur Verfügbarkeit“** (Frei/Beschäftigt) fehlen die Titel; das Dashboard zeigt dann nur Zeitblöcke und „Im Termin“. Für Aufgaben-/Termintitel auf dem Display also „Titel, Ort“ wählen. Die ICS-URL ist für jeden mit dem Link lesbar: behandeln Sie sie vertraulich.
  - Manche Organisationen erlauben das Veröffentlichen nicht (Administrator-Richtlinie).
- `refresh_seconds`, `timeout_seconds`: Abruf und Zeitlimit.
- `days_ahead`: wie viele Folgetage angezeigt werden.
- `timezone`: Anzeigezone und Fallback (Standard `Europe/Berlin`).
- `day_start_hour`, `day_end_hour`: Ausschnitt der Zeitleiste.
- `show_titles_in_status`: `false` zeigt im Status „Im Termin“ statt des Titels (Datenschutz, auch im MQTT-Label).

**`mqtt`**

- `enabled`: `false` deaktiviert MQTT.
- `host`/`port`: bei lokalem Mosquitto `localhost:1883`.
- `username`/`password`: Benutzer `dashboard` und das in Schritt 5 vergebene Passwort.
- `topic`, `online_topic`, `heartbeat_seconds`: siehe [MQTT-Schnittstelle](#mqtt-schnittstelle).

**`display`**

- `default_view` (`tasks` | `calendar` | `summary`), `return_after_seconds` (Rückkehr zur Standardansicht nach Inaktivität), `theme` (`dark` | `light`), `priority_colors`, `status_colors`.

Konfiguration testen (kurzer Start im Vordergrund, Abbruch mit Strg+C):

```bash
cd /home/pi/pi-dashboard
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8080
curl http://127.0.0.1:8080/api/health
```

### 5. Mosquitto (MQTT-Broker)

```bash
sudo cp /home/pi/pi-dashboard/deploy/mosquitto.conf /etc/mosquitto/conf.d/dashboard.conf
sudo mosquitto_passwd -c /etc/mosquitto/passwd dashboard    # fragt nach Passwort; -c legt die Datei neu an
sudo mosquitto_passwd /etc/mosquitto/passwd esp             # zweiter Benutzer für das Display-Gerät (ohne -c!)
sudo chown mosquitto:mosquitto /etc/mosquitto/passwd
sudo chmod 640 /etc/mosquitto/passwd
sudo systemctl enable --now mosquitto
sudo systemctl restart mosquitto
```

Die Konfiguration enthält `listener 1883` (alle Schnittstellen, damit der ESP im LAN verbinden kann), `allow_anonymous false` und `password_file`. `persistence true` (nötig für die retained-Nachrichten) ist in der Debian-Hauptkonfiguration `/etc/mosquitto/mosquitto.conf` bereits gesetzt. Mosquitto 2.x (Bookworm) lauscht ohne expliziten Listener nur lokal.

Das Passwort für `dashboard` gehört in `config.yaml` (`mqtt.password`); das für `esp` in die ESP-Firmware.

**Optional: Zugriffsrechte (ACL).** Datei `/etc/mosquitto/acl`:

```
user dashboard
topic readwrite dashboard/#

user esp
topic read dashboard/#
```

Dann in `/etc/mosquitto/conf.d/dashboard.conf` die Zeile `acl_file /etc/mosquitto/acl` aktivieren und `sudo systemctl restart mosquitto` ausführen.

Schnelltest:

```bash
mosquitto_pub -h localhost -u dashboard -P '<passwort>' -t test -m hallo
mosquitto_sub -h localhost -u esp -P '<passwort>' -t 'dashboard/#' -v
```

Hinweis: Der Port 1883 ist unverschlüsselt. Nur im vertrauenswürdigen LAN betreiben, nicht ins Internet freigeben.

### 6. Backend als systemd-Dienst

```bash
sudo cp /home/pi/pi-dashboard/deploy/pi-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-dashboard
systemctl status pi-dashboard
curl http://127.0.0.1:8080/api/health
```

Der Dienst startet nach Netzwerk und Mosquitto, wird bei Fehlern nach 5 s neu gestartet und läuft ohne Anmeldung.

#### Anderen Benutzer verwenden

Heißt Ihr Benutzer nicht `pi`, ersetzen Sie `pi` bzw. `/home/pi` in drei Dateien:

```bash
cd /home/pi/pi-dashboard   # bzw. Ihr Pfad
sed -i 's#/home/pi#/home/MEINUSER#g; s#^User=pi#User=MEINUSER#; s#^Group=pi#Group=MEINUSER#' deploy/pi-dashboard.service
sed -i 's#/home/pi#/home/MEINUSER#g' deploy/labwc-autostart
```

`kiosk.sh` benötigt keine Anpassung (nutzt `$HOME`). Den korrekten Pfad nutzen Sie auch bei `systemctl`-Befehlen und in `config.yaml`.

### 7. Kiosk-Autostart (labwc)

Als der am Desktop angemeldete Benutzer:

```bash
mkdir -p ~/.config/labwc
cp /home/pi/pi-dashboard/deploy/labwc-autostart ~/.config/labwc/autostart
```

Die Datei startet `deploy/kiosk.sh` im Hintergrund (`&`). Das Skript wartet, bis `http://127.0.0.1:8080/api/health` antwortet (max. 120 s), startet dann Chromium im Kiosk-Modus und startet ihn neu, falls er beendet wird.

Die systemweite Datei `/etc/xdg/labwc/autostart` startet weiterhin Desktop-Komponenten (Panel, Hintergrund usw.). Die Datei in `~/.config/labwc/` ergänzt sie bzw. ersetzt sie je nach labwc-Version; prüfen Sie nach dem Neustart, ob das Panel erscheint.

#### Panel ausblenden (optional)

Für einen sauberen Kiosk die Leiste (`wf-panel-pi`) beenden: in `~/.config/labwc/autostart` die Zeile `pkill wf-panel-pi` (vor dem Aufruf von `kiosk.sh`) aktivieren. Da das Panel evtl. erst nach Ihrem Eintrag startet, kann stattdessen `sleep 3; pkill wf-panel-pi &` helfen. Chromium im Kiosk-Modus liegt ohnehin im Vordergrund und deckt das Panel in der Regel ab.

#### Mauszeiger ausblenden (optional)

Bei reiner Touch-Bedienung verschwindet der Zeiger meist von selbst. Bleibt er sichtbar, ist eine einfache Möglichkeit ein transparentes bzw. leeres Cursor-Theme: in `~/.config/labwc/environment` `XCURSOR_THEME=<theme>` und `XCURSOR_SIZE=1` setzen, wobei `<theme>` ein transparentes Theme ist (z. B. selbst erstelltes leeres Theme unter `~/.icons/`). Dies ist optional und je nach labwc-Version unterschiedlich; nicht getestet.

#### Anzeige drehen (falls nötig)

Mit `wlr-randr` (z. B. `wlr-randr --output DSI-1 --transform 90`; Ausgabenamen mit `wlr-randr` ohne Argumente anzeigen) oder dem Programm **Screen Configuration** im Menü. Zum dauerhaften Übernehmen die `wlr-randr`-Zeile in `~/.config/labwc/autostart` vor `kiosk.sh` eintragen. Touch-Koordinaten müssen ggf. mitgedreht werden (labwc `rc.xml`, `<touch mapToOutput=...>`).

### 8. Bildschirmschoner deaktivieren

```bash
sudo raspi-config nonint do_blanking 1
```

(`1` = Bildschirmausschaltung deaktivieren.) Alternativ in `sudo raspi-config` unter Display Options → Screen Blanking. Auf Bookworm/labwc greift dies über `swayidle`/den Display-Timeout; falls der Bildschirm dennoch dunkel wird, prüfen Sie `~/.config/labwc/autostart` und `/etc/xdg/labwc/autostart` auf `swayidle`-Einträge.

### 9. Neustart und Test

```bash
sudo reboot
```

Nach dem Start sollte das Dashboard automatisch im Vollbild erscheinen. Kontrolle:

- Status-Buttons am Display bedienen, dann `mosquitto_sub -h <pi> -u esp -P '<passwort>' -t 'dashboard/#' -v` beobachten.
- Backend kurz stoppen (`sudo systemctl stop pi-dashboard`): Das Dashboard zeigt „Keine Verbindung zum Pi-Backend“ und behält die Daten; nach `start` verschwindet der Hinweis.

## Entwicklung

Das Dashboard läuft auch auf Windows, macOS und Linux ohne Pi:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp config.yaml.example config.yaml     # Windows: copy config.yaml.example config.yaml
```

In `config.yaml` den Mock-Modus aktivieren (keine Zugangsdaten nötig):

```yaml
server:
  mock: true
```

Starten und im Browser öffnen:

```bash
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8080 --reload
# http://127.0.0.1:8080  (Fenstergröße 800x480 / 1024x600 testen)
```

Tests:

```bash
pytest
```

## MQTT-Schnittstelle

| | |
|---|---|
| Broker | Mosquitto auf dem Pi, Port 1883, Benutzer/Passwort |
| Status-Topic | `dashboard/availability` (QoS 1, **retained**) |
| Online-Topic | `dashboard/online` (retained) |

**Payload** (JSON) auf `dashboard/availability`:

```json
{"status": "busy", "mode": "auto", "until": "2026-11-15T15:30:00+01:00", "label": "Teams-Meeting"}
```

| Feld | Werte |
|---|---|
| `status` | `free`, `busy`, `dnd` |
| `mode` | `auto` (aus Kalender) oder `manual` |
| `until` | Ende des aktuellen Termins (ISO 8601 mit Offset) oder `null` |
| `label` | Anzeigetext (Terminname, „Im Termin“, „Frei“, „Beschäftigt“, „Nicht stören“) |

**Verhalten**

- Versand bei jeder Änderung (sofort beim Tastendruck) und als **Heartbeat** alle `heartbeat_seconds` (Standard 300 s). Ein Empfänger kann bei Ausbleiben des Heartbeats einen Ausfall annehmen.
- **Last Will:** `dashboard/online` = `false` (retained), wenn das Backend unerwartet die Verbindung verliert; nach dem Verbinden `true` (retained).
- Da die Nachrichten retained sind, erhält ein neu verbundener ESP sofort den aktuellen Stand.
- Ein Broker-Ausfall stört das Backend nicht; es verbindet automatisch neu.

**Test vom Entwicklungsrechner oder Pi:**

```bash
mosquitto_sub -h <pi> -u esp -P '<passwort>' -t 'dashboard/#' -v
```

## Schnelleinstellungen (Pull-Down)

Vom oberen Rand bzw. der App-Bar nach unten wischen (Touch oder Maus) öffnet das Panel mit Anwesenheitsstatus (Auto/Frei/Beschäftigt/Nicht stören) und Netzwerk-Adressen. Schließen: nach oben wischen, auf den abgedunkelten Hintergrund tippen, den Schließen-Button oder Esc.

## REST-API

Das Backend lauscht standardmäßig nur auf `127.0.0.1`.

| Methode und Pfad | Zweck |
|---|---|
| `GET /api/tasks` | Aufgaben als Kacheln je Projekt bzw. Abschnitt (`id`, `name`, `section`, `parent`, `color`, `tasks`; jede Aufgabe mit `depth` = Einrückungstiefe, 0 = oberste Ebene) |
| `GET /api/calendar` | Kalendertage (heute + `days_ahead`) |
| `GET /api/availability` | aktueller Status |
| `POST /api/availability` | Status setzen: `{"mode": "auto"}` oder `{"mode": "manual", "status": "free\|busy\|dnd"}` (ungültig: HTTP 422) |
| `GET /api/settings` | Anzeige-Einstellungen für das Frontend |
| `GET /api/network` | IP-Adressen des Geräts: `{"interfaces": [{"name": "wlan0", "address": "192.168.1.23", "family": "ipv4"}]}` (ohne Loopback, IPv4 zuerst) |
| `GET /api/health` | Zustand von Todoist, Kalender, MQTT |
| `GET /` | Oberfläche |

Beispiel:

```bash
curl -X POST http://127.0.0.1:8080/api/availability \
  -H 'Content-Type: application/json' -d '{"mode":"manual","status":"dnd"}'
```

## Fehlersuche

- **Backend-Log:** `journalctl -u pi-dashboard -f`
- **Läuft das Backend?** `systemctl status pi-dashboard` und `curl http://127.0.0.1:8080/api/health` (zeigt `last_success` / `last_error` je Quelle).
- **Chromium zeigt eine Fehlerseite (z. B. „Seite nicht erreichbar“):** Backend läuft nicht oder noch nicht. Dienst prüfen; Chromium wird von `kiosk.sh` neu gestartet, oder Seite per Touch/Neustart neu laden (`pkill chromium` löst einen Neustart durch die Schleife aus).
- **Kein Kiosk nach Neustart:** Datei `~/.config/labwc/autostart` vorhanden? `kiosk.sh` ausführbar und mit LF-Zeilenenden? Manuell testen: `/home/pi/pi-dashboard/deploy/kiosk.sh`. Läuft der Desktop wirklich unter labwc/Wayland (`echo $XDG_SESSION_TYPE`)? Unter X11 den Flag `--ozone-platform=wayland` entfernen.
- **Kalender: ICS 404 / 403:** Link ungültig oder Veröffentlichung widerrufen/nicht erlaubt. In Outlook im Web neu veröffentlichen und den neuen ICS-Link eintragen.
- **Kalender ohne Titel:** Veröffentlichung steht auf „Nur Verfügbarkeit“; auf „Kann Titel, Ort sehen“ ändern.
- **Todoist 401/403:** Token falsch oder widerrufen. Neu aus Todoist (Einstellungen → Integrationen → Entwickler) kopieren; keine Anführungszeichen-/Leerzeichenfehler in `config.yaml`.
- **MQTT nicht verbunden (`mqtt.connected: false`):** `systemctl status mosquitto`; Benutzer/Passwort in `config.yaml` gegen `/etc/mosquitto/passwd` prüfen (Test mit `mosquitto_pub`); Logs mit `journalctl -u mosquitto`; fehlt der `listener`, ist der Broker von außen nicht erreichbar. Firewall/Port 1883 beachten.
- **Bildschirm wird dunkel:** Schritt „Bildschirmschoner deaktivieren“ wiederholen.
- **Schrift sieht anders aus:** `sudo apt install fonts-roboto`, Chromium neu starten.

## Update

```bash
cd /home/pi/pi-dashboard
# neue Dateien einspielen (git pull oder scp), config.yaml bleibt unverändert
.venv/bin/pip install -r requirements.txt
sudo cp deploy/pi-dashboard.service /etc/systemd/system/   # nur falls geändert; Benutzer/Pfade prüfen
sudo systemctl daemon-reload
sudo systemctl restart pi-dashboard
pkill chromium    # kiosk.sh startet den Browser neu und lädt das Frontend
```

Neue Optionen aus `config.yaml.example` bei Bedarf in die eigene `config.yaml` übernehmen. Zum Sichern genügt `config.yaml` (und optional `state.json`).
