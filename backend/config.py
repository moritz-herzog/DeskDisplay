"""Laden und Prüfen der Konfiguration (config.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

DEFAULT_PRIORITY_COLORS = {"p1": "#FF5A5F", "p2": "#FFA94D", "p3": "#5C9DFF", "p4": "#8A8F98"}
DEFAULT_STATUS_COLORS = {"free": "#2E7D32", "busy": "#C62828", "dnd": "#6A1B9A"}

PLACEHOLDER_TOKENS = {"", "DEIN_TOKEN"}


class ConfigError(Exception):
    """Konfigurationsfehler mit verständlicher (deutscher) Meldung."""


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    state_file: str = "state.json"
    mock: bool = False


@dataclass
class TodoistConfig:
    token: str = ""
    filter: str = ""
    project: str = ""
    refresh_seconds: int = 120
    timeout_seconds: int = 10


@dataclass
class CalendarConfig:
    ics_url: str = ""
    refresh_seconds: int = 300
    timeout_seconds: int = 15
    days_ahead: int = 5
    timezone: str = "Europe/Berlin"
    day_start_hour: int = 7
    day_end_hour: int = 20
    show_titles_in_status: bool = True

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    topic: str = "dashboard/availability"
    online_topic: str = "dashboard/online"
    heartbeat_seconds: int = 300


@dataclass
class DisplayConfig:
    default_view: str = "tasks"
    return_after_seconds: int = 120
    theme: str = "dark"
    priority_colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PRIORITY_COLORS))
    status_colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATUS_COLORS))


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    todoist: TodoistConfig = field(default_factory=TodoistConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    base_dir: Path = PROJECT_ROOT

    @property
    def state_path(self) -> Path:
        p = Path(self.server.state_file)
        return p if p.is_absolute() else self.base_dir / p

    @property
    def mqtt_active(self) -> bool:
        return self.mqtt.enabled and not self.server.mock

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Any, base_dir: Path | None = None) -> "Config":
        errors: list[str] = []
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError("Die Konfiguration muss ein YAML-Objekt (Abschnitte wie 'server:', 'todoist:') sein.")

        def section(name: str, required: bool) -> dict[str, Any]:
            val = raw.get(name)
            if val is None:
                if required:
                    errors.append(f"Abschnitt '{name}' fehlt in der Konfiguration.")
                return {}
            if not isinstance(val, dict):
                errors.append(f"Abschnitt '{name}' muss ein Objekt mit Einstellungen sein.")
                return {}
            return val

        def get_str(sec: dict, sname: str, key: str, default: str, required: bool = False) -> str:
            val = sec.get(key, default)
            if val is None:
                val = default
            if not isinstance(val, (str, int, float)) or isinstance(val, bool):
                errors.append(f"'{sname}.{key}' muss ein Text sein.")
                return default
            val = str(val).strip() if key != "password" else str(val)
            if required and not val:
                errors.append(f"Pflichtfeld '{sname}.{key}' fehlt oder ist leer.")
            return val

        def get_int(sec: dict, sname: str, key: str, default: int, lo: int, hi: int | None = None) -> int:
            val = sec.get(key, default)
            if val is None:
                return default
            if isinstance(val, bool) or not isinstance(val, int):
                errors.append(f"'{sname}.{key}' muss eine ganze Zahl sein (gefunden: {val!r}).")
                return default
            if val < lo or (hi is not None and val > hi):
                rng = f"{lo} bis {hi}" if hi is not None else f"mindestens {lo}"
                errors.append(f"'{sname}.{key}' muss {rng} sein (gefunden: {val}).")
                return default
            return val

        def get_bool(sec: dict, sname: str, key: str, default: bool) -> bool:
            val = sec.get(key, default)
            if val is None:
                return default
            if not isinstance(val, bool):
                errors.append(f"'{sname}.{key}' muss true oder false sein (gefunden: {val!r}).")
                return default
            return val

        # --- server
        s = section("server", False)
        server = ServerConfig(
            host=get_str(s, "server", "host", "127.0.0.1"),
            port=get_int(s, "server", "port", 8080, 1, 65535),
            state_file=get_str(s, "server", "state_file", "state.json"),
            mock=get_bool(s, "server", "mock", False),
        )
        mock = server.mock

        # --- todoist
        t = section("todoist", not mock)
        todoist = TodoistConfig(
            token=get_str(t, "todoist", "token", ""),
            filter=get_str(t, "todoist", "filter", ""),
            project=get_str(t, "todoist", "project", ""),
            refresh_seconds=get_int(t, "todoist", "refresh_seconds", 120, 10),
            timeout_seconds=get_int(t, "todoist", "timeout_seconds", 10, 1),
        )
        if not mock and todoist.token in PLACEHOLDER_TOKENS:
            errors.append("Pflichtfeld 'todoist.token' fehlt (oder steht noch auf DEIN_TOKEN). "
                          "Den API-Token findest du in Todoist unter Einstellungen > Integrationen > Entwickler.")

        # --- calendar
        c = section("calendar", not mock)
        calendar = CalendarConfig(
            ics_url=get_str(c, "calendar", "ics_url", ""),
            refresh_seconds=get_int(c, "calendar", "refresh_seconds", 300, 10),
            timeout_seconds=get_int(c, "calendar", "timeout_seconds", 15, 1),
            days_ahead=get_int(c, "calendar", "days_ahead", 5, 0, 60),
            timezone=get_str(c, "calendar", "timezone", "Europe/Berlin"),
            day_start_hour=get_int(c, "calendar", "day_start_hour", 7, 0, 23),
            day_end_hour=get_int(c, "calendar", "day_end_hour", 20, 1, 24),
            show_titles_in_status=get_bool(c, "calendar", "show_titles_in_status", True),
        )
        if calendar.ics_url.lower().startswith("webcal://"):
            calendar.ics_url = "https://" + calendar.ics_url[len("webcal://"):]
        if not mock:
            if not calendar.ics_url:
                errors.append("Pflichtfeld 'calendar.ics_url' fehlt oder ist leer.")
            elif not calendar.ics_url.lower().startswith(("http://", "https://")):
                errors.append("'calendar.ics_url' muss mit https:// beginnen.")
        try:
            ZoneInfo(calendar.timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            errors.append(f"'calendar.timezone' ist keine gültige Zeitzone: {calendar.timezone!r} "
                          "(erwartet z. B. Europe/Berlin; auf Windows: Paket 'tzdata' installieren).")
        if calendar.day_start_hour >= calendar.day_end_hour:
            errors.append("'calendar.day_start_hour' muss kleiner als 'calendar.day_end_hour' sein.")

        # --- mqtt
        m = section("mqtt", False)
        mqtt = MqttConfig(
            enabled=get_bool(m, "mqtt", "enabled", bool(m)),
            host=get_str(m, "mqtt", "host", "localhost"),
            port=get_int(m, "mqtt", "port", 1883, 1, 65535),
            username=get_str(m, "mqtt", "username", ""),
            password=get_str(m, "mqtt", "password", ""),
            topic=get_str(m, "mqtt", "topic", "dashboard/availability"),
            online_topic=get_str(m, "mqtt", "online_topic", "dashboard/online"),
            heartbeat_seconds=get_int(m, "mqtt", "heartbeat_seconds", 300, 5),
        )
        if mqtt.enabled and not mock:
            if not mqtt.host:
                errors.append("Pflichtfeld 'mqtt.host' fehlt (oder 'mqtt.enabled: false' setzen).")
            if not mqtt.topic:
                errors.append("Pflichtfeld 'mqtt.topic' fehlt.")
            if not mqtt.online_topic:
                errors.append("Pflichtfeld 'mqtt.online_topic' fehlt.")

        # --- display
        d = section("display", False)
        display = DisplayConfig(
            default_view=get_str(d, "display", "default_view", "tasks"),
            return_after_seconds=get_int(d, "display", "return_after_seconds", 120, 0),
            theme=get_str(d, "display", "theme", "dark"),
        )
        if display.default_view not in ("tasks", "calendar"):
            errors.append(f"'display.default_view' muss 'tasks' oder 'calendar' sein (gefunden: {display.default_view!r}).")
        if display.theme not in ("dark", "light"):
            errors.append(f"'display.theme' muss 'dark' oder 'light' sein (gefunden: {display.theme!r}).")
        for key, defaults, allowed in (
            ("priority_colors", DEFAULT_PRIORITY_COLORS, ("p1", "p2", "p3", "p4")),
            ("status_colors", DEFAULT_STATUS_COLORS, ("free", "busy", "dnd")),
        ):
            val = d.get(key)
            merged = dict(defaults)
            if val is not None:
                if not isinstance(val, dict):
                    errors.append(f"'display.{key}' muss ein Objekt sein, z. B. {{{allowed[0]}: \"#FF0000\"}}.")
                else:
                    for k, v in val.items():
                        if k not in allowed:
                            errors.append(f"'display.{key}.{k}' ist unbekannt (erlaubt: {', '.join(allowed)}).")
                        elif not isinstance(v, str) or not v.strip():
                            errors.append(f"'display.{key}.{k}' muss ein Farbwert wie \"#FF0000\" sein.")
                        else:
                            merged[k] = v.strip()
            setattr(display, key, merged)

        if errors:
            raise ConfigError("Ungültige Konfiguration:\n  - " + "\n  - ".join(errors))

        return cls(server=server, todoist=todoist, calendar=calendar, mqtt=mqtt, display=display,
                   base_dir=base_dir or PROJECT_ROOT)


def config_path() -> Path:
    env = os.environ.get("DASHBOARD_CONFIG")
    return Path(env).expanduser() if env else DEFAULT_CONFIG_PATH


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else config_path()
    if not p.is_file():
        raise ConfigError(
            f"Konfigurationsdatei nicht gefunden: {p}\n"
            "Kopiere 'config.yaml.example' nach 'config.yaml' und passe sie an "
            "(oder setze die Umgebungsvariable DASHBOARD_CONFIG)."
        )
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Konfigurationsdatei {p} ist kein gültiges YAML: {exc}") from exc
    return Config.from_dict(raw, base_dir=p.resolve().parent)
