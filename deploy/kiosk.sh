#!/bin/bash
# Startet Chromium im Kiosk-Modus, sobald das Dashboard-Backend antwortet.
set -eu

URL="${DASHBOARD_URL:-http://127.0.0.1:8080}"
HEALTH_URL="${URL}/api/health"
WAIT_SECONDS="${DASHBOARD_WAIT_SECONDS:-120}"

# Chromium-Binary erkennen (Bookworm: chromium; aeltere Systeme: chromium-browser)
if command -v chromium >/dev/null 2>&1; then
    CHROMIUM=chromium
elif command -v chromium-browser >/dev/null 2>&1; then
    CHROMIUM=chromium-browser
else
    echo "kiosk.sh: Chromium nicht gefunden (sudo apt install chromium)" >&2
    exit 1
fi

# Auf das Backend warten (max. WAIT_SECONDS), danach trotzdem starten:
# das Frontend zeigt selbst "Keine Verbindung" und laedt neu.
waited=0
until curl -fsS -o /dev/null --max-time 2 "$HEALTH_URL"; do
    waited=$((waited + 2))
    if [ "$waited" -ge "$WAIT_SECONDS" ]; then
        echo "kiosk.sh: Backend nach ${WAIT_SECONDS}s nicht erreichbar, starte Chromium trotzdem" >&2
        break
    fi
    sleep 2
done

# Endlosschleife: Chromium bei Absturz/Beenden neu starten
while true; do
    # Absturz-Hinweis ("Chromium wurde nicht korrekt beendet") unterdruecken
    PREFS="$HOME/.config/chromium/Default/Preferences"
    if [ -f "$PREFS" ]; then
        sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"Crashed"/"exit_type":"Normal"/' "$PREFS" || true
    fi

    "$CHROMIUM" \
        --kiosk \
        --noerrdialogs \
        --disable-infobars \
        --disable-session-crashed-bubble \
        --no-first-run \
        --password-store=basic \
        --incognito \
        --overscroll-history-navigation=0 \
        --disable-pinch \
        --check-for-update-interval=31536000 \
        --ozone-platform=wayland \
        --start-maximized \
        --touch-events=enabled \
        --disable-translate \
        --disable-features=Translate,TranslateUI \
        --autoplay-policy=no-user-gesture-required \
        "$URL" || true

    echo "kiosk.sh: Chromium beendet, Neustart in 3s" >&2
    sleep 3
done
