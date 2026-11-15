import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from backend.availability import AvailabilityStore, ModeState, compute_status
from backend.calendar_ics import CalEvent

TZ = ZoneInfo("Europe/Berlin")


def dt(h, m=0, day=15):
    return datetime(2026, 11, day, h, m, tzinfo=TZ)


def ev(title, start, end, busy=True, all_day=False):
    return CalEvent(uid=title, title=title, start=start, end=end, all_day=all_day, busy=busy)


AUTO = ModeState()


def test_free_without_events():
    s = compute_status(AUTO, [], dt(12))
    assert s == {"status": "free", "mode": "auto", "until": None, "label": "Frei"}


def test_busy_inside_event():
    s = compute_status(AUTO, [ev("Meeting", dt(14), dt(15, 30))], dt(14, 30))
    assert s["status"] == "busy" and s["mode"] == "auto"
    assert s["label"] == "Meeting"
    assert s["until"] == "2026-11-15T15:30:00+01:00"


def test_start_inclusive_end_exclusive():
    e = [ev("M", dt(14), dt(15))]
    assert compute_status(AUTO, e, dt(13, 59))["status"] == "free"
    assert compute_status(AUTO, e, dt(14))["status"] == "busy"
    assert compute_status(AUTO, e, dt(14, 59))["status"] == "busy"
    assert compute_status(AUTO, e, dt(15))["status"] == "free"


def test_free_event_and_all_day_ignored():
    events = [ev("Frei", dt(10), dt(12), busy=False),
              ev("Urlaub", date(2026, 11, 15), date(2026, 11, 16), busy=True, all_day=True)]
    assert compute_status(AUTO, events, dt(11))["status"] == "free"


def test_overlapping_events_until_is_latest_end_label_first_started():
    events = [ev("B", dt(14, 30), dt(16)), ev("A", dt(14), dt(15))]
    s = compute_status(AUTO, events, dt(14, 45))
    assert s["label"] == "A"
    assert s["until"] == "2026-11-15T16:00:00+01:00"


def test_hide_titles():
    s = compute_status(AUTO, [ev("Geheim", dt(14), dt(15))], dt(14, 10), show_titles=False)
    assert s["label"] == "Im Termin"


def test_manual_overrides_calendar():
    events = [ev("M", dt(14), dt(15))]
    for status, label in (("free", "Frei"), ("busy", "Beschäftigt"), ("dnd", "Nicht stören")):
        s = compute_status(ModeState("manual", status), events, dt(14, 30))
        assert s == {"status": status, "mode": "manual", "until": None, "label": label}


def test_manual_to_auto_transition(tmp_path):
    store = AvailabilityStore(tmp_path / "state.json")
    events = [ev("M", dt(14), dt(15))]
    now = dt(14, 30)
    assert compute_status(store.state, events, now)["status"] == "busy"
    store.set_manual("free")
    assert compute_status(store.state, events, now)["status"] == "free"
    store.set_manual("dnd")
    assert compute_status(store.state, events, now)["status"] == "dnd"
    store.set_auto()
    assert compute_status(store.state, events, now)["mode"] == "auto"
    assert compute_status(store.state, events, now)["status"] == "busy"


def test_store_persists_atomically(tmp_path):
    path = tmp_path / "sub" / "state.json"
    store = AvailabilityStore(path)
    assert store.state == ModeState()
    store.set_manual("dnd")
    assert json.loads(path.read_text(encoding="utf-8")) == {"mode": "manual", "status": "dnd"}
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]  # keine tmp-Reste
    assert AvailabilityStore(path).state == ModeState("manual", "dnd")
    store.set_auto()
    assert AvailabilityStore(path).state == ModeState("auto", None)


def test_store_corrupt_file_falls_back_to_auto(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{kaputt", encoding="utf-8")
    assert AvailabilityStore(path).state == ModeState()
