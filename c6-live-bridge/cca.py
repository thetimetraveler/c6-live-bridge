"""CCA ingress coordinator: cached reads, one command path, and resumable staging."""
from collections import deque
import cca_occupancy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import threading
import time

from ccx_events import CcxFeed

MAX_BODY = 110 * 1024
MAX_NODE_IMAGE = 1024 * 1024
MIN_NODE_IMAGE = 8192
CCA_NODE_MAC = "10:bd:a3:b1:5b:1c"
CHUNK = 40
MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z")
HEX = re.compile(r"(?:[0-9a-fA-F]{2})*\Z")
ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# Device layouts a target can be drawn as ("none": a plain target): Picos with their buttons, shades and dimmers.
PICO_LAYOUTS = {"none": (), "pj2-2b": ("on", "off"), "pj2-2brl": ("on", "raise", "lower", "off"),
                "pj2-3b": ("on", "fav", "off"), "pj2-3brl": ("on", "raise", "fav", "lower", "off"),
                "pj2-4b": ("1", "2", "3", "4"), "shade": (), "dimmer": ()}
PICO_BUTTONS = {b for keys in PICO_LAYOUTS.values() for b in keys}


class CcaError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def uint(value, label, maximum=0xffffffff, minimum=0):
    if isinstance(value, str):
        if not re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", value):
            raise ValueError(f"invalid {label}")
        value = int(value, 16 if value.lower().startswith("0x") else 10)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {label}")
    return value


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try: temp.unlink()
        except FileNotFoundError: pass


class CcaService:
    """All CCA writes go through one lock; reads come from this service's cache."""
    def __init__(self, gateway, directory="/data", publisher=None, poll_interval=3, timeout=30):
        self.gateway = gateway
        self.directory = Path(directory)
        self.config_path = self.directory / "cca.json"
        self.upload_path = self.directory / "cca-upload.bin"
        self.lock = threading.RLock()
        self.operation = threading.RLock()
        self.node_upload_lock = threading.Lock()
        self.node_in_flight = self.node_awaiting_status = False
        self.node_finished_at = 0.0
        self.stop_event, self.cancel_staging = threading.Event(), threading.Event()
        self.poll_interval, self.timeout = poll_interval, timeout
        self.publisher = publisher
        self.events = CcxFeed(None, self)
        self.results = deque(maxlen=128)
        self.occupancy = {}
        self.occupancy_path = self.directory / "cca-occupancy.json"
        self.occupancy_known = set()
        self.config = {"expected_mac": "", "targets": [], "presets": []}
        self.capabilities = None
        self.gateway_status = None
        self.status_at = None
        self.error = None
        self.supported = None
        self.job = {"state": "idle", "error": None, "staged": 0, "expected": 0, "upload": None}
        self.thread = self.worker = None
        self.next_read = 0.0
        self._load()

    def _load(self):
        try:
            self.config = self._validate_config(json.loads(self.config_path.read_text()))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.error = "CCA configuration could not be read"
        try:
            known = json.loads(self.occupancy_path.read_text())
            self.occupancy_known = {v for v in known if type(v) is int and 0 < v <= 0xffffffff} if isinstance(known, list) else set()
        except (OSError, ValueError):
            pass
        try:
            if 0 < self.upload_path.stat().st_size <= MAX_BODY:
                body = self.upload_path.read_bytes()
                self.job["upload"] = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                self.job["expected"] = len(body)
                self.job["state"] = "ready"
        except FileNotFoundError:
            pass
        except OSError:
            self.error = "CCA upload could not be read"

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread = threading.Thread(target=self.run, name="cca-status", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.cancel_staging.set()
        if self.thread: self.thread.join(timeout=7)
        if self.worker: self.worker.join(timeout=7)

    def run(self):
        while not self.stop_event.is_set():
            if self.operation.acquire(blocking=False):
                try:
                    if self.capabilities is None or self.supported is False:
                        try: self._capabilities()
                        except CcaError as exc:
                            with self.lock: self.error = str(exc)
                    if self.supported is not False:
                        try: self._status()
                        except CcaError as exc:
                            with self.lock: self.error = str(exc)
                finally:
                    self.operation.release()
            self.stop_event.wait(self.poll_interval if self.supported is not False else 60)

    def _request(self, path, method="GET", payload=None):
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        try:
            code, _, raw = self.gateway.request(path, method, body, "application/json" if body is not None else None)
        except (OSError, ValueError, http.client.HTTPException):
            if method != "GET":
                raise CcaError("Gateway request interrupted; outcome unknown; command was not retried", 502) from None
            raise CcaError("Gateway unavailable", 502) from None
        if code in (404, 405): raise CcaError("CCA is unsupported by this gateway", 404)
        if code in (401, 403): raise CcaError("Gateway authorization failed", 502)
        if code == 503: raise CcaError("Gateway busy; try again", 503)
        if code < 200 or code >= 300:
            raise CcaError(f"Gateway returned HTTP {code}", 502 if code >= 500 else code)
        try: result = json.loads(raw)
        except (ValueError, UnicodeDecodeError): raise CcaError("Invalid gateway response", 502) from None
        if not isinstance(result, dict): raise CcaError("Invalid gateway response", 502)
        return result

    def _read(self, path, retry=False):
        deadline = time.monotonic() + self.timeout
        while True:
            delay = self.next_read - time.monotonic()
            if delay > 0:
                if self.stop_event.wait(delay): raise CcaError("CCA service stopped")
            try:
                data = self._request(path)
                self.next_read = time.monotonic() + .3
                return data
            except CcaError as exc:
                self.next_read = time.monotonic() + max(.3, min(2, getattr(self.gateway, "retry_after", 0) or 1))
                if not retry or exc.status != 503 or time.monotonic() >= deadline:
                    raise

    def _capabilities(self):
        try: data = self._read("/api/cca/capabilities")
        except CcaError as exc:
            if exc.status != 404: raise
            with self.lock:
                self.supported = False
                self.capabilities = {"schema_version": 1, "supported": False, "commands": []}
                self.error = None
            self.stream_state("unsupported")
            return
        if data.get("schema_version") != 1 or not isinstance(data.get("commands"), list):
            raise CcaError("Invalid CCA capabilities", 502)
        with self.lock:
            self.capabilities = data
            self.supported = True
            self.error = None

    def _status(self, retry=False):
        started = time.monotonic()
        data = self._read("/api/cca/status", retry=retry)
        with self.lock:
            if self.gateway_status is not None:
                def marker(status):
                    peer = status.get("c6") or {}
                    capabilities = status.get("capabilities") or {}
                    return (peer.get("boot_id") if isinstance(peer, dict) else None,
                            capabilities.get("shade_feedback") if isinstance(capabilities, dict) else None)
                if marker(data) != marker(self.gateway_status):
                    self.capabilities = None
            self.gateway_status, self.status_at, self.error = data, time.monotonic(), None
            if not self.node_in_flight and started > self.node_finished_at:
                self.node_awaiting_status = False
        return data

    def capabilities_snapshot(self):
        with self.lock:
            return dict(self.capabilities or {"schema_version": 1, "supported": None, "commands": []})

    def status_snapshot(self):
        with self.lock:
            return {"supported": self.supported, "status": self.gateway_status,
                    "status_age_s": None if self.status_at is None else max(0, time.monotonic() - self.status_at),
                    "ota": dict(self.job), "error": self.error, "results": list(self.results),
                    "occupancy": self._occupancy_list(),
                    "occupancy_bridges": self.occupancy_bridge.snapshot() if getattr(self, "occupancy_bridge", None) else [],
                    "node_upload": {"in_flight": self.node_in_flight, "awaiting_status": self.node_awaiting_status}}

    def node_update(self, image):
        if not isinstance(image, bytes) or not MIN_NODE_IMAGE <= len(image) <= MAX_NODE_IMAGE or len(image) % 4096 or image[0] != 0xe9:
            raise ValueError("CCA node image must start with 0xE9 and be 8,192–1,048,576 bytes in 4,096-byte blocks")
        if not self.node_upload_lock.acquire(blocking=False):
            raise CcaError("A CCA node upload is already in progress", 409)
        try:
            with self.lock:
                status = self.gateway_status or {}
                peer = status.get("c6") or {}
                update = status.get("node_update") or {}
                caps = status.get("capabilities") or {}
                if (self.config["expected_mac"] != CCA_NODE_MAC or not isinstance(peer, dict) or
                        str(peer.get("mac", "")).lower() != CCA_NODE_MAC or not isinstance(update, dict) or
                        str(update.get("mac", "")).lower() != CCA_NODE_MAC):
                    raise CcaError("Expected and observed CCA node MAC must match the dedicated board", 409)
                if (self.error is not None or self.status_at is None or time.monotonic() - self.status_at > 15 or
                        not isinstance(caps, dict) or caps.get("node_update") is not True or
                        update.get("supported") is not True or update.get("online") is not True or
                        update.get("busy") is not False or update.get("retry_allowed") is not True or
                        self.node_in_flight or self.node_awaiting_status):
                    raise CcaError("Fresh CCA node updater status does not permit an update", 409)
                self.node_in_flight = self.node_awaiting_status = True
            try:
                try:
                    code, _, raw = self.gateway.request("/api/cca/node-update", "POST", image, "application/octet-stream")
                except (OSError, ValueError, http.client.HTTPException):
                    raise CcaError("CCA node upload outcome unknown; checking status without retrying", 502) from None
                try: result = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise CcaError("Invalid CCA node upload response; checking status without retrying", 502) from None
                if not isinstance(result, dict):
                    raise CcaError("Invalid CCA node upload response; checking status without retrying", 502)
                if code != 202:
                    reason = result.get("error")
                    raise CcaError(str(reason) if isinstance(reason, str) and reason else f"Gateway returned HTTP {code}",
                                   code if 400 <= code < 500 else 503 if code == 503 else 502)
                return result
            finally:
                with self.lock:
                    self.node_in_flight = False
                    self.node_finished_at = time.monotonic()
        finally:
            self.node_upload_lock.release()

    def offer(self, event):
        if event.get("kind") == "ccx.stream_gap":
            event = dict(event, kind="cca.stream_gap")
        if event.get("kind") in ("cca.command", "cca.feedback", "cca.feedback_mapping"):
            with self.lock: self.results.append(event)
        if self.publisher: self.publisher.offer("events", event)
        # Only CRC-verified frames set occupancy; a corrupted byte 12 would read as the other state.
        if event.get("kind") == "cca.rx" and event.get("crc_valid") is True:
            seen = cca_occupancy.decode(event.get("raw_hex"))
            if seen: self._occupancy(seen, event.get("rssi"))

    MAX_SENSORS = 64

    def _sensor_name(self, serial):
        target = next((t for t in self.config["targets"] if t.get("device_id") == serial), None)
        return target["name"] if target else "Occupancy %08X" % serial

    def _occupancy(self, seen, rssi):
        now = time.time()
        with self.lock:
            entry = self.occupancy.get(seen["serial"])
            if entry is None:
                if len(self.occupancy) >= self.MAX_SENSORS: return
                entry = self.occupancy[seen["serial"]] = {"serial": seen["serial"], "occupied": None, "changed_at": now}
                if seen["serial"] not in self.occupancy_known:
                    # Remembered so a restarted app can mark the sensor unavailable until it is heard again.
                    self.occupancy_known.add(seen["serial"])
                    try: atomic_write(self.occupancy_path, json.dumps(sorted(self.occupancy_known)).encode())
                    except OSError: pass
            changed = entry["occupied"] != seen["occupied"]
            if changed: entry["changed_at"] = now
            entry.update(occupied=seen["occupied"], report=seen["report"], seen_at=now,
                         rssi=rssi if type(rssi) is int else None, name=self._sensor_name(seen["serial"]))
            entry = dict(entry)
        # One MQTT state per change; a burst's repeated copies are not republished.
        if changed and self.publisher and hasattr(self.publisher, "occupancy"): self.publisher.occupancy(entry)

    def _occupancy_list(self):
        with self.lock:
            return [dict(e, name=self._sensor_name(e["serial"])) for _, e in sorted(self.occupancy.items())]

    def occupancy_snapshot(self):
        return self._occupancy_list()

    def occupancy_unheard(self):
        """Sensors known from an earlier run and not heard since this start."""
        with self.lock: return sorted(self.occupancy_known - set(self.occupancy))

    def snapshot(self):
        return self.publisher.snapshot() if self.publisher and hasattr(self.publisher, "snapshot") else {"online": False, "queued": 0}

    def accept_events(self, payload, require_baseline=False):
        self.events.accept(payload, require_baseline)

    def stream_state(self, state, error=None):
        with self.events.lock:
            self.events.state = "unsupported" if self.supported is False else state
            self.events.error = error
            reconnecting = state == "connecting" and self.events.connected_once
            if reconnecting:
                self.events.reconnects += 1
        if reconnecting:
            with self.lock: self.capabilities = None

    def read_events(self, after, boot=None):
        return self.events.read(after, boot)

    @staticmethod
    def _validate_config(payload):
        if not isinstance(payload, dict) or set(payload) != {"expected_mac", "targets", "presets"}:
            raise ValueError("expected expected_mac, targets and presets")
        mac = payload["expected_mac"]
        if not isinstance(mac, str) or (mac and not MAC.fullmatch(mac.lower())):
            raise ValueError("expected_mac must be a MAC address or empty")
        result = {"expected_mac": mac.lower(), "targets": [], "presets": []}
        for group in ("targets", "presets"):
            values = payload[group]
            if not isinstance(values, list) or len(values) > 100:
                raise ValueError(f"invalid {group}")
            seen = set()
            for item in values:
                if not isinstance(item, dict) or not ID.fullmatch(str(item.get("id", ""))) or item["id"] in seen:
                    raise ValueError(f"invalid or duplicate {group} id")
                if not isinstance(item.get("name"), str) or not 1 <= len(item["name"]) <= 120:
                    raise ValueError(f"invalid {group} name")
                seen.add(item["id"])
                if group == "presets":
                    if set(item) != {"id", "name", "command"} or not isinstance(item["command"], dict):
                        raise ValueError("invalid preset")
                else:
                    if set(item) - {"id", "name", "device_id", "target_id", "subnet", "layout", "buttons"}:
                        raise ValueError("invalid target field")
                    item = dict(item)
                    # layout absent means "detect from Home Assistant's Lutron data"; buttons map a button to a preset ID.
                    if item.get("layout") in (None, ""): item.pop("layout", None)
                    elif item["layout"] not in PICO_LAYOUTS: raise ValueError("invalid target layout")
                    buttons = item.pop("buttons", None) or {}
                    if not isinstance(buttons, dict): raise ValueError("invalid target buttons")
                    allowed = PICO_LAYOUTS[item["layout"]] if "layout" in item else PICO_BUTTONS
                    for button, preset in buttons.items():
                        if button not in allowed or not isinstance(preset, str) or not ID.fullmatch(preset):
                            raise ValueError("invalid target button")
                    if buttons: item["buttons"] = dict(buttons)
                    for name in ("device_id", "target_id", "subnet"):
                        if name in item and item[name] not in (None, ""):
                            item[name] = uint(item[name], name, 65535 if name == "subnet" else 0xffffffff)
                result[group].append(item)
        presets = {p["id"] for p in result["presets"]}
        if any(p not in presets for t in result["targets"] for p in t.get("buttons", {}).values()):
            raise ValueError("target button uses an unknown preset")
        return result

    def get_config(self):
        with self.lock: return json.loads(json.dumps(self.config))

    def put_config(self, payload):
        config = self._validate_config(payload)
        # Reject malformed presets even if the gateway has not yet advertised its catalogue.
        for preset in config["presets"]:
            if self.capabilities is not None and self.supported:
                preset["command"] = self.validate_command(preset["command"])
            elif not isinstance(preset["command"].get("cmd"), str):
                raise ValueError("preset needs a command name")
        with self.lock:
            atomic_write(self.config_path, json.dumps(config, separators=(",", ":")).encode())
            if self.config["expected_mac"] != config["expected_mac"] and self.job["state"] not in ("staging", "starting", "clearing"):
                self.job.update(state="ready" if self.job["upload"] else "idle", staged=0)
            self.config = config
        return self.get_config()

    def validate_command(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("cmd"), str):
            raise ValueError("expected a native command object")
        with self.lock:
            caps = self.capabilities
        if not caps or self.supported is not True: raise CcaError("CCA capabilities unavailable")
        spec = next((s for s in caps["commands"] if isinstance(s, dict) and s.get("name") == payload["cmd"]), None)
        if spec is None: raise ValueError("unknown CCA command")
        fields = {field.get("name"): field for field in spec.get("fields", []) if isinstance(field, dict)}
        if set(payload) - {"cmd", *fields}: raise ValueError("unknown command field")
        result = {"cmd": payload["cmd"]}
        for name, field in fields.items():
            if name not in payload or payload[name] in (None, ""):
                if field.get("required"): raise ValueError(f"{name} is required")
                continue
            value = payload[name]
            if field.get("type") in ("hex", "bytes", "hex_bytes") or name == "raw_payload":
                max_bytes = min(field.get("max", caps.get("max_raw_bytes", 53)), caps.get("max_raw_bytes", 53))
                min_bytes = field.get("min", 0)
                if not isinstance(value, str) or not HEX.fullmatch(value) or not 2 * min_bytes <= len(value) <= 2 * max_bytes:
                    raise ValueError(f"invalid {name} hex bytes")
                value = value.lower()
            else:
                value = uint(value, name, field.get("max", 0xffffffff), field.get("min", 0))
                options = field.get("options")
                if isinstance(options, list) and options:
                    allowed = [x.get("value") if isinstance(x, dict) else x for x in options]
                    if value not in allowed: raise ValueError(f"invalid {name} option")
            result[name] = value
        return result

    def _verified(self, expected_boot=None, retry=False, trust_s=0):
        with self.lock:
            mac = self.config["expected_mac"]
            recent = self.gateway_status if trust_s and self.status_at is not None and time.monotonic() - self.status_at <= trust_s else None
        if not mac: raise CcaError("Set the expected CCA MAC before sending commands", 409)
        # trust_s lets a time-critical sender rely on the status the poller read moments ago.
        status = recent if isinstance(recent, dict) else self._status(retry=retry)
        peer = status.get("c6")
        if not status.get("ready") or not status.get("i2c", {}).get("online") or not isinstance(peer, dict):
            raise CcaError("CCA node is not online")
        if str(peer.get("mac", "")).lower() != mac: raise CcaError("CCA node MAC does not match expected_mac", 409)
        if not peer.get("radio_present"): raise CcaError("CCA radio is not ready")
        if expected_boot is not None and peer.get("boot_id") != expected_boot:
            raise CcaError("CCA node restarted; reconcile before continuing", 409)
        if type(peer.get("boot_id")) is not int: raise CcaError("CCA node boot ID unavailable")
        return status

    def _submit(self, command):
        result = self._request("/api/cca/command", "POST", command)
        if result.get("accepted") is not True or type(result.get("request_id")) is not int or not result["request_id"]:
            raise CcaError("Gateway did not accept command; inspect status", 502)
        return result

    def command(self, payload, trust_s=0):
        command = self.validate_command(payload)
        if not self.operation.acquire(timeout=2): raise CcaError("CCA operation busy; try again")
        try:
            with self.lock:
                active = self.job["state"] in ("staging", "starting", "clearing")
            if active and command["cmd"] in ("ota_upload_reset", "ota_upload_start", "ota_upload_chunk", "ota_digest", "ota_full_tx"):
                raise CcaError("Guided OTA operation is using the staging buffer", 409)
            self._verified(trust_s=trust_s)
            if command["cmd"] in ("ota_upload_reset", "ota_upload_start", "ota_upload_chunk"):
                with self.lock:
                    self.job.update(state="ready" if self.job["upload"] else "idle", staged=0,
                                    message="Direct OTA staging action requested; guided verification must be repeated")
            return self._submit(command)
        finally: self.operation.release()

    def state(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"serial", "state"}:
            raise ValueError("expected serial and state")
        value = {"serial": uint(payload["serial"], "serial", minimum=1), "state": payload["state"]}
        if value["state"] not in ("occupied", "vacant", "unavailable"): raise ValueError("invalid state")
        if not self.operation.acquire(timeout=2): raise CcaError("CCA operation busy; try again")
        try:
            self._verified()
            return self._request("/api/cca/state", "PUT", value)
        finally: self.operation.release()

    def _wait_command(self, command, boot, progress=lambda peer: True):
        with self.operation:
            return self._wait_command_locked(command, boot, progress)

    def _wait_command_locked(self, command, boot, progress):
        if self.cancel_staging.is_set(): raise CcaError("Staging stopped", 409)
        deadline = time.monotonic() + self.timeout
        status = self._verified(boot, retry=True)
        while status.get("control", {}).get("pending"):
            if time.monotonic() >= deadline: raise CcaError("Gateway command mailbox remained busy", 503)
            if self.cancel_staging.wait(.05): raise CcaError("Staging stopped", 409)
            status = self._verified(boot, retry=True)
        if self.cancel_staging.is_set(): raise CcaError("Staging stopped", 409)
        accepted = self._submit(command)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            status = self._verified(boot, retry=True)
            peer, control = status["c6"], status.get("control", {})
            if control.get("transfer_id") == accepted["request_id"]:
                if control.get("transfer") == "unknown": raise CcaError("Command transfer unknown; reconcile before resuming", 409)
                receipt = control.get("receipt")
                if receipt in ("invalid", "queue_full", "radio_unavailable"):
                    raise CcaError(f"CCA node rejected command: {receipt}", 409)
                if receipt == "queued" and progress(peer): return accepted
            if self.cancel_staging.wait(.05): raise CcaError("Staging stopped", 409)
        raise CcaError("Command receipt timed out; reconcile before resuming", 409)

    def _digest(self, body, boot):
        with self.operation:
            return self._digest_locked(body, boot)

    def _digest_locked(self, body, boot):
        if not self.capabilities_snapshot().get("ota_digest"):
            raise CcaError("Gateway does not support OTA digest reconciliation", 409)
        request = self._wait_command({"cmd": "ota_digest"}, boot)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            digest = self._read("/api/cca/ota", retry=True)
            with self.lock: self.job["digest"] = digest
            if digest.get("request_id") == request["request_id"] and digest.get("boot_id") == boot and digest.get("valid") is True:
                expected, received = digest.get("expected"), digest.get("received")
                if type(expected) is not int or type(received) is not int or not 0 <= received <= len(body) or expected != len(body):
                    raise CcaError("Staged size differs; explicitly stage to reset", 409)
                if digest.get("sha256") != hashlib.sha256(body[:received]).hexdigest():
                    raise CcaError("Staged prefix differs; explicitly stage to reset", 409)
                return received
            if self.cancel_staging.wait(.1): raise CcaError("Staging stopped", 409)
        raise CcaError("OTA digest timed out; reconcile before resuming", 409)

    def file(self, file_format, raw):
        if file_format not in ("ldf", "body"): raise ValueError("select ldf or body format")
        if file_format == "ldf":
            if len(raw) < 128: raise ValueError("LDF header is shorter than 128 bytes")
            raw = raw[128:]
        if not 0 < len(raw) <= MAX_BODY: raise ValueError("OTA body must contain 1–112640 bytes")
        if not self.operation.acquire(blocking=False): raise CcaError("CCA operation busy; try again")
        try:
            with self.lock:
                if self.job["state"] in ("staging", "starting", "clearing"):
                    raise CcaError("Guided OTA operation is using the staging buffer", 409)
            atomic_write(self.upload_path, raw)
            meta = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            with self.lock: self.job = {"state": "ready", "error": None, "staged": 0, "expected": len(raw), "upload": meta}
            return self.status_snapshot()["ota"]
        finally: self.operation.release()

    def _body(self):
        with self.lock: meta = self.job.get("upload")
        if not meta: raise CcaError("Select an OTA body first", 409)
        try: body = self.upload_path.read_bytes()
        except OSError: raise CcaError("OTA body is unavailable", 409) from None
        if len(body) != meta["size"] or hashlib.sha256(body).hexdigest() != meta["sha256"]:
            raise CcaError("OTA body changed; select it again", 409)
        return body

    def action(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str): raise ValueError("expected an OTA action")
        action = payload["action"]
        if action == "stop_staging":
            if set(payload) != {"action"}: raise ValueError("unexpected OTA action field")
            with self.lock:
                if self.job["state"] == "staging": self.cancel_staging.set()
            return self.status_snapshot()["ota"]
        if action not in ("stage", "resume", "start", "clear"): raise ValueError("unknown OTA action")
        if action == "start":
            if set(payload) != {"action", "device_id", "subnet"}: raise ValueError("start requires device_id and subnet")
            device = uint(payload["device_id"], "device_id", minimum=1)
            subnet = uint(payload["subnet"], "subnet", 65535)
        elif set(payload) != {"action"}: raise ValueError("unexpected OTA action field")
        if not self.operation.acquire(blocking=False): raise CcaError("CCA operation busy; try again")
        try:
            with self.lock:
                if self.job["state"] in ("staging", "starting", "clearing"):
                    raise CcaError("Guided OTA operation is using the staging buffer", 409)
            body = self._body() if action != "clear" else None
            self.cancel_staging.clear()
            with self.lock:
                self.job.update(state="staging" if action in ("stage", "resume") else "starting" if action == "start" else "clearing", error=None, message=None)
                if action in ("stage", "resume"): self.job.update(staged=0, digest=None)
            args = (body, action, device if action == "start" else None, subnet if action == "start" else None)
            self.worker = threading.Thread(target=self._run_action, args=args, name="cca-ota", daemon=True)
            self.worker.start()
            return self.status_snapshot()["ota"]
        finally:
            self.operation.release()

    def _run_action(self, body, action, device, subnet):
        try:
            with self.operation: status = self._verified(retry=True)
            boot = status["c6"]["boot_id"]
            if action == "clear":
                self._wait_command({"cmd": "ota_upload_reset"}, boot,
                                   lambda peer: peer.get("ota_expected") == peer.get("ota_received") == 0)
                with self.lock: self.job.update(state="ready" if self.job["upload"] else "idle", error=None, staged=0)
                return
            if action == "start":
                received = self._digest(body, boot)
                if received != len(body): raise CcaError("Body is not fully staged", 409)
                with self.operation:
                    self._verified(boot, retry=True)
                    result = self._submit({"cmd": "ota_full_tx", "device_id": device,
                                           "raw_payload": subnet.to_bytes(2, "big").hex()})
                with self.lock: self.job.update(state="transmit_queued", error=None, staged=received, request_id=result["request_id"])
                return
            if action == "stage":
                self._wait_command({"cmd": "ota_upload_reset"}, boot,
                                   lambda peer: peer.get("ota_expected") == peer.get("ota_received") == 0)
                self._wait_command({"cmd": "ota_upload_start", "target_id": len(body)}, boot,
                                   lambda peer: peer.get("ota_expected") == len(body) and peer.get("ota_received") == 0)
                offset = 0
            else:
                offset = self._digest(body, boot)
            with self.lock: self.job.update(staged=offset)
            while offset < len(body):
                chunk = body[offset:offset + CHUNK]
                end = offset + len(chunk)
                self._wait_command({"cmd": "ota_upload_chunk", "target_id": offset, "raw_payload": chunk.hex()}, boot,
                                   lambda peer, end=end: peer.get("ota_expected") == len(body) and peer.get("ota_received") == end)
                offset = end
                with self.lock: self.job.update(staged=offset)
                # Yield briefly between chunks so ordinary radio commands can
                # take the coordinator without disturbing a chunk receipt.
                if self.cancel_staging.wait(.05): raise CcaError("Staging stopped", 409)
            if self._digest(body, boot) != len(body): raise CcaError("Staged body is incomplete", 409)
            with self.lock: self.job.update(state="staged", error=None)
        except (CcaError, OSError, ValueError) as exc:
            with self.lock: self.job.update(state="stopped" if self.cancel_staging.is_set() else "error", error=str(exc))
