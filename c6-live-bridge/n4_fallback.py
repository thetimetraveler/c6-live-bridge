#!/usr/bin/env python3
"""App-side N4 fallback: drive the bridged Ketra lamps through the N4 hub while the gateway is unreachable.

Nothing in this module talks to the gateway, the endpoint or the CCX mesh. Its
only writes are PUT /Groups/{group}/State on the N4 hub, and only while engaged.
"""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import http.client
import json
import logging
import math
import os
from pathlib import Path
import socket
import ssl
import struct
import threading
import time
from urllib.parse import quote, urlsplit

LOG = logging.getLogger("c6-management")
MODES = ("auto", "off", "force")
# The gateway must have been silent this long before the app takes over; after a
# recent hand-back it has to stay silent longer, so a flapping gateway can't
# pass control back and forth.
ENGAGE_AFTER_S = 120
FLAP_WINDOW_S, FLAP_ENGAGE_AFTER_S = 1800, 300
# A failed gateway request must be this recent: silence alone is not proof that it is unreachable.
FAILURE_FRESH_S = 90
# Ketra documents one group control command per 1.5 s.
GROUP_INTERVAL_S, WRITE_GAP_S = 1.5, 0.25
SHOW_REAPPLY_S, POLL_S, PROBE_S, REMAP_S = 30, 5, 60, 60
TRANSITION_MS, SHOW_TRANSITION_MS = 500, 2000
MAX_N4_RESPONSE = 262_144
MAX_WS_MESSAGE = 4_194_304
WARM_DIM_NONE = 0xFE
CA_PATH = Path(__file__).with_name("ketra-ca.pem")
API_PATH = "/ketra.cgi/api/v1"

# ---------------------------------------------------------------------------
# Warm-dim curves: a port of the gateway firmware's warm_dim.c.
# ---------------------------------------------------------------------------

CURVE_NAMES = {0: "Default", 1: "Halogen", 2: "Finiré 2700", 3: "Finiré 3000", 4: "Generic Daylight", WARM_DIM_NONE: "None"}
CURVES = (((32, 32, 32, 187, 607, 2162, 4713, 8223, 32767, 32767, 65535), (1800, 1859, 1944, 2086, 2239, 2417, 2683, 2800)),
          ((3, 3, 3, 208, 639, 1305, 2206, 8489, 32639, 32639, 65535), (1798, 1794, 1940, 2047, 2117, 2397, 2691, 2802)),
          ((32, 32, 32, 1948, 4345, 7663, 18518, 32767, 32767, 65535, 65535), (1784, 1760, 1999, 2226, 2520, 2688, 2720)),
          ((32, 32, 32, 1756, 7243, 18570, 32767, 32767, 65535, 65535, 65535), (1794, 1764, 2278, 2759, 2991, 3040)))
POWER_KNOTS = (1, 1, 1, 300, 800, 1350, 1820, 2042, 2044, 2047, 2047, 65535)
POWER_COEFFS = (32, 34, 1905, 8461, 19208, 29037, 32615, 32767, 32767)
HALOGEN_KNOTS = (7, 7, 7, 58, 279, 885, 2593, 8644, 32767, 32767, 65535)
HALOGEN_X = (4917, 4637, 4561, 4326, 4220, 3979, 3781, 3711)
HALOGEN_Y = (3222, 3274, 3367, 3375, 3417, 3405, 3388, 3365)


def _basis(knots, count, i, p, t):
    if p == 0:
        if i == count - 2:
            return 1.0 if knots[i] <= t <= knots[i + 1] else 0.0
        return 1.0 if knots[i] <= t < knots[i + 1] else 0.0
    left = right = 0.0
    d1 = knots[i + p] - knots[i]
    if d1 > 0:
        left = (t - knots[i]) / d1 * _basis(knots, count, i, p - 1, t)
    d2 = knots[i + p + 1] - knots[i + 1]
    if d2 > 0:
        right = (knots[i + p + 1] - t) / d2 * _basis(knots, count, i + 1, p - 1, t)
    return left + right


def _integer_spline(knots, coeffs, t):
    # The processor's integer quadratic spline, including its unsigned 32-bit wrap.
    t = max(t, knots[0])
    span = 2
    for i in range(2, 9):
        if knots[i + 2] == 65535:
            break
        if knots[i] <= t:
            span = i
    d = [coeffs[span - 2], coeffs[span - 1], coeffs[span]]
    for degree in (2, 1):
        old = list(d)
        for i in (span - 1, span):
            j = i + 2 - span
            den = knots[i + degree] - knots[i]
            d[j] = (((t - knots[i]) * old[j] + (knots[i + degree] - t) * old[j - 1]) & 0xffffffff) // den if den else 0
    return d[2] & 0xffff


def _intensity_power(level):
    level = min(level, 10000)
    native = 1 + ((level - 100 if level > 100 else 0) * 65278 + 4950) // 9900 if level else 0
    t = ((((native - 1) * 65470 + 32639) // 65278 + 65) & 65535) >> 5 if native else 2
    return _integer_spline(POWER_KNOTS, POWER_COEFFS, t)


def _f32(value):
    return struct.unpack("f", struct.pack("f", value))[0]


def _generic_daylight(level):
    power = (_intensity_power(level) & 32767) * 2
    # Single-precision steps, as the firmware evaluates them.
    x = 0.0 if power < 66 else _f32(power / _f32(65535.0))
    gain = _f32(_f32(2500.0) * _f32(_f32(0.29) + 1.0))
    return int(_f32(2500.0 + _f32(_f32(gain * x) / _f32(x + _f32(0.29)))))


def warm_dim_cct(curve, level_x100):
    """Colour temperature (K) for a level-only command on a zone's warm-dim curve."""
    if curve not in range(5):
        curve = 0
    if curve == 4:
        return _generic_daylight(level_x100)
    knots, coeffs = CURVES[curve]
    count = len(coeffs) + 3
    t = knots[2] + min(level_x100, 10000) / 10000 * (knots[count - 3] - knots[2])
    return int(sum(c * _basis(knots, count, i, 2, t) for i, c in enumerate(coeffs)) + 0.5)


def warm_dim_native(curve, level_x100):
    """Lutron's own warm-dim evaluation (the "white" colour mode): {"cct": K} or {"x", "y"} in 0..1."""
    if curve not in range(5):
        curve = 0
    power = _intensity_power(level_x100)
    if curve == 1:
        return {"x": (_integer_spline(HALOGEN_KNOTS, HALOGEN_X, power) * 4 * 10000 + 16383) // 32767 / 10000,
                "y": (_integer_spline(HALOGEN_KNOTS, HALOGEN_Y, power) * 4 * 10000 + 16383) // 32767 / 10000}
    if curve == 4:
        return {"cct": _generic_daylight(level_x100)}
    knots, coeffs = CURVES[curve]
    return {"cct": max(1400, min(10000, _integer_spline(knots, coeffs + (0,) * (8 - len(coeffs)), power)))}


# ---------------------------------------------------------------------------
# Lutron light state (from Home Assistant) -> N4 LampState
# ---------------------------------------------------------------------------

def light_from_state(state):
    """The fields the fallback uses from a Home Assistant state object; None if it is not one."""
    if not isinstance(state, dict) or not isinstance(state.get("state"), str):
        return None
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    brightness, kelvin, xy = attrs.get("brightness"), attrs.get("color_temp_kelvin"), attrs.get("xy_color")
    return {"state": state["state"][:32],
            "brightness": brightness if type(brightness) is int and 0 <= brightness <= 255 else None,
            "color_mode": attrs.get("color_mode") if isinstance(attrs.get("color_mode"), str) else None,
            "color_temp_kelvin": kelvin if type(kelvin) in (int, float) and 1000 <= kelvin <= 25000 else None,
            "xy_color": [float(v) for v in xy] if isinstance(xy, (list, tuple)) and len(xy) == 2
            and all(type(v) in (int, float) and 0 < v < 1 for v in xy) else None}


def light_level(light):
    """Lutron's level as percent x 100; None when it is not reported."""
    if light["state"] == "off":
        return 0
    brightness = light.get("brightness")
    return max(1, round(brightness * 10000 / 255)) if type(brightness) is int else None


def _on(level_x100):
    return {"PowerOn": True, "Brightness": round(min(level_x100, 10000) / 10000, 4)}


def lamp_body(light, curve, transition_ms=TRANSITION_MS):
    """The N4 LampState for a Lutron light state; None when the state gives nothing to send.

    The body has the gateway's shape (n4_body.c). Colour comes from, in order:
    the colour temperature Lutron reports; Lutron's xy for a colour (hs) command;
    Lutron's own warm-dim evaluation in "white" mode; otherwise the zone's
    warm-dim curve at this level. A zone without a curve gets brightness only.
    """
    if light["state"] == "off":
        return {"PowerOn": False, "TransitionTime": transition_ms}
    level = light_level(light) if light["state"] == "on" else None
    if level is None:
        return None
    body = _on(level)
    mode, kelvin, xy = light.get("color_mode"), light.get("color_temp_kelvin"), light.get("xy_color")
    if mode == "color_temp" and kelvin:
        body["CCT"] = int(max(1400, min(25000, kelvin)))
    elif mode == "hs" and xy:
        # Home Assistant derives xy from Lutron's hue and saturation. Sending it as
        # chromaticity keeps a saturated colour, which a CCT conversion would lose,
        # and is what the gateway does with an xy command.
        body["xChromaticity"], body["yChromaticity"] = round(xy[0], 4), round(xy[1], 4)
    elif mode == "white":
        native = warm_dim_native(curve, level)
        if "cct" in native:
            body["CCT"] = native["cct"]
        else:
            body["xChromaticity"], body["yChromaticity"] = native["x"], native["y"]
    elif curve in range(5):
        body["CCT"] = warm_dim_cct(curve, level)
    body["TransitionTime"] = transition_ms
    return body


def show_body(sample, override_x100, transition_ms=SHOW_TRANSITION_MS):
    """The N4 LampState for a Natural Show sample; an override replaces the show's brightness only."""
    level = override_x100 if override_x100 is not None else sample["level_x100"]
    if not level:
        return {"PowerOn": False, "TransitionTime": transition_ms}
    body = _on(level)
    if 1400 <= sample["cct"] <= 25000:
        body["CCT"] = sample["cct"]
    if sample.get("vibrancy") is not None:
        body["Vibrancy"] = round(sample["vibrancy"] / 100, 4)
    body["TransitionTime"] = transition_ms
    return body


# ---------------------------------------------------------------------------
# N4 hub client
# ---------------------------------------------------------------------------

class N4Error(OSError):
    """The hub answered but refused or returned something unexpected."""


class N4AuthError(N4Error):
    """The hub rejected the access token."""


class N4RateLimited(N4Error):
    """The hub returned HTTP 429."""


def n4_address(value):
    """(host, port) from the n4_host option: an address, host:port or https:// origin."""
    value = value.strip() if isinstance(value, str) else ""
    parts = urlsplit(value if "://" in value else "//" + value)
    if (not parts.hostname or parts.scheme not in ("", "https") or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError("n4_host must be the N4 hub's address, for example 192.168.1.50")
    return parts.hostname, parts.port or 443


def n4_tls_context(verify=True, ca_path=CA_PATH):
    """The TLS context the hub's embedded stack needs.

    The hub certificate is signed by the Ketra device root and named <serial>.ketra.com,
    so the chain is verified without a host name check.
    """
    context = ssl.create_default_context(cafile=str(ca_path)) if verify else ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED if verify else ssl.CERT_NONE
    # The hub predates RFC 5746 secure renegotiation.
    context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    try:
        context.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        LOG.debug("N4: the TLS security level could not be lowered")
    return context


class N4Client:
    """The Ketra N4 local REST API calls the fallback needs. The password is never logged."""
    def __init__(self, host, password, verify=True, timeout=5, tls=True):
        self.host, self.port = n4_address(host)
        if not isinstance(password, str) or not password or len(password) > 4096:
            raise ValueError("n4_password is required")
        # The hub ignores the user name; the password is a Ketra access token.
        self.auth = "Basic " + base64.b64encode(b"na:" + password.encode()).decode()
        self.timeout, self.tls = timeout, tls
        self.context = n4_tls_context(verify) if tls else None

    def request(self, method, path, body=None):
        if self.tls:
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.context)
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, API_PATH + path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read(MAX_N4_RESPONSE + 1)
        finally:
            conn.close()
        if response.status in (401, 403):
            raise N4AuthError("the hub rejected the access token")
        if response.status == 429:
            raise N4RateLimited("the hub is rate limiting commands")
        try:
            payload = json.loads(raw) if len(raw) <= MAX_N4_RESPONSE else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or "Success" not in payload:
            raise N4Error(f"unexpected response from the hub (HTTP {response.status})")
        if response.status >= 400 or payload["Success"] is not True:
            error = payload.get("Error")
            raise N4Error(error[:80] if isinstance(error, str) and error else f"HTTP {response.status}")
        return payload.get("Content")

    def hub(self):
        return self.request("GET", "/Hub")

    def group_state(self, group):
        return self.request("GET", f"/Groups/{quote(group, safe='')}/State")

    def set_group_state(self, group, state):
        return self.request("PUT", f"/Groups/{quote(group, safe='')}/State", state)


# ---------------------------------------------------------------------------
# Home Assistant state changes over the Supervisor's websocket proxy
# ---------------------------------------------------------------------------

class HaEvents:
    """state_changed events from Home Assistant; reconnects with backoff. Read-only."""
    IDLE_S, PING_S = 90, 30

    def __init__(self, token, on_state, on_connect=None, host="supervisor", stop=None):
        self.token, self.on_state, self.on_connect, self.host = token, on_state, on_connect, host
        self.stop = stop or threading.Event()
        self.connected, self.error, self.failures = False, None, 0
        self.sock, self.buffer = None, b""

    def _fill(self):
        deadline = time.monotonic() + self.IDLE_S
        while not self.stop.is_set():
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                now = time.monotonic()
                if now >= deadline:
                    raise ConnectionError("Home Assistant websocket went quiet")
                if now - self.pinged >= self.PING_S:
                    self._send(b"", 0x9)
                    self.pinged = now
                continue
            if not chunk:
                raise ConnectionError("Home Assistant closed the websocket")
            self.buffer += chunk
            return
        raise ConnectionAbortedError("stopping")

    def _read(self, count):
        while len(self.buffer) < count:
            self._fill()
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def _send(self, payload, opcode=0x1):
        mask = os.urandom(4)
        size = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | size]) if size < 126 else
                                         bytes([0x80 | 126]) + struct.pack(">H", size) if size < 65536 else
                                         bytes([0x80 | 127]) + struct.pack(">Q", size))
        self.sock.sendall(head + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def _message(self):
        """The next text message, decoded; control frames are handled here."""
        parts, total = [], 0
        while True:
            first, second = self._read(2)
            opcode, size = first & 0x0f, second & 0x7f
            if size == 126:
                size = struct.unpack(">H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read(8))[0]
            total += size
            if second & 0x80 or total > MAX_WS_MESSAGE:
                raise ConnectionError("invalid websocket frame from Home Assistant")
            payload = self._read(size)
            if opcode == 0x8:
                raise ConnectionError("Home Assistant closed the websocket")
            if opcode == 0x9:
                self._send(payload, 0xA)
            elif opcode in (0x0, 0x1, 0x2):
                parts.append(payload)
                if first & 0x80:
                    return json.loads(b"".join(parts))

    def _json(self, value):
        self._send(json.dumps(value, separators=(",", ":")).encode())

    def session(self):
        parts = urlsplit("//" + self.host)
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=10)
        try:
            self.buffer, self.pinged = b"", time.monotonic()
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((f"GET /core/websocket HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
                               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                               f"Authorization: Bearer {self.token}\r\n\r\n").encode())
            self.sock.settimeout(1)
            while b"\r\n\r\n" not in self.buffer:
                if len(self.buffer) > 16384:
                    raise ConnectionError("oversized websocket handshake")
                self._fill()
            head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if lines[0].split(" ")[1:2] != ["101"] or headers.get("sec-websocket-accept") != accept:
                raise ConnectionError(f"websocket upgrade refused: {lines[0][:60]}")
            if self._message().get("type") != "auth_required":
                raise ConnectionError("unexpected websocket greeting")
            self._json({"type": "auth", "access_token": self.token})
            if self._message().get("type") != "auth_ok":
                raise PermissionError("Home Assistant rejected the websocket token")
            self._json({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
            while not self.stop.is_set():
                message = self._message()
                if not isinstance(message, dict) or message.get("id") != 1:
                    continue
                if message.get("type") == "result":
                    if message.get("success") is not True:
                        raise ConnectionError("Home Assistant refused the state subscription")
                    self.connected, self.error, self.failures = True, None, 0
                    LOG.info("N4 fallback: Home Assistant websocket connected")
                    if self.on_connect:
                        self.on_connect()
                elif message.get("type") == "event":
                    data = (message.get("event") or {}).get("data") if isinstance(message.get("event"), dict) else None
                    if isinstance(data, dict) and isinstance(data.get("entity_id"), str):
                        self.on_state(data["entity_id"], data.get("new_state"))
        finally:
            self.connected = False
            try: self.sock.close()
            except OSError: pass

    def run(self):
        while not self.stop.is_set():
            try:
                self.session()
            except ConnectionAbortedError:
                return
            except (OSError, ValueError, TypeError, AttributeError, struct.error) as exc:
                self.failures += 1
                self.error = f"{type(exc).__name__}: {exc}"[:160]
                delay = 300 if isinstance(exc, PermissionError) else min(60, 2 ** min(self.failures - 1, 6))
                if self.failures == 1 or self.failures % 10 == 0:
                    LOG.warning("N4 fallback: Home Assistant websocket down, polling instead; retrying in %s s: %s", delay, self.error)
                self.stop.wait(delay)


# ---------------------------------------------------------------------------
# What survives a restart
# ---------------------------------------------------------------------------

def clean_zone(zone):
    """A zone mapping row (gateway /api/config or a seed) the fallback can drive; None otherwise."""
    if not isinstance(zone, dict):
        return None
    zone_id, group, curve, serial = zone.get("zone_id"), zone.get("n4_group"), zone.get("warm_dim_curve"), zone.get("serial")
    if type(zone_id) is not int or not 0 < zone_id <= 65535 or not isinstance(group, str) or not group.strip() or len(group) > 127:
        return None
    return {"zone_id": zone_id, "name": zone["name"][:64] if isinstance(zone.get("name"), str) and zone["name"] else f"Zone {zone_id}",
            "n4_group": group.strip(), "serial": serial if type(serial) is int and 0 < serial <= 0xffffffff else 0,
            "warm_dim_curve": curve if type(curve) is int and (curve in range(5) or curve == WARM_DIM_NONE) else WARM_DIM_NONE}


class FallbackCache:
    """The last good gateway mapping and per-zone programming, kept in /data."""
    EMPTY = {"zones": [], "programs": {}, "timezone": None, "maintenance": False}

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.data = json.loads(json.dumps(self.EMPTY))
        self.saved_at = None
        if self.path is None:
            return
        try:
            stored = json.loads(self.path.read_text())
            self.data = {"zones": [z for z in map(clean_zone, stored.get("zones") or []) if z],
                         "programs": {str(k): v for k, v in (stored.get("programs") or {}).items() if self._program(v)},
                         "timezone": stored.get("timezone") if isinstance(stored.get("timezone"), dict) else None,
                         "maintenance": stored.get("maintenance") is True}
            self.saved_at = stored.get("saved_at") if type(stored.get("saved_at")) in (int, float) else None
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            LOG.warning("N4 fallback: cache unreadable, starting empty: %s", exc)

    @staticmethod
    def _program(value):
        return isinstance(value, dict) and type(value.get("selected")) is bool and isinstance(value.get("shows"), dict)

    def store(self, data):
        if data == self.data:
            return False
        self.data, self.saved_at = data, time.time()
        if self.path is not None:
            try:
                temporary = self.path.with_name(self.path.name + ".tmp")
                temporary.write_text(json.dumps(dict(data, saved_at=self.saved_at), separators=(",", ":")))
                os.replace(temporary, self.path)
            except OSError as exc:
                LOG.warning("N4 fallback: cache not saved: %s", exc)
        return True

    def program(self, zone_id):
        """A zone's cached programming, with show IDs as integers again."""
        program = self.data["programs"].get(str(zone_id))
        if program is None:
            return None
        try:
            return dict(program, shows={int(k): v for k, v in program["shows"].items()})
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------

class N4Fallback:
    """Decides who drives the lamps, and drives them through N4 while the gateway is unreachable.

    lib supplies the app's existing helpers: show_state, level16_to_x100, cct_close and match_light.
    clock(gateway_zone) returns (milliseconds since local midnight, zone name).
    """
    def __init__(self, panel, ha, n4, mode="auto", seeds=(), cache=None, lib=None, clock=None, events=None,
                 now=time.monotonic, engage_after=ENGAGE_AFTER_S):
        self.panel, self.ha, self.n4, self.lib, self.clock, self.events, self.now = panel, ha, n4, lib, clock, events, now
        self.mode = mode if mode in MODES else "auto"
        self.seeds = [z for z in map(clean_zone, seeds or []) if z]
        self.cache = cache or FallbackCache()
        self.engage_after = engage_after
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.stop = threading.Event()
        self.started = now()
        self.gateway_at = self.gateway_status = self.gateway_failed_at = None
        self.engaged, self.since, self.reason, self.blocked = False, None, None, None
        self.generation, self.released_at = 0, None
        self.history = deque(maxlen=20)
        self.zones, self.entities, self.lights, self.zstate = {}, {}, {}, {}
        self.need_sync, self.sync_error = False, None
        self.sync_due = 0.0
        self.poll_due = self.show_due = self.probe_due = self.remap_due = 0.0
        self.pending, self.written, self.group_at, self.write_at = {}, {}, {}, -math.inf
        self.last_write, self.writes, self.failures = None, 0, 0
        self.n4_state = {"reachable": None, "error": None, "checked_at": None, "hub": None}
        self.threads = []
        self._load_zones()

    # -- gateway facts -------------------------------------------------------

    def gateway_response(self, status):
        """Called for every HTTP response from the gateway, whatever its status: it is reachable."""
        with self.lock:
            self.gateway_at, self.gateway_status = self.now(), status
            if self.engaged and self.mode == "auto":
                self._release(f"the gateway answered (HTTP {status})")

    def gateway_failure(self):
        """Called when a gateway request ended without any HTTP response."""
        with self.lock:
            self.gateway_failed_at = self.now()

    def silent_s(self):
        return self.now() - (self.gateway_at if self.gateway_at is not None else self.started)

    def threshold_s(self):
        flapped = self.released_at is not None and self.now() - self.released_at < FLAP_WINDOW_S
        return max(self.engage_after, FLAP_ENGAGE_AFTER_S) if flapped else self.engage_after

    def maintenance(self):
        """Whether maintenance was active in the last status the gateway gave (this run, or the cached one)."""
        with self.panel.lock:
            entry = self.panel.cache.get("status")
        if entry and isinstance(entry[0], dict):
            diagnostics = entry[0].get("diagnostics")
            return isinstance(diagnostics, dict) and diagnostics.get("maintenance") is True
        return self.cache.data["maintenance"]

    # -- mapping and cache ---------------------------------------------------

    def _load_zones(self):
        zones = {z["zone_id"]: dict(z, source="gateway") for z in self.cache.data["zones"]}
        for seed in self.seeds:
            zones.setdefault(seed["zone_id"], dict(seed, source="seed"))
        with self.lock:
            self.zones = zones
            for stale in [z for z in self.zstate if z not in zones]:
                del self.zstate[stale]

    def remember(self):
        """Keep the gateway's latest mapping and programming for the next outage or restart."""
        with self.panel.lock:
            cache = self.panel.cache
            config, devices, status, timezone = (cache.get(k) for k in ("config", "devices", "status", "timezone"))
            programs = dict(self.panel.zone_programs)
        data = dict(self.cache.data, programs=dict(self.cache.data["programs"]))
        if config and isinstance(config[0], dict) and isinstance(config[0].get("zones"), list):
            data["zones"] = [z for z in map(clean_zone, config[0]["zones"]) if z]
        index = {z.get("zone"): z.get("zone_id") for z in (devices[0].get("zones") or [] if devices and isinstance(devices[0], dict) else [])
                 if isinstance(z, dict)}
        for zone, (program, _) in programs.items():
            if type(index.get(zone)) is int:
                data["programs"][str(index[zone])] = json.loads(json.dumps(program))
        known = {str(z["zone_id"]) for z in data["zones"]}
        data["programs"] = {k: v for k, v in data["programs"].items() if k in known}
        if status and isinstance(status[0], dict):
            diagnostics = status[0].get("diagnostics")
            data["maintenance"] = isinstance(diagnostics, dict) and diagnostics.get("maintenance") is True
        if timezone and isinstance(timezone[0], dict) and isinstance(timezone[0].get("timezone"), str):
            data["timezone"] = {"timezone": timezone[0]["timezone"]}
        if self.cache.store(data):
            self._load_zones()

    def _remap(self):
        """Match each zone to its Lutron light in Home Assistant."""
        snapshot = self.ha.snapshot()
        lights = snapshot.get("lights") if isinstance(snapshot, dict) and snapshot.get("available") else None
        if lights is None:
            return False
        with self.lock:
            zones = list(self.zones.values())
        entities = {}
        for zone in zones:
            light = self.lib.match_light(zone, lights)
            if light:
                entities[zone["zone_id"]] = light["entity_id"]
        with self.lock:
            self.entities = entities
        return True

    # -- engage / release ----------------------------------------------------

    def _note(self, event, reason):
        self.history.append({"at": time.time(), "event": event, "reason": reason})
        LOG.warning("N4 fallback %s: %s", event, reason)

    def _engage(self, reason):
        self.engaged, self.since, self.reason, self.blocked = True, time.time(), reason, None
        self.generation += 1
        self.need_sync, self.sync_error = True, None
        self.zstate.clear()
        self.written.clear()
        self._note("engaged", reason)

    def _release(self, reason):
        # From here nothing more is sent: queued writes are dropped and the
        # writer re-checks the generation immediately before each request.
        self.engaged, self.since, self.reason = False, None, None
        self.generation += 1
        self.pending.clear()
        self.need_sync = False
        self.released_at = self.now()
        self._note("released", reason)
        self.wake.notify_all()

    def evaluate(self):
        with self.lock:
            if self.mode == "off" or self.n4 is None:
                if self.engaged:
                    self._release("the fallback is off")
                return
            if self.mode == "force":
                if not self.engaged:
                    self._engage("n4_fallback is set to force")
                return
            if self.engaged:
                return
            silent, threshold = self.silent_s(), self.threshold_s()
            failed = self.gateway_failed_at is not None and self.now() - self.gateway_failed_at <= FAILURE_FRESH_S
            if silent < threshold or not failed:
                self.blocked = None
            elif self.maintenance():
                self._block("maintenance was active when the gateway went quiet")
            elif not self.zones:
                self._block("no zone mapping is cached or seeded")
            else:
                self._engage(f"the gateway has been unreachable for {round(silent)} s")

    def _block(self, reason):
        if self.blocked != reason:
            self.blocked = reason
            LOG.warning("N4 fallback not engaging: %s", reason)

    # -- deciding what a zone's lamp should do ---------------------------------

    def _knots(self, zone_id):
        program = self.cache.program(zone_id)
        if not program or not (program.get("selected") and program.get("show")):
            return None, None
        override = program.get("override_x100")
        return program["shows"].get(program["show"]), override if type(override) is int else None

    def _at_keyframe(self, knots, light, level):
        """Lutron reports a running show's next keyframe, so a state at a keyframe is the show itself."""
        kelvin = light.get("color_temp_kelvin") if light.get("color_mode") == "color_temp" else None
        for knot in knots:
            target = self.lib.level16_to_x100(knot["level"])
            if level == 0 or target == 0:
                if level == target:
                    return True
            elif abs(target - level) > 200:
                continue
            elif not knot["cct"]:
                return True
            elif kelvin and self.lib.cct_close(knot["cct"], kelvin):
                # Only a colour temperature at the keyframe's is the show; a colour or warm-dim command is not.
                return True
        return False

    def _show_sample(self, zone_id):
        knots, _ = self._knots(zone_id)
        local_ms, _ = self.clock(self.cache.data["timezone"]) if self.clock else (None, None)
        return self.lib.show_state(knots, local_ms) if knots and local_ms is not None else None

    def _desired(self, zone_id, light):
        """The N4 state for a zone given Lutron's latest state; updates the zone's show tracking."""
        zone, state = self.zones[zone_id], self.zstate.setdefault(zone_id, {"show": None, "override": None, "light": None})
        previous, state["light"] = state["light"], light
        if light["state"] not in ("on", "off"):
            return None  # unavailable or unknown: leave the lamp alone
        level = light_level(light)
        knots, override = self._knots(zone_id)
        if state["show"] is None:
            # First look since engaging: the cached programming says whether a show was selected.
            state["show"], state["override"] = bool(knots), override
        if state["show"]:
            if not knots or level is None:
                state["show"] = False
            elif self._at_keyframe(knots, light, level):
                pass
            elif state["override"] is not None and abs(state["override"] - level) <= 200:
                pass
            elif (previous and previous["state"] == "on" and light["state"] == "on" and light_level(previous) != level
                  and all(previous.get(k) == light.get(k) for k in ("color_mode", "color_temp_kelvin", "xy_color"))):
                state["override"] = level  # a brightness-only change: the show's colour keeps running
            else:
                state["show"] = False
            if not state["show"]:
                LOG.info("N4 fallback: zone %s left its Natural Show after a Lutron command", zone_id)
        if state["show"]:
            sample = self._show_sample(zone_id)
            if sample:
                return show_body(sample, state["override"])
        return lamp_body(light, zone["warm_dim_curve"])

    def apply(self, zone_id, light, force=False):
        with self.lock:
            if not self.engaged or zone_id not in self.zones or light is None:
                return
            state = self.zstate.get(zone_id)
            if not force and state and state["light"] == light:
                return
            body = self._desired(zone_id, light)
            if body is not None:
                self._submit(zone_id, body, force)

    def on_state(self, entity_id, new_state):
        """A Home Assistant state change (websocket or poll)."""
        light = light_from_state(new_state)
        if light is None:
            return
        with self.lock:
            self.lights[entity_id] = light
            zones = [z for z, e in self.entities.items() if e == entity_id] if self.engaged else []
        for zone_id in zones:
            self.apply(zone_id, light)

    def on_connect(self):
        with self.lock:
            if self.engaged:
                self.need_sync = True

    def _fetch(self, entity_id):
        return light_from_state(json.loads(self.ha.request("GET", "/states/" + entity_id)))

    def sync(self, force=True):
        """Bring every mapped zone to Lutron's current state."""
        try:
            if force or self.now() >= self.remap_due or not self.entities:
                if not self._remap():
                    raise ValueError("Home Assistant's Lutron lights are unavailable")
                self.remap_due = self.now() + REMAP_S
            with self.lock:
                entities = dict(self.entities)
            for zone_id, entity_id in entities.items():
                light = self._fetch(entity_id)
                if light is not None:
                    with self.lock:
                        self.lights[entity_id] = light
                    self.apply(zone_id, light, force)
            self.sync_error = None
            return True
        except (OSError, http.client.HTTPException, ValueError, TypeError) as exc:
            self.sync_error = f"{type(exc).__name__}: {exc}"[:160]
            return False

    def reapply_shows(self):
        """Move each zone that is following a Natural Show to the show's current sample."""
        with self.lock:
            for zone_id, state in self.zstate.items():
                if self.engaged and state["show"] and state["light"] and zone_id in self.zones:
                    sample = self._show_sample(zone_id)
                    if sample:
                        self._submit(zone_id, show_body(sample, state["override"]), False)

    # -- writing to N4: one pending state per group, latest wins --------------

    def _submit(self, zone_id, body, force):
        group = self.zones[zone_id]["n4_group"]
        if not force and group not in self.pending and self.written.get(group) == body:
            return
        self.pending[group] = {"zone_id": zone_id, "body": body, "tries": 0, "due": 0.0}
        self.wake.notify_all()

    def _next_write(self):
        """(group, job, generation) when a write is due; otherwise seconds to wait. Caller holds the lock."""
        if not self.engaged or not self.pending:
            return 1.0
        now = self.now()
        due, group = min((max(job["due"], self.group_at.get(g, -math.inf) + GROUP_INTERVAL_S, self.write_at + WRITE_GAP_S), g)
                         for g, job in self.pending.items())
        if due > now:
            return min(1.0, due - now)
        return group, self.pending.pop(group), self.generation

    def write_once(self):
        """Send at most one due write. Returns seconds to wait when nothing was due."""
        with self.lock:
            job = self._next_write()
            if not isinstance(job, tuple):
                return job
            # Taken under the lock while engaged. A release clears the queue under the same
            # lock, so after it only a request that had already left can still complete.
            group, item, generation = job
            self.group_at[group] = self.write_at = self.now()
        error = retry = None
        try:
            self.n4.set_group_state(group, item["body"])
        except N4AuthError as exc:
            error, retry = str(exc), 300
        except N4RateLimited as exc:
            error, retry = str(exc), 2 * GROUP_INTERVAL_S
        except (OSError, http.client.HTTPException) as exc:
            error, retry = f"{type(exc).__name__}: {exc}"[:160], min(30, 2 ** item["tries"])
        with self.lock:
            self.last_write = {"at": time.time(), "zone_id": item["zone_id"], "group": group, "ok": error is None,
                               "error": error, "state": item["body"]}
            if error is None:
                self.writes += 1
                self.n4_state.update(reachable=True, error=None, checked_at=time.time())
                if generation == self.generation:
                    self.written[group] = item["body"]
            else:
                self.failures += 1
                self.n4_state.update(reachable=False, error=error, checked_at=time.time())
                if item["tries"] == 0 or item["tries"] % 10 == 9:
                    LOG.warning("N4 fallback: write to group %s failed, retrying in %s s: %s", group, retry, error)
                # A newer command for the group replaces the retry.
                if self.engaged and generation == self.generation and group not in self.pending:
                    self.pending[group] = dict(item, tries=item["tries"] + 1, due=self.now() + retry)
        return 0.0

    def run_writer(self):
        while not self.stop.is_set():
            wait = self.write_once()
            if wait:
                with self.lock:
                    self.wake.wait(wait)

    # -- housekeeping ----------------------------------------------------------

    def probe(self):
        """Hub reachability for the Devices card. A read; it changes nothing on the hub."""
        if self.n4 is None:
            return
        try:
            hub = self.n4.hub()
            hub = hub if isinstance(hub, dict) else {}
            state = {"reachable": True, "error": None,
                     "hub": {k: str(hub[k])[:64] for k in ("Name", "InstallationName", "SerialNumber", "CPUVersion", "ModelName")
                             if type(hub.get(k)) in (str, int)}}
        except (OSError, http.client.HTTPException) as exc:
            state = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
        with self.lock:
            self.n4_state.update(state, checked_at=time.time())

    def tick(self):
        now = self.now()
        self.remember()
        self.evaluate()
        if now >= self.probe_due:
            self.probe_due = now + PROBE_S
            self.probe()
        with self.lock:
            engaged = self.engaged
            need_sync = engaged and self.need_sync and now >= self.sync_due
            if need_sync:
                self.need_sync = False
        if not engaged:
            return
        connected = bool(self.events and self.events.connected)
        if need_sync:
            if not self.sync():
                # Home Assistant isn't answering yet: try the full sync again shortly.
                with self.lock:
                    self.need_sync = self.engaged
                self.sync_due = now + POLL_S
            self.poll_due = now + POLL_S
        elif not connected and now >= self.poll_due:
            # Without the websocket, read the mapped lights instead.
            self.poll_due = now + POLL_S
            self.sync(force=False)
        if now >= self.show_due:
            self.show_due = now + SHOW_REAPPLY_S
            self.reapply_shows()

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:  # the controller must outlive any single bad reading
                LOG.exception("N4 fallback step failed")
            self.stop.wait(1)

    def start(self):
        if self.mode == "off" or self.n4 is None:
            return
        # Programming is cached for an outage even when no dashboard or sensor asks for it.
        self.panel.keep_programs = True
        targets = [(self.run, "n4-fallback"), (self.run_writer, "n4-writer")]
        if self.events:
            targets.append((self.events.run, "n4-ha-events"))
        for target, name in targets:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        if self.events:
            self.events.stop.set()
        with self.lock:
            self.wake.notify_all()

    def status(self):
        """Everything the dashboard, the MQTT sensor and /api/zones show. No secrets."""
        with self.lock:
            zones = []
            for zone_id, zone in sorted(self.zones.items()):
                state = self.zstate.get(zone_id) or {}
                zones.append({"zone_id": zone_id, "name": zone["name"], "n4_group": zone["n4_group"],
                              "warm_dim_curve": zone["warm_dim_curve"], "curve_name": CURVE_NAMES.get(zone["warm_dim_curve"]),
                              "source": zone["source"], "entity_id": self.entities.get(zone_id),
                              "show": bool(state.get("show")), "program_cached": self.cache.program(zone_id) is not None,
                              "state": self.written.get(zone["n4_group"]) if self.engaged else None})
            return {"mode": self.mode, "configured": self.n4 is not None, "engaged": self.engaged, "since": self.since,
                    "reason": self.reason, "blocked": self.blocked, "gateway_silent_s": round(self.silent_s(), 1),
                    "engage_after_s": self.threshold_s(), "gateway_status": self.gateway_status,
                    "n4": dict(self.n4_state, host=self.n4.host if self.n4 else None),
                    "last_write": self.last_write, "writes": self.writes, "failures": self.failures, "pending": len(self.pending),
                    "home_assistant": {"websocket": bool(self.events and self.events.connected),
                                       "error": self.sync_error or (self.events.error if self.events else None)},
                    "cache": {"saved_at": self.cache.saved_at, "zones": len(self.cache.data["zones"]),
                              "programs": len(self.cache.data["programs"])},
                    "zones": zones, "history": list(self.history)}


def seed_zones(value):
    """The n4_seed_zones option as zone rows; invalid rows are skipped with a warning."""
    rows = []
    for item in value if isinstance(value, list) else []:
        row = clean_zone(item)
        if row is None:
            LOG.warning("N4 fallback: ignoring an invalid seed zone")
        else:
            rows.append(row)
    return rows
