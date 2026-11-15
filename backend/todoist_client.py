"""Todoist API v1: Aufgaben, Projekte (inkl. Unterprojekte) + Abschnitte laden, sortieren, Überfälligkeit berechnen."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.todoist.com/api/v1/"
PAGE_LIMIT = 200
MAX_PAGES = 100


class TodoistError(Exception):
    pass


def parse_due(due: dict | None, tz: ZoneInfo) -> dict | None:
    """Todoist-`due` -> {"date","datetime","string","is_recurring", + interne Felder}."""
    if not due:
        return None
    raw_date = due.get("date") or ""
    raw_dt = due.get("datetime")
    if not raw_dt and "T" in raw_date:
        raw_dt = raw_date
    dt_val: datetime | None = None
    if raw_dt:
        try:
            dt_val = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            if dt_val.tzinfo is None:
                zone = due.get("timezone")
                try:
                    dt_val = dt_val.replace(tzinfo=ZoneInfo(zone) if zone else tz)
                except Exception:  # noqa: BLE001
                    dt_val = dt_val.replace(tzinfo=tz)
            dt_val = dt_val.astimezone(tz)
        except ValueError:
            dt_val = None
    if dt_val is not None:
        d = dt_val.date()
    else:
        try:
            d = date.fromisoformat(raw_date[:10])
        except ValueError:
            return None
    return {"date": d.isoformat(), "datetime": dt_val.isoformat() if dt_val else None,
            "string": due.get("string") or "", "is_recurring": bool(due.get("is_recurring")),
            "_dt": dt_val, "_date": d}


def is_overdue(parsed_due: dict | None, now: datetime) -> bool:
    if not parsed_due:
        return False
    if parsed_due["_dt"] is not None:
        return parsed_due["_dt"] < now
    return parsed_due["_date"] < now.date()


def _descendant_ids(projects: list[dict], root_ids: set[str]) -> set[str]:
    """Alle Projekt-IDs aus `root_ids` samt aller Unterprojekte (beliebige Tiefe, zyklensicher)."""
    children: dict[str, list[str]] = {}
    for p in projects:
        parent = p.get("parent_id")
        if parent:
            children.setdefault(str(parent), []).append(str(p["id"]))
    found = set(root_ids)
    stack = list(root_ids)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _tree_order(projects: list[dict]) -> list[str]:
    """Projekt-IDs in Baumreihenfolge (Tiefensuche, Geschwister nach `child_order`)."""
    ids = {str(p["id"]) for p in projects}
    children: dict[str | None, list[dict]] = {}
    for p in projects:
        parent = str(p["parent_id"]) if p.get("parent_id") else None
        if parent not in ids or parent == str(p["id"]):
            parent = None  # unbekannter Elternteil -> Wurzel
        children.setdefault(parent, []).append(p)

    order: list[str] = []
    seen: set[str] = set()

    def visit(parent: str | None) -> None:
        for p in sorted(children.get(parent, []), key=lambda x: int(x.get("child_order") or 0)):
            pid = str(p["id"])
            if pid in seen:
                continue
            seen.add(pid)
            order.append(pid)
            visit(pid)

    visit(None)
    for p in projects:  # Zyklen ohne Wurzel: übrige Projekte trotzdem anhängen
        pid = str(p["id"])
        if pid not in seen:
            seen.add(pid)
            order.append(pid)
    return order


def build_projects(tasks: list[dict], projects: list[dict], tz: ZoneInfo, now: datetime,
                   project_name: str = "", sections: list[dict] | None = None) -> list[dict]:
    """Rohdaten -> Kachelliste laut API-Vertrag (sortiert, ohne leere Kacheln).

    Jede Kachel ist ein Projekt ohne Abschnitt oder ein Abschnitt eines Projekts:
    `{"id","name","section","parent","color","tasks"}`. Aufgaben ohne (oder mit unbekanntem,
    archiviertem bzw. gelöschtem) Abschnitt landen in der Projektkachel. Reihenfolge: Projekte
    in Baumreihenfolge (Unterprojekte direkt nach ihrem Elternprojekt), je Projekt zuerst die
    Kachel ohne Abschnitt, dann die Abschnitte nach `section_order`.
    Mit `project_name` werden das gleichnamige Projekt und alle Unterprojekte berücksichtigt.
    """
    now = now.astimezone(tz)
    by_id = {str(p["id"]): p for p in projects}

    if project_name:
        wanted = [p for p in projects if str(p.get("name", "")).strip().lower() == project_name.strip().lower()]
        if not wanted:
            raise TodoistError(f"Projekt '{project_name}' nicht gefunden")
        ids = _descendant_ids(projects, {str(p["id"]) for p in wanted})
        tasks = [t for t in tasks if str(t.get("project_id")) in ids]

    sec_by_id: dict[str, dict] = {}
    for s in sections or []:
        if s.get("is_deleted") or s.get("is_archived"):
            continue
        sec_by_id[str(s["id"])] = s

    groups: dict[tuple[str, str | None], list[tuple]] = {}
    for t in tasks:
        if t.get("checked") or t.get("is_deleted"):
            continue
        due = parse_due(t.get("due"), tz)
        overdue = is_overdue(due, now)
        priority = int(t.get("priority") or 1)
        if due is None:
            due_key = (1, date.max, time.max)
        else:
            dt = due["_dt"]
            due_key = (0, due["_date"], dt.timetz().replace(tzinfo=None) if dt else time.min)
        key = (0 if overdue else 1,) + due_key + (-priority, int(t.get("child_order") or 0))
        out_due = None
        if due:
            out_due = {k: due[k] for k in ("date", "datetime", "string", "is_recurring")}
        item = {
            "id": str(t["id"]),
            "content": t.get("content", ""),
            "description": t.get("description", "") or "",
            "priority": priority,
            "labels": list(t.get("labels") or []),
            "overdue": overdue,
            "due": out_due,
        }
        pid = str(t.get("project_id"))
        sid = str(t["section_id"]) if t.get("section_id") else None
        sec = sec_by_id.get(sid) if sid else None
        groups.setdefault((pid, str(sec["id"]) if sec is not None else None), []).append((key, item))

    tree = {pid: i for i, pid in enumerate(_tree_order(projects))}

    def tile_order(k: tuple[str, str | None]):
        pid, sid = k
        proj_pos = (0, tree[pid]) if pid in tree else (1, 0)
        if sid is None:
            return proj_pos + (0, 0)
        return proj_pos + (1, int(sec_by_id[sid].get("section_order") or 0))

    result = []
    for pid, sid in sorted(groups, key=tile_order):
        p = by_id.get(pid)
        items = [it for _, it in sorted(groups[(pid, sid)], key=lambda x: x[0])]
        parent_p = by_id.get(str(p["parent_id"])) if p and p.get("parent_id") else None
        result.append({
            "id": pid if sid is None else f"{pid}:{sid}",
            "name": p.get("name", "") if p else "Unbekannt",
            "section": sec_by_id[sid].get("name", "") if sid is not None else None,
            "parent": parent_p.get("name", "") if parent_p else None,
            "color": (p.get("color") if p else None) or "grey",
            "tasks": items,
        })
    return result


class TodoistService:
    def __init__(self, cfg, tz: ZoneInfo, transport: httpx.AsyncBaseTransport | None = None,
                 now_func: Callable[[], datetime] | None = None):
        self.cfg = cfg  # TodoistConfig
        self.tz = tz
        self._transport = transport
        self._now_func = now_func
        self.raw_tasks: list[dict] = []
        self.raw_projects: list[dict] = []
        self.raw_sections: list[dict] = []
        self.updated_at: datetime | None = None
        self.last_success: datetime | None = None
        self.error: str | None = None
        self.stale = False

    def _now(self) -> datetime:
        return self._now_func() if self._now_func else datetime.now(self.tz)

    async def _fetch_all(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            q = dict(params or {})
            q["limit"] = PAGE_LIMIT
            if cursor:
                q["cursor"] = cursor
            resp = await client.get(path, params=q)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):  # defensiv: alte API ohne Paginierung
                return results + data
            results.extend(data.get("results") or [])
            cursor = data.get("next_cursor")
            if not cursor:
                return results
        raise TodoistError("Zu viele Seiten bei der Todoist-Abfrage")

    async def refresh(self) -> None:
        try:
            headers = {"Authorization": f"Bearer {self.cfg.token}"}
            async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=self.cfg.timeout_seconds,
                                         follow_redirects=True, transport=self._transport) as client:
                projects = await self._fetch_all(client, "projects")
                sections = await self._fetch_all(client, "sections")
                if self.cfg.filter:
                    tasks = await self._fetch_all(client, "tasks/filter", {"query": self.cfg.filter})
                else:
                    tasks = await self._fetch_all(client, "tasks")
            # Vorab prüfen, ob sich das Ergebnis bauen lässt (z. B. Projektname)
            build_projects(tasks, projects, self.tz, self._now(), self.cfg.project, sections)
            self.raw_tasks, self.raw_projects, self.raw_sections = tasks, projects, sections
            self.updated_at = self.last_success = self._now()
            self.error, self.stale = None, False
        except Exception as exc:  # noqa: BLE001 - Hintergrundjob darf nie abstürzen
            self.stale = True
            self.error = _short_error(exc)
            log.warning("Todoist-Abruf fehlgeschlagen: %s", self.error)

    def snapshot(self, now: datetime | None = None) -> dict:
        now = now or self._now()
        try:
            projects = build_projects(self.raw_tasks, self.raw_projects, self.tz, now, self.cfg.project,
                                     self.raw_sections)
        except TodoistError:
            projects = []
        return {
            "updated_at": self.updated_at.astimezone(self.tz).isoformat() if self.updated_at else None,
            "stale": self.stale,
            "error": self.error,
            "projects": projects,
        }

    def health(self) -> dict:
        return {"last_success": self.last_success.astimezone(self.tz).isoformat() if self.last_success else None,
                "last_error": self.error}


def _short_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return "Token ungültig (HTTP %d)" % code if code in (401, 403) else f"HTTP {code}"
    if isinstance(exc, httpx.TimeoutException):
        return "Zeitüberschreitung"
    if isinstance(exc, httpx.HTTPError):
        return f"Verbindungsfehler ({type(exc).__name__})"
    return f"{type(exc).__name__}: {exc}"[:160]
