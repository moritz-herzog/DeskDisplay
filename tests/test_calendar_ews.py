import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from backend.calendar_ews import EwsCalendarService, _short_error, map_item
from backend.config import Config, ConfigError

TZ = ZoneInfo("Europe/Berlin")


def item(**kw):
    base = dict(subject="Termin", start=datetime(2026, 11, 16, 9, 0, tzinfo=TZ),
                end=datetime(2026, 11, 16, 10, 0, tzinfo=TZ), location="Raum 1", is_all_day=False,
                legacy_free_busy_status="Busy", is_cancelled=False, uid="u1", id="id1")
    base.update(kw)
    return SimpleNamespace(**base)


# ---- Abbildung ------------------------------------------------------------------
def test_timed_busy_event():
    e = map_item(item(), TZ)
    assert e.title == "Termin" and e.location == "Raum 1" and e.busy and not e.all_day
    assert e.start == datetime(2026, 11, 16, 9, 0, tzinfo=TZ) and e.end.hour == 10
    assert e.uid.startswith("u1")


def test_uid_falls_back_to_id_and_occurrences_differ():
    a = map_item(item(uid=None, start=datetime(2026, 11, 16, 9, tzinfo=TZ)), TZ)
    b = map_item(item(uid=None, start=datetime(2026, 11, 17, 9, tzinfo=TZ),
                      end=datetime(2026, 11, 17, 10, tzinfo=TZ)), TZ)
    assert a.uid.startswith("id1") and a.uid != b.uid


@pytest.mark.parametrize("status,busy", [("Free", False), ("WorkingElsewhere", False), ("Busy", True),
                                          ("Tentative", True), ("OOF", True), ("NoData", True), (None, True)])
def test_busy_mapping(status, busy):
    assert map_item(item(legacy_free_busy_status=status), TZ).busy is busy


def test_cancelled_skipped_and_no_subject():
    assert map_item(item(is_cancelled=True), TZ) is None
    assert map_item(item(subject=None), TZ).title == "(Ohne Titel)"
    assert map_item(item(subject="  "), TZ).title == "(Ohne Titel)"


def test_timezone_conversion_from_utc():
    e = map_item(item(start=datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc),
                      end=datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)), TZ)
    assert e.start.hour == 9 and e.end.hour == 10 and e.end.minute == 30
    assert e.start.utcoffset().total_seconds() == 3600


def test_exchangelib_datetimes_are_converted():
    from exchangelib import EWSDateTime, EWSTimeZone
    tz = EWSTimeZone.from_timezone(TZ)
    start = EWSDateTime(2026, 7, 1, 14, 0, tzinfo=tz)
    e = map_item(item(start=start, end=EWSDateTime(2026, 7, 1, 15, 0, tzinfo=tz)), TZ)
    assert type(e.start) is datetime and e.start.hour == 14
    assert e.start.utcoffset().total_seconds() == 7200


def test_all_day_with_ewsdate_and_ewsdatetime():
    from exchangelib import EWSDate, EWSDateTime, EWSTimeZone
    e = map_item(item(is_all_day=True, start=EWSDate(2026, 11, 18), end=EWSDate(2026, 11, 19)), TZ)
    assert e.all_day and e.start == date(2026, 11, 18) and e.end == date(2026, 11, 19)
    assert type(e.start) is date
    utc = EWSTimeZone("UTC")
    e = map_item(item(is_all_day=True, start=EWSDateTime(2026, 11, 18, 0, 0, tzinfo=utc),
                      end=EWSDateTime(2026, 11, 19, 0, 0, tzinfo=utc)), TZ)
    assert e.start == date(2026, 11, 18) and e.end == date(2026, 11, 19)


def test_multi_day_all_day_and_end_after_start():
    e = map_item(item(is_all_day=True, start=date(2026, 11, 18), end=date(2026, 11, 21)), TZ)
    assert (e.end - e.start).days == 3
    e = map_item(item(is_all_day=True, start=date(2026, 11, 18), end=date(2026, 11, 18)), TZ)
    assert e.end == date(2026, 11, 19)


# ---- Service ----------------------------------------------------------------------
class FakeQuery:
    def __init__(self, items):
        self.items = items

    def only(self, *fields):
        self.fields = fields
        return self

    def __iter__(self):
        return iter(self.items)


class FakeAccount:
    def __init__(self, items=None, error=None):
        self.items, self.error, self.views = items or [], error, []
        self.calendar = self

    def view(self, start, end):
        self.views.append((start, end))
        if self.error:
            raise self.error
        return FakeQuery(self.items)


def make_service(account_or_factory, **cal):
    cfg = Config.from_dict({"server": {"mock": True}, "calendar": cal}).calendar
    factory = account_or_factory if callable(account_or_factory) else (lambda c: account_or_factory)
    return EwsCalendarService(cfg, account_factory=factory)


def test_refresh_ready_and_snapshot_shape():
    acc = FakeAccount([item(), item(subject="Später", start=datetime(2026, 11, 16, 14, tzinfo=TZ),
                                    end=datetime(2026, 11, 16, 15, tzinfo=TZ)),
                       item(subject="Weg", is_cancelled=True)])
    svc = make_service(acc, days_ahead=2)
    assert not svc.ready and svc.events() == []
    asyncio.run(svc.refresh())
    assert svc.ready and not svc.stale and svc.error is None
    assert svc.updated_at and svc.last_success
    start, end = acc.views[0]
    assert (end - start).days == 3 and start.hour == 0  # heute + days_ahead + 1
    snap = svc.snapshot(datetime(2026, 11, 16, 12, tzinfo=TZ))
    assert set(snap) == {"updated_at", "stale", "error", "timezone", "day_start_hour", "day_end_hour", "days"}
    assert len(snap["days"]) == 3
    assert svc.health()["last_error"] is None


def test_snapshot_days_content():
    svc = make_service(FakeAccount([item(), item(subject="Reise", is_all_day=True, start=date(2026, 11, 16),
                                                  end=date(2026, 11, 18))]), days_ahead=2)
    asyncio.run(svc.refresh())
    days = svc.snapshot(datetime(2026, 11, 16, 12, tzinfo=TZ))["days"]
    assert [d["date"] for d in days] == ["2026-11-16", "2026-11-17", "2026-11-18"]
    assert [e["title"] for e in days[0]["events"]] == ["Reise", "Termin"]
    assert [e["title"] for e in days[1]["events"]] == ["Reise"] and days[2]["events"] == []


def test_error_keeps_old_data_and_reconnects():
    from exchangelib.errors import UnauthorizedError
    created = []
    acc = FakeAccount([item()])

    def factory(cfg):
        created.append(1)
        return acc

    svc = make_service(factory)
    asyncio.run(svc.refresh())
    assert len(svc.events()) == 1 and len(created) == 1
    ok_time = svc.last_success
    acc.error = UnauthorizedError("nope")
    asyncio.run(svc.refresh())
    assert svc.stale and svc.error == "Anmeldung fehlgeschlagen"
    assert len(svc.events()) == 1 and svc.last_success == ok_time
    assert svc.health()["last_error"] == "Anmeldung fehlgeschlagen"
    acc.error = None
    asyncio.run(svc.refresh())
    assert not svc.stale and svc.error is None and len(created) == 2  # neu verbunden


def test_ready_after_failed_first_refresh():
    def boom(cfg):
        raise RuntimeError("kaputt")

    svc = make_service(boom)
    assert not svc.ready
    asyncio.run(svc.refresh())
    assert svc.ready and svc.stale and svc.error == "RuntimeError: kaputt" and svc.events() == []


def test_short_errors():
    import requests
    from exchangelib.errors import TransportError
    assert _short_error(requests.exceptions.SSLError("x")) == "Zertifikatsfehler"
    assert _short_error(requests.exceptions.ConnectTimeout("x")) == "Zeitüberschreitung"
    assert _short_error(TransportError("x")) == "Server nicht erreichbar"
    assert _short_error(requests.exceptions.ConnectionError("x")) == "Server nicht erreichbar"
    assert len(_short_error(ValueError("a" * 500))) <= 160


# ---- Konfiguration ----------------------------------------------------------------
def ews_raw(**ews):
    base = {"server": "mail.firma.de", "username": "FIRMA\\max", "password": "geheim", "email": "max@firma.de"}
    base.update(ews)
    return {"todoist": {"token": "abc"}, "calendar": {"source": "ews", "ews": base}}


def test_config_default_source_is_ics():
    cfg = Config.from_dict({"todoist": {"token": "abc"}, "calendar": {"ics_url": "https://x/y.ics"}})
    assert cfg.calendar.source == "ics" and cfg.calendar.ews.auth_type == "ntlm"


def test_config_ews_valid_defaults():
    cfg = Config.from_dict(ews_raw())  # ics_url nicht nötig
    e = cfg.calendar.ews
    assert cfg.calendar.source == "ews" and e.auth_type == "ntlm" and e.verify_ssl and not e.autodiscover
    assert e.server == "mail.firma.de" and e.username == "FIRMA\\max" and e.email == "max@firma.de"


def test_config_ews_email_defaults_to_upn():
    raw = ews_raw(username="max@firma.de")
    del raw["calendar"]["ews"]["email"]
    assert Config.from_dict(raw).calendar.ews.email == "max@firma.de"


def test_config_ews_email_required_for_domain_user():
    raw = ews_raw()
    del raw["calendar"]["ews"]["email"]
    with pytest.raises(ConfigError, match="calendar.ews.email"):
        Config.from_dict(raw)


def test_config_ews_env_password(monkeypatch):
    raw = ews_raw()
    del raw["calendar"]["ews"]["password"]
    with pytest.raises(ConfigError, match="calendar.ews.password"):
        Config.from_dict(raw)
    monkeypatch.setenv("DASHBOARD_EWS_PASSWORD", "aus-env")
    assert Config.from_dict(raw).calendar.ews.password == "aus-env"
    raw2 = ews_raw(password="aus-datei")
    assert Config.from_dict(raw2).calendar.ews.password == "aus-env"  # Umgebung überschreibt


def test_config_ews_missing_fields(monkeypatch):
    monkeypatch.delenv("DASHBOARD_EWS_PASSWORD", raising=False)
    with pytest.raises(ConfigError) as exc:
        Config.from_dict({"todoist": {"token": "abc"}, "calendar": {"source": "ews"}})
    msg = str(exc.value)
    for key in ("calendar.ews.username", "calendar.ews.password", "calendar.ews.email", "calendar.ews.server"):
        assert key in msg
    assert "ics_url" not in msg


def test_config_ews_autodiscover_replaces_server():
    cfg = Config.from_dict(ews_raw(server="", autodiscover=True))
    assert cfg.calendar.ews.autodiscover


def test_config_invalid_source_and_auth_type():
    with pytest.raises(ConfigError, match="calendar.source"):
        Config.from_dict({"todoist": {"token": "abc"}, "calendar": {"source": "caldav", "ics_url": "https://x"}})
    with pytest.raises(ConfigError, match="calendar.ews.auth_type"):
        Config.from_dict(ews_raw(auth_type="kerberos"))


def test_config_ews_ca_bundle(tmp_path):
    with pytest.raises(ConfigError, match="ca_bundle"):
        Config.from_dict(ews_raw(ca_bundle="fehlt.pem"), base_dir=tmp_path)
    (tmp_path / "ca.pem").write_text("x", encoding="utf-8")
    cfg = Config.from_dict(ews_raw(ca_bundle="ca.pem"), base_dir=tmp_path)
    assert Path(cfg.calendar.ews.ca_bundle) == tmp_path / "ca.pem"


def test_config_ews_not_validated_in_mock_mode():
    cfg = Config.from_dict({"server": {"mock": True}, "calendar": {"source": "ews"}})
    assert cfg.calendar.source == "ews"


def test_app_context_picks_service_and_ready():
    from backend.main import AppContext
    from backend.calendar_ics import CalendarService
    ctx = AppContext(Config.from_dict(ews_raw()))
    assert isinstance(ctx.calendar, EwsCalendarService) and not ctx.calendar_ready()
    ctx = AppContext(Config.from_dict({"todoist": {"token": "abc"}, "calendar": {"ics_url": "https://x/y.ics"}}))
    assert isinstance(ctx.calendar, CalendarService) and not ctx.calendar_ready()
    ctx = AppContext(Config.from_dict({"server": {"mock": True}}))
    assert ctx.calendar_ready()
