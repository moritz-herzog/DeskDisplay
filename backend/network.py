"""Netzwerk-Adressen des Geräts (ohne zusätzliche Abhängigkeiten)."""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys

log = logging.getLogger("backend")


def _from_ip_json() -> list[dict]:
    """Linux: `ip -j addr` auswerten (Loopback und Link-Local-IPv6 ausgelassen)."""
    out = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=3, check=True).stdout
    result: list[dict] = []
    for ifc in json.loads(out):
        name = ifc.get("ifname", "")
        if name == "lo" or "LOOPBACK" in (ifc.get("flags") or []):
            continue
        for a in ifc.get("addr_info", []):
            fam, ip = a.get("family"), a.get("local")
            if not ip:
                continue
            if fam == "inet":
                result.append({"name": name, "address": ip, "family": "ipv4"})
            elif fam == "inet6" and not ip.lower().startswith("fe80"):
                result.append({"name": name, "address": ip, "family": "ipv6"})
    return result


def _from_socket() -> list[dict]:
    """Fallback: primäre IPv4 über einen UDP-Socket (es werden keine Pakete gesendet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    finally:
        s.close()
    if ip.startswith("127."):
        return []
    return [{"name": "unbekannt", "address": ip, "family": "ipv4"}]


def get_interfaces() -> list[dict]:
    """Liste von {name, address, family}; IPv4 zuerst. Wirft nie."""
    result: list[dict] = []
    if sys.platform.startswith("linux"):
        try:
            result = _from_ip_json()
        except Exception as exc:  # ip fehlt, Timeout, ungültiges JSON …
            log.debug("ip addr fehlgeschlagen: %s", exc)
    if not result:
        try:
            result = _from_socket()
        except Exception as exc:
            log.debug("Socket-Fallback fehlgeschlagen: %s", exc)
    result.sort(key=lambda r: r["family"] != "ipv4")  # stabil: IPv4 vor IPv6
    return result
