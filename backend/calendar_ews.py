"""Exchange-Kalender direkt per EWS (on-premise) über die Bibliothek `exchangelib`."""
from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .calendar_ics import FREE_STATUSES, CalEvent, CalendarBase, _sort_key

log = logging.getLogger(__name__)

# Felder, die für die Anzeige benötigt werden (hält die Antworten klein)
EWS_FIELDS = ("subject", "start", "end", "location", "is_all_day", "legacy_free_busy_status",
              "is_cancelled", "uid")


# ---------------------------------------------------------------------------
# Abbildung exchangelib-Termin -> CalEvent
# ---------------------------------------------------------------------------
def _as_date(value: Any, tz: ZoneInfo) -> date:
    """Ganztägige Termine liefert exchangelib je nach Version als EWSDate oder EWSDateTime (Mitternacht)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() == timedelta(0):
            # UTC-Mitternacht -> Datum in der Kalenderzone
            return datetime.fromtimestamp(value.timestamp(), tz).date()
        return date(value.year, value.month, value.day)
    return date(value.year, value.month, value.day)


def _as_aware(value: Any, tz: ZoneInfo) -> datetime:
    """Beliebige (auch EWS-)datetime -> tz-bewusste datetime in `tz`."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return datetime(value.year, value.month, value.day, value.hour, value.minute,
                            value.second, value.microsecond, tzinfo=tz)
        return datetime.fromtimestamp(value.timestamp(), tz)
    return datetime.combine(date(value.year, value.month, value.day), time.min, tzinfo=tz)


def map_item(item: Any, tz: ZoneInfo) -> CalEvent | None:
    """Wandelt einen exchangelib-CalendarItem in ein CalEvent um (None = überspringen)."""
    if getattr(item, "is_cancelled", False):
        return None
    raw_start = getattr(item, "start", None)
    if raw_start is None:
        return None
    raw_end = getattr(item, "end", None)
    all_day = bool(getattr(item, "is_all_day", False))
    if all_day:
        start: Any = _as_date(raw_start, tz)
        end: Any = _as_date(raw_end, tz) if raw_end is not None else start + timedelta(days=1)
        if end <= start:
            end = start + timedelta(days=1)
    else:
        start = _as_aware(raw_start, tz)
        end = _as_aware(raw_end, tz) if raw_end is not None else start
        if end < start:
            end = start
    status = str(getattr(item, "legacy_free_busy_status", "") or "").strip().upper()
    busy = status not in FREE_STATUSES  # NoData/leer -> beschäftigt
    uid = str(getattr(item, "uid", None) or getattr(item, "id", None) or "")
    # Serientermine teilen sich die UID: die Startzeit macht jedes Vorkommen eindeutig
    uid = f"{uid}@{start.isoformat()}"
    title = str(getattr(item, "subject", "") or "").strip() or "(Ohne Titel)"
    return CalEvent(uid=uid, title=title, start=start, end=end, all_day=all_day, busy=busy,
                    location=str(getattr(item, "location", "") or "").strip())


# ---------------------------------------------------------------------------
# Verbindung
# ---------------------------------------------------------------------------
def _configure_http(ews_cfg, timeout: int) -> None:
    """Setzt Zertifikats-Optionen und Zeitlimit. ACHTUNG: exchangelib kennt diese Einstellungen nur
    prozessweit (BaseProtocol); sie gelten für alle EWS-Verbindungen dieses Prozesses."""
    from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
    from requests.adapters import HTTPAdapter

    BaseProtocol.TIMEOUT = timeout
    if not ews_cfg.verify_ssl:
        log.warning("EWS: Zertifikatsprüfung ist ABGESCHALTET (calendar.ews.verify_ssl: false)")
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
    elif ews_cfg.ca_bundle:
        bundle = ews_cfg.ca_bundle

        class CaBundleAdapter(HTTPAdapter):
            def cert_verify(self, conn, url, verify, cert):  # noqa: ANN001
                super().cert_verify(conn, url, bundle, cert)

        BaseProtocol.HTTP_ADAPTER_CLS = CaBundleAdapter
    else:
        BaseProtocol.HTTP_ADAPTER_CLS = HTTPAdapter


def create_account(cfg):
    """Baut aus der CalendarConfig ein exchangelib-`Account`-Objekt (blockierend, ggf. Netzwerkzugriff)."""
    from exchangelib import BASIC, DELEGATE, NTLM, Account, Configuration, Credentials, EWSTimeZone

    ews = cfg.ews
    _configure_http(ews, cfg.timeout_seconds)
    creds = Credentials(username=ews.username, password=ews.password)
    tz = EWSTimeZone.from_timezone(ZoneInfo(cfg.timezone))
    auth = BASIC if ews.auth_type == "basic" else NTLM
    if ews.autodiscover:
        return Account(primary_smtp_address=ews.email, credentials=creds, autodiscover=True,
                       access_type=DELEGATE, default_timezone=tz)
    server = ews.server.strip()
    if server.lower().startswith(("http://", "https://")):
        conf = Configuration(service_endpoint=server, credentials=creds, auth_type=auth)
    else:
        conf = Configuration(server=server, credentials=creds, auth_type=auth)
    return Account(primary_smtp_address=ews.email, config=conf, autodiscover=False,
                   access_type=DELEGATE, default_timezone=tz)


def _short_error(exc: BaseException) -> str:
    import requests
    from exchangelib.errors import ErrorTimeoutExpired, TransportError, UnauthorizedError

    text = str(exc)
    if isinstance(exc, UnauthorizedError):
        return "Anmeldung fehlgeschlagen"
    if isinstance(exc, (ssl.SSLError, requests.exceptions.SSLError)) or "CERTIFICATE_VERIFY" in text \
            or "SSLError" in text:
        return "Zertifikatsfehler"
    if isinstance(exc, (TimeoutError, requests.exceptions.Timeout, ErrorTimeoutExpired)):
        return "Zeitüberschreitung"
    if isinstance(exc, (OSError, requests.exceptions.ConnectionError, TransportError)):
        return "Server nicht erreichbar"
    return f"{type(exc).__name__}: {exc}"[:160]


class EwsCalendarService(CalendarBase):
    """Holt Termine per EWS (Serien serverseitig aufgelöst); gleiche Schnittstelle wie CalendarService."""

    def __init__(self, cfg, account_factory: Callable[[Any], Any] | None = None):
        super().__init__(cfg)
        self._account_factory = account_factory or create_account
        self._account: Any = None
        self._parsed: list[CalEvent] = []

    # --- blockierender Teil (läuft im Thread)
    def _fetch_sync(self, start: datetime, end: datetime) -> list[CalEvent]:
        if self._account is None:
            self._account = self._account_factory(self.cfg)
        try:
            from exchangelib import EWSDateTime
            ews_start, ews_end = EWSDateTime.from_datetime(start), EWSDateTime.from_datetime(end)
        except Exception:  # noqa: BLE001 - Zeitzone nicht konvertierbar: normale datetime versuchen
            ews_start, ews_end = start, end
        items = self._account.calendar.view(start=ews_start, end=ews_end).only(*EWS_FIELDS)
        events: list[CalEvent] = []
        for item in items:
            ev = map_item(item, self.tz)
            if ev is not None:
                events.append(ev)
        events.sort(key=_sort_key)
        return events

    async def refresh(self) -> None:
        try:
            start = datetime.combine(self._now().date(), time.min, tzinfo=self.tz)
            end = start + timedelta(days=self.cfg.days_ahead + 1)
            self._parsed = await asyncio.to_thread(self._fetch_sync, start, end)
            self.updated_at = self.last_success = self._now()
            self.error, self.stale = None, False
        except Exception as exc:  # noqa: BLE001 - Hintergrundjob darf nie abstürzen
            self._account = None  # nächster Abruf verbindet neu
            self.stale = True
            self.error = _short_error(exc)
            log.warning("EWS-Kalender-Abruf fehlgeschlagen: %s", self.error)
            log.debug("Details zum EWS-Fehler", exc_info=True)
        finally:
            self._attempted = True

    def events(self, now: datetime | None = None) -> list[CalEvent]:
        return self._parsed
