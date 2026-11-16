import hashlib
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from cca import CcaError, CcaService
from cca_mqtt import CcaMqtt
from ccx_events import CcxFeed

MAC = "10:bd:a3:b1:5b:1c"
CAPS = {"schema_version": 1, "max_raw_bytes": 53, "ota_digest": True, "commands": [
    {"name": "bridge_level", "fields": [{"name": "device_id", "type": "integer", "min": 1, "max": 0xffffffff, "required": True},
                                        {"name": "level_pct", "type": "integer", "min": 0, "max": 100, "required": True}]},
    {"name": "ota_full_tx", "fields": [{"name": "device_id", "type": "integer", "min": 1, "max": 0xffffffff, "required": True},
                                       {"name": "raw_payload", "type": "hex", "min": 2, "max": 2, "required": True}]},
    {"name": "dim_config", "fields": [{"name": "raw_payload", "type": "hex", "min": 0, "max": 35}]}]}


class Gateway:
    def __init__(self):
        self.mac, self.boot, self.radio = MAC, 123, True
        self.buffer, self.expected, self.request_id = bytearray(), 0, 0
        self.control, self.digest, self.writes = {}, {}, []
        self.fail_after, self.code = None, None

    def request(self, path, method="GET", body=None, content_type=None, timeout=None):
        if self.code: return self.code, "application/json", b'{}'
        if path == "/api/cca/capabilities": result = CAPS
        elif path == "/api/cca/status":
            result = {"ready": True, "i2c": {"online": True}, "control": self.control,
                      "c6": {"mac": self.mac, "boot_id": self.boot, "radio_present": self.radio,
                             "ota_expected": self.expected, "ota_received": len(self.buffer)}}
        elif path == "/api/cca/ota": result = self.digest
        elif path == "/api/cca/state":
            self.writes.append(json.loads(body)); result = {"accepted": True, "revision": 1}
        elif path == "/api/cca/command":
            command = json.loads(body); self.writes.append(command); self.request_id += 1
            self.control = {"transfer_id": self.request_id, "transfer": "written", "receipt": "queued"}
            name = command["cmd"]
            if name == "ota_upload_reset": self.buffer, self.expected = bytearray(), 0
            if name == "ota_upload_start": self.expected = command["target_id"]
            if name == "ota_upload_chunk":
                assert command["target_id"] == len(self.buffer)
                self.buffer.extend(bytes.fromhex(command["raw_payload"]))
            if name == "ota_digest":
                self.digest = {"valid": True, "request_id": self.request_id, "boot_id": self.boot,
                               "expected": self.expected, "received": len(self.buffer),
                               "sha256": hashlib.sha256(self.buffer).hexdigest()}
            if name == self.fail_after:
                self.fail_after = None
                raise OSError("response lost")
            result = {"accepted": True, "request_id": self.request_id}
        else: raise AssertionError(path)
        return 200, "application/json", json.dumps(result).encode()


class FastService(CcaService):
    def _read(self, path, retry=False): return self._request(path)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.gateway = Gateway()
        self.service = FastService(self.gateway, self.tmp.name, timeout=.15)
        self.service.capabilities, self.service.supported = CAPS, True
        self.service.put_config({"expected_mac": MAC, "targets": [], "presets": []})

    def tearDown(self): self.service.stop(); self.tmp.cleanup()

    def action(self, payload):
        self.service.action(payload); self.service.worker.join(2)
        self.assertFalse(self.service.worker.is_alive())
        return self.service.status_snapshot()["ota"]

    def test_config_and_catalogue(self):
        config = {"expected_mac": MAC.upper(), "targets": [{"id": "office", "name": "Office", "device_id": "0x1234", "subnet": "0x22"}],
                  "presets": [{"id": "dim", "name": "Dim", "command": {"cmd": "bridge_level", "device_id": "0x1234", "level_pct": 50}}]}
        saved = self.service.put_config(config)
        self.assertEqual(saved["targets"][0]["device_id"], 0x1234)
        self.assertEqual(saved["presets"][0]["command"]["device_id"], 0x1234)
        self.assertEqual(json.loads((Path(self.tmp.name) / "cca.json").read_text()), saved)
        for command in ({"cmd": "bridge_level", "device_id": 1, "level_pct": 101},
                        {"cmd": "bridge_level", "device_id": 1, "level_pct": 1, "extra": 9},
                        {"cmd": "ota_full_tx", "device_id": 1, "raw_payload": "001122"},
                        {"cmd": "ota_full_tx", "device_id": 1, "raw_payload": "00"},
                        {"cmd": "dim_config", "raw_payload": "00" * 36}):
            with self.assertRaises(ValueError): self.service.command(command)
        self.assertEqual(self.gateway.writes, [])

    def test_shade_catalogue_is_capability_driven_and_flat(self):
        def field(name, maximum, required=True, minimum=0):
            return {"name": name, "type": "integer", "min": minimum, "max": maximum, "required": required}
        address = [field("subnet", 65535), field("target_id", 0xffffffff)]
        serial = field("device_id", 0xffffffff, minimum=1)
        commands = [
            {"name": "shade_level", "fields": address + [field("level_pct", 100), field("fade_qs", 255, False), field("raw_repeat", 255, False)]},
            {"name": "feedback_map", "fields": [serial] + address + [field("link_index", 99), field("component", 3, False, 3)]},
            {"name": "feedback_remove", "fields": [serial] + address},
            {"name": "feedback_list", "fields": []},
        ]
        # Older gateways remain limited to their advertised commands.
        with self.assertRaisesRegex(ValueError, "unknown CCA command"):
            self.service.command({"cmd": "feedback_list"})
        self.service.capabilities = dict(CAPS, commands=CAPS["commands"] + commands)
        values = [
            {"cmd": "shade_level", "subnet": "0x22", "target_id": "0x1234", "level_pct": 50, "raw_repeat": 6},
            {"cmd": "feedback_map", "device_id": "0x89", "target_id": 0x1234, "subnet": 34, "link_index": 0, "component": 3},
            {"cmd": "feedback_remove", "device_id": 0x89, "target_id": 0x1234, "subnet": 34},
            {"cmd": "feedback_list"},
        ]
        for value in values: self.assertTrue(self.service.command(value)["accepted"])
        self.assertEqual(self.gateway.writes[0], {"cmd": "shade_level", "subnet": 34, "target_id": 0x1234, "level_pct": 50, "raw_repeat": 6})
        self.assertEqual(self.gateway.writes[1]["link_index"], 0)
        for value in (dict(values[0], level_pct=101), dict(values[1], link_index=100), dict(values[1], component=2)):
            with self.assertRaises(ValueError): self.service.command(value)
        self.assertEqual(len(self.gateway.writes), 4)

    def test_capabilities_refresh_on_peer_feature_or_stream_change(self):
        def status(boot, supported=None):
            value = {"c6": {"boot_id": boot}}
            if supported is not None: value["capabilities"] = {"shade_feedback": supported}
            return value
        self.service.gateway_status = status(1)  # Old firmware omits the field.
        with patch.object(self.service, "_read", return_value=status(1)):
            self.service._status()
        self.assertIs(self.service.capabilities, CAPS)
        for value in (status(1, False), status(1, True), status(2, False)):
            self.service.capabilities = CAPS
            with patch.object(self.service, "_read", return_value=value): self.service._status()
            self.assertIsNone(self.service.capabilities)
        self.service.capabilities = CAPS
        self.service.stream_state("connecting")
        self.assertIs(self.service.capabilities, CAPS)  # First connection is not a reconnect.
        self.service.events.connected_once = True
        self.service.stream_state("connecting")
        self.assertIsNone(self.service.capabilities)
        self.service._capabilities()
        self.assertIsNotNone(self.service.capabilities)

    def test_identity_and_radio_guard_commands_and_presence(self):
        self.gateway.mac = "aa:bb:cc:dd:ee:ff"
        with self.assertRaisesRegex(CcaError, "MAC"): self.service.command({"cmd": "bridge_level", "device_id": 1, "level_pct": 20})
        with self.assertRaisesRegex(CcaError, "MAC"): self.service.state({"serial": "0x10", "state": "occupied"})
        self.gateway.mac, self.gateway.radio = MAC, False
        with self.assertRaisesRegex(CcaError, "radio"): self.service.command({"cmd": "bridge_level", "device_id": 1, "level_pct": 20})
        self.assertEqual(self.gateway.writes, [])

    def test_unknown_write_is_never_resent(self):
        self.gateway.fail_after = "bridge_level"
        with self.assertRaisesRegex(CcaError, "unknown"):
            self.service.command({"cmd": "bridge_level", "device_id": 1, "level_pct": 20})
        self.assertEqual([w["cmd"] for w in self.gateway.writes], ["bridge_level"])

    def test_native_ota_commands_are_available_but_cannot_mutate_a_guided_upload(self):
        command = {"cmd": "ota_full_tx", "device_id": 1, "raw_payload": "1234"}
        self.assertTrue(self.service.command(command)["accepted"])
        with self.service.lock: self.service.job["state"] = "staging"
        with self.assertRaisesRegex(CcaError, "staging buffer"): self.service.command(command)
        self.assertTrue(self.service.command({"cmd": "bridge_level", "device_id": 1, "level_pct": 20})["accepted"])
        self.assertEqual([w["cmd"] for w in self.gateway.writes], ["ota_full_tx", "bridge_level"])

    def test_staging_unknown_resume_and_explicit_start(self):
        body = bytes(range(90)); upload = self.service.file("ldf", b"h" * 128 + body)
        self.assertEqual(upload["upload"], {"size": 90, "sha256": hashlib.sha256(body).hexdigest()})
        self.gateway.fail_after = "ota_upload_chunk"
        failed = self.action({"action": "stage"})
        self.assertEqual(failed["state"], "error"); self.assertIn("unknown", failed["error"])
        self.assertEqual(len(self.gateway.buffer), 40)
        self.assertEqual([w["cmd"] for w in self.gateway.writes].count("ota_upload_chunk"), 1)
        self.assertEqual(self.action({"action": "resume"})["state"], "staged")
        self.assertEqual(bytes(self.gateway.buffer), body)
        self.assertEqual([w["cmd"] for w in self.gateway.writes].count("ota_upload_reset"), 1)
        self.assertNotIn("ota_full_tx", [w["cmd"] for w in self.gateway.writes])
        self.assertEqual(self.action({"action": "start", "device_id": "0x123456", "subnet": "0xabcd"})["state"], "transmit_queued")
        self.assertEqual(self.gateway.writes[-1], {"cmd": "ota_full_tx", "device_id": 0x123456, "raw_payload": "abcd"})

    def test_resume_after_restart_and_prefix_mismatch(self):
        body = b"a" * 50; self.service.file("body", body)
        self.gateway.expected, self.gateway.buffer = len(body), bytearray(b"a" * 19)
        self.service = FastService(self.gateway, self.tmp.name, timeout=.15)
        self.service.capabilities, self.service.supported = CAPS, True
        self.assertEqual(self.action({"action": "resume"})["state"], "staged")
        self.assertEqual(bytes(self.gateway.buffer), body)
        self.service.file("body", b"b" * 50)
        result = self.action({"action": "resume"})
        self.assertEqual(result["state"], "error"); self.assertIn("prefix differs", result["error"])
        self.assertFalse(any(w["cmd"] == "ota_upload_reset" for w in self.gateway.writes))

    def test_wrong_boot_digest_never_authorizes_resume(self):
        self.service.file("body", b"a" * 5); self.gateway.expected, self.gateway.buffer = 5, bytearray(b"a" * 5)
        original = self.gateway.request
        def stale(path, *args, **kwargs):
            code, ctype, data = original(path, *args, **kwargs)
            if path == "/api/cca/ota":
                value = json.loads(data); value["boot_id"] -= 1; data = json.dumps(value).encode()
            return code, ctype, data
        self.gateway.request = stale
        result = self.action({"action": "resume"})
        self.assertEqual(result["state"], "error"); self.assertIn("digest timed out", result["error"])
        self.assertEqual([w["cmd"] for w in self.gateway.writes], ["ota_digest"])

    def test_unsupported_and_auth(self):
        self.gateway.code = 404; self.service._capabilities()
        self.assertFalse(self.service.capabilities_snapshot()["supported"])
        self.gateway.code = 401
        with self.assertRaisesRegex(CcaError, "authorization"): self.service._status()

    def test_shared_sse_baselines_are_independent(self):
        saved = []
        self.service.publisher = SimpleNamespace(offer=lambda suffix, event: saved.append(event))
        feed = CcxFeed(None); feed.cca = self.service
        def env(boot, next_id, baseline=False, events=None):
            return {"schema_version": 1, "boot_id": boot, "next": next_id, "baseline": baseline, "events": events or []}
        def stream(items):
            data = b"".join(b"event: " + name.encode() + b"\ndata: " + json.dumps(payload).encode() + b"\n\n" for name, payload in items)
            with self.assertRaises(ConnectionError): feed._consume(io.BytesIO(data))
        stream([("ccx_events", env(1, 5, True)), ("cca_events", env(8, 10, True)),
                ("cca_events", env(8, 11, events=[{"id": 10, "kind": "cca.rx"}]))])
        self.assertEqual([e["id"] for e in self.service.read_events(0)["events"]], [10]); self.assertEqual(feed.read(0)["cursor"], 4)
        stream([("ccx_events", env(1, 5, True)), ("cca_events", env(8, 15, True))])
        self.assertEqual([e["kind"] for e in saved], ["cca.rx", "cca.stream_gap"])
        self.assertEqual(self.service.read_events(0)["cursor"], 14)

    def test_feedback_events_reach_results_cache_and_mqtt(self):
        mqtt = CcaMqtt(self.service, None)
        mqtt.online = True
        self.service.publisher = mqtt
        baseline = {"schema_version": 1, "boot_id": 8, "next": 1, "baseline": True, "events": []}
        self.service.accept_events(baseline, require_baseline=True)
        events = [
            {"id": 1, "kind": "cca.command", "request_id": 7, "stage": "sequence_finished"},
            {"id": 2, "kind": "cca.feedback", "request_id": 7, "serial": 123, "link_index": 0,
             "stage": "ack_received", "reason": "none", "ack_byte": 0, "quantized_target": 0, "attempt": 1},
            {"id": 3, "kind": "cca.feedback_mapping", "request_id": 8, "serial": 123, "target_id": 456,
             "subnet": 34, "link_index": 0, "component": 3, "current": 1, "total": 1},
            {"id": 4, "kind": "cca.rx", "raw_hex": "00"},
        ]
        self.service.accept_events({"schema_version": 1, "boot_id": 8, "next": 5, "events": events})
        cached = self.service.read_events(0)["events"]
        self.assertEqual([e["kind"] for e in self.service.status_snapshot()["results"]],
                         ["cca.command", "cca.feedback", "cca.feedback_mapping"])
        self.assertEqual(cached[1]["ack_byte"], 0)
        self.assertEqual(cached[2]["link_index"], 0)
        published = [mqtt.pending.get_nowait() for _ in events]
        self.assertEqual([suffix for suffix, _ in published], ["events"] * len(events))
        self.assertEqual([payload for _, payload in published], cached)


class MqttTests(unittest.TestCase):
    def test_retained_duplicate_and_preset(self):
        calls, sent = [], []
        service = SimpleNamespace(command=lambda cmd: (calls.append(cmd) or {"accepted": True, "request_id": 7}),
                                  get_config=lambda: {"presets": [{"id": "p", "command": {"cmd": "x"}}]}, state=lambda value: calls.append(value))
        mqtt = CcaMqtt(service, None); mqtt.client = SimpleNamespace(publish=lambda *a, **k: sent.append((a, k)))
        command = json.dumps({"id": "1", "command": {"cmd": "x"}}).encode()
        mqtt._message(mqtt.COMMAND, command, True); mqtt._message(mqtt.COMMAND, command, False); mqtt._message(mqtt.COMMAND, command, False)
        mqtt._message(mqtt.COMMAND, b'{"id":"2","preset_id":"p"}', False)
        mqtt._message(mqtt.PRESENCE, b'{"serial":1,"state":"occupied"}', True)
        self.assertEqual(calls, [{"cmd": "x"}, {"cmd": "x"}]); self.assertEqual(len(sent), 2)
        self.assertTrue(all(not kw.get("retain", False) for _, kw in sent)); self.assertEqual(json.loads(sent[0][0][1])["request_id"], 7)

    def test_partial_mqtt_packets(self):
        client = app.MqttClient("h", 1883, None, None, "x", "s")
        packet = app._mqtt_packet(0x31, app._mqtt_string("ccx_bridge/cca/command") + b'{}')
        class Socket:
            def __init__(self): self.chunks = []
            def recv(self, size): return self.chunks.pop(0)
        sock = Socket(); client.sock = sock; client.last_send = app.time.monotonic()
        def readable(read, write, errors, timeout): return ([sock] if sock.chunks else [], [], [])
        with patch("app.select.select", side_effect=readable):
            for part in (packet[:2], packet[2:9]):
                sock.chunks = [part]; self.assertEqual(client.service(), [])
            sock.chunks = [packet[9:]]
            self.assertEqual(client.service(), [("ccx_bridge/cca/command", b'{}', True)])

    def test_offline_events_not_queued(self):
        mqtt = CcaMqtt(None, None, capacity=1)
        self.assertFalse(mqtt.offer("events", {"id": 1})); self.assertEqual(mqtt.pending.qsize(), 0)

    def test_rejected_subscription_disconnects(self):
        client = app.MqttClient("h", 1883, None, None, "x", "s")
        packet = app._mqtt_packet(0x90, b'\x00\x01\x80')
        class Socket:
            def __init__(self): self.data = [packet]
            def recv(self, size): return self.data.pop(0)
        sock = Socket(); client.sock = sock; client.last_send = app.time.monotonic()
        def readable(read, write, errors, timeout): return ([sock] if sock.data else [], [], [])
        with patch("app.select.select", side_effect=readable), self.assertRaisesRegex(ConnectionError, "subscription"):
            client.service()


class RouteTests(unittest.TestCase):
    def test_fixed_routes_json_and_binary(self):
        calls, replies = [], []
        service = SimpleNamespace(capabilities_snapshot=lambda: CAPS, status_snapshot=lambda: {"supported": True},
                                  read_events=lambda after, boot: {"after": after, "boot_id": boot, "events": []},
                                  get_config=lambda: {"expected_mac": "", "targets": [], "presets": []},
                                  put_config=lambda value: value, command=lambda value: (calls.append(value) or {"accepted": True, "request_id": 3}),
                                  file=lambda fmt, value: (calls.append((fmt, value)) or {"state": "ready"}))
        handler = object.__new__(app.Handler)
        handler.client_address = ("172.30.32.2", 1)
        handler.server = SimpleNamespace(cca=service)
        handler.reply = lambda code, value: replies.append((code, value))
        handler.send = lambda code, body, content_type="application/json": replies.append((code, body))
        handler.path = "/api/cca/events?after=9&boot_id=12"; handler.do_GET()
        self.assertEqual(replies[-1], (200, {"after": 9, "boot_id": 12, "events": []}))
        handler.path = "/api/cca/events?after=9&after=10"; handler.do_GET(); self.assertEqual(replies[-1][0], 400)
        def body(value, ctype):
            handler.headers = Message(); handler.headers["Content-Type"] = ctype; handler.headers["Content-Length"] = str(len(value)); handler.rfile = io.BytesIO(value)
        handler.path = "/api/cca/command"; body(b'{"cmd":"button"}', "application/json"); handler.do_POST()
        self.assertEqual(replies[-1][1]["request_id"], 3)
        handler.path = "/api/cca/ota/file?format=body"; body(b'abc', "application/octet-stream"); handler.do_POST()
        self.assertEqual(calls[-1], ("body", b'abc'))
        handler.path = "/api/cca/ota/file?format=body&url=x"; body(b'abc', "application/octet-stream"); handler.do_POST()
        self.assertEqual(replies[-1][0], 400)


if __name__ == "__main__": unittest.main()
