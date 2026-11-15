"""Statuslogik (auto/manuell) und Persistenz des Modus."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .calendar_ics import CalEvent

log = logging.getLogger(__name__)

STATUSES = ("free", "busy", "dnd")
MANUAL_LABELS = {"free": "Frei", "busy": "Beschäftigt", "dnd": "Nicht stören"}
DEFAULT_BUSY_LABEL = "Im Termin"


@dataclass(frozen=True)
class ModeState:
    mode: str = "auto"  # auto | manual
    status: str | None = None  # nur bei manual: free | busy | dnd


def compute_status(mode_state: ModeState, events: Iterable[CalEvent], now: datetime,
                   show_titles: bool = True) -> dict:
    """Reine Funktion: {"status","mode","until","label"} (until als ISO-String oder None)."""
    if mode_state.mode == "manual" and mode_state.status in STATUSES:
        return {"status": mode_state.status, "mode": "manual", "until": None,
                "label": MANUAL_LABELS[mode_state.status]}

    active = [e for e in events
              if e.busy and not e.all_day and e.start <= now < e.end]
    if not active:
        return {"status": "free", "mode": "auto", "until": None, "label": "Frei"}

    active.sort(key=lambda e: e.start)
    until = max(e.end for e in active)
    label = active[0].title if show_titles and active[0].title else DEFAULT_BUSY_LABEL
    return {"status": "busy", "mode": "auto",
            "until": until.astimezone(now.tzinfo).isoformat() if now.tzinfo else until.isoformat(),
            "label": label}


class AvailabilityStore:
    """Hält den Modus und speichert ihn atomar in `state_file`."""

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        self.state = ModeState()
        self.load()

    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            mode = data.get("mode", "auto")
            status = data.get("status")
            if mode == "manual" and status in STATUSES:
                self.state = ModeState("manual", status)
            else:
                self.state = ModeState()
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("State-Datei %s nicht lesbar (%s) - starte im Auto-Modus", self.path, exc)
            self.state = ModeState()

    def set_auto(self) -> ModeState:
        return self._set(ModeState("auto", None))

    def set_manual(self, status: str) -> ModeState:
        if status not in STATUSES:
            raise ValueError(f"Ungültiger Status: {status!r}")
        return self._set(ModeState("manual", status))

    def _set(self, state: ModeState) -> ModeState:
        self.state = state
        self.save()
        return state

    def save(self) -> None:
        if not self.path:
            return
        payload = json.dumps({"mode": self.state.mode, "status": self.state.status}, ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.error("State-Datei %s konnte nicht gespeichert werden: %s", self.path, exc)
