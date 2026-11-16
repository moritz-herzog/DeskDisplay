"""`python -m backend` - startet uvicorn mit Host/Port aus der Konfiguration.

`python -m backend --check-calendar` ruft den Kalender einmal ab und zeigt die heutigen Termine."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime

import uvicorn

from .config import Config, ConfigError, load_config
from .main import create_app


def _fmt_time(value) -> str:
    return value.strftime("%H:%M")


async def check_calendar(cfg: Config) -> int:
    """Einmal abrufen und heutige Termine ausgeben. Rückgabe: Exit-Code (0 = Abruf erfolgreich)."""
    from .main import AppContext

    ctx = AppContext(cfg)
    cal = ctx.calendar
    quelle = "Mock" if cfg.server.mock else ("Exchange (EWS)" if cfg.calendar.source == "ews" else "ICS-Datei")
    print(f"Kalenderquelle: {quelle}")
    await cal.refresh()
    if getattr(cal, "error", None):
        print(f"FEHLER beim Kalenderabruf: {cal.error}", file=sys.stderr)
        return 1
    now = datetime.now(ctx.tz)
    snap = cal.snapshot(now)
    today = snap["days"][0]["events"] if snap["days"] else []
    print(f"Heute ({now.date().strftime('%d.%m.%Y')}): {len(today)} Termin(e)")
    for ev in today:
        if ev["all_day"]:
            zeit = "ganztägig"
        else:
            zeit = f"{_fmt_time(datetime.fromisoformat(ev['start']))}-{_fmt_time(datetime.fromisoformat(ev['end']))}"
        print(f"  {zeit:<11} {'beschäftigt' if ev['busy'] else 'frei':<12} {ev['title']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend", description="Pi-Dashboard Backend")
    parser.add_argument("--check-calendar", action="store_true",
                        help="Kalender einmal abrufen, heutige Termine anzeigen und beenden")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"\nFEHLER: {exc}\n", file=sys.stderr)
        return 1
    if args.check_calendar:
        return asyncio.run(check_calendar(cfg))
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
