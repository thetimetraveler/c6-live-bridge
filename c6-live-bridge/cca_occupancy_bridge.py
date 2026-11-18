"""Send a Home Assistant binary sensor's state as a Lutron CCA occupancy sensor.

Each mapping pairs one entity with one sensor serial that the Lutron processor already
knows (activated with cca_occupancy.encode_announce). A change of state sends one change
report; while the state holds, a status report is repeated at the interval real sensors
use. Frames go out through the gateway's raw CCA command.
"""
import json
import logging
import re
import select
import threading
import time

import cca_occupancy

LOG = logging.getLogger("occupancy-bridge")
ENTITY = re.compile(r"binary_sensor\.[a-z0-9_]{1,200}")
SERIAL = re.compile(r"[0-9A-Fa-f]{8}")
MAX_BRIDGES = 16


def mappings(value):
    """Validate the add-on's occupancy_bridges option; invalid entries are rejected, not guessed."""
    if value is None: return []
    if not isinstance(value, list) or len(value) > MAX_BRIDGES: raise ValueError("occupancy_bridges must be a short list")
    result, serials, entities = [], set(), set()
    for item in value:
        if not isinstance(item, dict) or not set(item) <= {"name", "entity_id", "serial", "on_unavailable", "mqtt_topic"}:
            raise ValueError("invalid occupancy bridge")
        name, entity, serial = item.get("name"), item.get("entity_id"), item.get("serial")
        mode = item.get("on_unavailable", "hold")
        if not isinstance(name, str) or not 1 <= len(name) <= 64: raise ValueError("invalid occupancy bridge name")
        if not isinstance(entity, str) or not ENTITY.fullmatch(entity): raise ValueError("invalid occupancy bridge entity_id")
        if not isinstance(serial, str) or not SERIAL.fullmatch(serial) or not int(serial, 16): raise ValueError("invalid occupancy bridge serial")
        if mode not in ("hold", "vacant"): raise ValueError("invalid occupancy bridge on_unavailable")
        topic = item.get("mqtt_topic") or None
        if topic is not None and (not isinstance(topic, str) or len(topic) > 200 or any(c in topic for c in "#+\0")):
            raise ValueError("invalid occupancy bridge mqtt_topic")
        number = int(serial, 16)
        if number in serials or entity in entities: raise ValueError("duplicate occupancy bridge")
        serials.add(number); entities.add(entity)
        result.append({"name": name, "entity_id": entity, "serial": number, "on_unavailable": mode, "mqtt_topic": topic})
    return result


class OccupancyBridge:
    POLL_S = 1.0
    MIN_GAP_S = 2.0            # between transmissions for one sensor
    RETRY_S = 1.0              # after the gateway refuses a command
    STATUS_S = 900.0           # real sensors repeat their state about every 16 minutes
    UNAVAILABLE_S = 600.0
    REPEAT = 12

    def __init__(self, service, read_state, bridges, path=None, clock=time.monotonic, connect=None):
        self.service, self.read_state, self.path, self.clock = service, read_state, path, clock
        self.lock, self.stop_event, self.thread = threading.Lock(), threading.Event(), None
        self.connect, self.listener, self.pushes = connect, None, 0
        saved = self._load()
        self.items = []
        for bridge in bridges:
            old = saved.get("%08x" % bridge["serial"], {})
            sent = old.get("occupied") if isinstance(old.get("occupied"), bool) else None
            event = old.get("event") if type(old.get("event")) is int and old.get("event") >= 0 else 0
            # After a restart the first report is a status report when the state is unchanged.
            self.items.append(dict(bridge, source=None, desired=None, sent=sent, event=event, last_tx=None,
                                   not_before=0.0, bad_since=None, error=None, sends=0, restated=False))

    def _load(self):
        try:
            value = json.loads(self.path.read_text()) if self.path else {}
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError): return {}

    def _save(self):
        if not self.path: return
        value = {"%08x" % i["serial"]: {"occupied": i["sent"], "event": i["event"]} for i in self.items if i["sent"] is not None}
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(value)); tmp.replace(self.path)
        except OSError: pass

    def _send(self, item, occupied, report, event):
        payload = cca_occupancy.encode(item["serial"], occupied, report, event)
        try:
            self.service.command({"cmd": "raw", "raw_payload": payload, "raw_repeat": self.REPEAT})
            item["error"] = None; item["sends"] += 1
            return True
        except Exception as exc:  # gateway busy, offline or refusing: retry with the then-current state
            item["error"] = str(exc)[:200]
            return False

    def step(self, item, source, now):
        """Advance one mapping with the entity state just read (None when the read failed)."""
        item["source"] = source
        if source in ("on", "off"):
            item["desired"], item["bad_since"] = source == "on", None
        else:
            if item["bad_since"] is None: item["bad_since"] = now
            if item["on_unavailable"] == "vacant" and now - item["bad_since"] >= self.UNAVAILABLE_S: item["desired"] = False
        desired = item["desired"]
        if desired is None or now < item["not_before"]: return
        if desired != item["sent"]:
            if self._send(item, desired, "change", item["event"] + 1):
                item.update(sent=desired, event=item["event"] + 1, last_tx=now, not_before=now + self.MIN_GAP_S, restated=True)
                self._save()
            else: item["not_before"] = now + self.RETRY_S
        elif not item["restated"] or now - item["last_tx"] >= self.STATUS_S:
            ok = self._send(item, desired, "status", item["event"])
            item.update(last_tx=now, restated=True, not_before=now + (self.MIN_GAP_S if ok else 60.0))

    def run(self):
        while not self.stop_event.wait(self.POLL_S):
            for item in self.items:
                try: source = self.read_state(item["entity_id"])
                except Exception: source = None
                with self.lock:
                    try: self.step(item, source, self.clock())
                    except Exception: LOG.exception("occupancy bridge step failed")

    def offer(self, topic, raw):
        """A sensor's own MQTT state message ({"occupancy": true|false}); acted on at once."""
        try: value = json.loads(raw)
        except (ValueError, UnicodeDecodeError): return
        occupancy = value.get("occupancy") if isinstance(value, dict) else None
        if not isinstance(occupancy, bool): return
        for item in self.items:
            if item["mqtt_topic"] == topic:
                with self.lock:
                    self.pushes += 1
                    try: self.step(item, "on" if occupancy else "off", self.clock())
                    except Exception: LOG.exception("occupancy bridge step failed")

    def listen(self):
        """Subscribe to the mapped sensors' MQTT topics; the once-a-second read stays as the safety net."""
        topics = sorted({i["mqtt_topic"] for i in self.items if i["mqtt_topic"]})
        retry = 1
        while topics and not self.stop_event.is_set():
            client = None
            try:
                client = self.connect()
                for topic in topics: client.subscribe(topic)
                retry = 1
                while not self.stop_event.is_set():
                    select.select([client.sock], [], [], 1.0)
                    for topic, raw, _retained in client.service(): self.offer(topic, raw)
            except (OSError, ConnectionError, ValueError) as exc:
                LOG.warning("occupancy bridge MQTT unavailable: %s", exc)
            finally:
                if client:
                    try: client.close()
                    except Exception: pass
            self.stop_event.wait(retry); retry = min(retry * 2, 30)

    def start(self):
        if self.items and not self.thread:
            self.thread = threading.Thread(target=self.run, name="occupancy-bridge", daemon=True)
            self.thread.start()
            if self.connect and any(i["mqtt_topic"] for i in self.items):
                self.listener = threading.Thread(target=self.listen, name="occupancy-bridge-mqtt", daemon=True)
                self.listener.start()

    def stop(self): self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return [{"name": i["name"], "entity_id": i["entity_id"], "serial": i["serial"], "source": i["source"],
                     "occupied": i["sent"], "sends": i["sends"], "error": i["error"], "mqtt_topic": i["mqtt_topic"]} for i in self.items]
