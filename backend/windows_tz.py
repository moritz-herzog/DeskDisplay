"""Windows-Zeitzonennamen (Exchange/Outlook) -> IANA-Namen und ICS-Vorverarbeitung."""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

WINDOWS_TO_IANA: dict[str, str] = {
    # UTC / GMT
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    # Europa
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau",
    "GTB Standard Time": "Europe/Bucharest",
    "FLE Standard Time": "Europe/Kiev",
    "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Belarus Standard Time": "Europe/Minsk",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "Greenland Standard Time": "America/Godthab",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Egypt Standard Time": "Africa/Cairo",
    "Israel Standard Time": "Asia/Jerusalem",
    "Jordan Standard Time": "Asia/Amman",
    "Middle East Standard Time": "Asia/Beirut",
    "Arab Standard Time": "Asia/Riyadh",
    "Arabian Standard Time": "Asia/Dubai",
    "E. Africa Standard Time": "Africa/Nairobi",
    # USA / Kanada / Amerika
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "Canada Central Standard Time": "America/Regina",
    "Central America Standard Time": "America/Guatemala",
    "Mexico Standard Time": "America/Mexico_City",
    "SA Pacific Standard Time": "America/Bogota",
    "SA Western Standard Time": "America/La_Paz",
    "SA Eastern Standard Time": "America/Cayenne",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Pacific SA Standard Time": "America/Santiago",
    # Asien / Pazifik
    "India Standard Time": "Asia/Kolkata",
    "Pakistan Standard Time": "Asia/Karachi",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei",
    "W. Australia Standard Time": "Australia/Perth",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "E. Australia Standard Time": "Australia/Brisbane",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "New Zealand Standard Time": "Pacific/Auckland",
}

_WIN_LOWER = {k.lower(): v for k, v in WINDOWS_TO_IANA.items()}

# TZID=... als Parameter (Wert evtl. in Anführungszeichen)
_TZID_PARAM = re.compile(r'TZID=(?:"([^"]*)"|([^:;,]+))')
_VTIMEZONE_BLOCK = re.compile(r"BEGIN:VTIMEZONE\r?\n.*?END:VTIMEZONE\r?\n?", re.DOTALL)
_X_WR_TZ = re.compile(r"^(X-WR-TIMEZONE[^:\n]*:)(.*?)(\r?)$", re.MULTILINE)


@lru_cache(maxsize=256)
def _is_iana(name: str) -> bool:
    if not name or name.startswith("/") or ".." in name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def resolve_tzid(name: str, fallback: str) -> str:
    """Windows-/IANA-Name -> IANA-Name; unbekannt -> fallback (mit Warnung)."""
    name = name.strip()
    mapped = _WIN_LOWER.get(name.lower())
    if mapped:
        return mapped
    if _is_iana(name):
        return name
    log.warning("Unbekannte Zeitzone %r im Kalender, verwende %s", name, fallback)
    return fallback


def _unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text)


def preprocess_ics(text: str, fallback_tz: str) -> str:
    """Ersetzt TZID-Parameter durch IANA-Namen und entfernt alle VTIMEZONE-Blöcke.

    Dadurch nutzt icalendar immer zoneinfo; die oft fehlerhaften oder
    Windows-spezifischen VTIMEZONE-Definitionen von Exchange werden ignoriert.
    """
    text = text.lstrip("﻿")
    text = _unfold(text)
    text = _VTIMEZONE_BLOCK.sub("", text)

    def repl(m: re.Match) -> str:
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        return "TZID=" + resolve_tzid(raw, fallback_tz)

    text = _TZID_PARAM.sub(repl, text)
    text = _X_WR_TZ.sub(lambda m: m.group(1) + resolve_tzid(m.group(2), fallback_tz) + m.group(3), text)
    return text
