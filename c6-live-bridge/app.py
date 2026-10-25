#!/usr/bin/env python3
"""Ingress-only, fixed-route management panel proxy for a C6 gateway."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import http.client
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

LOG = logging.getLogger("c6-management")
MAX_RESPONSE = 1_048_576
MAX_FIRMWARE = 786_432
SAFE_INT = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
ROUTES = {"status": ("GET", "/api/status"), "devices": ("GET", "/api/devices"),
          "events": ("GET", "/api/events"), "program": ("GET", "/api/program"),
          "config": ("GET", "/api/config"), "config_put": ("PUT", "/api/config"),
          "update": ("POST", "/api/endpoint/update")}


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
  "area": area_name(eid), "device_id": dev,
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

    def request(self, path, method="GET", body=None, content_type=None):
        conn_cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        long_operation = path == "/api/endpoint/update" or path.startswith("/api/program?")
        conn = conn_cls(self.hostname, self.port, timeout=300 if long_operation else self.timeout)
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            headers["Content-Length"] = str(len(body))
            if path == "/api/endpoint/update":
                headers["X-SHA256"] = hashlib.sha256(body).hexdigest()
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
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
        return {"entity_id": item["entity_id"][:255], "integration": clean_text(item.get("integration"), 64),
                "name": clean_text(item.get("name")), "state": clean_text(item.get("state"), 32),
                "brightness": brightness if type(brightness) is int and 0 <= brightness <= 255 else None,
                "color_temp_kelvin": kelvin if type(kelvin) in (int, float) and 1000 <= kelvin <= 20000 else None,
                "area": clean_text(item.get("area")), "device_id": clean_text(item.get("device_id"), 64),
                "device": clean_text(item.get("device")), "model": clean_text(item.get("model")),
                "serial": clean_text(str(serial), 64) if type(serial) in (str, int) and serial != "" else None}

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


class Panel:
    def __init__(self, gateway, archive):
        self.gateway, self.archive = gateway, archive
        self.lock = threading.Lock()
        self.cursor = 0
        self.boot_id = None
        self.archive_cursor = 0
        self.archive_boot_id = None
        self.last_error = None
        self.last_contact = None
        self.online = False
        self.stop = threading.Event()

    def archive_poll_once(self):
        """Fetch the event log for host retention independently of browser polling."""
        after = self.archive_cursor
        known_boot = self.archive_boot_id
        path = "/api/events?" + urlencode({"after": 0 if known_boot is None else after})
        try:
            code, _ctype, raw = self.gateway.request(path, "GET")
            if code != 200:
                raise ValueError(f"gateway events returned HTTP {code}")
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise ValueError("gateway event response is invalid")
            new_boot = data.get("boot_id")
            if new_boot is not None and known_boot is not None and new_boot != known_boot and after != 0:
                code, _ctype, raw = self.gateway.request("/api/events?after=0", "GET")
                if code != 200:
                    raise ValueError(f"gateway events rewind returned HTTP {code}")
                data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                    raise ValueError("gateway event rewind response is invalid")
                new_boot = data.get("boot_id")
            if new_boot is not None and new_boot != known_boot:
                self.archive_boot_id = new_boot
                self.archive_cursor = 0
            self.archive.record(data)
            for event in data["events"]:
                if isinstance(event, dict) and type(event.get("id")) is int:
                    self.archive_cursor = max(self.archive_cursor, event["id"])
            with self.lock:
                self.last_contact = time.time()
                self.last_error = None
            return data
        except (OSError, http.client.HTTPException, ValueError, TypeError) as exc:
            with self.lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
            LOG.warning("Event archive poll will retry: %s", exc)
            return None

    def run_event_archive(self):
        while not self.stop.is_set():
            self.archive_poll_once()
            self.stop.wait(1)

    def proxy(self, route, query="", body=None, content_type=None):
        method, base = ROUTES[route]
        if route == "events":
            params = dict(query)
            after = bounded_int(params.get("after", "0"), "event cursor", 0xffffffff)
            with self.lock: known_boot = self.boot_id
            path = base + "?" + urlencode({"after": 0 if known_boot is None else after})
        elif route == "program":
            zone = bounded_int(dict(query).get("zone", "0"), "zone", 65535)
            page = bounded_int(dict(query).get("page", "0"), "page", 100000)
            path = base + "?" + urlencode({"zone": zone, "page": page})
        else:
            path = base
        try:
            code, ctype, raw = self.gateway.request(path, method, body, content_type)
            with self.lock:
                self.online = True
                self.last_contact = time.time()
                self.last_error = None
            if route == "events" and code == 200:
                try:
                    data = json.loads(raw)
                    new_boot = data.get("boot_id") if isinstance(data, dict) else None
                    if new_boot is not None and known_boot is not None and new_boot != known_boot and after != 0:
                        code, ctype, raw = self.gateway.request(base + "?after=0", method)
                        data = json.loads(raw)
                        new_boot = data.get("boot_id") if isinstance(data, dict) else None
                    if new_boot is not None and new_boot != known_boot:
                        with self.lock:
                            self.boot_id = new_boot
                            self.cursor = 0
                    self.archive.record(data)
                    if isinstance(data, dict):
                        events = data.get("events", [])
                    else:
                        events = data
                    if isinstance(events, list):
                        for event in events:
                            if isinstance(event, dict) and type(event.get("id")) is int:
                                self.cursor = max(self.cursor, event["id"])
                except (ValueError, TypeError):
                    pass
            return code, ctype, raw
        except (OSError, http.client.HTTPException, ValueError) as exc:
            with self.lock:
                self.online = False
                self.last_error = f"{type(exc).__name__}: {exc}"
            raise


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
        route = {"/api/status": "status", "/api/devices": "devices", "/api/config": "config"}.get(parts.path)
        if parts.path == "/api/events": route = "events"
        if parts.path == "/api/program": route = "program"
        allowed = {"events": {"after"}, "program": {"zone", "page"}}
        if len(parts.query) > 256: return self.reply(400, {"error": "query is too long"})
        try: query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=2)
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
        if self.path != "/api/config": return self.reply(404, {"error": "unknown endpoint"})
        try:
            if self.headers.get_content_type() != "application/json": raise ValueError("application/json required")
            body = self.read_body(65536)
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"generation", "hash", "zones"} or not isinstance(payload["zones"], list):
                raise ValueError("expected generation, hash, and zones")
            packed = json.dumps(payload, separators=(",", ":")).encode()
        except OverflowError as exc: return self.reply(413, {"error": str(exc)})
        except EOFError as exc: return self.reply(400, {"error": str(exc)})
        except (ValueError, UnicodeDecodeError) as exc: return self.reply(400, {"error": str(exc)})
        try:
            code, ctype, raw = self.server.panel.proxy("config_put", body=packed, content_type="application/json")
            return self.send(code, raw, ctype)
        except (OSError, http.client.HTTPException, ValueError) as exc: return self.reply(502, {"error": str(exc)})

    def do_POST(self):
        if not self.authorized(): return
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
    ha = HomeAssistant(os.environ.get("SUPERVISOR_TOKEN", ""),
                       integrations if isinstance(integrations, list) else DEFAULT_INTEGRATIONS)
    server = Server(("0.0.0.0", 8099), panel, ha)
    threading.Thread(target=panel.run_event_archive, name="event-archive", daemon=True).start()
    try: server.serve_forever()
    finally:
        panel.stop.set()
        server.server_close()


if __name__ == "__main__": main()
