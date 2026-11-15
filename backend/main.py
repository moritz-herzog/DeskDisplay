"""FastAPI-App: Routen, Lifespan, Hintergrund-Jobs."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator

from .availability import AvailabilityStore, compute_status
from .calendar_ics import CalendarService, build_days
from .config import PROJECT_ROOT, Config, ConfigError, load_config
from .mock_data import mock_calendar_events, mock_todoist_raw
from .mqtt_publisher import MqttPublisher, NullPublisher
from .todoist_client import TodoistService, build_projects

log = logging.getLogger("backend")

FRONTEND_DIR = PROJECT_ROOT / "frontend"
NO_CACHE = {"Cache-Control": "no-cache"}


# ---------------------------------------------------------------------------
# Mock-Dienste (gleiche Schnittstelle wie die echten, ohne Netzwerk)
# ---------------------------------------------------------------------------
class MockTodoistService:
    def __init__(self, tz: ZoneInfo):
        self.tz = tz
        self.updated_at: datetime | None = None

    async def refresh(self) -> None:
        self.updated_at = datetime.now(self.tz)

    def snapshot(self, now: datetime | None = None) -> dict:
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        tasks, projects = mock_todoist_raw(now)
        return {"updated_at": (self.updated_at or now).isoformat(), "stale": False, "error": None,
                "projects": build_projects(tasks, projects, self.tz, now)}

    def health(self) -> dict:
        return {"last_success": self.updated_at.isoformat() if self.updated_at else None, "last_error": None}


class MockCalendarService:
    def __init__(self, cfg, tz: ZoneInfo):
        self.cfg, self.tz = cfg, tz
        self.updated_at: datetime | None = None

    async def refresh(self) -> None:
        self.updated_at = datetime.now(self.tz)

    def events(self, now: datetime | None = None):
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        return mock_calendar_events(now, self.tz, self.cfg.days_ahead)

    def snapshot(self, now: datetime | None = None) -> dict:
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        return {"updated_at": (self.updated_at or now).isoformat(), "stale": False, "error": None,
                "timezone": self.cfg.timezone, "day_start_hour": self.cfg.day_start_hour,
                "day_end_hour": self.cfg.day_end_hour,
                "days": build_days(self.events(now), self.tz, now.date(), self.cfg.days_ahead)}

    def health(self) -> dict:
        return {"last_success": self.updated_at.isoformat() if self.updated_at else None, "last_error": None}


# ---------------------------------------------------------------------------
# Anwendungs-Kontext
# ---------------------------------------------------------------------------
class AppContext:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.calendar.timezone)
        self.store = AvailabilityStore(cfg.state_path)
        if cfg.server.mock:
            self.todoist = MockTodoistService(self.tz)
            self.calendar = MockCalendarService(cfg.calendar, self.tz)
        else:
            self.todoist = TodoistService(cfg.todoist, self.tz)
            self.calendar = CalendarService(cfg.calendar)
        self.mqtt = MqttPublisher(cfg.mqtt) if cfg.mqtt_active else NullPublisher()
        self.tasks: list[asyncio.Task] = []

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def status(self, now: datetime | None = None) -> dict:
        now = now or self.now()
        return compute_status(self.store.state, self.calendar.events(now), now,
                              self.cfg.calendar.show_titles_in_status)

    def availability_response(self) -> dict:
        data = self.status()
        data["mqtt_connected"] = bool(self.mqtt.connected)
        return data

    def calendar_ready(self) -> bool:
        """Kalender mindestens einmal abgerufen (erfolgreich oder fehlgeschlagen)?
        Verhindert, dass nach einem Neustart kurz "free" gesendet wird."""
        cal = self.calendar
        return getattr(cal, "ics_text", "") is not None or getattr(cal, "error", None) is not None

    def publish(self, force: bool = False) -> dict:
        data = self.status()
        if data.get("mode") == "auto" and not self.calendar_ready():
            return data
        try:
            self.mqtt.publish_status(data, force=force)
        except Exception:  # noqa: BLE001
            log.exception("MQTT-Publish fehlgeschlagen")
        return data


# ---------------------------------------------------------------------------
# Hintergrund-Jobs
# ---------------------------------------------------------------------------
async def _loop(name: str, interval: float, fn) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Hintergrundjob %s fehlgeschlagen", name)
        await asyncio.sleep(interval)


async def _todoist_job(ctx: AppContext) -> None:
    await ctx.todoist.refresh()


async def _calendar_job(ctx: AppContext) -> None:
    await ctx.calendar.refresh()
    ctx.publish()


async def _tick_job(ctx: AppContext) -> None:
    """Jede Minute (Sekunde 0): Status neu berechnen, bei Änderung senden."""
    while True:
        now = ctx.now()
        nxt = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        await asyncio.sleep(max((nxt - now).total_seconds(), 0.05))
        try:
            ctx.publish()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Status-Tick fehlgeschlagen")


async def _heartbeat_job(ctx: AppContext) -> None:
    interval = ctx.cfg.mqtt.heartbeat_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            ctx.status()  # Status aktuell halten
            ctx.publish()
            ctx.mqtt.heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Heartbeat fehlgeschlagen")


def _setup_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# API-Modelle
# ---------------------------------------------------------------------------
class AvailabilityRequest(BaseModel):
    mode: Literal["auto", "manual"]
    status: Optional[Literal["free", "busy", "dnd"]] = None

    @model_validator(mode="after")
    def _check(self):
        if self.mode == "manual" and self.status is None:
            raise ValueError("Bei mode 'manual' ist 'status' (free|busy|dnd) erforderlich")
        return self


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# ---------------------------------------------------------------------------
# App-Fabrik
# ---------------------------------------------------------------------------
def create_app(config: Config | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _setup_logging()
        cfg = config
        if cfg is None:
            try:
                cfg = load_config()
            except ConfigError as exc:
                log.error("%s", exc)
                raise SystemExit(f"\nFEHLER: {exc}\n") from exc
        ctx = AppContext(cfg)
        app.state.ctx = ctx
        log.info("Dashboard startet (mock=%s, mqtt=%s, Zeitzone %s)", cfg.server.mock, cfg.mqtt_active,
                 cfg.calendar.timezone)
        ctx.mqtt.start()
        ctx.tasks = [
            asyncio.create_task(_loop("todoist", cfg.todoist.refresh_seconds, lambda: _todoist_job(ctx))),
            asyncio.create_task(_loop("calendar", cfg.calendar.refresh_seconds, lambda: _calendar_job(ctx))),
            asyncio.create_task(_tick_job(ctx)),
        ]
        if cfg.mqtt_active:
            ctx.tasks.append(asyncio.create_task(_heartbeat_job(ctx)))
        try:
            yield
        finally:
            for t in ctx.tasks:
                t.cancel()
            await asyncio.gather(*ctx.tasks, return_exceptions=True)
            ctx.mqtt.stop()

    app = FastAPI(title="Pi-Dashboard", lifespan=lifespan)

    def ctx_of() -> AppContext:
        ctx = getattr(app.state, "ctx", None)
        if ctx is None:
            raise HTTPException(status_code=503, detail="Backend startet noch")
        return ctx

    @app.get("/api/tasks")
    async def api_tasks():
        return ctx_of().todoist.snapshot()

    @app.get("/api/calendar")
    async def api_calendar():
        return ctx_of().calendar.snapshot()

    @app.get("/api/availability")
    async def api_availability():
        return ctx_of().availability_response()

    @app.post("/api/availability")
    async def api_set_availability(req: AvailabilityRequest):
        ctx = ctx_of()
        if req.mode == "auto":
            ctx.store.set_auto()
        else:
            ctx.store.set_manual(req.status)  # type: ignore[arg-type]
        ctx.publish()
        return ctx.availability_response()

    @app.get("/api/settings")
    async def api_settings():
        d = ctx_of().cfg.display
        return {"default_view": d.default_view, "return_after_seconds": d.return_after_seconds,
                "theme": d.theme, "priority_colors": d.priority_colors, "status_colors": d.status_colors}

    @app.get("/api/health")
    async def api_health():
        ctx = ctx_of()
        return {"todoist": ctx.todoist.health(), "calendar": ctx.calendar.health(),
                "mqtt": {"enabled": bool(ctx.mqtt.enabled), "connected": bool(ctx.mqtt.connected)}}

    index = FRONTEND_DIR / "index.html"

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root():
        if not index.is_file():
            raise HTTPException(status_code=404, detail="frontend/index.html fehlt")
        return FileResponse(index, headers=NO_CACHE)

    if FRONTEND_DIR.is_dir():
        # nach den API-Routen: /style.css, /app.js, /icons.js, ...
        app.mount("/", NoCacheStaticFiles(directory=FRONTEND_DIR), name="frontend")

    return app


app = create_app()
