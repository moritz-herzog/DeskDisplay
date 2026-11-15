"""ICS-Kalender (Exchange/Outlook): Laden, Serien auflösen, Frei/Beschäftigt."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from .windows_tz import preprocess_ics

log = logging.getLogger(__name__)

FREE_STATUSES = {"FREE", "WORKINGELSEWHERE"}


@dataclass(frozen=True)
class CalEvent:
    """Ein Termin. Ganztägig: start/end sind `date` (end exklusiv), sonst tz-bewusste datetimes."""
    uid: str
    title: str
    start: Any  # datetime | date
    end: Any  # datetime | date
    all_day: bool
    busy: bool
    location: str = ""


def _is_busy(component) -> bool:
    status = str(component.get("X-MICROSOFT-CDO-BUSYSTATUS", "") or "").strip().upper()
    if status:
        return status not in FREE_STATUSES
    transp = str(component.get("TRANSP", "") or "").strip().upper()
    return transp != "TRANSPARENT"


def _to_dt(value, tz: ZoneInfo) -> datetime:
    """date/datetime (evtl. floating) -> tz-bewusste datetime in `tz`."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)  # floating -> Kalenderzone
        return value.astimezone(tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def parse_ics(text: str, tz: ZoneInfo, start_date: date, days: int) -> list[CalEvent]:
    """Parst ICS-Text und liefert alle Termine, die das Fenster
    [start_date 00:00, start_date + days 00:00) in `tz` berühren (Serien aufgelöst)."""
    prepared = preprocess_ics(text, tz.key)
    cal = icalendar.Calendar.from_ical(prepared)

    win_start = datetime.combine(start_date, time.min, tzinfo=tz)
    win_end = datetime.combine(start_date + timedelta(days=max(days, 1)), time.min, tzinfo=tz)

    events: list[CalEvent] = []
    try:
        occurrences = recurring_ical_events.of(cal).between(win_start, win_end)
    except Exception:
        log.exception("Auflösen der Terminserien fehlgeschlagen")
        raise

    for comp in occurrences:
        if comp.name != "VEVENT":
            continue
        if str(comp.get("STATUS", "") or "").strip().upper() == "CANCELLED":
            continue
        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        raw_start = dtstart.dt
        all_day = not isinstance(raw_start, datetime)
        try:
            raw_end = comp.end
        except Exception:
            raw_end = raw_start
        if all_day:
            start: Any = raw_start
            end: Any = raw_end if isinstance(raw_end, date) and not isinstance(raw_end, datetime) else raw_start + timedelta(days=1)
            if end <= start:
                end = start + timedelta(days=1)
        else:
            start = _to_dt(raw_start, tz)
            end = _to_dt(raw_end, tz) if raw_end is not None else start
            if end < start:
                end = start
        title = str(comp.get("SUMMARY", "") or "").strip() or "(Ohne Titel)"
        events.append(CalEvent(
            uid=str(comp.get("UID", "") or ""),
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            busy=_is_busy(comp),
            location=str(comp.get("LOCATION", "") or "").strip(),
        ))

    events.sort(key=_sort_key)
    return events


def _sort_key(e: CalEvent):
    start = e.start
    if e.all_day:
        return (0, datetime.combine(start, time.min, tzinfo=timezone.utc), e.title)
    return (1, start.astimezone(timezone.utc), e.title)


def event_to_json(e: CalEvent, tz: ZoneInfo) -> dict:
    if e.all_day:
        start, end = e.start.isoformat(), e.end.isoformat()
    else:
        start, end = e.start.astimezone(tz).isoformat(), e.end.astimezone(tz).isoformat()
    return {"uid": e.uid, "title": e.title, "start": start, "end": end,
            "all_day": e.all_day, "busy": e.busy, "location": e.location}


def events_overlapping_day(events: list[CalEvent], day: date, tz: ZoneInfo) -> list[CalEvent]:
    day_start = datetime.combine(day, time.min, tzinfo=tz)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    out = []
    for e in events:
        if e.all_day:
            if e.start <= day < e.end:
                out.append(e)
        else:
            if e.start < day_end and e.end > day_start:
                out.append(e)
            elif e.start == e.end and day_start <= e.start < day_end:
                out.append(e)
    out.sort(key=_sort_key)
    return out


def build_days(events: list[CalEvent], tz: ZoneInfo, today: date, days_ahead: int) -> list[dict]:
    """Heute + days_ahead Tage (auch leere); mehrtägige Termine an jedem Tag."""
    result = []
    for i in range(days_ahead + 1):
        d = today + timedelta(days=i)
        result.append({
            "date": d.isoformat(),
            "events": [event_to_json(e, tz) for e in events_overlapping_day(events, d, tz)],
        })
    return result


class CalendarService:
    """Holt die ICS-Datei, cached den Text und liefert geparste Termine."""

    def __init__(self, cfg, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg  # CalendarConfig
        self.tz = ZoneInfo(cfg.timezone)
        self._transport = transport
        self.ics_text: str | None = None
        self.updated_at: datetime | None = None
        self.last_success: datetime | None = None
        self.error: str | None = None
        self.stale: bool = False
        self._parsed: list[CalEvent] = []
        self._parsed_for: date | None = None
        self._parse_error: str | None = None

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    async def fetch_text(self) -> str:
        async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds, follow_redirects=True,
                                     transport=self._transport) as client:
            resp = await client.get(self.cfg.ics_url, headers={"Accept": "text/calendar, */*"})
            resp.raise_for_status()
            return resp.text

    async def refresh(self) -> None:
        try:
            text = await self.fetch_text()
            if "BEGIN:VCALENDAR" not in text:
                raise ValueError("Antwort ist keine ICS-Datei")
            # erst parsen (in Thread), dann übernehmen: kaputte Datei überschreibt den Cache nicht
            today = self._now().date()
            parsed = await asyncio.to_thread(parse_ics, text, self.tz, today, self.cfg.days_ahead + 1)
            self.ics_text = text
            self._parsed, self._parsed_for = parsed, today
            self.updated_at = self.last_success = self._now()
            self.error, self.stale = None, False
        except Exception as exc:  # noqa: BLE001 - Hintergrundjob darf nie abstürzen
            self.stale = True
            self.error = _short_error(exc)
            log.warning("Kalender-Abruf fehlgeschlagen: %s", self.error)

    def events(self, now: datetime | None = None) -> list[CalEvent]:
        now = now or self._now()
        today = now.astimezone(self.tz).date()
        if self.ics_text is not None and self._parsed_for != today:
            try:
                self._parsed = parse_ics(self.ics_text, self.tz, today, self.cfg.days_ahead + 1)
                self._parsed_for = today
            except Exception as exc:  # noqa: BLE001
                log.warning("Kalender neu parsen fehlgeschlagen: %s", exc)
        return self._parsed

    def snapshot(self, now: datetime | None = None) -> dict:
        now = (now or self._now()).astimezone(self.tz)
        return {
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "stale": self.stale,
            "error": self.error,
            "timezone": self.cfg.timezone,
            "day_start_hour": self.cfg.day_start_hour,
            "day_end_hour": self.cfg.day_end_hour,
            "days": build_days(self.events(now), self.tz, now.date(), self.cfg.days_ahead),
        }

    def health(self) -> dict:
        return {"last_success": self.last_success.isoformat() if self.last_success else None,
                "last_error": self.error}


def _short_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "Zeitüberschreitung"
    if isinstance(exc, httpx.HTTPError):
        return f"Verbindungsfehler ({type(exc).__name__})"
    return f"{type(exc).__name__}: {exc}"[:160]
