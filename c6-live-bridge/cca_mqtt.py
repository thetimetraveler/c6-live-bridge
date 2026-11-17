"""Best-effort CCA MQTT integration; no command or event survives disconnect."""
from collections import OrderedDict
import json
import logging
import queue
import threading
import time

from cca import CcaError

LOG = logging.getLogger("c6-management.cca-mqtt")
ROOT = "ccx_bridge/cca/"


class CcaMqtt:
    AVAILABILITY = ROOT + "availability"
    COMMAND = ROOT + "command"
    PRESENCE = ROOT + "presence/set"
    STATE = ROOT + "state"
    DISCOVERY = "homeassistant/sensor/ccx_bridge_cca/status/config"
    OCCUPANCY_DISCOVERY = "homeassistant/binary_sensor/ccx_bridge_cca/occupancy_%08x/config"
    DEVICE = {"identifiers": ["ccx_bridge_cca"], "name": "CCX Bridge CCA"}

    def __init__(self, service, connect, capacity=256):
        self.service, self.connect = service, connect
        self.pending = queue.Queue(maxsize=capacity)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.online = False
        self.seen = OrderedDict()
        self.announced = {}
        self.occupancy_latest = {}
        self.thread = None
        self.client = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="cca-mqtt", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=12)

    def offer(self, suffix, payload):
        with self.lock:
            if not self.online: return False
            try: self.pending.put_nowait((suffix, payload))
            except queue.Full: return False
        return True

    def occupancy(self, entry):
        # Latest state per sensor, outside the bounded event queue: a burst of events cannot drop a change.
        with self.lock:
            if not self.online: return False
            self.occupancy_latest[entry["serial"]] = entry
        return True

    def _occupancy_available(self, serial, online):
        self.client.publish(ROOT + "occupancy/%08x/availability" % serial, "online" if online else "offline", retain=True)

    def _occupancy(self, entry):
        """Retained discovery, per-sensor availability and state for one sensor.

        A sensor is available only while the add-on is online and the sensor has been heard since
        the add-on started, so neither a stopped nor a restarted bridge shows an old state as current."""
        serial = entry["serial"]
        topic = ROOT + "occupancy/%08x" % serial
        name = entry.get("name") or "Occupancy %08X" % serial
        if self.announced.get(serial) != name:
            discovery = {"name": name, "unique_id": "ccx_bridge_cca_occupancy_%08x" % serial, "device_class": "occupancy",
                         "state_topic": topic, "availability_mode": "all",
                         "availability": [{"topic": self.AVAILABILITY}, {"topic": topic + "/availability"}],
                         "value_template": "{{ 'ON' if value_json.occupied else 'OFF' }}",
                         "json_attributes_topic": topic, "device": self.DEVICE}
            self.client.publish(self.OCCUPANCY_DISCOVERY % serial, json.dumps(discovery, separators=(",", ":")), retain=True)
            self.announced[serial] = name
        state = {k: entry.get(k) for k in ("occupied", "report", "rssi", "serial")}
        self.client.publish(topic, json.dumps(state, separators=(",", ":")), retain=True)
        self._occupancy_available(serial, True)

    def _occupancy_flush(self):
        with self.lock: latest, self.occupancy_latest = self.occupancy_latest, {}
        for _, entry in sorted(latest.items()): self._occupancy(entry)

    def snapshot(self):
        with self.lock: return {"online": self.online, "queued": self.pending.qsize()}

    def _publish(self, suffix, value, retain=False):
        self.client.publish(ROOT + suffix, json.dumps(value, separators=(",", ":")), retain=retain)

    def _discard(self):
        while True:
            try: self.pending.get_nowait()
            except queue.Empty: return

    def _state(self):
        snapshot = self.service.status_snapshot()
        status = snapshot.get("status") or {}
        peer = status.get("c6") or {}
        age = snapshot.get("status_age_s")
        fresh = isinstance(age, (int, float)) and age <= 10 and snapshot.get("error") is None
        return {"supported": snapshot.get("supported"), "online": bool(fresh and status.get("i2c", {}).get("online")),
                "radio_present": bool(peer.get("radio_present")), "mac": peer.get("mac"),
                "ota": snapshot.get("ota"), "error": snapshot.get("error")}

    def _message(self, topic, raw, retained):
        if retained or topic not in (self.COMMAND, self.PRESENCE) or len(raw) > 16384: return
        try:
            value = json.loads(raw)
            if not isinstance(value, dict): raise ValueError("expected an object")
        except (ValueError, UnicodeDecodeError):
            return
        if topic == self.PRESENCE:
            try: self.service.state(value)
            except (ValueError, CcaError): pass
            return
        request_id = value.get("id")
        if type(request_id) not in (str, int) or not 1 <= len(str(request_id)) <= 128:
            return
        key = str(request_id)
        if key in self.seen: return
        self.seen[key] = None
        while len(self.seen) > 1024: self.seen.popitem(last=False)
        result = {"id": request_id}
        try:
            if set(value) == {"id", "command"}:
                command = value["command"]
            elif set(value) == {"id", "preset_id"}:
                preset = next((p for p in self.service.get_config()["presets"] if p["id"] == value["preset_id"]), None)
                if not preset: raise ValueError("unknown preset")
                command = preset["command"]
            else: raise ValueError("expected command or preset_id")
            result.update(self.service.command(command))
        except (ValueError, CcaError) as exc:
            result.update(accepted=False, error=str(exc))
        # Results are intentionally not retained, queued or retried.
        self._publish("results", result)

    def run(self):
        retry = 1
        while not self.stop_event.is_set():
            try:
                self.client = self.connect()
                self.client.subscribe(self.COMMAND)
                self.client.subscribe(self.PRESENCE)
                self.client.publish(self.AVAILABILITY, "online", retain=True)
                discovery = {"name": "CCA radio", "unique_id": "ccx_bridge_cca_status",
                             "state_topic": self.STATE, "availability_topic": self.AVAILABILITY,
                             "value_template": "{{ 'online' if value_json.online else 'offline' }}",
                             "json_attributes_topic": self.STATE, "entity_category": "diagnostic",
                             "device": self.DEVICE}
                self.client.publish(self.DISCOVERY, json.dumps(discovery, separators=(",", ":")), retain=True)
                self.announced = {}
                for serial in getattr(self.service, "occupancy_unheard", lambda: [])(): self._occupancy_available(serial, False)
                with self.lock: self.online = True; self.occupancy_latest = {}
                # After going online, so a change during the snapshot is kept rather than lost.
                for entry in getattr(self.service, "occupancy_snapshot", lambda: [])(): self._occupancy(entry)
                retry, next_state = 1, 0
                while not self.stop_event.is_set():
                    for topic, raw, retained in self.client.service():
                        self._message(topic, raw, retained)
                    if time.monotonic() >= next_state:
                        self._publish("state", self._state(), retain=True)
                        next_state = time.monotonic() + 10
                    self._occupancy_flush()
                    try: suffix, payload = self.pending.get(timeout=.2)
                    except queue.Empty: continue
                    self._publish(suffix, payload)
            except (OSError, ConnectionError, ValueError) as exc:
                LOG.warning("CCA MQTT unavailable: %s", exc)
            finally:
                with self.lock: self.online = False
                self._discard()
                if self.client:
                    try: self.client.publish(self.AVAILABILITY, "offline", retain=True)
                    except (OSError, ConnectionError, ValueError): pass
                    self.client.close()
                    self.client = None
            self.stop_event.wait(retry)
            retry = min(30, retry * 2)
