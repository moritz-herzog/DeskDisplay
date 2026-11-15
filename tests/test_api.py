from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.config import Config, ConfigError, load_config
from backend.main import create_app

TZ = ZoneInfo("Europe/Berlin")


@pytest.fixture()
def client(tmp_path):
    cfg = Config.from_dict({"server": {"mock": True, "state_file": "state.json"}}, base_dir=tmp_path)
    with TestClient(create_app(cfg)) as c:
        yield c


def test_tasks_contract(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"updated_at", "stale", "error", "projects"}
    assert data["stale"] is False and data["error"] is None
    assert 3 <= len(data["projects"]) <= 5
    tasks = [t for p in data["projects"] for t in p["tasks"]]
    assert len(tasks) >= 10
    for p in data["projects"]:
        assert set(p) == {"id", "name", "color", "tasks"} and p["tasks"]
        for t in p["tasks"]:
            assert set(t) == {"id", "content", "description", "priority", "labels", "overdue", "due"}
            assert t["priority"] in (1, 2, 3, 4)
            if t["due"] is not None:
                assert set(t["due"]) == {"date", "datetime", "string", "is_recurring"}
    assert any(t["overdue"] for t in tasks)
    assert any(t["due"] is None for t in tasks)
    assert {t["priority"] for t in tasks} == {1, 2, 3, 4}


def test_calendar_contract(client):
    data = client.get("/api/calendar").json()
    assert data["timezone"] == "Europe/Berlin"
    assert data["day_start_hour"] == 7 and data["day_end_hour"] == 20
    assert len(data["days"]) == 6  # heute + 5
    today = data["days"][0]
    assert today["date"] == datetime.now(TZ).date().isoformat()
    events = today["events"]
    assert any(e["all_day"] for e in events)
    assert events[0]["all_day"] is True  # ganztägig zuerst
    timed = [e for e in events if not e["all_day"]]
    assert [e["start"] for e in timed] == sorted(e["start"] for e in timed)
    assert any(not e["busy"] for e in timed)
    assert any(e["busy"] for e in timed)
    for e in timed:
        assert set(e) == {"uid", "title", "start", "end", "all_day", "busy", "location"}
        assert "T" in e["start"] and e["start"][-6] in "+-"
    assert any(d["events"] for d in data["days"][1:])


def test_availability_auto_uses_mock_events(client):
    data = client.get("/api/availability").json()
    assert set(data) == {"status", "mode", "until", "label", "mqtt_connected"}
    assert data["mode"] == "auto" and data["status"] == "busy"  # laufender Mock-Termin
    assert data["label"] == "Projekt-Review mit Kunde" and data["until"]
    assert data["mqtt_connected"] is False


def test_availability_post_manual_and_back_to_auto(client):
    r = client.post("/api/availability", json={"mode": "manual", "status": "dnd"})
    assert r.status_code == 200
    assert r.json()["status"] == "dnd" and r.json()["mode"] == "manual" and r.json()["until"] is None
    assert client.get("/api/availability").json()["label"] == "Nicht stören"
    r = client.post("/api/availability", json={"mode": "auto"})
    assert r.json()["mode"] == "auto" and r.json()["status"] == "busy"


@pytest.mark.parametrize("body", [
    {"mode": "manual"},
    {"mode": "manual", "status": "away"},
    {"mode": "sleep"},
    {"status": "free"},
    {},
])
def test_availability_post_validation(client, body):
    assert client.post("/api/availability", json=body).status_code == 422
    assert client.get("/api/availability").json()["mode"] == "auto"


def test_manual_mode_persists_in_state_file(tmp_path):
    cfg = Config.from_dict({"server": {"mock": True, "state_file": "state.json"}}, base_dir=tmp_path)
    with TestClient(create_app(cfg)) as c:
        c.post("/api/availability", json={"mode": "manual", "status": "free"})
    assert (tmp_path / "state.json").is_file()
    with TestClient(create_app(cfg)) as c:
        data = c.get("/api/availability").json()
        assert data["mode"] == "manual" and data["status"] == "free"


def test_settings_and_health(client):
    s = client.get("/api/settings").json()
    assert s["default_view"] == "tasks" and s["theme"] == "dark" and s["return_after_seconds"] == 120
    assert s["priority_colors"]["p1"] == "#FF5A5F" and s["status_colors"]["busy"] == "#C62828"
    h = client.get("/api/health").json()
    assert h["mqtt"] == {"enabled": False, "connected": False}
    assert h["todoist"]["last_error"] is None and h["calendar"]["last_error"] is None
    assert h["todoist"]["last_success"] and h["calendar"]["last_success"]


def test_static_frontend_no_cache(client):
    from backend.main import FRONTEND_DIR
    if not (FRONTEND_DIR / "index.html").is_file():
        pytest.skip("frontend/index.html nicht vorhanden")
    r = client.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    for name in ("style.css", "app.js", "icons.js"):
        if (FRONTEND_DIR / name).is_file():
            rr = client.get("/" + name)
            assert rr.status_code == 200 and rr.headers["cache-control"] == "no-cache"
    assert client.get("/api/gibtsnicht").status_code == 404


# ---- Konfiguration ----------------------------------------------------------------
def test_config_mock_needs_no_credentials():
    cfg = Config.from_dict({"server": {"mock": True}})
    assert cfg.server.mock and not cfg.mqtt_active


def test_config_missing_fields_german_messages():
    with pytest.raises(ConfigError) as exc:
        Config.from_dict({"server": {"port": "abc"}, "todoist": {}, "calendar": {"timezone": "Mars/Olympus"}})
    msg = str(exc.value)
    assert "todoist.token" in msg and "calendar.ics_url" in msg
    assert "server.port" in msg and "calendar.timezone" in msg


def test_load_config_from_env(tmp_path, monkeypatch):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"server": {"mock": True, "port": 9999, "state_file": "s.json"}}), encoding="utf-8")
    monkeypatch.setenv("DASHBOARD_CONFIG", str(path))
    cfg = load_config()
    assert cfg.server.port == 9999 and cfg.state_path == tmp_path / "s.json"
    monkeypatch.setenv("DASHBOARD_CONFIG", str(tmp_path / "fehlt.yaml"))
    with pytest.raises(ConfigError):
        load_config()


def test_example_config_is_valid_with_placeholders_replaced():
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).parent.parent / "config.yaml.example").read_text(encoding="utf-8"))
    with pytest.raises(ConfigError):  # Platzhalter-Token wird abgelehnt
        Config.from_dict(raw)
    raw["todoist"]["token"] = "abc"
    cfg = Config.from_dict(raw)
    assert cfg.calendar.days_ahead == 5 and cfg.mqtt.topic == "dashboard/availability"
