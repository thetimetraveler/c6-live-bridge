#!/usr/bin/env python3
"""Ingress-only, fixed-route management panel proxy for a C6 gateway."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque, OrderedDict
import hashlib
import http.client
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import select
import socket
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger("c6-management")
MAX_RESPONSE = 1_048_576
MAX_FIRMWARE = 786_432
# Show sensors: one programming read per collector slot, each zone at most every two minutes.
PROGRAM_SLOT_S, PROGRAM_REFRESH_S, PROGRAM_STALE_S, PROGRAM_TIMEOUT_S = 30, 120, 600, 20
SNAPSHOT_STALE_S = 120
LEVEL_FULL_ON = 0xFEFF
DAY_MS = 86_400_000
DEFAULT_TIME_ZONE = "America/Los_Angeles"
SHOW_TIME_ZONES = ("UTC", "America/Los_Angeles", "America/Denver", "America/Phoenix",
                   "America/Chicago", "America/New_York", "America/Anchorage", "Pacific/Honolulu")
# Older/newly installed gateways report the compiled POSIX rule until a zone is saved.
POSIX_SHOW_ZONES = {"UTC0": "UTC", "PST8PDT,M3.2.0,M11.1.0": "America/Los_Angeles",
                    "MST7MDT,M3.2.0,M11.1.0": "America/Denver", "MST7": "America/Phoenix",
                    "CST6CDT,M3.2.0,M11.1.0": "America/Chicago",
                    "EST5EDT,M3.2.0,M11.1.0": "America/New_York",
                    "AKST9AKDT,M3.2.0,M11.1.0": "America/Anchorage", "HST10": "Pacific/Honolulu"}

def show_timezone(value):
    if value in SHOW_TIME_ZONES:
        return value
    return POSIX_SHOW_ZONES.get(value) if isinstance(value, str) else None
SAFE_INT = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
ROUTES = {"status": ("GET", "/api/status"), "devices": ("GET", "/api/devices"),
          "events": ("GET", "/api/events"), "program": ("GET", "/api/program"),
          "config": ("GET", "/api/config"), "config_put": ("PUT", "/api/config"),
          "timezone": ("GET", "/api/timezone"), "timezone_put": ("PUT", "/api/timezone"),
          "update": ("POST", "/api/endpoint/update"),
          "maintenance": ("POST", "/api/maintenance")}


INTEGRATION = re.compile(r"[a-z0-9_]{1,64}\Z")
DEFAULT_INTEGRATIONS = ["lutron_caseta", "lutron"]
# Rendered by Home Assistant with the integration list as a variable; never contains request data.
HA_TEMPLATE = """{%- set ns = namespace(lights=[]) -%}
{%- for integration in integrations -%}
{%- for eid in integration_entities(integration) if eid.startswith('light.') -%}
{%- set dev = device_id(eid) -%}
{%- set ns.lights = ns.lights + [{"entity_id": eid, "integration": integration,
  "name": state_attr(eid, 'friendly_name'), "state": states(eid),
  "brightness": state_attr(eid, 'brightness'), "color_temp_kelvin": state_attr(eid, 'color_temp_kelvin'),
  "area": area_name(eid), "device_id": dev, "zone_id": state_attr(eid, 'zone_id'),
  "device": (device_attr(dev, 'name_by_user') or device_attr(dev, 'name')) if dev else none,
  "serial": device_attr(dev, 'serial_number') if dev else none,
  "model": device_attr(dev, 'model') if dev else none}] -%}
{%- endfor -%}
{%- endfor -%}
{{ {"lights": ns.lights, "sun": {"next_rising": state_attr('sun.sun', 'next_rising'),
    "next_setting": state_attr('sun.sun', 'next_setting')}} | tojson }}"""


def bounded_int(value, label, maximum=0xffffffff):
    if not isinstance(value, str) or not SAFE_INT.fullmatch(value) or int(value) > maximum:
        raise ValueError(f"invalid {label}")
    return int(value)


class Gateway:
    def __init__(self, host, token, timeout=6):
        parts = urlsplit(host)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                or parts.path not in ("", "/") or parts.query or parts.fragment):
            raise ValueError("gateway_host must be an HTTP(S) origin without credentials or path")
        _ = parts.port
        if not isinstance(token, str) or not token or len(token) > 512:
            raise ValueError("gateway_token is required")
        self.scheme, self.hostname, self.port = parts.scheme, parts.hostname, parts.port
        self.token, self.timeout = token, timeout
        self.retry_after = 0

    def request(self, path, method="GET", body=None, content_type=None, timeout=None):
        conn_cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        long_operation = path == "/api/endpoint/update" or path.startswith("/api/program?")
        conn = conn_cls(self.hostname, self.port, timeout=timeout or (300 if long_operation else self.timeout))
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            headers["Content-Length"] = str(len(body))
            if path == "/api/endpoint/update":
                headers["X-SHA256"] = hashlib.sha256(body).hexdigest()
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            retry = response.getheader("Retry-After", "0")
            self.retry_after = min(300, int(retry)) if retry.isdecimal() else 0
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("gateway response exceeds size limit")
            return response.status, response.getheader("Content-Type", "application/json"), raw
        finally:
            conn.close()


def clean_text(value, limit=200):
    return value[:limit] if isinstance(value, str) and value else None


class HomeAssistant:
    """Read-only Lutron light, area and sun lookups through the Supervisor's Core API proxy."""
    def __init__(self, token, integrations, host="supervisor", ttl=15, timeout=6):
        self.token, self.host, self.ttl, self.timeout = token or "", host, ttl, timeout
        self.integrations = [i for i in integrations if isinstance(i, str) and INTEGRATION.fullmatch(i)][:8]
        self.lock = threading.Lock()
        self.cached, self.cached_at, self.core_config = None, 0.0, None

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection(self.host, timeout=self.timeout)
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, "/core/api" + path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
        finally:
            conn.close()
        if len(raw) > MAX_RESPONSE:
            raise ValueError("Home Assistant response exceeds size limit")
        if response.status != 200:
            raise ValueError(f"Home Assistant returned HTTP {response.status}")
        return raw

    @staticmethod
    def light(item):
        if not isinstance(item, dict) or not isinstance(item.get("entity_id"), str):
            return None
        brightness, kelvin, serial = item.get("brightness"), item.get("color_temp_kelvin"), item.get("serial")
        zone = item.get("zone_id")
        return {"entity_id": item["entity_id"][:255], "integration": clean_text(item.get("integration"), 64),
                "name": clean_text(item.get("name")), "state": clean_text(item.get("state"), 32),
                "brightness": brightness if type(brightness) is int and 0 <= brightness <= 255 else None,
                "color_temp_kelvin": kelvin if type(kelvin) in (int, float) and 1000 <= kelvin <= 20000 else None,
                "area": clean_text(item.get("area")), "device_id": clean_text(item.get("device_id"), 64),
                "device": clean_text(item.get("device")), "model": clean_text(item.get("model")),
                "serial": clean_text(str(serial), 64) if type(serial) in (str, int) and serial != "" else None,
                "zone_id": str(zone) if type(zone) in (str, int) and SAFE_INT.match(str(zone)) else None}

    def snapshot(self):
        if not self.token:
            return {"available": False, "error": "Home Assistant API access is not enabled for this add-on"}
        with self.lock:
            if self.cached is not None and time.monotonic() - self.cached_at < self.ttl:
                return self.cached
            try:
                if self.core_config is None:
                    config = json.loads(self.request("GET", "/config"))
                    self.core_config = config if isinstance(config, dict) else {}
                data = json.loads(self.request("POST", "/template", {"template": HA_TEMPLATE,
                                                                      "variables": {"integrations": self.integrations}}))
                if not isinstance(data, dict) or not isinstance(data.get("lights"), list):
                    raise ValueError("unexpected template result")
                sun = data.get("sun") if isinstance(data.get("sun"), dict) else {}
                result = {"available": True, "integrations": self.integrations,
                          "time_zone": clean_text(self.core_config.get("time_zone"), 64),
                          "location_name": clean_text(self.core_config.get("location_name")),
                          "sun": {k: clean_text(sun.get(k), 64) for k in ("next_rising", "next_setting")},
                          "lights": [l for l in map(self.light, data["lights"][:500]) if l]}
            except (OSError, http.client.HTTPException, ValueError, TypeError) as exc:
                LOG.warning("Home Assistant lookup failed: %s", exc)
                result = {"available": False, "error": f"Home Assistant lookup failed: {exc}"}
            self.cached, self.cached_at = result, time.monotonic()
            return result

    def time_zone(self):
        """Home Assistant's time zone, read once; None while it cannot be read."""
        with self.lock:
            if self.core_config is None and self.token:
                try:
                    config = json.loads(self.request("GET", "/config"))
                    self.core_config = config if isinstance(config, dict) else {}
                except (OSError, http.client.HTTPException, ValueError, TypeError):
                    return None
            return clean_text((self.core_config or {}).get("time_zone"), 64)


class EventArchive:
    """Best-effort append-only daily event snapshots with seven-day retention."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        self.seen = set()
        self.prune()

    def prune(self):
        cutoff = time.time() - 7 * 86400
        for path in self.root.glob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                LOG.warning("Could not prune event archive %s", path.name)

    def record(self, payload):
        events = payload.get("events", []) if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            return
        with self.lock:
            if isinstance(payload, dict) and type(payload.get("lost")) is int and payload["lost"] > 0:
                gap_key = f"gap:{payload.get('boot_id')}:{payload.get('oldest')}:{payload['lost']}"
                if gap_key not in self.seen:
                    self.seen.add(gap_key)
                    gap = {"type": "cursor_gap", "lost": payload["lost"],
                           "boot_id": payload.get("boot_id"),
                           "ha_received_at": datetime.now(timezone.utc).isoformat()}
                    marker = self.root / (time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl")
                    try:
                        fd = os.open(marker, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, "a", encoding="utf-8") as out:
                            if os.fstat(out.fileno()).st_size < 8 * 1024 * 1024:
                                out.write(json.dumps(gap, separators=(",", ":")) + "\n")
                    except OSError:
                        LOG.warning("Could not append event gap marker %s", marker.name)
            for event in events[-2000:]:
                if isinstance(event, dict) and type(event.get("id")) is int:
                    boot = payload.get("boot_id", "") if isinstance(payload, dict) else ""
                    key = f"{boot}:{event['id']}"
                    if key in self.seen: continue
                    self.seen.add(key)
                    if len(self.seen) > 10000: self.seen = set(sorted(self.seen)[-5000:])
                archived = dict(event) if isinstance(event, dict) else {"event": event}
                if isinstance(payload, dict) and "boot_id" in payload:
                    archived["boot_id"] = payload["boot_id"]
                archived["ha_received_at"] = datetime.now(timezone.utc).isoformat()
                line = json.dumps(archived, separators=(",", ":"), ensure_ascii=False)
                if len(line) > 16384:
                    continue
                day = time.strftime("%Y-%m-%d", time.gmtime())
                target = self.root / (day + ".jsonl")
                try:
                    fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "a", encoding="utf-8") as out:
                        if os.fstat(out.fileno()).st_size < 8 * 1024 * 1024:
                            out.write(line + "\n")
                except OSError:
                    LOG.warning("Could not append event archive %s", target.name)
            self.prune()


def _interpolate(a, b, elapsed, duration):
    # The endpoint's integer interpolation, including C's rounding and truncating division.
    change = (b - a) * elapsed
    change += -(duration // 2) if change < 0 else duration // 2
    return a + (abs(change) // duration) * (1 if change >= 0 else -1)


def level16_to_x100(level):
    if level == 0:
        return 0
    if level >= LEVEL_FULL_ON:
        return 10000
    return 100 + (level * 9900 + LEVEL_FULL_ON // 2) // LEVEL_FULL_ON


def show_state(knots, local_ms):
    """Evaluate a show the way the endpoint does: knots in stored order, wrapping at midnight."""
    if not knots or not 0 <= local_ms < DAY_MS:
        return None
    left, right = knots[-1], knots[0]
    start, end = left["minute"] * 60000 - DAY_MS, right["minute"] * 60000
    for i, knot in enumerate(knots):
        if knot["minute"] * 60000 <= local_ms:
            left, right = knot, knots[(i + 1) % len(knots)]
            start = left["minute"] * 60000
            end = right["minute"] * 60000 + (DAY_MS if i + 1 == len(knots) else 0)
    elapsed, duration = local_ms - start, end - start
    if duration <= 0:
        return None
    state = {"level_x100": level16_to_x100(_interpolate(left["level"], right["level"], elapsed, duration)),
             "cct": _interpolate(left["cct"], right["cct"], elapsed, duration), "vibrancy": None}
    # Only a transferred vibrancy track is evaluated; a missing endpoint does not invent one.
    if left["field2"] is not None and right["field2"] is not None and left["field2"] <= 100 and right["field2"] <= 100:
        state["vibrancy"] = _interpolate(left["field2"], right["field2"], elapsed, duration)
    return state


def _u16(value):
    return type(value) is int and 0 <= value <= 0xffff


def show_program(data):
    """The selection, override and shows of a zone programming snapshot; None if it is not one."""
    if not isinstance(data, dict) or type(data.get("selected")) is not bool:
        return None
    shows = {}
    for show in data.get("shows") if isinstance(data.get("shows"), list) else []:
        knots = show.get("knots") if isinstance(show, dict) else None
        if type(show.get("id") if isinstance(show, dict) else None) is not int or not isinstance(knots, list) or not 0 < len(knots) <= 64:
            continue
        if all(isinstance(k, dict) and type(k.get("minute")) is int and 0 <= k["minute"] < 1440 and _u16(k.get("level"))
               and _u16(k.get("cct")) and (k.get("field2") is None or _u16(k.get("field2"))) for k in knots):
            shows[show["id"]] = [{key: k.get(key) for key in ("minute", "level", "cct", "field2")} for k in knots]
    show, override = data.get("show"), data.get("brightness_override_x100")
    return {"selected": data["selected"], "show": show if type(show) is int else 0, "shows": shows,
            "programmed": data.get("programmed") is True,
            "override_x100": override if type(override) is int and 0 <= override <= 10000 else None}


def _percent(value):
    return round(value / 100, 2) if type(value) is int and 0 <= value <= 10000 else None


def zone_show_state(zone, program, program_age_s, local_ms):
    """Sensor values for one zone: what its show calls for, what the bridge sent, what N4 confirmed."""
    requested = zone.get("requested") if isinstance(zone.get("requested"), dict) else {}
    confirmed = zone.get("confirmed") if isinstance(zone.get("confirmed"), dict) else {}
    flags = requested.get("flags") if type(requested.get("flags")) is int else 0
    cct = requested.get("cct")
    xy = (requested.get("x"), requested.get("y"))
    state = {"active_show": None, "target_cct": None, "target_brightness": None,
             "requested_cct": cct if flags & 0x02 and type(cct) is int and 1000 <= cct <= 20000 else None,
             "requested_brightness": _percent(requested.get("level_x100")) if flags & 0x01 else None,
             "confirmed_brightness": _percent(confirmed.get("level_x100"))}
    attributes = {"zone": zone.get("zone"), "zone_id": zone.get("zone_id"), "serial": zone.get("serial"),
                  "selected": None, "programmed": None, "installed_shows": [], "brightness_override": None,
                  "target_vibrancy": None,
                  "requested_vibrancy": requested.get("vibrancy") if flags & 0x08 else None,
                  "requested_xy": [v / 10000 for v in xy] if flags & 0x04 and all(type(v) is int and 0 <= v <= 10000 for v in xy) else None,
                  "requested_seq": requested.get("seq"), "confirmed_source": clean_text(confirmed.get("source"), 32)}
    if program is not None and program_age_s is not None and program_age_s <= PROGRAM_STALE_S:
        attributes.update(selected=program["selected"], programmed=program["programmed"],
                          installed_shows=sorted(program["shows"]),
                          brightness_override=_percent(program["override_x100"]))
        if program["selected"] and program["show"]:
            state["active_show"] = str(program["show"])
            sample = show_state(program["shows"].get(program["show"]), local_ms) if local_ms is not None else None
            if sample:
                # A brightness override is absolute; the show's colour keeps running beneath it.
                level = program["override_x100"] if program["override_x100"] is not None else sample["level_x100"]
                state.update(target_cct=sample["cct"], target_brightness=_percent(level))
                attributes["target_vibrancy"] = sample["vibrancy"]
        else:
            state["active_show"] = "not_selected"
    state["attributes"] = attributes
    return state


# Zone sync check tolerances. Lutron/HA brightness is 0-255 or whole percent, so allow 1%.
SYNC_LEVEL_TOL, SHOW_LEVEL_TOL, SHOW_CCT_TOL, SYNC_CCT_TOL = 100, 50, 15, 60
CONFIRM_STALE_MS = 120_000
CHECK_ORDER = {"bad": 0, "warn": 1, "wait": 2, "unknown": 3, "info": 4, "good": 5}


def _level(value):
    return value if type(value) is int and 0 <= value <= 10000 else None


def _cct(value):
    return value if type(value) is int and 1000 <= value <= 20000 else None


def _pct_text(level):
    return "off" if level == 0 else f"{level / 100:g}%"


def _age(uptime_ms, at_ms):
    if type(uptime_ms) not in (int, float) or type(at_ms) not in (int, float) or at_ms <= 0 or uptime_ms < at_ms:
        return None
    return int(uptime_ms - at_ms)


def _sent(zone, uptime_ms):
    """The last command the bridge sent to the N4, in display units; None before the first one."""
    requested = zone.get("requested")
    if type(requested) is int:  # gateways before the typed command record
        requested = {"flags": 1, "level_x100": requested} if _level(requested) is not None else None
    if not isinstance(requested, dict):
        return None
    flags = requested.get("flags") if type(requested.get("flags")) is int else 0
    xy = (requested.get("x"), requested.get("y"))
    vibrancy = requested.get("vibrancy")
    return {"level_x100": _level(requested.get("level_x100")) if flags & 0x01 else None,
            "cct": _cct(requested.get("cct")) if flags & 0x02 else None,
            "xy": [v / 10000 for v in xy] if flags & 0x04 and all(type(v) is int and 0 <= v <= 10000 for v in xy) else None,
            "vibrancy": vibrancy if flags & 0x08 and type(vibrancy) is int and 0 <= vibrancy <= 100 else None,
            "fade_ms": requested.get("transition_ms") if type(requested.get("transition_ms")) is int else 0,
            "delay_ms": requested.get("delay_ms") if type(requested.get("delay_ms")) is int else 0,
            "seq": requested.get("seq") if type(requested.get("seq")) is int else None,
            "age_ms": _age(uptime_ms, zone.get("last_command_ms"))}


def _lamp(zone, uptime_ms):
    """What the N4 last reported for the lamp; colour only from firmware that reports it."""
    confirmed = zone.get("confirmed")
    if not isinstance(confirmed, dict) or _level(confirmed.get("level_x100")) is None:
        return None
    lamp = {"level_x100": confirmed["level_x100"], "seq": confirmed.get("seq") if type(confirmed.get("seq")) is int else None,
            "source": confirmed.get("source") if confirmed.get("source") in ("applied", "observed", "correction") else None,
            "age_ms": _age(uptime_ms, confirmed.get("at_ms")), "cct": None, "xy": None, "vibrancy": None}
    color = confirmed.get("color")
    if isinstance(color, dict):
        xy = (color.get("x"), color.get("y"))
        lamp.update(cct=_cct(color.get("cct")),
                    xy=[v / 10000 for v in xy] if all(type(v) is int and 0 < v <= 10000 for v in xy) else None,
                    vibrancy=color.get("vibrancy") if type(color.get("vibrancy")) is int and 0 <= color["vibrancy"] <= 100 else None)
    return lamp


def match_light(zone, lights):
    """The Home Assistant Lutron light for a bridged zone: by zone ID, then serial, then name."""
    lights = [l for l in lights or [] if isinstance(l, dict)]
    zone_id, serial = zone.get("zone_id"), zone.get("serial")
    for key, value in (("zone_id", zone_id), ("serial", serial)):
        if type(value) is int and value > 0:
            hit = next((l for l in lights if l.get(key) == str(value)), None)
            if hit:
                return hit
    name = re.sub(r"[^a-z0-9]+", "", str(zone.get("name") or "").lower())
    return next((l for l in lights if name and re.sub(r"[^a-z0-9]+", "", str(l.get("name") or "").lower()) == name), None)


def _lutron(light):
    if not light:
        return None
    state = light.get("state")
    on = True if state == "on" else False if state == "off" else None
    brightness = light.get("brightness")
    level = 0 if on is False else round(brightness * 10000 / 255) if on and type(brightness) is int else None
    return {"entity_id": light.get("entity_id"), "name": light.get("name"), "area": light.get("area"),
            "on": on, "state": state, "level_x100": level, "cct": _cct(light.get("color_temp_kelvin"))}


def _show(program, program_age_s, local_ms):
    if program is None or program_age_s is None or program_age_s > PROGRAM_STALE_S:
        return {"status": "unknown"}
    base = {"installed": sorted(program["shows"]), "override_x100": program["override_x100"]}
    if not (program["selected"] and program["show"]):
        return dict(base, status="not_selected")
    knots = program["shows"].get(program["show"])
    show = dict(base, status="running", id=program["show"])
    if not knots or local_ms is None:
        return dict(show, status="no_clock" if knots else "missing")
    sample = show_state(knots, local_ms)
    if not sample:
        return dict(show, status="missing")
    following = sorted(knots, key=lambda k: k["minute"])
    upcoming = next((k for k in following if k["minute"] * 60000 > local_ms), following[0])
    # Every 15 minutes plus the end of the day, for the dashboard's curve.
    curve = []
    for minute, ms in [(m, m * 60000) for m in range(0, 1440, 15)] + [(1440, DAY_MS - 1)]:
        point = show_state(knots, ms)
        if point:
            curve.append({"minute": minute, "level_x100": point["level_x100"], "cct": point["cct"]})
    show.update(now_ms=local_ms, curve=curve,
                target={"level_x100": program["override_x100"] if program["override_x100"] is not None else sample["level_x100"],
                        "show_level_x100": sample["level_x100"], "cct": sample["cct"], "vibrancy": sample["vibrancy"]},
                next={"minute": upcoming["minute"], "level_x100": level16_to_x100(upcoming["level"]), "cct": upcoming["cct"]})
    return show


def _check(key, status, title, detail=""):
    return {"key": key, "status": status, "title": title, "detail": detail}


def zone_summary(zone, program, program_age_s, local_ms, light, uptime_ms):
    """One bridged zone: what Lutron shows, what the bridge sent, what the lamp reports, and whether they agree."""
    sent, lamp, lutron = _sent(zone, uptime_ms), _lamp(zone, uptime_ms), _lutron(light)
    show = _show(program, program_age_s, local_ms)
    running = show["status"] == "running" and "target" in show
    checks = []

    # 1. Lamp against Lutron: the question "is the light where Lutron thinks it is?"
    if lutron is None:
        checks.append(_check("lutron", "unknown", "No matching Lutron light in Home Assistant"))
    elif lamp is None:
        checks.append(_check("lutron", "wait", "Waiting for the lamp's first reading"))
    elif lutron["on"] is None:
        checks.append(_check("lutron", "unknown", f"Lutron state is {lutron['state'] or 'unknown'}"))
    elif not lutron["on"] and lamp["level_x100"] > 0:
        checks.append(_check("lutron", "bad", f"Lamp is on at {_pct_text(lamp['level_x100'])}, Lutron shows off",
                             "Changed outside Lutron, for example after a power cut or from the Ketra app."))
    elif lutron["on"] and lamp["level_x100"] == 0:
        checks.append(_check("lutron", "bad", "Lutron shows on, lamp is off"))
    elif not lutron["on"]:
        checks.append(_check("lutron", "good", "Lamp and Lutron are both off"))
    elif running:
        checks.append(_check("lutron", "info", "Lutron shows the show's next keyframe",
                             f"Lutron reports {_pct_text(lutron['level_x100'])} while the show fades the lamp there; see the show check."))
    elif lutron["level_x100"] is not None and abs(lutron["level_x100"] - lamp["level_x100"]) > SYNC_LEVEL_TOL:
        checks.append(_check("lutron", "warn", f"Lamp at {_pct_text(lamp['level_x100'])}, Lutron shows {_pct_text(lutron['level_x100'])}"))
    elif lutron["cct"] and lamp["cct"] and abs(lutron["cct"] - lamp["cct"]) > SYNC_CCT_TOL:
        checks.append(_check("lutron", "warn", f"Lamp colour {lamp['cct']} K, Lutron shows {lutron['cct']} K"))
    else:
        checks.append(_check("lutron", "good", "Lamp matches Lutron", f"Both on at {_pct_text(lamp['level_x100'])}."))

    # 2. Natural Show.
    if show["status"] == "unknown":
        checks.append(_check("show", "unknown", "Waiting for the zone's programming"))
    elif show["status"] == "not_selected":
        checks.append(_check("show", "info", "No Natural Show selected", "A scene, colour or off command cancels the show; selecting it again resumes it."))
    elif not running:
        checks.append(_check("show", "warn", "Show selected but it can't be evaluated", show["status"].replace("_", " ")))
    else:
        target = show["target"]
        level = lamp["level_x100"] if lamp else sent["level_x100"] if sent else None
        cct = lamp["cct"] if lamp and lamp["cct"] else sent["cct"] if sent else None
        if level is None:
            checks.append(_check("show", "wait", "Waiting for the lamp to report"))
        elif abs(level - target["level_x100"]) > SHOW_LEVEL_TOL:
            checks.append(_check("show", "warn", f"Show calls for {_pct_text(target['level_x100'])}, lamp at {_pct_text(level)}"))
        elif level and cct and target["cct"] and abs(cct - target["cct"]) > SHOW_CCT_TOL:
            checks.append(_check("show", "warn", f"Show calls for {target['cct']} K, lamp at {cct} K"))
        else:
            what = "manual brightness override" if show["override_x100"] is not None else "the show's brightness"
            checks.append(_check("show", "good", f"Following Natural Show {show['id']}", f"Lamp matches {what} and colour for this minute."))

    # 3. Lamp against the bridge's last command.
    if sent is None or sent["level_x100"] is None:
        checks.append(_check("hub", "info", "No brightness command since the gateway started"))
    elif lamp is None:
        checks.append(_check("hub", "wait", "Waiting for the hub to confirm"))
    elif lamp["seq"] == sent["seq"] and lamp["source"] == "applied":
        checks.append(_check("hub", "good", f"Hub confirmed command #{sent['seq']}"))
    elif abs(lamp["level_x100"] - sent["level_x100"]) <= SHOW_LEVEL_TOL:
        checks.append(_check("hub", "good", "Hub reading matches the last command"))
    elif sent["age_ms"] is not None and sent["age_ms"] < sent["fade_ms"] + sent["delay_ms"] + 5000:
        checks.append(_check("hub", "wait", f"Fading to {_pct_text(sent['level_x100'])}"))
    else:
        checks.append(_check("hub", "warn", f"Hub reads {_pct_text(lamp['level_x100'])}; last command was {_pct_text(sent['level_x100'])}",
                             "The lamp changed after the bridge's last command."))

    # 4. Freshness of the lamp reading.
    if lamp and (lamp["age_ms"] is None or lamp["age_ms"] > CONFIRM_STALE_MS):
        checks.append(_check("fresh", "warn", "No recent reading from the hub",
                             "" if lamp["age_ms"] is None else f"Last reading {lamp['age_ms'] // 60000} min ago."))

    worst = min(checks, key=lambda c: CHECK_ORDER[c["status"]])
    if worst["status"] in ("bad", "warn"):
        verdict = {"tone": worst["status"], "title": worst["title"]}
    elif lamp is None:
        verdict = {"tone": "wait", "title": "Waiting for the lamp's first reading"}
    elif lutron is None or lutron["on"] is None:
        verdict = {"tone": "info", "title": "Lamp reporting; Lutron state unknown"}
    else:
        verdict = {"tone": "good", "title": "In sync · following Natural Show" if running else "In sync"}
    return {"zone": zone.get("zone"), "zone_id": zone.get("zone_id"), "name": zone.get("name"),
            "n4_group": zone.get("n4_group"), "serial": zone.get("serial"),
            "lutron": lutron, "sent": sent, "lamp": lamp, "show": show, "checks": checks, "verdict": verdict}


def show_clock(gateway_zone, ha_time_zone, now):
    """Milliseconds since local midnight in the gateway's show time zone, and that zone's name."""
    # A known gateway rule is authoritative. Unknown rules must not silently
    # produce a target in HA's potentially different timezone.
    if isinstance(gateway_zone, dict) and not gateway_zone.get("unsupported"):
        name = show_timezone(gateway_zone.get("timezone"))
        candidates = (name,) if name else ()
    else:
        candidates = (ha_time_zone(), DEFAULT_TIME_ZONE)
    for name in candidates:
        try:
            tz = ZoneInfo(name) if name else None
        except (ZoneInfoNotFoundError, ValueError):
            continue
        if tz:
            t = now(tz)
            return ((t.hour * 60 + t.minute) * 60 + t.second) * 1000 + t.microsecond // 1000, name
    return None, None


class Panel:
    """One upstream owner; browser reads never create gateway traffic."""
    def __init__(self, gateway, archive):
        self.gateway, self.archive = gateway, archive
        self.lock = threading.Lock()
        self.upstream = threading.Lock()
        self.cache = {}
        self.events = deque(maxlen=2048)
        self.programs = OrderedDict()
        self.cursor = self.archive_cursor = 0
        self.boot_id = self.archive_boot_id = None
        self.boot_epoch_ms = None
        self.last_error = self.last_contact = None
        self.online = False
        self.stop = threading.Event()
        self.snapshot_supported = True
        self.snapshot_retry_at = 0
        self.failures = 0
        self.next_poll = 0
        self.config_due = 0
        self.timezone_due = 0
        self.legacy_index = 0
        # Programming reads for show sensors; off unless sensors are publishing.
        self.track_programs = False
        # The dashboard's zone view also needs programming; reads continue briefly after it stops asking.
        self.programs_wanted_until = 0.0
        self.program_due = 0
        self.zone_programs = {}
        self.program_attempts = {}

    @staticmethod
    def result(value, code=200):
        return code, "application/json", json.dumps(value, separators=(",", ":")).encode()

    @staticmethod
    def _millis(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return None
        return value

    def _anchor(self, status):
        # The gateway has no wall clock; its uptime plus the host clock places events in time.
        uptime = self._millis(status.get("gateway_uptime_ms")) if isinstance(status, dict) else None
        if uptime is not None:
            self.boot_epoch_ms = round(time.time() * 1000 - uptime)

    def _events(self, data, status=None):
        boot = data.get("boot_id")
        if boot != self.boot_id:
            self.boot_id = self.archive_boot_id = boot
            self.cursor = self.archive_cursor = 0
            self.boot_epoch_ms = None
            self.events.clear()
            self.programs.clear()
            self.zone_programs.clear()
            self.program_attempts.clear()
            self.config_due = 0
            self.timezone_due = time.monotonic() + 5
            self.cache.pop("timezone", None)
        if status is not None:
            self._anchor(status)
        for event in data["events"]:
            if isinstance(event, dict) and type(event.get("id")) is int and event["id"] > self.cursor:
                ts = self._millis(event.get("ts_ms"))
                if ts is not None and self.boot_epoch_ms is not None and "wall_ms" not in event:
                    event["wall_ms"] = round(self.boot_epoch_ms + ts)
                self.events.append(event)
                self.cursor = self.archive_cursor = event["id"]

    def _program_zone(self, now):
        """The bridged zone whose programming was read least recently, if one is due."""
        with self.lock:
            entry = self.cache.get("devices")
            zones = entry[0].get("zones") if entry and isinstance(entry[0], dict) else None
            due = [(self.program_attempts.get(z["zone"], -math.inf), z["zone"]) for z in zones or []
                   if isinstance(z, dict) and type(z.get("zone")) is int and type(z.get("serial")) is int and z["serial"] > 0]
            due = [d for d in due if now - d[0] >= PROGRAM_REFRESH_S]
            return min(due)[1] if due else None

    def show_inputs(self):
        """A consistent copy of what the show sensors need."""
        now = time.monotonic()
        with self.lock:
            entry = self.cache.get("devices")
            zones = entry[0].get("zones") if entry and isinstance(entry[0], dict) else None
            return {"age_s": now - entry[1] if entry else None,
                    "timezone": self.cache.get("timezone", (None, 0))[0],
                    "zones": [dict(z) for z in zones if isinstance(z, dict)] if isinstance(zones, list) else [],
                    "programs": {zone: (program, now - at) for zone, (program, at) in self.zone_programs.items()}}

    def zone_summaries(self, ha_snapshot, clock):
        """Per-zone sync summaries for the dashboard, from cached data only (never gateway traffic)."""
        self.programs_wanted_until = time.monotonic() + PROGRAM_REFRESH_S
        inputs = self.show_inputs()
        with self.lock:
            entry = self.cache.get("status")
            uptime = entry[0].get("gateway_uptime_ms") if entry and isinstance(entry[0], dict) else None
            status_age = time.monotonic() - entry[1] if entry else None
        local_ms, tz_name = clock(inputs.get("timezone"))
        # Device records and status are cached separately; bring both to the newer snapshot's time.
        if type(uptime) in (int, float) and status_age is not None and inputs["age_s"] is not None:
            uptime += max(0.0, status_age - inputs["age_s"]) * 1000
        lights = ha_snapshot.get("lights") if isinstance(ha_snapshot, dict) and ha_snapshot.get("available") else None
        zones = []
        for zone in inputs["zones"]:
            program, age = inputs["programs"].get(zone.get("zone"), (None, None))
            zones.append(zone_summary(zone, program, age, local_ms, match_light(zone, lights) if lights else None, uptime))
        return {"zones": zones, "time_zone": tz_name, "local_ms": local_ms,
                "home_assistant": lights is not None, "age_s": inputs["age_s"]}

    def archive_poll_once(self):
        """Single collector step, also used during mixed-version rollout."""
        if time.monotonic() < self.next_poll or not self.upstream.acquire(blocking=False):
            return None
        try:
            now = time.monotonic()
            self.next_poll = now + 1
            wanted = self.track_programs or now < self.programs_wanted_until
            program_zone = self._program_zone(now) if wanted and now >= self.program_due else None
            if self.cache and now >= self.config_due:
                route, path = "config", "/api/config"
            elif self.cache and now >= self.timezone_due:
                route, path = "timezone", "/api/timezone"
            elif program_zone is not None:
                route, path = "program", "/api/program?" + urlencode({"zone": program_zone, "page": 0})
                with self.lock:
                    self.program_attempts[program_zone] = now
            elif self.snapshot_supported or now >= self.snapshot_retry_at:
                route = "snapshot"
                path = "/api/snapshot?" + urlencode({"after": self.cursor, "boot_id": self.boot_id or 0})
            else:
                route = ("status", "devices", "events")[self.legacy_index % 3]
                self.legacy_index += 1
                path = "/api/" + route
                if route == "events":
                    path += "?" + urlencode({"after": self.cursor, "boot_id": self.boot_id or 0})
            if route == "program":
                # A bounded wait: programming inspection must not hold the collector for minutes.
                code, ctype, raw = self.gateway.request(path, timeout=PROGRAM_TIMEOUT_S)
            else:
                code, ctype, raw = self.gateway.request(path)
            if route == "snapshot" and code in (404, 405):
                self.snapshot_supported = False
                self.snapshot_retry_at = now + 60
                return None
            if route == "config" and code != 200:
                self.config_due = now + max(30, getattr(self.gateway, "retry_after", 0))
                self.next_poll = now + max(1, getattr(self.gateway, "retry_after", 0))
                return None
            if route == "timezone" and code != 200:
                with self.lock:
                    if code in (404, 405):
                        self.cache["timezone"] = ({"unsupported": True}, now)
                    self.timezone_due = now + (300 if code in (404, 405) else max(30, getattr(self.gateway, "retry_after", 0)))
                    self.next_poll = now + max(1, getattr(self.gateway, "retry_after", 0))
                return None
            if route == "program" and code != 200:
                self.program_due = now + max(PROGRAM_SLOT_S, getattr(self.gateway, "retry_after", 0))
                self.next_poll = now + max(1, getattr(self.gateway, "retry_after", 0))
                return None
            if code != 200:
                raise ValueError(f"gateway {route} returned HTTP {code}")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("invalid gateway response")
            if route == "timezone" and not isinstance(data.get("timezone"), str):
                raise ValueError("invalid gateway time zone")
            program = show_program(data) if route == "program" else None
            if route == "program" and program is None:
                raise ValueError("invalid gateway programming snapshot")
            events = data.get("events") if route == "snapshot" else data if route == "events" else None
            if events is not None and (not isinstance(events, dict) or not isinstance(events.get("events"), list)):
                raise ValueError("invalid gateway events")
            if route == "snapshot" and (events is None or not isinstance(data.get("status"), dict) or not isinstance(data.get("devices"), dict)):
                raise ValueError("invalid gateway snapshot")
            with self.lock:
                if route == "status":
                    self._anchor(data)
                if events is not None:
                    self._events(events, data["status"] if route == "snapshot" else None)
                if route == "snapshot":
                    self.snapshot_supported = True
                    for key in ("status", "devices"):
                        self.cache[key] = (data[key], now)
                elif route == "program":
                    self.zone_programs[program_zone] = (program, now)
                    # The dashboard reads the same snapshot without another gateway request.
                    self.programs[path] = (code, ctype, raw)
                    while len(self.programs) > 16:
                        self.programs.popitem(last=False)
                    self.program_due = now + PROGRAM_SLOT_S
                elif route != "events":
                    self.cache[route] = (data, now)
                if route == "timezone":
                    self.timezone_due = now + 30
                if route == "config":
                    previous = self.program_generation if hasattr(self, "program_generation") else None
                    generation = (data.get("generation"), data.get("hash"))
                    if generation != previous:
                        self.programs.clear()
                        self.zone_programs.clear()
                        self.program_attempts.clear()
                    self.program_generation = generation
                    self.config_due = now + 30
                self.last_contact = time.time()
                self.last_error = None
                self.online = True
                self.failures = 0
                self.next_poll = now + 1
            if events is not None:
                self.archive.record(events)
            return events or data
        except (OSError, http.client.HTTPException, ValueError, TypeError) as exc:
            if route == "timezone":
                self.timezone_due = time.monotonic() + 30
                self.next_poll = time.monotonic() + 1
                LOG.warning("Optional time-zone snapshot deferred: %s", exc)
                return None
            if route == "config":
                self.config_due = time.monotonic() + 30
                self.next_poll = time.monotonic() + 1
                LOG.warning("Optional configuration snapshot deferred: %s", exc)
                return None
            if route == "program":
                self.program_due = time.monotonic() + PROGRAM_SLOT_S
                self.next_poll = time.monotonic() + 1
                LOG.warning("Show programming read deferred: %s", exc)
                return None
            with self.lock:
                self.failures += 1
                delay = max(min(30, 2 ** min(self.failures, 5)), getattr(self.gateway, "retry_after", 0))
                self.next_poll = time.monotonic() + delay
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.online = False
            LOG.warning("Monitoring delayed; retrying in %s seconds: %s", delay, exc)
            return None
        finally:
            self.upstream.release()

    def run_event_archive(self):
        while not self.stop.is_set():
            self.archive_poll_once()
            self.stop.wait(max(0.1, self.next_poll - time.monotonic()))

    def proxy(self, route, query="", body=None, content_type=None):
        method, base = ROUTES[route]
        params = dict(query)
        if route == "events":
            after = bounded_int(params.get("after", "0"), "event cursor")
            boot = params.get("boot_id")
            with self.lock:
                # A cursor from an old boot must be rewound at the cache, never upstream.
                reset = boot is not None and str(self.boot_id) != boot
                if reset or after > self.cursor:
                    after = 0
                oldest = self.events[0]["id"] if self.events else self.cursor + 1
                selected = [e for e in self.events if e["id"] > after]
                previous, lost = after, 0
                for event in selected:
                    lost += max(0, event["id"] - previous - 1)
                    previous = event["id"]
                return self.result({"boot_id": self.boot_id, "reset": reset, "after": after, "next": self.cursor + 1,
                                    "oldest": oldest, "lost": lost, "boot_epoch_ms": self.boot_epoch_ms,
                                    "events": selected})
        if route in ("status", "devices", "config", "timezone"):
            with self.lock:
                entry = self.cache.get(route)
                if entry is None:
                    return self.result({"error": "Monitoring is waiting for its first snapshot"}, 503)
                data, at = entry
                data = dict(data)
                data["monitoring"] = {"age_s": max(0, time.monotonic() - at),
                                      "delayed": bool(self.last_error), "error": self.last_error}
                return self.result(data)
        if route == "program":
            zone = bounded_int(params.get("zone", "0"), "zone", 65535)
            page = bounded_int(params.get("page", "0"), "page", 100000)
            refresh = params.get("refresh", "0")
            if refresh not in ("0", "1"):
                raise ValueError("invalid refresh flag")
            base += "?" + urlencode({"zone": zone, "page": page})
            with self.lock:
                if refresh != "1" and base in self.programs:
                    return self.programs[base]
        # Never accumulate a queue of browser work behind a slow operation.
        if route != "maintenance" and time.monotonic() < self.next_poll and self.last_error:
            return self.result({"error": "Monitoring backoff active; try again later"}, 503)
        if not self.upstream.acquire(blocking=False):
            return self.result({"error": "Gateway collector busy; try again later"}, 503)
        try:
            result = self.gateway.request(base, method, body, content_type)
            with self.lock:
                if route == "program" and result[0] == 200:
                    self.programs[base] = result
                    while len(self.programs) > 16:
                        self.programs.popitem(last=False)
                if route == "timezone_put" and result[0] == 200:
                    self.timezone_due = 0
                if route in ("config_put", "update") and result[0] == 202:
                    self.programs.clear()
                    self.config_due = 0
            return result
        finally:
            self.upstream.release()


def _mqtt_string(value):
    data = value if isinstance(value, bytes) else value.encode()
    if len(data) > 0xffff:
        raise ValueError("MQTT string too long")
    return len(data).to_bytes(2, "big") + data


def _mqtt_packet(header, body):
    length, encoded = len(body), bytearray()
    if length > 268_435_455:
        raise ValueError("MQTT packet too long")
    while True:
        length, digit = divmod(length, 128)
        encoded.append(digit | (0x80 if length else 0))
        if not length:
            return bytes([header]) + bytes(encoded) + body


class MqttClient:
    """Minimal MQTT 3.1.1 publisher: QoS 0, retained messages, a last will, no subscriptions."""
    def __init__(self, host, port, username, password, client_id, will_topic, keepalive=60, timeout=10):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.client_id, self.will_topic, self.keepalive, self.timeout = client_id, will_topic, keepalive, timeout
        self.sock, self.last_send = None, 0.0

    def connect(self):
        flags = 0x02 | 0x04 | 0x20  # clean session, will flag, retained will (QoS 0)
        payload = _mqtt_string(self.client_id) + _mqtt_string(self.will_topic) + _mqtt_string(b"offline")
        if self.username:
            flags |= 0x80
            payload += _mqtt_string(self.username)
            if self.password:
                flags |= 0x40
                payload += _mqtt_string(self.password)
        header = _mqtt_string(b"MQTT") + bytes([4, flags]) + self.keepalive.to_bytes(2, "big")
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            sock.sendall(_mqtt_packet(0x10, header + payload))
            ack = b""
            while len(ack) < 4:
                chunk = sock.recv(4 - len(ack))
                if not chunk:
                    raise ConnectionError("MQTT broker closed the connection")
                ack += chunk
            if ack[:2] != b"\x20\x02" or ack[3] != 0:
                raise ConnectionError(f"MQTT broker refused the connection (code {ack[3]})")
        except BaseException:
            sock.close()
            raise
        self.sock, self.last_send = sock, time.monotonic()
        return self

    def _send(self, packet):
        self.sock.sendall(packet)
        self.last_send = time.monotonic()

    def publish(self, topic, payload, retain=False):
        data = payload if isinstance(payload, bytes) else payload.encode()
        self._send(_mqtt_packet(0x31 if retain else 0x30, _mqtt_string(topic) + data))

    def service(self):
        """Keep the session alive and notice a broker that has gone away."""
        if time.monotonic() - self.last_send >= self.keepalive / 2:
            self._send(b"\xc0\x00")
        while select.select([self.sock], [], [], 0)[0]:
            if not self.sock.recv(4096):  # only PINGRESP is ever sent to a publisher
                raise ConnectionError("MQTT broker closed the connection")

    def close(self):
        if self.sock:
            try: self._send(b"\xe0\x00")
            except OSError: pass
            self.sock.close()
            self.sock = None


def supervisor_mqtt(token, host="supervisor", timeout=6):
    """Broker settings the Supervisor provides to apps that declare the mqtt service."""
    conn = http.client.HTTPConnection(host, timeout=timeout)
    try:
        conn.request("GET", "/services/mqtt", headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        response = conn.getresponse()
        raw = response.read(65537)
    finally:
        conn.close()
    if response.status != 200 or len(raw) > 65536:
        raise ConnectionError(f"no MQTT broker is available (Supervisor HTTP {response.status})")
    body = json.loads(raw)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("host"), str):
        raise ConnectionError("Supervisor returned no MQTT broker")
    if data.get("ssl"):
        raise ConnectionError("TLS MQTT brokers are not supported")
    port = data.get("port", 1883)
    return {"host": data["host"], "port": port if type(port) is int and 0 < port < 65536 else 1883,
            "username": data.get("username") if isinstance(data.get("username"), str) else None,
            "password": data.get("password") if isinstance(data.get("password"), str) else None}


SHOW_SENSORS = (
    # key, name, unit, icon, display precision
    ("active_show", "Active show", None, "mdi:weather-sunset", None),
    ("target_cct", "Show target color temperature", "K", "mdi:sun-thermometer-outline", 0),
    ("requested_cct", "Requested color temperature", "K", "mdi:thermometer-lines", 0),
    ("target_brightness", "Show target brightness", "%", "mdi:brightness-percent", 1),
    ("requested_brightness", "Requested brightness", "%", "mdi:brightness-6", 1),
    ("confirmed_brightness", "Confirmed brightness", "%", "mdi:check-circle-outline", 1),
)


class ShowSensors:
    """Per-zone Natural Show diagnostics, published to Home Assistant through MQTT discovery.

    The target is the show evaluated with the endpoint's arithmetic in the gateway's
    time zone when available. Requested values are what the bridge last sent to N4; N4 confirms brightness only.
    """
    PREFIX = "ccx_bridge"
    AVAILABILITY = PREFIX + "/status"

    def __init__(self, panel, ha, connect, now=None):
        self.panel, self.ha, self.connect = panel, ha, connect
        self.now = now or (lambda tz: datetime.now(tz))
        self.client, self.retry_at, self.failures = None, 0.0, 0
        self.discovered, self.published, self.available = {}, {}, None
        self.stop = threading.Event()

    def local_ms(self, gateway_zone=None):
        return show_clock(gateway_zone, self.ha.time_zone, self.now)

    def discovery(self, zone_id, zone):
        state_topic = f"{self.PREFIX}/zone/{zone_id}/state"
        name = clean_text(zone.get("name"), 64) or f"Zone {zone_id}"
        device = {"identifiers": [f"{self.PREFIX}_zone_{zone_id}"], "name": f"CCX Bridge {name}",
                  "manufacturer": "CCX Bridge", "model": "Bridged A20 zone"}
        if type(zone.get("serial")) is int:
            device["serial_number"] = str(zone["serial"])
        for key, label, unit, icon, precision in SHOW_SENSORS:
            config = {"name": label, "unique_id": f"{self.PREFIX}_{zone_id}_{key}", "state_topic": state_topic,
                      "value_template": "{{ value_json.%s }}" % key, "availability_topic": self.AVAILABILITY,
                      "icon": icon, "device": device,
                      "origin": {"name": "CCX Bridge", "support_url": "https://github.com/thetimetraveler/c6-live-bridge"}}
            if unit:
                config.update(unit_of_measurement=unit, state_class="measurement", suggested_display_precision=precision)
            if key == "active_show":
                config.update(json_attributes_topic=state_topic, json_attributes_template="{{ value_json.attributes | tojson }}")
            yield f"homeassistant/sensor/{self.PREFIX}_{zone_id}/{key}/config", json.dumps(config, separators=(",", ":"))

    def remove(self, zone_id):
        for key, *_ in SHOW_SENSORS:
            self.client.publish(f"homeassistant/sensor/{self.PREFIX}_{zone_id}/{key}/config", b"", retain=True)
        self.client.publish(f"{self.PREFIX}/zone/{zone_id}/state", b"", retain=True)

    def publish(self):
        inputs = self.panel.show_inputs()
        available = inputs["age_s"] is not None and inputs["age_s"] <= SNAPSHOT_STALE_S
        local_ms, tz_name = self.local_ms(inputs.get("timezone"))
        zones = {}
        for zone in inputs["zones"]:
            if type(zone.get("zone_id")) is int and zone["zone_id"] > 0 and type(zone.get("serial")) is int and zone["serial"] > 0:
                zones[zone["zone_id"]] = zone
        for zone_id in [z for z in self.discovered if z not in zones]:
            self.remove(zone_id)
            del self.discovered[zone_id]
            self.published.pop(zone_id, None)
        for zone_id, zone in zones.items():
            signature = json.dumps([zone.get("name"), zone.get("serial")])
            if self.discovered.get(zone_id) != signature:
                for topic, config in self.discovery(zone_id, zone):
                    self.client.publish(topic, config, retain=True)
                self.discovered[zone_id] = signature
            program, age = inputs["programs"].get(zone.get("zone"), (None, None))
            state = zone_show_state(zone, program, age, local_ms)
            state["attributes"]["time_zone"] = tz_name
            payload = json.dumps(state, separators=(",", ":"), sort_keys=True)
            if self.published.get(zone_id) != payload:
                self.client.publish(f"{self.PREFIX}/zone/{zone_id}/state", payload, retain=True)
                self.published[zone_id] = payload
        if available != self.available:
            self.client.publish(self.AVAILABILITY, "online" if available else "offline", retain=True)
            self.available = available

    def step(self):
        if self.client is None:
            if time.monotonic() < self.retry_at:
                return
            try:
                self.client = self.connect()
                self.discovered, self.published, self.available = {}, {}, None
                self.failures = 0
                self.panel.track_programs = True  # programming reads only while someone consumes them
                LOG.info("Show sensors connected to MQTT")
            except (OSError, ConnectionError, ValueError, http.client.HTTPException) as exc:
                self.failures += 1
                self.retry_at = time.monotonic() + min(300, 15 * 2 ** min(self.failures, 4))
                if self.failures == 1 or self.failures % 10 == 0:
                    LOG.warning("Show sensors waiting for MQTT: %s", exc)
                return
        try:
            self.publish()
            self.client.service()
        except (OSError, ConnectionError, ValueError) as exc:
            LOG.warning("Show sensors lost MQTT; reconnecting: %s", exc)
            self.client.close()
            self.client, self.retry_at = None, time.monotonic() + 5
            self.panel.track_programs = False

    def run(self, interval=5):
        while not self.stop.is_set():
            self.step()
            self.stop.wait(interval)

    def close(self):
        self.stop.set()
        if self.client:
            try: self.client.publish(self.AVAILABILITY, "offline", retain=True)
            except OSError: pass
            self.client.close()
            self.client = None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, panel, ha=None):
        self.panel = panel
        self.ha = ha or HomeAssistant("", [])
        self.html = Path(__file__).with_name("index.html").read_bytes()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "C6Management/0.1"
    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def authenticated(self):
        return self.client_address[0] == "172.30.32.2"

    def send(self, code, body, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(body)

    def reply(self, code, value):
        self.send(code, json.dumps(value, separators=(",", ":")).encode())

    def authorized(self):
        if not self.authenticated():
            self.reply(403, {"error": "Home Assistant ingress required"})
            return False
        return True

    def do_GET(self):
        if not self.authorized(): return
        if self.path in ("/", "/index.html"):
            return self.send(200, self.server.html, "text/html; charset=utf-8")
        parts = urlsplit(self.path)
        if parts.path == "/api/ha":
            if parts.query or parts.fragment: return self.reply(404, {"error": "unknown endpoint"})
            return self.reply(200, self.server.ha.snapshot())
        if parts.path == "/api/zones":
            if parts.query or parts.fragment: return self.reply(404, {"error": "unknown endpoint"})
            ha = self.server.ha
            return self.reply(200, self.server.panel.zone_summaries(ha.snapshot(), lambda gz: show_clock(gz, ha.time_zone, datetime.now)))
        route = {"/api/status": "status", "/api/devices": "devices", "/api/config": "config", "/api/timezone": "timezone"}.get(parts.path)
        if parts.path == "/api/events": route = "events"
        if parts.path == "/api/program": route = "program"
        allowed = {"events": {"after", "boot_id"}, "program": {"zone", "page", "refresh"}}
        if len(parts.query) > 256: return self.reply(400, {"error": "query is too long"})
        try: query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=3)
        except ValueError as exc: return self.reply(400, {"error": str(exc)})
        if route is None or parts.fragment or (route in allowed and any(k not in allowed[route] for k, _ in query)) or (route not in allowed and query):
            return self.reply(404, {"error": "unknown endpoint"})
        try:
            if route in allowed and len(dict(query)) != len(query): raise ValueError("duplicate query parameter")
            code, ctype, raw = self.server.panel.proxy(route, query)
            return self.send(code, raw, ctype)
        except ValueError as exc: return self.reply(400, {"error": str(exc)})
        except (OSError, http.client.HTTPException) as exc: return self.reply(502, {"error": str(exc)})

    def read_body(self, maximum):
        length_text = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,9}", length_text):
            raise ValueError("Content-Length is required")
        length = int(length_text)
        if not 1 <= length <= maximum: raise OverflowError("request body exceeds size limit")
        body = self.rfile.read(length)
        if len(body) != length: raise EOFError("incomplete request")
        return body

    def do_PUT(self):
        if not self.authorized(): return
        if self.path not in ("/api/config", "/api/timezone"): return self.reply(404, {"error": "unknown endpoint"})
        try:
            if self.headers.get_content_type() != "application/json": raise ValueError("application/json required")
            body = self.read_body(128 if self.path == "/api/timezone" else 65536)
            payload = json.loads(body)
            if self.path == "/api/timezone":
                if not isinstance(payload, dict) or set(payload) != {"timezone"} or payload["timezone"] not in SHOW_TIME_ZONES:
                    raise ValueError("expected a supported time zone")
            elif not isinstance(payload, dict) or set(payload) != {"generation", "hash", "zones"} or not isinstance(payload["zones"], list):
                raise ValueError("expected generation, hash, and zones")
            packed = json.dumps(payload, separators=(",", ":")).encode()
        except OverflowError as exc: return self.reply(413, {"error": str(exc)})
        except EOFError as exc: return self.reply(400, {"error": str(exc)})
        except (ValueError, UnicodeDecodeError) as exc: return self.reply(400, {"error": str(exc)})
        try:
            code, ctype, raw = self.server.panel.proxy("timezone_put" if self.path == "/api/timezone" else "config_put", body=packed, content_type="application/json")
            return self.send(code, raw, ctype)
        except (OSError, http.client.HTTPException, ValueError) as exc: return self.reply(502, {"error": str(exc)})

    def do_POST(self):
        if not self.authorized(): return
        if self.path == "/api/maintenance":
            try:
                value = json.loads(self.read_body(128))
                if not isinstance(value, dict) or set(value) != {"enabled"} or type(value["enabled"]) is not bool:
                    raise ValueError("expected enabled boolean")
                code, ctype, raw = self.server.panel.proxy("maintenance", body=json.dumps(value).encode(), content_type="application/json")
                return self.send(code, raw, ctype)
            except (ValueError, EOFError, OverflowError) as exc:
                return self.reply(400, {"error": str(exc)})
            except (OSError, http.client.HTTPException) as exc:
                return self.reply(502, {"error": str(exc)})
        if self.path != "/api/endpoint/update": return self.reply(404, {"error": "unknown endpoint"})
        ctype = self.headers.get("Content-Type", "application/octet-stream")
        if ctype.split(";", 1)[0].strip().lower() != "application/octet-stream":
            return self.reply(415, {"error": "application/octet-stream required"})
        try:
            body = self.read_body(MAX_FIRMWARE)
        except OverflowError as exc: return self.reply(413, {"error": str(exc)})
        except EOFError as exc: return self.reply(400, {"error": str(exc)})
        except ValueError as exc: return self.reply(411, {"error": str(exc)})
        try:
            code, out_type, raw = self.server.panel.proxy("update", body=body, content_type="application/octet-stream")
            return self.send(code, raw, out_type)
        except (OSError, http.client.HTTPException, ValueError) as exc: return self.reply(502, {"error": str(exc)})

    def log_message(self, fmt, *args): LOG.info(fmt, *args)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    options = json.loads(Path("/data/options.json").read_text())
    gateway = Gateway(options["gateway_host"], options["gateway_token"])
    panel = Panel(gateway, EventArchive("/share/c6-management/events"))
    integrations = options.get("lutron_integrations")
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    ha = HomeAssistant(supervisor_token, integrations if isinstance(integrations, list) else DEFAULT_INTEGRATIONS)
    server = Server(("0.0.0.0", 8099), panel, ha)
    threading.Thread(target=panel.run_event_archive, name="event-archive", daemon=True).start()
    sensors = sensor_thread = None
    if options.get("show_sensors", True) is True and supervisor_token:
        def connect():
            broker = supervisor_mqtt(supervisor_token)
            return MqttClient(broker["host"], broker["port"], broker["username"], broker["password"],
                              "ccx-bridge-show-sensors", ShowSensors.AVAILABILITY).connect()
        sensors = ShowSensors(panel, ha, connect)
        sensor_thread = threading.Thread(target=sensors.run, name="show-sensors", daemon=True)
        sensor_thread.start()
    try: server.serve_forever()
    finally:
        panel.stop.set()
        if sensors:
            sensors.stop.set()
            sensor_thread.join(timeout=10)
            sensors.close()
        server.server_close()


if __name__ == "__main__": main()
