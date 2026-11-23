import base64
import hashlib
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import n4_fallback as n4

OFFICE = {"zone_id": 30861, "name": "Office lamp", "n4_group": "office", "serial": 90130861, "warm_dim_curve": 3}
SITTING = {"zone_id": 31467, "name": "Sitting room lamp", "n4_group": "sitting room", "serial": 90131467, "warm_dim_curve": 0}
ENTITY = {30861: "light.office_lamp", 31467: "light.sitting_room_lamp"}
# One show, 06:00 dim and warm to 18:00 bright and cool (and back overnight).
PROGRAM = {"selected": True, "show": 7, "programmed": True, "override_x100": None,
           "shows": {7: [{"minute": 360, "level": 0x4000, "cct": 2000, "field2": None},
                         {"minute": 1080, "level": 0xFEFF, "cct": 4000, "field2": None}]}}
NOON_MS = 12 * 3600 * 1000


def state(value="on", **attributes):
    return {"state": value, "attributes": attributes}


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class StubGateway:
    def request(self, *args, **kwargs): raise OSError("offline")


class StubHomeAssistant:
    """Template lights for matching, and /states/<entity> for the current state."""
    def __init__(self, zones=(OFFICE, SITTING)):
        self.lights = [{"entity_id": ENTITY[z["zone_id"]], "zone_id": str(z["zone_id"]), "serial": None, "name": z["name"]} for z in zones]
        self.states = {e: state("off") for e in ENTITY.values()}
        self.available, self.calls = True, []
    def snapshot(self):
        return {"available": True, "lights": self.lights} if self.available else {"available": False, "error": "down"}
    def request(self, method, path, body=None):
        self.calls.append(path)
        if not self.available: raise OSError("supervisor unreachable")
        return json.dumps(self.states[path.rsplit("/", 1)[1]]).encode()
    def time_zone(self): return "UTC"


class FakeN4(ThreadingHTTPServer):
    """A Ketra N4 REST API: records every request and answers with the hub's envelope."""
    daemon_threads = True
    def __init__(self):
        self.requests, self.status, self.error = [], 200, "N4Status_NoStatus"
        super().__init__(("127.0.0.1", 0), FakeN4Handler)
        threading.Thread(target=self.serve_forever, args=(0.01,), daemon=True).start()
    @property
    def writes(self): return [r for r in self.requests if r["method"] == "PUT"]
    def close(self): self.shutdown(); self.server_close()


class FakeN4Handler(BaseHTTPRequestHandler):
    def handle_any(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.requests.append({"method": self.command, "path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        ok = self.server.status == 200
        content = {"Name": "Hub", "SerialNumber": "KP00001", "CPUVersion": "2.1.7"} if self.path.endswith("/Hub") else {}
        raw = json.dumps({"Success": ok, "Error": self.server.error if ok else "N4Status_Failed", "Content": content}).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    do_GET = do_PUT = handle_any
    def log_message(self, *args): pass


class FakeHA(ThreadingHTTPServer):
    """Home Assistant behind the Supervisor proxy: /core/api template and states, and /core/websocket."""
    daemon_threads = True
    def __init__(self, zones=(OFFICE, SITTING)):
        self.lights = [{"entity_id": ENTITY[z["zone_id"]], "integration": "lutron_caseta", "name": z["name"],
                        "state": "off", "zone_id": str(z["zone_id"])} for z in zones]
        self.states = {e: state("off") for e in ENTITY.values()}
        self.sockets, self.subscribed, self.auth = [], threading.Event(), []
        super().__init__(("127.0.0.1", 0), FakeHAHandler)
        threading.Thread(target=self.serve_forever, args=(0.01,), daemon=True).start()
    @property
    def host(self): return f"127.0.0.1:{self.server_address[1]}"
    def push(self, entity_id, new_state, event=True):
        self.states[entity_id] = new_state
        if event:
            payload = json.dumps({"id": 1, "type": "event", "event": {"event_type": "state_changed", "data": {
                "entity_id": entity_id, "old_state": None, "new_state": new_state}}}).encode()
            for sock in list(self.sockets):
                sock.sendall(ws_frame(payload))
    def drop(self):
        for sock in list(self.sockets):
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        self.sockets.clear()
        self.subscribed.clear()
    def close(self): self.drop(); self.shutdown(); self.server_close()


def ws_frame(payload, opcode=0x1):
    size = len(payload)
    head = bytes([0x80 | opcode]) + (bytes([size]) if size < 126 else bytes([126]) + struct.pack(">H", size))
    return head + payload


class FakeHAHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def reply(self, value, code=200):
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True
    def read_frame(self):
        first, second = self.rfile.read(2)
        size = second & 0x7f
        if size == 126: size = struct.unpack(">H", self.rfile.read(2))[0]
        mask = self.rfile.read(4)
        return first & 0x0f, bytes(b ^ mask[i & 3] for i, b in enumerate(self.rfile.read(size)))
    def websocket(self):
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.wfile.write((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        send = lambda value: self.wfile.write(ws_frame(json.dumps(value).encode()))
        send({"type": "auth_required"})
        token = json.loads(self.read_frame()[1]).get("access_token")
        self.server.auth.append(token)
        if token != "supervisor-token":
            return send({"type": "auth_invalid"})
        send({"type": "auth_ok"})
        request = json.loads(self.read_frame()[1])
        assert request == {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}, request
        self.server.sockets.append(self.connection)
        send({"id": 1, "type": "result", "success": True, "result": None})
        self.server.subscribed.set()
        try:
            while True:
                opcode, payload = self.read_frame()
                if opcode == 0x9: self.wfile.write(ws_frame(payload, 0xA))
        except (OSError, ValueError): pass
        self.close_connection = True
    def do_GET(self):
        if self.path == "/core/websocket": return self.websocket()
        if self.path == "/core/api/config": return self.reply({"time_zone": "UTC"})
        if self.path.startswith("/core/api/states/"):
            return self.reply(self.server.states[self.path.rsplit("/", 1)[1]])
        self.reply({}, 404)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.reply({"lights": self.server.lights, "sun": {}})
    def log_message(self, *args): pass


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition(): return True
        time.sleep(0.01)
    return False


class WarmDimTests(unittest.TestCase):
    def test_curves_match_the_gateway_firmware(self):
        # Reference values from the firmware's warm_dim.c, compiled and run natively.
        expected = {0: [(1, 1802), (1000, 2225), (5000, 2592), (10000, 2800)],
                    1: [(1, 1798), (1000, 2230), (5000, 2596), (10000, 2802)],
                    2: [(1, 1784), (1000, 2005), (5000, 2558), (10000, 2720)],
                    3: [(1, 1794), (1000, 2089), (5000, 2814), (10000, 3040)],
                    4: [(1, 2500), (1000, 2600), (5000, 3979), (10000, 4999)]}
        for curve, points in expected.items():
            for level, kelvin in points:
                self.assertEqual(n4.warm_dim_cct(curve, level), kelvin, (curve, level))
        self.assertEqual(n4.warm_dim_cct(99, 5000), 2592)  # an unknown curve evaluates as the default

    def test_native_white_mode_matches_the_gateway_firmware(self):
        self.assertEqual(n4.warm_dim_native(0, 5000), {"cct": 2446})
        self.assertEqual(n4.warm_dim_native(2, 2500), {"cct": 1867})
        self.assertEqual(n4.warm_dim_native(1, 1000), {"x": 0.5471, "y": 0.4113})
        self.assertEqual(n4.warm_dim_native(4, 5000), {"cct": 3979})


class MappingTests(unittest.TestCase):
    def light(self, value="on", **attributes): return n4.light_from_state(state(value, **attributes))

    def test_off_turns_the_group_off(self):
        self.assertEqual(n4.lamp_body(self.light("off"), 3), {"PowerOn": False, "TransitionTime": 500})

    def test_reported_colour_temperature_is_sent_with_the_level(self):
        # Lutron reports hs alongside a colour temperature; the colour mode decides.
        light = self.light(brightness=102, color_mode="color_temp", color_temp_kelvin=1980, hs_color=[30.7, 95.6], xy_color=[0.599, 0.382])
        self.assertEqual(n4.lamp_body(light, 3), {"PowerOn": True, "Brightness": 0.4, "CCT": 1980, "TransitionTime": 500})

    def test_hs_colour_is_sent_as_lutrons_xy(self):
        light = self.light(brightness=255, color_mode="hs", hs_color=[240, 100], xy_color=[0.136, 0.04])
        self.assertEqual(n4.lamp_body(light, 3), {"PowerOn": True, "Brightness": 1.0, "xChromaticity": 0.136,
                                                 "yChromaticity": 0.04, "TransitionTime": 500})

    def test_hs_without_xy_falls_back_to_the_zone_curve(self):
        self.assertEqual(n4.lamp_body(self.light(brightness=255, color_mode="hs", hs_color=[240, 100]), 3)["CCT"], 3040)

    def test_white_mode_uses_lutrons_warm_dim_evaluation(self):
        self.assertEqual(n4.lamp_body(self.light(brightness=255, color_mode="white"), 3)["CCT"], 3040)
        halogen = n4.lamp_body(self.light(brightness=26, color_mode="white"), 1)
        self.assertNotIn("CCT", halogen)
        self.assertEqual((halogen["xChromaticity"], halogen["yChromaticity"]), (n4.warm_dim_native(1, 1020)["x"], n4.warm_dim_native(1, 1020)["y"]))
        # A zone without a curve still warm-dims in white mode, on the default curve, as the gateway does.
        self.assertEqual(n4.lamp_body(self.light(brightness=255, color_mode="white"), n4.WARM_DIM_NONE)["CCT"], 2800)

    def test_level_only_uses_the_zone_curve(self):
        self.assertEqual(n4.lamp_body(self.light(brightness=128, color_mode="brightness"), 3),
                         {"PowerOn": True, "Brightness": 0.502, "CCT": n4.warm_dim_cct(3, 5020), "TransitionTime": 500})
        self.assertEqual(n4.lamp_body(self.light(brightness=128), n4.WARM_DIM_NONE),
                         {"PowerOn": True, "Brightness": 0.502, "TransitionTime": 500})

    def test_unavailable_or_levelless_states_send_nothing(self):
        self.assertIsNone(n4.lamp_body(self.light("unavailable"), 3))
        self.assertIsNone(n4.lamp_body(self.light("on"), 3))
        self.assertIsNone(n4.light_from_state("junk"))

    def test_show_sample_with_override_and_vibrancy(self):
        sample = {"level_x100": 6000, "cct": 3200, "vibrancy": 45}
        self.assertEqual(n4.show_body(sample, None), {"PowerOn": True, "Brightness": 0.6, "CCT": 3200, "Vibrancy": 0.45, "TransitionTime": 2000})
        self.assertEqual(n4.show_body(sample, 2500)["Brightness"], 0.25)
        self.assertEqual(n4.show_body(dict(sample, level_x100=0), None), {"PowerOn": False, "TransitionTime": 2000})


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeN4()
        self.addCleanup(self.hub.close)
        self.client = n4.N4Client(f"127.0.0.1:{self.hub.server_address[1]}", "s3cret-token", tls=False)

    def test_group_write_and_read_use_the_documented_calls(self):
        body = {"PowerOn": True, "Brightness": 0.4, "CCT": 1980, "TransitionTime": 500}
        self.client.set_group_state("sitting room", body)
        self.client.group_state("sitting room")
        self.assertEqual(self.client.hub()["SerialNumber"], "KP00001")
        put, get, hub = self.hub.requests
        self.assertEqual((put["method"], put["path"], put["body"]), ("PUT", "/ketra.cgi/api/v1/Groups/sitting%20room/State", body))
        self.assertEqual((get["method"], get["path"]), ("GET", "/ketra.cgi/api/v1/Groups/sitting%20room/State"))
        self.assertEqual(hub["path"], "/ketra.cgi/api/v1/Hub")
        self.assertEqual(base64.b64decode(put["auth"].split()[1]), b"na:s3cret-token")

    def test_errors_are_classified_and_never_carry_the_password(self):
        for status, error in ((403, n4.N4AuthError), (401, n4.N4AuthError), (429, n4.N4RateLimited), (500, n4.N4Error)):
            self.hub.status = status
            with self.assertRaises(error) as raised:
                self.client.set_group_state("office", {"PowerOn": False})
            self.assertNotIn("s3cret", str(raised.exception))
        self.hub.status, self.hub.error = 200, "N4Status_NoStatus"
        with patch.object(n4.N4Client, "request", side_effect=n4.N4Error("boom")):
            with self.assertRaises(n4.N4Error): self.client.hub()

    def test_address_and_password_validation(self):
        self.assertEqual(n4.n4_address("192.168.4.60"), ("192.168.4.60", 443))
        self.assertEqual(n4.n4_address("https://hub.local:8443/"), ("hub.local", 8443))
        for bad in ("", "http://192.168.4.60", "https://u:p@hub", "https://hub/path", None):
            with self.assertRaises(ValueError): n4.n4_address(bad)
        with self.assertRaises(ValueError): n4.N4Client("192.168.4.60", "", tls=False)

    def test_tls_context_suits_the_hub(self):
        import ssl
        verified = n4.n4_tls_context(True)
        self.assertEqual(verified.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(verified.check_hostname)
        self.assertTrue(verified.options & getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4))
        self.assertEqual(n4.n4_tls_context(False).verify_mode, ssl.CERT_NONE)


class FallbackCase(unittest.TestCase):
    """A controller stepped by hand against a fake clock, a stub Home Assistant and a fake N4 hub."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub = FakeN4()
        self.addCleanup(self.hub.close)
        self.clock, self.ha, self.local_ms = Clock(), StubHomeAssistant(), NOON_MS
        self.panel = app.Panel(StubGateway(), SimpleNamespace(program_events=lambda: {}, record=lambda payload: None))
        self.path = Path(self.tmp.name) / "n4-fallback.json"
        self.fallback = self.make()

    def make(self, mode="auto", seeds=(OFFICE, SITTING), client=True, panel=None):
        client = n4.N4Client(f"127.0.0.1:{self.hub.server_address[1]}", "s3cret-token", tls=False) if client else None
        return n4.N4Fallback(panel or self.panel, self.ha, client, mode, list(seeds), n4.FallbackCache(self.path), lib=app,
                             clock=lambda gateway_zone: (self.local_ms, "UTC"), now=self.clock)

    def silence(self, seconds, fallback=None):
        """Let the gateway stay unreachable: the collector keeps failing while time passes."""
        fallback = fallback or self.fallback
        self.clock.t += seconds
        fallback.gateway_failure()
        fallback.tick()

    def drain(self, fallback=None, limit=20):
        """Send everything queued, letting the pacing intervals pass."""
        fallback = fallback or self.fallback
        for _ in range(limit):
            wait = fallback.write_once()
            if wait:
                if not fallback.pending: return
                self.clock.t += wait

    def engage(self):
        self.silence(120)
        self.assertTrue(self.fallback.engaged)
        self.drain()

    def bodies(self, group=None):
        return [w["body"] for w in self.hub.writes if group is None or w["path"].endswith(f"/Groups/{group}/State")]

    def set(self, zone_id, new_state):
        self.ha.states[ENTITY[zone_id]] = new_state
        self.fallback.on_state(ENTITY[zone_id], new_state)


class EngageTests(FallbackCase):
    def test_engages_only_after_two_minutes_unreachable(self):
        self.silence(119)
        self.assertFalse(self.fallback.engaged)
        self.assertEqual(self.hub.writes, [])
        with self.assertLogs("c6-management", "WARNING") as logs:
            self.silence(1)
        self.assertTrue(self.fallback.engaged)
        self.assertIn("N4 fallback engaged: the gateway has been unreachable for 120 s", "\n".join(logs.output))
        self.assertEqual(self.fallback.status()["reason"], "the gateway has been unreachable for 120 s")

    def test_silence_without_a_failed_request_is_not_unreachable(self):
        self.clock.t += 600
        self.fallback.tick()
        self.assertFalse(self.fallback.engaged)

    def test_auth_and_other_http_errors_never_engage(self):
        for status in (401, 403, 500):
            fallback = self.make()
            for _ in range(10):
                self.clock.t += 60
                fallback.gateway_failure()          # a dropped connection now and then...
                fallback.gateway_response(status)   # ...but the gateway keeps answering
                fallback.tick()
                self.assertFalse(fallback.engaged, status)
        self.assertEqual(self.hub.writes, [])

    def test_maintenance_blocks_engaging(self):
        self.panel.cache["status"] = ({"diagnostics": {"maintenance": True}}, 0)
        with self.assertLogs("c6-management", "WARNING") as logs:
            self.silence(120)
            self.silence(600)
        self.assertFalse(self.fallback.engaged)
        self.assertEqual(sum("not engaging" in line for line in logs.output), 1)
        self.assertEqual(self.fallback.status()["blocked"], "maintenance was active when the gateway went quiet")
        self.panel.cache["status"] = ({"diagnostics": {"maintenance": False}}, 0)
        self.silence(1)
        self.assertTrue(self.fallback.engaged)

    def test_maintenance_flag_survives_a_restart(self):
        self.panel.cache["status"] = ({"diagnostics": {"maintenance": True}}, 0)
        self.fallback.remember()
        restarted = self.make(panel=app.Panel(StubGateway(), SimpleNamespace(program_events=lambda: {})))
        self.silence(300, restarted)
        self.assertFalse(restarted.engaged)

    def test_off_and_unconfigured_never_engage_and_force_always_does(self):
        for fallback in (self.make("off"), self.make(client=False), self.make(seeds=())):
            self.silence(600, fallback)
            self.assertFalse(fallback.engaged)
        forced = self.make("force")
        forced.tick()
        self.assertTrue(forced.engaged)
        forced.gateway_response(200)
        self.assertTrue(forced.engaged)
        self.assertEqual(forced.status()["reason"], "n4_fallback is set to force")

    def test_engaging_syncs_every_zone_to_lutron(self):
        self.ha.states[ENTITY[30861]] = state("on", brightness=255, color_mode="color_temp", color_temp_kelvin=3000)
        self.engage()
        self.assertEqual(self.bodies("office"), [{"PowerOn": True, "Brightness": 1.0, "CCT": 3000, "TransitionTime": 500}])
        self.assertEqual(self.bodies("sitting%20room"), [{"PowerOn": False, "TransitionTime": 500}])

    def test_sync_waits_for_home_assistant(self):
        self.ha.available = False
        self.silence(120)
        self.assertTrue(self.fallback.engaged)
        self.assertEqual(self.hub.writes, [])
        self.assertIn("unavailable", self.fallback.status()["home_assistant"]["error"])
        self.ha.available = True
        self.silence(5)
        self.drain()
        self.assertEqual(len(self.hub.writes), 2)


class ReleaseTests(FallbackCase):
    def test_release_on_first_gateway_answer_then_nothing_is_sent(self):
        self.engage()
        sent = len(self.hub.writes)
        self.set(30861, state("on", brightness=255, color_mode="color_temp", color_temp_kelvin=3000))
        self.set(31467, state("on", brightness=128))
        self.assertEqual(len(self.fallback.pending), 2)
        with self.assertLogs("c6-management", "WARNING") as logs:
            self.fallback.gateway_response(200)
        self.assertIn("N4 fallback released: the gateway answered (HTTP 200)", "\n".join(logs.output))
        self.assertFalse(self.fallback.engaged)
        self.assertEqual(self.fallback.pending, {})
        # Lutron keeps changing, the clock keeps running, shows would be due: still no writes.
        self.set(30861, state("off"))
        self.set(31467, state("on", brightness=10))
        for _ in range(10):
            self.clock.t += 30
            self.fallback.tick()
            self.fallback.write_once()
        self.assertEqual(len(self.hub.writes), sent)

    def test_any_gateway_answer_releases(self):
        self.engage()
        self.fallback.gateway_response(401)
        self.assertFalse(self.fallback.engaged)

    def test_hysteresis_after_a_release(self):
        self.engage()
        self.fallback.gateway_response(200)
        self.silence(120)
        self.assertFalse(self.fallback.engaged)   # a flapping gateway doesn't get the app back after two minutes
        self.assertEqual(self.fallback.status()["engage_after_s"], 300)
        self.silence(179)
        self.assertFalse(self.fallback.engaged)
        self.silence(1)
        self.assertTrue(self.fallback.engaged)
        self.assertEqual([h["event"] for h in self.fallback.status()["history"]], ["engaged", "released", "engaged"])

    def test_hysteresis_expires(self):
        self.engage()
        self.fallback.gateway_response(200)
        self.clock.t += n4.FLAP_WINDOW_S
        self.fallback.gateway_response(200)
        self.silence(120)
        self.assertTrue(self.fallback.engaged)

    def test_gateway_request_reports_reachability(self):
        seen = []
        hub_port = self.hub.server_address[1]
        gateway = app.Gateway(f"http://127.0.0.1:{hub_port}", "token")
        gateway.on_response, gateway.on_failure = seen.append, lambda: seen.append("failed")
        self.hub.status = 401
        gateway.request("/api/snapshot")
        closed = socket.socket(); closed.bind(("127.0.0.1", 0)); port = closed.getsockname()[1]; closed.close()
        unreachable = app.Gateway(f"http://127.0.0.1:{port}", "token", timeout=1)
        unreachable.on_response, unreachable.on_failure = seen.append, lambda: seen.append("failed")
        with self.assertRaises(OSError): unreachable.request("/api/snapshot")
        self.assertEqual(seen, [401, "failed"])


class WriteTests(FallbackCase):
    def test_latest_command_wins_and_bursts_coalesce(self):
        self.engage()
        self.hub.requests.clear()
        for brightness in (10, 60, 120, 200, 255):
            self.set(30861, state("on", brightness=brightness, color_mode="color_temp", color_temp_kelvin=2700))
        self.assertEqual(len(self.fallback.pending), 1)   # one slot per group, never a queue
        self.drain()
        self.assertEqual(self.bodies("office"), [{"PowerOn": True, "Brightness": 1.0, "CCT": 2700, "TransitionTime": 500}])

    def test_group_writes_are_paced(self):
        self.engage()
        self.hub.requests.clear()
        self.clock.t += 10
        self.set(30861, state("on", brightness=255))
        self.assertEqual(self.fallback.write_once(), 0.0)
        self.set(30861, state("on", brightness=128))
        self.set(31467, state("on", brightness=128))
        self.assertAlmostEqual(self.fallback.write_once(), n4.WRITE_GAP_S)     # another group: a short gap
        self.clock.t += n4.WRITE_GAP_S
        self.assertEqual(self.fallback.write_once(), 0.0)
        self.assertEqual(len(self.bodies("sitting%20room")), 1)
        self.assertGreater(self.fallback.write_once(), 0.9)                    # the same group: 1.5 s apart
        self.assertEqual(len(self.bodies("office")), 1)
        self.clock.t += n4.GROUP_INTERVAL_S
        self.assertEqual(self.fallback.write_once(), 0.0)
        self.assertEqual(len(self.bodies("office")), 2)

    def test_failed_write_retries_with_backoff_and_a_newer_command_replaces_it(self):
        self.engage()
        self.hub.requests.clear()
        self.clock.t += 10
        self.hub.status = 500
        with self.assertLogs("c6-management", "WARNING") as logs:
            self.set(30861, state("on", brightness=255))
            self.fallback.write_once()
        self.assertNotIn("s3cret", "\n".join(logs.output))
        self.assertEqual(self.fallback.pending["office"]["tries"], 1)
        self.assertEqual(self.fallback.pending["office"]["due"], self.clock.t + 1)
        self.assertFalse(self.fallback.status()["last_write"]["ok"])
        self.clock.t += 2
        self.fallback.write_once()
        self.assertEqual(self.fallback.pending["office"]["due"], self.clock.t + 2)
        self.set(30861, state("off"))
        self.assertEqual(self.fallback.pending["office"]["tries"], 0)
        self.hub.status = 200
        self.drain()
        self.assertEqual(self.bodies("office")[-1], {"PowerOn": False, "TransitionTime": 500})
        self.assertTrue(self.fallback.status()["last_write"]["ok"])
        self.assertEqual(self.fallback.pending, {})

    def test_rejected_token_backs_off_for_minutes(self):
        self.engage()
        self.clock.t += 10
        self.hub.status = 403
        with self.assertLogs("c6-management", "WARNING"):
            self.set(30861, state("on", brightness=255))
            self.fallback.write_once()
        self.assertEqual(self.fallback.pending["office"]["due"], self.clock.t + 300)

    def test_unchanged_state_is_not_rewritten(self):
        self.engage()
        self.hub.requests.clear()
        self.set(30861, state("off"))
        self.set(30861, state("off", friendly_name="renamed"))
        self.drain()
        self.assertEqual(self.hub.writes, [])

    def test_unavailable_lutron_light_leaves_the_lamp_alone(self):
        self.engage()
        self.hub.requests.clear()
        self.set(30861, state("unavailable"))
        self.drain()
        self.assertEqual(self.hub.writes, [])

    def test_polling_picks_up_changes_without_the_websocket(self):
        self.engage()
        self.hub.requests.clear()
        self.ha.states[ENTITY[30861]] = state("on", brightness=255, color_mode="white")
        self.silence(n4.POLL_S)
        self.drain()
        self.assertEqual(self.bodies(), [{"PowerOn": True, "Brightness": 1.0, "CCT": 3040, "TransitionTime": 500}])

    def test_home_assistant_reconnect_resyncs_every_zone(self):
        self.engage()
        self.hub.requests.clear()
        self.fallback.on_connect()
        self.silence(1)
        self.drain()
        self.assertEqual(len(self.hub.writes), 2)


class ShowTests(FallbackCase):
    def setUp(self):
        super().setUp()
        self.fallback.cache.store(dict(self.fallback.cache.data, zones=[OFFICE, SITTING],
                                       programs={"30861": json.loads(json.dumps(PROGRAM))}))
        self.fallback._load_zones()
        # Lutron reports a running show's next keyframe.
        self.keyframe = state("on", brightness=255, color_mode="color_temp", color_temp_kelvin=4000)
        self.ha.states[ENTITY[30861]] = self.keyframe

    def expected(self, local_ms, override=None):
        return n4.show_body(app.show_state(PROGRAM["shows"][7], local_ms), override)

    def test_show_runs_from_the_cached_program_and_is_reapplied(self):
        self.engage()
        self.assertEqual(self.bodies("office"), [self.expected(NOON_MS)])
        self.assertEqual(self.bodies("office")[0]["CCT"], 3000)
        self.assertTrue(self.fallback.status()["zones"][0]["show"])
        self.local_ms = NOON_MS + 3600 * 1000
        self.silence(n4.SHOW_REAPPLY_S)
        self.drain()
        self.assertEqual(self.bodies("office")[-1], self.expected(self.local_ms))
        self.assertEqual(len(self.bodies("office")), 2)
        # Nothing changed by the next slot: nothing is rewritten.
        self.silence(n4.SHOW_REAPPLY_S)
        self.drain()
        self.assertEqual(len(self.bodies("office")), 2)

    def test_brightness_only_change_overrides_the_shows_level(self):
        self.engage()
        self.set(30861, state("on", brightness=64, color_mode="color_temp", color_temp_kelvin=4000))
        self.drain()
        self.assertEqual(self.bodies("office")[-1], self.expected(NOON_MS, 2510))
        self.assertEqual(self.bodies("office")[-1]["CCT"], 3000)   # the show's colour keeps running
        self.assertTrue(self.fallback.status()["zones"][0]["show"])

    def test_any_other_command_ends_the_show(self):
        for command in (state("on", brightness=128, color_mode="color_temp", color_temp_kelvin=2700),
                        state("on", brightness=255, color_mode="hs", xy_color=[0.5, 0.4]), state("off")):
            self.fallback = self.make()
            self.ha.states[ENTITY[30861]] = self.keyframe
            self.clock.t += 10
            self.engage()
            self.set(30861, command)
            self.drain()
            self.assertEqual(self.bodies("office")[-1], n4.lamp_body(n4.light_from_state(command), 3))
            self.assertFalse(self.fallback.status()["zones"][0]["show"])
            before = len(self.hub.writes)
            self.local_ms += 3600 * 1000
            self.silence(n4.SHOW_REAPPLY_S)
            self.drain()
            self.assertEqual(len(self.hub.writes), before)
            self.local_ms = NOON_MS

    def test_show_steps_reported_by_lutron_keep_the_show(self):
        self.engage()
        self.set(30861, state("on", brightness=64, color_mode="color_temp", color_temp_kelvin=2000))   # the 06:00 keyframe
        self.drain()
        self.assertTrue(self.fallback.status()["zones"][0]["show"])
        self.assertEqual(self.bodies("office")[-1], self.expected(NOON_MS))

    def test_lutron_state_away_from_the_show_at_engage_means_it_ended(self):
        self.ha.states[ENTITY[30861]] = state("on", brightness=128, color_mode="color_temp", color_temp_kelvin=2700)
        self.engage()
        self.assertEqual(self.bodies("office"), [{"PowerOn": True, "Brightness": 0.502, "CCT": 2700, "TransitionTime": 500}])


class CacheTests(FallbackCase):
    def test_gateway_mapping_and_programs_survive_a_restart(self):
        fallback = self.make(seeds=())
        self.assertEqual(fallback.zones, {})
        self.panel.cache["config"] = ({"generation": 3, "hash": 1, "zones": [dict(OFFICE), dict(SITTING, n4_group=""), "junk"]}, 0)
        self.panel.cache["devices"] = ({"zones": [{"zone": 0, "zone_id": 30861}]}, 0)
        self.panel.cache["timezone"] = ({"timezone": "America/Los_Angeles"}, 0)
        self.panel.zone_programs[0] = (PROGRAM, 0)
        fallback.tick()
        self.assertEqual(sorted(fallback.zones), [30861])
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["zones"], [OFFICE])
        self.assertEqual(stored["timezone"], {"timezone": "America/Los_Angeles"})
        self.assertNotIn("s3cret", self.path.read_text())
        # A new process, the gateway already down: nothing in memory, everything from /data.
        restarted = self.make(seeds=(), panel=app.Panel(StubGateway(), SimpleNamespace(program_events=lambda: {})))
        self.assertEqual(restarted.zones[30861], dict(OFFICE, source="gateway"))
        self.assertEqual(restarted.cache.program(30861), PROGRAM)
        self.ha.states[ENTITY[30861]] = state("on", brightness=255, color_mode="color_temp", color_temp_kelvin=4000)
        self.silence(120, restarted)
        self.drain(restarted)
        self.assertEqual(self.bodies("office"), [n4.show_body(app.show_state(PROGRAM["shows"][7], NOON_MS), None)])

    def test_seed_fills_in_only_zones_the_gateway_has_not_reported(self):
        self.panel.cache["config"] = ({"zones": [dict(OFFICE, n4_group="office-from-gateway")]}, 0)
        self.fallback.tick()
        self.assertEqual(self.fallback.zones[30861]["n4_group"], "office-from-gateway")
        self.assertEqual(self.fallback.zones[31467], dict(SITTING, source="seed"))

    def test_seed_validation(self):
        with self.assertLogs("c6-management", "WARNING"):
            rows = n4.seed_zones([dict(OFFICE), {"zone_id": 0, "n4_group": "x"}, {"zone_id": 5, "n4_group": " "}, "junk"])
        self.assertEqual(rows, [OFFICE])
        self.assertEqual(n4.clean_zone({"zone_id": 7, "n4_group": "g"}),
                         {"zone_id": 7, "name": "Zone 7", "n4_group": "g", "serial": 0, "warm_dim_curve": n4.WARM_DIM_NONE})

    def test_corrupt_cache_starts_empty(self):
        self.path.write_text("{not json")
        with self.assertLogs("c6-management", "WARNING"):
            self.assertEqual(n4.FallbackCache(self.path).data["zones"], [])

    def test_programs_are_kept_for_an_outage(self):
        self.fallback.start()
        self.addCleanup(self.fallback.close)
        self.assertTrue(self.panel.keep_programs)


class WebsocketTests(unittest.TestCase):
    """The real Home Assistant client classes against a fake Supervisor proxy, and a fake hub."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub, self.server, self.clock = FakeN4(), FakeHA(), Clock()
        self.addCleanup(self.hub.close)
        self.addCleanup(self.server.close)
        self.ha = app.HomeAssistant("supervisor-token", ["lutron_caseta"], host=self.server.host, ttl=0)
        panel = app.Panel(StubGateway(), SimpleNamespace(program_events=lambda: {}))
        client = n4.N4Client(f"127.0.0.1:{self.hub.server_address[1]}", "s3cret-token", tls=False)
        self.fallback = n4.N4Fallback(panel, self.ha, client, "auto", [OFFICE, SITTING], n4.FallbackCache(), lib=app,
                                      clock=lambda gateway_zone: (NOON_MS, "UTC"), now=self.clock)
        self.events = self.fallback.events = n4.HaEvents("supervisor-token", self.fallback.on_state, self.fallback.on_connect, host=self.server.host)
        self.addCleanup(self.events.stop.set)

    def pump(self):
        """One controller step and whatever writes are due."""
        self.clock.t += 2
        self.fallback.tick()
        while True:
            wait = self.fallback.write_once()
            if wait and not self.fallback.pending: return
            self.clock.t += wait

    def writes(self, group): return [w["body"] for w in self.hub.writes if w["path"].endswith(f"/Groups/{group}/State")]

    def test_state_changes_arrive_over_the_websocket_and_fall_back_to_polling(self):
        threading.Thread(target=self.events.run, daemon=True).start()
        self.assertTrue(self.server.subscribed.wait(5))
        self.assertTrue(wait_for(lambda: self.events.connected))
        self.assertEqual(self.server.auth, ["supervisor-token"])
        # Not engaged: events are noted, nothing is written.
        self.server.push(ENTITY[30861], state("on", brightness=10))
        self.assertTrue(wait_for(lambda: ENTITY[30861] in self.fallback.lights))
        self.assertEqual(self.hub.writes, [])
        self.clock.t += 120
        self.fallback.gateway_failure()
        self.pump()
        self.assertTrue(self.fallback.engaged)
        self.assertEqual(self.writes("office"), [{"PowerOn": True, "Brightness": 0.0392, "CCT": n4.warm_dim_cct(3, 392), "TransitionTime": 500}])
        self.assertEqual(self.writes("sitting%20room"), [{"PowerOn": False, "TransitionTime": 500}])
        calls = len(self.hub.requests)
        # A pushed change is applied without any poll.
        self.server.push(ENTITY[31467], state("on", brightness=102, color_mode="color_temp", color_temp_kelvin=1980))
        self.assertTrue(wait_for(lambda: self.fallback.pending))
        self.pump()
        self.assertEqual(self.writes("sitting%20room")[-1], {"PowerOn": True, "Brightness": 0.4, "CCT": 1980, "TransitionTime": 500})
        # Other entities are ignored.
        self.server.push("light.kitchen", state("on", brightness=255))
        self.pump()
        self.assertEqual(len(self.hub.writes), 3)
        # The websocket drops: the next change is found by polling.
        self.server.drop()
        self.assertTrue(wait_for(lambda: not self.events.connected))
        self.server.push(ENTITY[31467], state("off"), event=False)
        self.clock.t += n4.POLL_S
        self.pump()
        self.assertEqual(self.writes("sitting%20room")[-1], {"PowerOn": False, "TransitionTime": 500})
        # It reconnects with backoff, and a reconnect resyncs.
        self.assertTrue(self.server.subscribed.wait(5))
        self.assertTrue(wait_for(lambda: self.events.connected and self.fallback.need_sync))
        self.assertTrue(self.fallback.status()["home_assistant"]["websocket"])
        self.assertGreater(len(self.hub.requests), calls)

    def test_rejected_token_is_not_retried_in_a_loop(self):
        self.events.token = "wrong"
        with self.assertLogs("c6-management", "WARNING") as logs:
            thread = threading.Thread(target=self.events.run, daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: self.events.error))
        self.events.stop.set()
        self.assertIn("PermissionError", self.events.error)
        self.assertIn("retrying in 300 s", "\n".join(logs.output))
        self.assertEqual(len(self.server.auth), 1)

    def test_template_carries_colour_mode_and_xy(self):
        self.server.lights[0].update(color_mode="hs", hs_color=[30.7, 95.6], xy_color=[0.599, 0.382], brightness=102, state="on")
        light = self.ha.snapshot()["lights"][0]
        self.assertEqual((light["color_mode"], light["hs_color"], light["xy_color"]), ("hs", [30.7, 95.6], [0.599, 0.382]))
        self.assertIn("'color_mode'", app.HA_TEMPLATE)
        self.assertIn("'xy_color'", app.HA_TEMPLATE)


class WiringTests(unittest.TestCase):
    """As main() wires it: the real Gateway, Panel and threads; only the hub and Home Assistant are fakes."""
    def test_unreachable_gateway_engages_and_its_return_releases(self):
        hub, server = FakeN4(), FakeHA()
        self.addCleanup(hub.close)
        self.addCleanup(server.close)
        probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
        gateway = app.Gateway(f"http://127.0.0.1:{port}", "gateway-token", timeout=1)
        panel = app.Panel(gateway, SimpleNamespace(program_events=lambda: {}, record=lambda payload: None))
        ha = app.HomeAssistant("supervisor-token", ["lutron_caseta"], host=server.host, ttl=0)
        client = n4.N4Client(f"127.0.0.1:{hub.server_address[1]}", "s3cret-token", tls=False)
        fallback = n4.N4Fallback(panel, ha, client, "auto", [OFFICE, SITTING], n4.FallbackCache(), lib=app,
                                 clock=lambda gateway_zone: (NOON_MS, "UTC"), engage_after=0.2)
        fallback.events = n4.HaEvents("supervisor-token", fallback.on_state, fallback.on_connect, host=server.host)
        panel.fallback = fallback
        gateway.on_response, gateway.on_failure = fallback.gateway_response, fallback.gateway_failure
        server.states[ENTITY[30861]] = state("on", brightness=255, color_mode="white")
        with patch.object(n4, "GROUP_INTERVAL_S", 0.05), patch.object(n4, "WRITE_GAP_S", 0.01):
            fallback.start()
            self.addCleanup(fallback.close)
            self.assertIsNone(panel.archive_poll_once())      # the collector's request fails: nothing listens there
            self.assertTrue(wait_for(lambda: len(hub.writes) >= 2), fallback.status())
            self.assertTrue(fallback.engaged)
            self.assertIn({"PowerOn": True, "Brightness": 1.0, "CCT": 3040, "TransitionTime": 500}, [w["body"] for w in hub.writes])
            server.push(ENTITY[30861], state("off"))
            self.assertTrue(wait_for(lambda: hub.writes[-1]["body"] == {"PowerOn": False, "TransitionTime": 500}))
            self.assertTrue(all(r["path"].startswith("/ketra.cgi/api/v1/") for r in hub.requests))
            # The gateway comes back (here answering 401): control is released on that very response.
            back = FakeN4.__new__(FakeN4)
            back.requests, back.status, back.error = [], 401, ""
            ThreadingHTTPServer.__init__(back, ("127.0.0.1", port), FakeN4Handler)
            threading.Thread(target=back.serve_forever, args=(0.01,), daemon=True).start()
            self.addCleanup(back.close)
            panel.next_poll = 0
            panel.archive_poll_once()
            self.assertFalse(fallback.engaged)
            sent = len(hub.writes)
            server.push(ENTITY[30861], state("on", brightness=128))
            server.push(ENTITY[31467], state("on", brightness=128))
            time.sleep(0.5)
            self.assertEqual(len(hub.writes), sent)
            # Nothing the fallback did reached the gateway: only the collector's own read is there.
            self.assertEqual([(r["method"], r["path"].split("?")[0]) for r in back.requests], [("GET", "/api/snapshot")])


class VisibilityTests(FallbackCase):
    def test_zone_view_says_the_app_is_driving(self):
        lights = [app.HomeAssistant.light(dict(l, state="on", brightness=255)) for l in self.ha.lights]
        clock = lambda gateway_zone: (NOON_MS, "UTC")
        idle = app.fallback_zones(self.panel.zone_summaries({"available": True, "lights": lights}, clock), self.fallback.status(), lights)
        self.assertEqual(idle["fallback"]["engaged"], False)
        self.assertEqual(idle["zones"], [])
        self.engage()
        view = app.fallback_zones(self.panel.zone_summaries({"available": True, "lights": lights}, clock), self.fallback.status(), lights)
        self.assertEqual([z["zone_id"] for z in view["zones"]], [30861, 31467])
        for zone in view["zones"]:
            self.assertEqual(zone["verdict"], {"tone": "warn", "title": "App fallback: the app, not the gateway, is driving this lamp"})
            self.assertEqual(zone["driver"], "app")
            self.assertEqual(zone["lutron"]["level_x100"], 10000)
        self.assertIn("N4 group office", view["zones"][0]["checks"][0]["detail"])
        self.assertEqual(view["fallback"]["reason"], "the gateway has been unreachable for 120 s")

    def test_status_reports_the_hub_and_last_write_without_secrets(self):
        self.assertEqual(app.fallback_status(self.panel)["configured"], False)
        self.panel.fallback = self.fallback
        self.engage()
        status = app.fallback_status(self.panel)
        self.assertTrue(status["n4"]["reachable"])
        self.assertEqual(status["n4"]["hub"]["SerialNumber"], "KP00001")
        self.assertEqual(status["last_write"]["ok"], True)
        self.assertEqual(status["zones"][0]["curve_name"], "Finiré 3000")
        self.assertNotIn("s3cret", json.dumps(status))
        self.hub.status = 500
        self.clock.t += n4.PROBE_S
        self.fallback.tick()
        self.assertFalse(self.fallback.status()["n4"]["reachable"])

    def test_binary_sensor_is_published_and_refreshed(self):
        self.panel.fallback = self.fallback
        published = []
        sensors = app.ShowSensors(self.panel, self.ha, lambda: None)
        sensors.client = SimpleNamespace(publish=lambda topic, payload, retain=False: published.append((topic, payload, retain)))
        sensors.publish_fallback()
        sensors.publish_fallback()
        topic, config, retain = published[0]
        config = json.loads(config)
        self.assertEqual(topic, "homeassistant/binary_sensor/ccx_bridge_app/n4_fallback_active/config")
        self.assertEqual((config["device"]["name"], config["name"]), ("CCX Bridge", "N4 fallback active"))
        self.assertNotIn("availability_topic", config)   # the shared topic goes offline with the gateway
        self.assertEqual(config["expire_after"], 180)
        self.assertEqual(len(published), 2)
        self.assertEqual(json.loads(published[1][1])["active"], "OFF")
        self.engage()
        sensors.publish_fallback()
        payload = json.loads(published[2][1])
        self.assertEqual((payload["active"], payload["attributes"]["reason"]), ("ON", "the gateway has been unreachable for 120 s"))
        self.assertEqual(payload["attributes"]["zones"], ["Office lamp", "Sitting room lamp"])
        sensors.fallback_due = 0
        sensors.publish_fallback()
        self.assertEqual(len(published), 4)


if __name__ == "__main__":
    unittest.main()
