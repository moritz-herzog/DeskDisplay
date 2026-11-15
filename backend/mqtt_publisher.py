"""MQTT-Veröffentlichung des Status (retained, QoS 1, Last Will). Darf die App nie stören."""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)


def _default_client_factory():
    import paho.mqtt.client as mqtt
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pi-dashboard")


class MqttPublisher:
    def __init__(self, cfg, client_factory: Callable[[], Any] | None = None):
        self.cfg = cfg  # MqttConfig
        self._factory = client_factory or _default_client_factory
        self._client: Any = None
        self._connected = False
        self._lock = threading.Lock()
        self._current: str | None = None  # zuletzt gewünschte Payload
        self._last_published: str | None = None

    @property
    def enabled(self) -> bool:
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    # ---- Lebenszyklus --------------------------------------------------
    def start(self) -> None:
        try:
            client = self._factory()
            if self.cfg.username:
                client.username_pw_set(self.cfg.username, self.cfg.password)
            client.will_set(self.cfg.online_topic, payload="false", qos=1, retain=True)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.reconnect_delay_set(min_delay=1, max_delay=60)
            self._client = client
            client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
            client.loop_start()
        except Exception:  # noqa: BLE001
            log.exception("MQTT konnte nicht gestartet werden - Dashboard läuft ohne MQTT weiter")

    def stop(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            if self._connected:
                info = client.publish(self.cfg.online_topic, payload="false", qos=1, retain=True)
                try:
                    info.wait_for_publish(timeout=2)  # vor dem Trennen wirklich zustellen
                except Exception:  # noqa: BLE001
                    pass
            client.disconnect()
            client.loop_stop()
        except Exception:  # noqa: BLE001
            log.exception("MQTT-Stop fehlgeschlagen")
        self._connected = False

    # ---- Callbacks (paho-Thread) --------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            log.warning("MQTT-Verbindung abgelehnt: %s", reason_code)
            return
        log.info("MQTT verbunden mit %s:%s", self.cfg.host, self.cfg.port)
        with self._lock:
            self._connected = True
            self._safe_publish(self.cfg.online_topic, "true")
            if self._current is not None:
                if self._safe_publish(self.cfg.topic, self._current):
                    self._last_published = self._current

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected = False
        log.warning("MQTT-Verbindung getrennt (automatischer Reconnect aktiv)")

    # ---- Veröffentlichen ---------------------------------------------------
    @staticmethod
    def make_payload(status: dict) -> str:
        return json.dumps({k: status.get(k) for k in ("status", "mode", "until", "label")},
                          ensure_ascii=False, sort_keys=True)

    def _safe_publish(self, topic: str, payload: str) -> bool:
        try:
            self._client.publish(topic, payload=payload, qos=1, retain=True)
            return True
        except Exception:  # noqa: BLE001
            log.exception("MQTT-Publish auf %s fehlgeschlagen", topic)
            return False

    def publish_status(self, status: dict, force: bool = False) -> bool:
        """Sendet nur bei Änderung (oder force). True, wenn tatsächlich gesendet."""
        payload = self.make_payload(status)
        with self._lock:
            self._current = payload
            if not self._connected or self._client is None:
                return False
            if not force and payload == self._last_published:
                return False
            if self._safe_publish(self.cfg.topic, payload):
                self._last_published = payload
                return True
            return False

    def heartbeat(self) -> bool:
        """Wiederholt den aktuellen Status (auch ohne Änderung)."""
        with self._lock:
            if not self._connected or self._client is None or self._current is None:
                return False
            ok = self._safe_publish(self.cfg.topic, self._current)
            if ok:
                self._last_published = self._current
                self._safe_publish(self.cfg.online_topic, "true")
            return ok


class NullPublisher:
    """Platzhalter, wenn MQTT deaktiviert ist."""
    enabled = False
    connected = False

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def publish_status(self, status: dict, force: bool = False) -> bool: return False
    def heartbeat(self) -> bool: return False
