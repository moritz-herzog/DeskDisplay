from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.calendar_ics import build_days, parse_ics
from backend.windows_tz import preprocess_ics, resolve_tzid

TZ = ZoneInfo("Europe/Berlin")
FIXTURE = (Path(__file__).parent / "fixtures" / "exchange_sample.ics").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def events():
    return parse_ics(FIXTURE, TZ, date(2026, 11, 2), 21)


def by_uid(events, uid):
    return [e for e in events if e.uid.endswith(uid) or e.uid == uid]


def single_event_ics(body: str) -> str:
    return "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\n" + body + "END:VCALENDAR\n"


def test_windows_tz_mapping():
    assert resolve_tzid("W. Europe Standard Time", "Europe/Berlin") == "Europe/Berlin"
    assert resolve_tzid("Pacific Standard Time", "Europe/Berlin") == "America/Los_Angeles"
    assert resolve_tzid("Europe/Paris", "Europe/Berlin") == "Europe/Paris"
    assert resolve_tzid("Customized Time Zone", "Europe/Berlin") == "Europe/Berlin"


def test_preprocess_removes_vtimezone_and_replaces_tzid():
    out = preprocess_ics(FIXTURE, "Europe/Berlin")
    assert "VTIMEZONE" not in out
    assert "W. Europe" not in out
    assert "TZID=Europe/Berlin" in out


def test_recurring_series_with_exception_and_exdate(events):
    series = [e for e in events if e.uid.endswith("SERIES01")]
    starts = sorted(e.start for e in series)
    # 3. Nov normal, 10. Nov verschoben auf 11. Nov 14:00, 17. Nov per EXDATE entfernt
    assert starts == [
        datetime(2026, 11, 3, 9, 0, tzinfo=TZ),
        datetime(2026, 11, 11, 14, 0, tzinfo=TZ),
    ]
    moved = [e for e in series if e.start.day == 11][0]
    assert moved.title == "Weekly Team Sync (verschoben)"
    assert moved.end == datetime(2026, 11, 11, 14, 45, tzinfo=TZ)
    assert not any(e.start.date() in (date(2026, 11, 10), date(2026, 11, 17)) for e in series)


def test_cancelled_event_skipped(events):
    assert not by_uid(events, "CANCELLED01")


def test_free_busystatus(events):
    free = by_uid(events, "FREE01")[0]
    assert free.busy is False
    elsewhere = by_uid(events, "ELSEWHERE01")[0]
    assert elsewhere.busy is False and elsewhere.all_day


def test_busy_rules(events):
    assert by_uid(events, "FLOATING01")[0].busy is True  # TENTATIVE
    assert by_uid(events, "VACATION01")[0].busy is True  # OOF
    assert by_uid(events, "TRANSP01")[0].busy is False  # nur TRANSPARENT
    assert by_uid(events, "UTC01")[0].busy is True  # nichts gesetzt


def test_all_day_events(events):
    vac = by_uid(events, "VACATION01")[0]
    assert vac.all_day
    assert vac.start == date(2026, 11, 16) and vac.end == date(2026, 11, 19)


def test_floating_and_utc_and_unknown_tz(events):
    floating = by_uid(events, "FLOATING01")[0]
    assert floating.start == datetime(2026, 11, 5, 10, 0, tzinfo=TZ)
    utc = by_uid(events, "UTC01")[0]
    assert utc.start == datetime(2026, 11, 6, 9, 0, tzinfo=TZ)  # 08:00Z = 09:00 MEZ
    unknown = by_uid(events, "UNKNOWNTZ01")[0]
    assert unknown.start == datetime(2026, 11, 9, 10, 0, tzinfo=TZ)  # Fallback calendar.timezone


def test_windows_tzid_utc_offset_and_dst():
    ics = single_event_ics(
        "BEGIN:VEVENT\nUID:summer\nSUMMARY:Sommer\n"
        "DTSTART;TZID=W. Europe Standard Time:20260701T100000\n"
        "DTEND;TZID=W. Europe Standard Time:20260701T110000\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:winter\nSUMMARY:Winter\n"
        "DTSTART;TZID=W. Europe Standard Time:20261201T100000\n"
        "DTEND;TZID=W. Europe Standard Time:20261201T110000\nEND:VEVENT\n"
    )
    evs = {e.uid: e for e in parse_ics(ics, TZ, date(2026, 6, 30), 200)}
    assert evs["summer"].start.astimezone(timezone.utc) == datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
    assert evs["summer"].start.utcoffset() == timedelta(hours=2)
    assert evs["winter"].start.astimezone(timezone.utc) == datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
    assert evs["winter"].start.utcoffset() == timedelta(hours=1)


def test_windows_tzid_other_zone_converted_to_display_zone():
    ics = single_event_ics(
        "BEGIN:VEVENT\nUID:ny\nSUMMARY:NY\n"
        'DTSTART;TZID="Eastern Standard Time":20261115T090000\n'
        'DTEND;TZID="Eastern Standard Time":20261115T100000\nEND:VEVENT\n'
    )
    ev = parse_ics(ics, TZ, date(2026, 11, 15), 1)[0]
    assert ev.start == datetime(2026, 11, 15, 15, 0, tzinfo=TZ)  # 09:00 EST = 15:00 MEZ


def test_weekly_series_keeps_local_time_across_dst_switch():
    # Sommerzeit endet am 25.10.2026
    ics = single_event_ics(
        "BEGIN:VEVENT\nUID:weekly\nSUMMARY:Samstag\n"
        "DTSTART;TZID=W. Europe Standard Time:20261017T100000\n"
        "DTEND;TZID=W. Europe Standard Time:20261017T110000\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\nEND:VEVENT\n"
    )
    evs = parse_ics(ics, TZ, date(2026, 10, 15), 20)
    assert [e.start.hour for e in evs] == [10, 10, 10]
    offsets = [e.start.utcoffset() for e in evs]
    assert offsets == [timedelta(hours=2), timedelta(hours=2), timedelta(hours=1)]  # 17.10., 24.10., 31.10.
    utc_hours = [e.start.astimezone(timezone.utc).hour for e in evs]
    assert utc_hours == [8, 8, 9]


def test_multi_day_event_on_each_day(events):
    offsite = by_uid(events, "OFFSITE01")[0]
    assert offsite.start == datetime(2026, 11, 4, 16, 0, tzinfo=TZ)
    assert offsite.end == datetime(2026, 11, 6, 12, 0, tzinfo=TZ)
    days = build_days(events, TZ, date(2026, 11, 2), 6)
    with_offsite = [d["date"] for d in days if any(e["uid"] == "OFFSITE01" for e in d["events"])]
    assert with_offsite == ["2026-11-04", "2026-11-05", "2026-11-06"]


def test_all_day_multi_day_in_days_and_sorting(events):
    days = {d["date"]: d["events"] for d in build_days(events, TZ, date(2026, 11, 15), 6)}
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        assert any(e["uid"] == "VACATION01" for e in days[day])
    assert not any(e["uid"] == "VACATION01" for e in days["2026-11-19"])
    # ganztägig zuerst
    nov_17 = days["2026-11-17"]
    assert nov_17[0]["all_day"] is True and nov_17[0]["start"] == "2026-11-16"
    # EXDATE-Tag hat keine Serie
    assert not any(e["uid"].endswith("SERIES01") for e in nov_17)


def test_build_days_includes_empty_days_and_iso_offsets(events):
    days = build_days(events, TZ, date(2026, 11, 7), 2)
    assert [d["date"] for d in days] == ["2026-11-07", "2026-11-08", "2026-11-09"]
    assert days[0]["events"] == [] and days[1]["events"] == []
    ev = days[2]["events"][0]
    assert ev["title"] == "Seltsame Zone"
    assert ev["start"] == "2026-11-09T10:00:00+01:00"
