import tempfile
import unittest
from pathlib import Path

import cca_occupancy
import cca_occupancy_bridge as ob


class Service:
    def __init__(self): self.sent, self.fail = [], False
    def command(self, payload):
        if self.fail: raise RuntimeError("cca command pending")
        self.sent.append(payload); return {"accepted": True, "request_id": len(self.sent)}


def decoded(service):
    return [(d["occupied"], d["report"], p["raw_payload"][:2]) for p in service.sent for d in [cca_occupancy.decode(p["raw_payload"])]]


class BridgeTests(unittest.TestCase):
    MAP = [{"name": "Test", "entity_id": "binary_sensor.motion", "serial": "0A1B2C3D"}]

    def make(self, path=None, mode=None):
        raw = [dict(self.MAP[0], **({"on_unavailable": mode} if mode else {}))]
        service = Service()
        bridge = ob.OccupancyBridge(service, lambda e: None, ob.mappings(raw), path)
        return service, bridge, bridge.items[0]

    def test_mappings(self):
        self.assertEqual(ob.mappings(None), [])
        self.assertEqual(ob.mappings(self.MAP), [{"name": "Test", "entity_id": "binary_sensor.motion", "serial": 0x0a1b2c3d, "on_unavailable": "hold", "mqtt_topic": None}])
        bad = ([{}], [dict(self.MAP[0], serial="00000000")], [dict(self.MAP[0], entity_id="light.x")], [dict(self.MAP[0], extra=1)],
               [dict(self.MAP[0], on_unavailable="occupied")], self.MAP * 2, "x", [dict(self.MAP[0], name="")])
        for value in bad:
            with self.assertRaises(ValueError): ob.mappings(value)

    def test_mqtt_push(self):
        service = Service()
        bridge = ob.OccupancyBridge(service, lambda e: None, ob.mappings([dict(self.MAP[0], mqtt_topic="zigbee2mqtt/Hall motion")]), clock=lambda: 5.0)
        for raw in (b"not json", b"[]", b'{"occupancy":"yes"}', b'{"battery":90}'): bridge.offer("zigbee2mqtt/Hall motion", raw)
        bridge.offer("zigbee2mqtt/Other", b'{"occupancy":true}')
        self.assertEqual(service.sent, [])
        bridge.offer("zigbee2mqtt/Hall motion", b'{"occupancy":true,"battery":90}')
        bridge.step(bridge.items[0], "off", 5.5)        # stale read just after the push: held by the minimum gap
        bridge.step(bridge.items[0], "on", 8)
        self.assertEqual(decoded(service), [(True, "change", "89")])
        for topic in ("a/#", "a/+/b", "x" * 201):
            with self.assertRaises(ValueError): ob.mappings([dict(self.MAP[0], mqtt_topic=topic)])

    def test_changes_and_status(self):
        service, bridge, item = self.make()
        bridge.step(item, "on", 0)
        bridge.step(item, "on", 1)          # unchanged: nothing more
        bridge.step(item, "off", 1.5)       # inside the minimum gap: held
        self.assertEqual(decoded(service), [(True, "change", "89")])
        bridge.step(item, "off", 2)
        bridge.step(item, "off", 2 + bridge.STATUS_S - 1)
        self.assertEqual(decoded(service), [(True, "change", "89"), (False, "change", "8a")])
        bridge.step(item, "off", 2 + bridge.STATUS_S)
        self.assertEqual(decoded(service)[-1], (False, "status", "8a"))
        self.assertEqual(service.sent[0]["raw_repeat"], bridge.REPEAT)

    def test_flap_inside_gap_sends_only_latest(self):
        service, bridge, item = self.make()
        bridge.step(item, "on", 0)
        bridge.step(item, "off", 0.5); bridge.step(item, "on", 1); bridge.step(item, "on", 3)
        self.assertEqual(decoded(service), [(True, "change", "89")])

    def test_refused_command_retries_current_state(self):
        service, bridge, item = self.make()
        service.fail = True
        bridge.step(item, "on", 0)
        self.assertEqual(service.sent, []); self.assertIn("pending", item["error"])
        service.fail = False
        bridge.step(item, "off", 0.5)       # before the retry time
        bridge.step(item, "off", 1)
        self.assertEqual(decoded(service), [(False, "change", "89")])
        self.assertIsNone(item["error"])

    def test_unavailable(self):
        service, bridge, item = self.make()
        bridge.step(item, "unavailable", 0)
        self.assertEqual(service.sent, [])              # never known: nothing sent
        bridge.step(item, "on", 1)
        bridge.step(item, None, 10); bridge.step(item, "unknown", 10 + bridge.UNAVAILABLE_S)
        self.assertEqual(decoded(service), [(True, "change", "89")])   # hold
        service, bridge, item = self.make(mode="vacant")
        bridge.step(item, "on", 1)
        bridge.step(item, None, 10); bridge.step(item, None, 9 + bridge.UNAVAILABLE_S)
        self.assertEqual(len(service.sent), 1)
        bridge.step(item, None, 10 + bridge.UNAVAILABLE_S)
        self.assertEqual(decoded(service)[-1], (False, "change", "8a"))

    def test_restart_keeps_event_counter(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            service, bridge, item = self.make(path)
            bridge.step(item, "on", 0)
            service, bridge, item = self.make(path)
            bridge.step(item, "on", 0)                   # same state after restart: status, same type byte
            bridge.step(item, "off", 5)
            self.assertEqual(decoded(service), [(True, "status", "89"), (False, "change", "8a")])
            self.assertEqual(bridge.snapshot()[0]["occupied"], False)


if __name__ == "__main__": unittest.main()
