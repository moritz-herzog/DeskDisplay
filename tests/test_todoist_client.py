import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.todoist_client import TodoistError, TodoistService, build_projects

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 15, 14, 0, tzinfo=TZ)


def task(i, content, project="p1", priority=1, due=None, order=0, **kw):
    return {"id": str(i), "content": content, "description": "", "project_id": project, "priority": priority,
            "labels": [], "due": due, "parent_id": None, "child_order": order, **kw}


def d(date, string="", dt=None):
    return {"date": date, "datetime": dt, "string": string, "is_recurring": False}


PROJECTS = [
    {"id": "p2", "name": "Arbeit", "color": "blue", "child_order": 2},
    {"id": "p1", "name": "Inbox", "color": "grey", "child_order": 1},
    {"id": "p3", "name": "Leer", "color": "red", "child_order": 3},
]


def test_sorting_and_overdue():
    tasks = [
        task(1, "ohne Datum p1", priority=4),
        task(2, "heute 16 Uhr", priority=1, due=d("2026-11-15", dt="2026-11-15T16:00:00")),
        task(3, "heute ganztägig", priority=2, due=d("2026-11-15")),
        task(4, "gestern", priority=1, due=d("2026-11-14")),
        task(5, "heute 10 Uhr (vorbei)", priority=1, due=d("2026-11-15", dt="2026-11-15T10:00:00")),
        task(6, "heute ganztägig p1", priority=4, due=d("2026-11-15")),
        task(7, "morgen", priority=3, due=d("2026-11-16")),
    ]
    out = build_projects(tasks, PROJECTS, TZ, NOW)
    assert [p["name"] for p in out] == ["Inbox"]  # leere Projekte entfallen
    names = [t["content"] for t in out[0]["tasks"]]
    assert names == ["gestern", "heute 10 Uhr (vorbei)", "heute ganztägig p1", "heute ganztägig",
                     "heute 16 Uhr", "morgen", "ohne Datum p1"]
    flags = {t["content"]: t["overdue"] for t in out[0]["tasks"]}
    assert flags["gestern"] and flags["heute 10 Uhr (vorbei)"]
    assert not flags["heute ganztägig"] and not flags["heute 16 Uhr"] and not flags["ohne Datum p1"]


def test_project_order_and_task_shape():
    tasks = [task(1, "A", project="p2", due=d("2026-11-15", "heute")), task(2, "B", project="p1")]
    out = build_projects(tasks, PROJECTS, TZ, NOW)
    assert [p["name"] for p in out] == ["Inbox", "Arbeit"]  # child_order
    t = out[1]["tasks"][0]
    assert set(t) == {"id", "content", "description", "priority", "labels", "overdue", "due"}
    assert t["due"] == {"date": "2026-11-15", "datetime": None, "string": "heute", "is_recurring": False}
    assert out[0]["tasks"][0]["due"] is None


def test_utc_datetime_due_converted_to_calendar_tz():
    tasks = [task(1, "x", due=d("2026-11-15", dt="2026-11-15T12:00:00Z"))]  # 13:00 Berlin -> um 14:00 überfällig
    out = build_projects(tasks, PROJECTS, TZ, NOW)
    t = out[0]["tasks"][0]
    assert t["due"]["datetime"] == "2026-11-15T13:00:00+01:00" and t["overdue"] is True


def test_project_filter_by_name():
    tasks = [task(1, "A", project="p2"), task(2, "B", project="p1")]
    out = build_projects(tasks, PROJECTS, TZ, NOW, project_name="arbeit")
    assert [p["name"] for p in out] == ["Arbeit"]
    with pytest.raises(TodoistError):
        build_projects(tasks, PROJECTS, TZ, NOW, project_name="Gibt es nicht")


def make_service(handler, **cfg):
    c = SimpleNamespace(token="tok", filter="", project="", refresh_seconds=120, timeout_seconds=5)
    for k, v in cfg.items():
        setattr(c, k, v)
    return TodoistService(c, TZ, transport=httpx.MockTransport(handler), now_func=lambda: NOW)


def test_pagination_with_cursor_and_auth():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer tok"
        assert request.url.host == "api.todoist.com"
        assert request.url.params["limit"] == "200"
        path = request.url.path
        if path == "/api/v1/projects":
            return httpx.Response(200, json={"results": PROJECTS, "next_cursor": None})
        assert path == "/api/v1/tasks"
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"results": [task(1, "eins")], "next_cursor": "abc"})
        if cursor == "abc":
            return httpx.Response(200, json={"results": [task(2, "zwei")], "next_cursor": "def"})
        assert cursor == "def"
        return httpx.Response(200, json={"results": [task(3, "drei")], "next_cursor": None})

    svc = make_service(handler)
    asyncio.run(svc.refresh())
    snap = svc.snapshot()
    assert snap["stale"] is False and snap["error"] is None
    assert [t["content"] for t in snap["projects"][0]["tasks"]] == ["eins", "zwei", "drei"]
    assert snap["updated_at"] == "2026-11-15T14:00:00+01:00"
    assert len([r for r in seen if r.url.path == "/api/v1/tasks"]) == 3


def test_filter_uses_filter_endpoint():
    paths = []

    def handler(request):
        paths.append((request.url.path, request.url.params.get("query")))
        if request.url.path.endswith("projects"):
            return httpx.Response(200, json={"results": PROJECTS, "next_cursor": None})
        return httpx.Response(200, json={"results": [task(1, "x")], "next_cursor": None})

    svc = make_service(handler, filter="today | overdue")
    asyncio.run(svc.refresh())
    assert ("/api/v1/tasks/filter", "today | overdue") in paths


def test_error_keeps_old_cache_and_sets_stale():
    state = {"fail": False}

    def handler(request):
        if state["fail"]:
            return httpx.Response(500)
        if request.url.path.endswith("projects"):
            return httpx.Response(200, json={"results": PROJECTS, "next_cursor": None})
        return httpx.Response(200, json={"results": [task(1, "alt")], "next_cursor": None})

    svc = make_service(handler)
    asyncio.run(svc.refresh())
    state["fail"] = True
    asyncio.run(svc.refresh())
    snap = svc.snapshot()
    assert snap["stale"] is True and snap["error"] == "HTTP 500"
    assert snap["projects"][0]["tasks"][0]["content"] == "alt"
    assert snap["updated_at"] is not None
    assert svc.health()["last_error"] == "HTTP 500"


def test_unauthorized_message():
    svc = make_service(lambda r: httpx.Response(401))
    asyncio.run(svc.refresh())
    assert "Token" in svc.snapshot()["error"]
