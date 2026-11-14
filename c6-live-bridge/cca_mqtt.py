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

    def __init__(self, service, connect, capacity=256):
        self.service, self.connect = service, connect
        self.pending = queue.Queue(maxsize=capacity)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.online = False
        self.seen = OrderedDict()
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
                             "device": {"identifiers": ["ccx_bridge_cca"], "name": "CCX Bridge CCA"}}
                self.client.publish(self.DISCOVERY, json.dumps(discovery, separators=(",", ":")), retain=True)
                with self.lock: self.online = True
                retry, next_state = 1, 0
                while not self.stop_event.is_set():
                    for topic, raw, retained in self.client.service():
                        self._message(topic, raw, retained)
                    if time.monotonic() >= next_state:
                        self._publish("state", self._state(), retain=True)
                        next_state = time.monotonic() + 10
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
