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
    assert set(t) == {"id", "content", "description", "priority", "labels", "overdue", "due", "depth"}
    assert t["depth"] == 0
    assert set(out[1]) == {"id", "name", "section", "parent", "color", "tasks"}
    assert out[1]["section"] is None and out[1]["parent"] is None
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


TREE = [
    {"id": "r2", "name": "Arbeit", "color": "blue", "child_order": 2, "parent_id": None},
    {"id": "r1", "name": "Privat", "color": "red", "child_order": 1, "parent_id": None},
    {"id": "c2", "name": "Kunde B", "color": "blue", "child_order": 2, "parent_id": "r2"},
    {"id": "c1", "name": "Kunde A", "color": "blue", "child_order": 1, "parent_id": "r2"},
    {"id": "g1", "name": "Enkel", "color": "green", "child_order": 0, "parent_id": "c1"},
]


def test_subprojects_included_with_project_filter():
    tasks = [task(1, "a", project="r2"), task(2, "b", project="c1"), task(3, "c", project="g1"),
             task(4, "d", project="r1")]
    out = build_projects(tasks, TREE, TZ, NOW, project_name="arbeit")
    assert [p["name"] for p in out] == ["Arbeit", "Kunde A", "Enkel"]
    assert [p["parent"] for p in out] == [None, "Arbeit", "Kunde A"]


def test_tree_order():
    tasks = [task(i, "x", project=pid) for i, pid in enumerate(["g1", "c2", "r1", "c1", "r2", "unbekannt"])]
    out = build_projects(tasks, TREE, TZ, NOW)
    assert [p["name"] for p in out] == ["Privat", "Arbeit", "Kunde A", "Enkel", "Kunde B", "Unbekannt"]
    assert out[-1]["parent"] is None


SECTIONS = [
    {"id": "s2", "project_id": "p1", "name": "Zweiter", "section_order": 2, "is_archived": False, "is_deleted": False},
    {"id": "s1", "project_id": "p1", "name": "Erster", "section_order": 1, "is_archived": False, "is_deleted": False},
    {"id": "sa", "project_id": "p1", "name": "Archiv", "section_order": 3, "is_archived": True, "is_deleted": False},
]


def test_sections_become_tiles():
    tasks = [task(1, "in s2", section_id="s2"), task(2, "in s1", section_id="s1"), task(3, "ohne"),
             task(4, "leer", section_id=""), task(5, "in Arbeit", project="p2")]
    out = build_projects(tasks, PROJECTS, TZ, NOW, sections=SECTIONS)
    assert [(p["id"], p["name"], p["section"]) for p in out] == [
        ("p1", "Inbox", None), ("p1:s1", "Inbox", "Erster"), ("p1:s2", "Inbox", "Zweiter"), ("p2", "Arbeit", None)]
    assert [t["content"] for t in out[0]["tasks"]] == ["ohne", "leer"]


def test_unknown_or_archived_section_falls_back_to_project():
    tasks = [task(1, "archiviert", section_id="sa"), task(2, "unbekannt", section_id="nix")]
    out = build_projects(tasks, PROJECTS, TZ, NOW, sections=SECTIONS)
    assert len(out) == 1 and out[0]["id"] == "p1" and out[0]["section"] is None
    assert len(out[0]["tasks"]) == 2


def test_refresh_fetches_sections_and_builds_tiles():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("projects"):
            return httpx.Response(200, json={"results": PROJECTS, "next_cursor": None})
        if request.url.path.endswith("sections"):
            return httpx.Response(200, json={"results": SECTIONS, "next_cursor": None})
        return httpx.Response(200, json={"results": [task(1, "x", section_id="s1"), task(2, "y")],
                                         "next_cursor": None})

    svc = make_service(handler)
    asyncio.run(svc.refresh())
    assert "/api/v1/sections" in paths
    assert [p["id"] for p in svc.snapshot()["projects"]] == ["p1", "p1:s1"]


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
        if path == "/api/v1/sections":
            return httpx.Response(200, json={"results": [], "next_cursor": None})
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
        if request.url.path.endswith("sections"):
            return httpx.Response(200, json={"results": [], "next_cursor": None})
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
        if request.url.path.endswith("sections"):
            return httpx.Response(200, json={"results": [], "next_cursor": None})
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


def _flat(out, project="Inbox"):
    return [(t["content"], t["depth"]) for p in out if p["name"] == project and p["section"] is None
            for t in p["tasks"]]


def test_subtasks_directly_after_parent_even_if_overdue():
    tasks = [
        task(1, "Eltern", due=d("2026-11-16")),
        task(2, "Kind überfällig", due=d("2026-11-10"), parent_id="1"),
        task(3, "andere", due=d("2026-11-17")),
    ]
    out = build_projects(tasks, PROJECTS, TZ, NOW)
    assert _flat(out) == [("Eltern", 0), ("Kind überfällig", 1), ("andere", 0)]


def test_sibling_order_and_depth_two():
    tasks = [
        task(1, "Eltern"),
        task(2, "Kind spät", due=d("2026-11-20"), parent_id="1"),
        task(3, "Kind früh", due=d("2026-11-16"), parent_id="1"),
        task(4, "Enkel", parent_id="3"),
        task(5, "Wurzel 2", due=d("2026-11-30")),
    ]
    out = build_projects(tasks, PROJECTS, TZ, NOW)
    assert _flat(out) == [("Wurzel 2", 0), ("Eltern", 0), ("Kind früh", 1), ("Enkel", 2), ("Kind spät", 1)]


def test_orphans_become_roots():
    sections = [{"id": "s1", "project_id": "p1", "name": "Abschnitt", "section_order": 1,
                 "is_archived": False, "is_deleted": False}]
    tasks = [
        task(1, "unbekannter Elternteil", parent_id="999"),
        task(2, "Elternteil erledigt", parent_id="3"),
        task(3, "erledigt", checked=True),
        task(4, "Eltern im Abschnitt", section_id="s1"),
        task(5, "Kind in Projektkachel", parent_id="4"),
    ]
    out = build_projects(tasks, PROJECTS, TZ, NOW, sections=sections)
    assert sorted(_flat(out)) == sorted([("unbekannter Elternteil", 0), ("Elternteil erledigt", 0),
                                         ("Kind in Projektkachel", 0)])
    sec = [p for p in out if p["section"] == "Abschnitt"][0]
    assert [(t["content"], t["depth"]) for t in sec["tasks"]] == [("Eltern im Abschnitt", 0)]


def test_cycles_do_not_loop_or_lose_tasks():
    tasks = [
        task(1, "selbst", parent_id="1"),
        task(2, "A", parent_id="3", order=1),
        task(3, "B", parent_id="2", order=2),
        task(4, "normal"),
    ]
    out = build_projects(tasks, PROJECTS, TZ, NOW)
    ids = [t["id"] for t in out[0]["tasks"]]
    assert sorted(ids) == ["1", "2", "3", "4"]
    assert all(t["depth"] >= 0 for t in out[0]["tasks"])


def test_subtasks_with_integer_ids():
    tasks = [{**task(1, "Eltern"), "id": 1}, {**task(2, "Kind"), "id": 2, "parent_id": 1}]
    out = build_projects(tasks, PROJECTS, TZ, NOW)
    assert _flat(out) == [("Eltern", 0), ("Kind", 1)]
