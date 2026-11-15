"""Beispieldaten für `server.mock: true` (keine Netzwerkzugriffe)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .calendar_ics import CalEvent, _sort_key


def mock_todoist_raw(now: datetime) -> tuple[list[dict], list[dict]]:
    """(tasks, projects) im Rohformat der Todoist-API v1, relativ zu `now`."""
    today = now.date()

    def d(offset: int) -> str:
        return (today + timedelta(days=offset)).isoformat()

    def due(offset: int, string: str, hour: int | None = None, recurring: bool = False) -> dict:
        if hour is None:
            return {"date": d(offset), "string": string, "is_recurring": recurring, "datetime": None}
        dt = datetime.combine(today + timedelta(days=offset), time(hour, 0), tzinfo=now.tzinfo)
        return {"date": dt.replace(tzinfo=None).isoformat(), "datetime": dt.isoformat(), "string": string,
                "is_recurring": recurring, "timezone": None}

    projects = [
        {"id": "proj_inbox", "name": "Inbox", "color": "grey", "child_order": 0},
        {"id": "proj_arbeit", "name": "Arbeit", "color": "blue", "child_order": 1},
        {"id": "proj_haushalt", "name": "Haushalt", "color": "orange", "child_order": 2},
        {"id": "proj_projekte", "name": "Raspberry-Pi-Dashboard", "color": "green", "child_order": 3},
    ]

    def task(i: str, content: str, project: str, priority: int, due_: dict | None, order: int,
             labels: list[str] | None = None, description: str = "", parent: str | None = None) -> dict:
        return {"id": f"mock_task_{i}", "content": content, "description": description, "project_id": project,
                "priority": priority, "labels": labels or [], "due": due_, "parent_id": parent,
                "child_order": order}

    tasks = [
        task("1", "Angebot an Müller GmbH schicken", "proj_arbeit", 4, due(-1, "gestern"), 0, ["arbeit"],
             "Preise aus der Kalkulation vom Montag verwenden"),
        task("2", "Quartalsbericht fertigstellen", "proj_arbeit", 3, due(0, "heute", 17), 1, ["arbeit"]),
        task("3", "Reisekostenabrechnung einreichen", "proj_arbeit", 2, due(-3, "vor 3 Tagen"), 2),
        task("4", "Team-Retro vorbereiten", "proj_arbeit", 2, due(2, "übermorgen"), 3, ["meeting"]),
        task("5", "Urlaubsantrag stellen", "proj_arbeit", 1, None, 4),
        task("6", "Milch und Brot einkaufen", "proj_haushalt", 3, due(0, "heute"), 0, ["einkauf"]),
        task("7", "Müll rausbringen", "proj_haushalt", 1, due(1, "jeden Dienstag", None, True), 1),
        task("8", "Waschmaschine entkalken", "proj_haushalt", 1, None, 2),
        task("9", "Steuerunterlagen sortieren", "proj_haushalt", 2, due(5, "in 5 Tagen"), 3),
        task("10", "Frontend: Kalenderansicht testen", "proj_projekte", 4, due(0, "heute"), 0, ["entwicklung"]),
        task("11", "MQTT-Anbindung am ESP prüfen", "proj_projekte", 3, due(3, "in 3 Tagen"), 1, ["entwicklung"]),
        task("12", "Gehäuse für das Display drucken", "proj_projekte", 1, None, 2),
        task("13", "Neue Idee: Wetter-Kachel", "proj_inbox", 1, None, 0),
    ]
    return tasks, projects


def mock_calendar_events(now: datetime, tz: ZoneInfo, days_ahead: int = 5) -> list[CalEvent]:
    """Termine relativ zu `now`: einer läuft gerade (busy), einer später, einer FREE usw."""
    now = now.astimezone(tz)
    today = now.date()

    def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(today + timedelta(days=day_offset), time(hour, minute), tzinfo=tz)

    # Zeitanker auf halbe Stunden gerundet: der laufende Termin bleibt bis zu 30 Minuten stabil
    anchor = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    running_start = anchor - timedelta(minutes=10)
    running_end = running_start + timedelta(minutes=60)
    later_start = running_end + timedelta(minutes=90)

    def ev(uid: str, title: str, start: datetime, end: datetime, busy: bool = True, location: str = "") -> CalEvent:
        return CalEvent(f"mock-{uid}", title, start, end, False, busy, location)

    def allday(uid: str, title: str, day_offset: int, length: int = 1, busy: bool = False) -> CalEvent:
        s = today + timedelta(days=day_offset)
        return CalEvent(f"mock-{uid}", title, s, s + timedelta(days=length), True, busy, "")

    events = [
        ev("past", "Daily Standup", anchor - timedelta(hours=3), anchor - timedelta(hours=2, minutes=45), True, "Teams"),
        ev("running", "Projekt-Review mit Kunde", running_start, running_end, True, "Teams"),
        ev("later", "Fokuszeit: Konzept schreiben", later_start, later_start + timedelta(hours=2), True, "Büro"),
        ev("free", "Mittagspause (Verfügbar)", later_start + timedelta(hours=3), later_start + timedelta(hours=4), False),
        allday("allday", "Geburtstag Anna", 0),
        ev("t1", "Sprint-Planung", at(1, 9, 30), at(1, 11), True, "Teams"),
        ev("t2", "Zahnarzt", at(1, 15), at(1, 16), True),
        ev("w1", "Kundentermin Hamburg", at(2, 10), at(2, 12, 30), True, "Hamburg"),
        ev("w2", "1:1 mit Chef", at(2, 14), at(2, 14, 30), True, "Teams"),
        allday("trip", "Dienstreise", 3, 2, True),
        ev("w3", "Workshop Architektur", at(3, 9), at(3, 17), True, "Raum 4.12"),
        ev("w4", "Sport", at(4, 18), at(4, 19, 30), False),
        ev("w5", "Quartalsplanung", at(5, 13), at(5, 15), True, "Teams"),
    ]
    events = [e for e in events if (e.start if e.all_day else e.start.date()) <= today + timedelta(days=days_ahead)]
    events.sort(key=_sort_key)
    return events
