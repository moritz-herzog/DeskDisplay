"""Todoist API v1: Aufgaben + Projekte laden, sortieren, Überfälligkeit berechnen."""
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


def build_projects(tasks: list[dict], projects: list[dict], tz: ZoneInfo, now: datetime,
                   project_name: str = "") -> list[dict]:
    """Rohdaten -> Projektliste laut API-Vertrag (sortiert, ohne leere Projekte)."""
    now = now.astimezone(tz)
    by_id = {str(p["id"]): p for p in projects}

    if project_name:
        wanted = [p for p in projects if str(p.get("name", "")).strip().lower() == project_name.strip().lower()]
        if not wanted:
            raise TodoistError(f"Projekt '{project_name}' nicht gefunden")
        ids = {str(p["id"]) for p in wanted}
        tasks = [t for t in tasks if str(t.get("project_id")) in ids]

    groups: dict[str, list[tuple]] = {}
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
        groups.setdefault(str(t.get("project_id")), []).append((key, item))

    def project_order(pid: str):
        p = by_id.get(pid)
        return (0, int(p.get("child_order") or 0)) if p else (1, 0)

    result = []
    for pid in sorted(groups, key=project_order):
        p = by_id.get(pid)
        items = [it for _, it in sorted(groups[pid], key=lambda x: x[0])]
        result.append({"id": pid, "name": p.get("name", "") if p else "Unbekannt",
                       "color": (p.get("color") if p else None) or "grey", "tasks": items})
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
                if self.cfg.filter:
                    tasks = await self._fetch_all(client, "tasks/filter", {"query": self.cfg.filter})
                else:
                    tasks = await self._fetch_all(client, "tasks")
            # Vorab prüfen, ob sich das Ergebnis bauen lässt (z. B. Projektname)
            build_projects(tasks, projects, self.tz, self._now(), self.cfg.project)
            self.raw_tasks, self.raw_projects = tasks, projects
            self.updated_at = self.last_success = self._now()
            self.error, self.stale = None, False
        except Exception as exc:  # noqa: BLE001 - Hintergrundjob darf nie abstürzen
            self.stale = True
            self.error = _short_error(exc)
            log.warning("Todoist-Abruf fehlgeschlagen: %s", self.error)

    def snapshot(self, now: datetime | None = None) -> dict:
        now = now or self._now()
        try:
            projects = build_projects(self.raw_tasks, self.raw_projects, self.tz, now, self.cfg.project)
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
