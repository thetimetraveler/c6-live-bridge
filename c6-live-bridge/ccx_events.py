"""Independent CCX live-feed cache and best-effort MQTT publisher."""
from collections import deque
import http.client
import json
import logging
import queue
import socket
import threading
import time

LOG = logging.getLogger("c6-management.ccx")
MAX_LINE = 262144
MAX_MESSAGE = 1048576


class CcxEventPublisher:
    """One MQTT socket, owned by one worker; the live-feed reader never waits for it."""
    TOPIC = "ccx_bridge/ccx/events"
    AVAILABILITY = "ccx_bridge/ccx/status"

    def __init__(self, connect, capacity=256):
        self.connect = connect
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.client = None
        self.online = False
        self.sent = self.dropped = 0
        self.last_queue_us = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="ccx-mqtt", daemon=True)
        self.thread.start()

    def offer(self, event):
        # No broker connection means there is no replayable backlog. The worker
        # discards the remaining queue if a connection later fails.
        with self.lock:
            if not self.online:
                self.dropped += 1
                return False
            try:
                self.queue.put_nowait((time.monotonic_ns(), event))
            except queue.Full:
                self.dropped += 1
                return False
        return True

    def snapshot(self):
        with self.lock:
            return {"online": self.online, "sent_to_socket": self.sent,
                    "dropped": self.dropped, "queued": self.queue.qsize(), "last_queue_us": self.last_queue_us}

    def _discard(self):
        count = 0
        while True:
            try:
                self.queue.get_nowait()
                count += 1
            except queue.Empty:
                break
        with self.lock:
            self.dropped += count

    def run(self):
        retry = 1
        while not self.stop_event.is_set():
            try:
                self.client = self.connect()
                self.client.publish(self.AVAILABILITY, "online", retain=True)
                with self.lock:
                    self.online = True
                retry = 1
                while not self.stop_event.is_set():
                    try:
                        queued_at, event = self.queue.get(timeout=1)
                    except queue.Empty:
                        self.client.service()
                        continue
                    message = dict(event)
                    message["app_mqtt_queue_us"] = max(0, (time.monotonic_ns() - queued_at) // 1000)
                    with self.lock:
                        message["app_mqtt_dropped"] = self.dropped
                    try:
                        self.client.publish(self.TOPIC, json.dumps(message, separators=(",", ":")), retain=False)
                    except (OSError, ConnectionError, ValueError):
                        with self.lock:
                            self.dropped += 1
                        raise
                    with self.lock:
                        self.sent += 1
                        self.last_queue_us = message["app_mqtt_queue_us"]
                    self.client.service()
            except (OSError, ConnectionError, ValueError, http.client.HTTPException) as exc:
                LOG.warning("CCX MQTT unavailable: %s", exc)
            finally:
                with self.lock:
                    self.online = False
                self._discard()
                if self.client:
                    try:
                        self.client.publish(self.AVAILABILITY, "offline", retain=True)
                    except (OSError, ConnectionError, ValueError):
                        pass
                    self.client.close()
                    self.client = None
            if not self.stop_event.wait(retry):
                retry = min(30, retry * 2)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=12)


class CcxFeed:
    """A single S3 SSE reader, independent of the dashboard's snapshot collector."""
    def __init__(self, gateway, publisher=None, capacity=2048):
        self.gateway, self.publisher = gateway, publisher
        self.events = deque(maxlen=capacity)
        self.lock = threading.Lock()
        self.connection_lock = threading.Lock()
        self.connection = None
        self.stream_socket = None
        self.stop_event = threading.Event()
        self.thread = None
        self.boot_id = None
        self.cursor = self.baseline = 0
        self.producer_next = 1
        self.connected_once = False
        self.state = "connecting"
        self.error = None
        self.last_contact = None
        self.missed = self.reconnects = 0

    def start(self):
        self.thread = threading.Thread(target=self.run, name="ccx-live-feed", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.connection_lock:
            conn = self.connection
            if conn:
                try:
                    sock = self.stream_socket or conn.sock
                    if sock:
                        sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
        if self.thread:
            self.thread.join(timeout=8)

    @staticmethod
    def _uint(value):
        return type(value) is int and 0 <= value <= 0xffffffff

    def accept(self, payload, require_baseline=False):
        """Consume a feed envelope. Only new non-baseline records go to MQTT."""
        if (not isinstance(payload, dict) or payload.get("schema_version") != 1
                or not self._uint(payload.get("boot_id")) or not self._uint(payload.get("next"))
                or not isinstance(payload.get("events"), list)):
            raise ValueError("invalid CCX event envelope")
        is_baseline = payload.get("baseline") is True
        if require_baseline and not is_baseline:
            raise ValueError("CCX stream did not start with a baseline")
        if is_baseline and payload["events"]:
            raise ValueError("CCX baseline must not contain historical events")
        now_ms = time.time_ns() // 1_000_000
        fresh = []
        gap = None
        with self.lock:
            boot = payload["boot_id"]
            old_boot, old_cursor = self.boot_id, self.cursor
            if boot != old_boot:
                self.boot_id, self.cursor, self.baseline = boot, 0, 0
                self.events.clear()
            self.producer_next = payload["next"]
            if is_baseline:
                producer_next = payload.get("producer_next")
                end = max(0, (producer_next if self._uint(producer_next) and producer_next > 0 else payload["next"]) - 1)
                if old_boot == boot and self.connected_once:
                    lost = max(0, end - old_cursor)
                    self.missed += lost
                    if lost:
                        gap = {"schema_version": 1, "kind": "ccx.stream_gap", "gateway_boot_id": boot,
                               "after": old_cursor, "through": end, "lost": lost, "reason": "reconnect",
                               "app_received_at_ms": now_ms}
                else:
                    self.baseline = end
                self.cursor = end
                self.connected_once = True
            else:
                previous = self.cursor
                observed_lost = 0
                for event in payload["events"]:
                    if not isinstance(event, dict) or not self._uint(event.get("id")) or event["id"] == 0:
                        raise ValueError("invalid CCX event")
                    if event["id"] <= self.cursor:
                        continue
                    observed_lost += max(0, event["id"] - previous - 1)
                    previous = self.cursor = event["id"]
                    saved = dict(event)
                    saved["gateway_boot_id"] = boot
                    saved["app_received_at_ms"] = now_ms
                    self.events.append(saved)
                    fresh.append(saved)
                delivered_cursor = payload.get("cursor")
                if self._uint(delivered_cursor):
                    observed_lost += max(0, delivered_cursor - previous)
                    self.cursor = max(self.cursor, delivered_cursor)
                reported = payload.get("lost", 0)
                lost = max(observed_lost, reported if self._uint(reported) else 0)
                self.missed += lost
                if lost:
                    gap = {"schema_version": 1, "kind": "ccx.stream_gap", "gateway_boot_id": boot,
                           "after": old_cursor, "through": self.cursor, "lost": lost, "reason": "ring_gap",
                           "app_received_at_ms": now_ms}
            self.state, self.error, self.last_contact = "online", None, time.monotonic()
        if self.publisher:
            if gap:
                self.publisher.offer(gap)
            for event in fresh:
                self.publisher.offer(event)

    def read(self, after, boot=None):
        if not self._uint(after):
            raise ValueError("invalid CCX event cursor")
        with self.lock:
            reset = boot is not None and str(self.boot_id) != str(boot)
            if reset or after > self.cursor:
                after = 0
            selected = [event for event in self.events if event["id"] > after]
            previous = max(after, self.baseline) if after == 0 else after
            lost = 0
            for event in selected:
                lost += max(0, event["id"] - previous - 1)
                previous = event["id"]
            lost += max(0, self.cursor - previous)
            status = {"state": self.state, "error": self.error, "missed": self.missed,
                      "reconnects": self.reconnects,
                      "age_s": None if self.last_contact is None else max(0, time.monotonic() - self.last_contact)}
            result = {"schema_version": 1, "boot_id": self.boot_id, "reset": reset,
                      "after": after, "cursor": self.cursor, "next": self.producer_next,
                      "producer_next": self.producer_next,
                      "oldest": self.events[0]["id"] if self.events else self.cursor + 1,
                      "lost": lost, "events": selected, "stream": status}
        if self.publisher:
            result["mqtt"] = self.publisher.snapshot()
        return result

    def _consume(self, response):
        name, data, first, size = "message", [], True, 0
        while not self.stop_event.is_set():
            raw = response.readline(MAX_LINE + 1)
            if not raw:
                raise ConnectionError("CCX stream ended")
            if len(raw) > MAX_LINE:
                raise ValueError("CCX stream line too long")
            line = raw.decode("utf-8").rstrip("\r\n")
            with self.lock:
                self.last_contact = time.monotonic()
            if not line:
                if name == "ccx_events" and data:
                    self.accept(json.loads("\n".join(data)), require_baseline=first)
                    first = False
                name, data, size = "message", [], 0
            elif line.startswith("event:"):
                name = line[6:].lstrip()
            elif line.startswith("data:"):
                part = line[5:].lstrip(" ")
                size += len(part)
                if size > MAX_MESSAGE:
                    raise ValueError("CCX stream message too long")
                data.append(part)

    def run(self):
        retry = 1
        while not self.stop_event.is_set():
            conn = None
            try:
                cls = http.client.HTTPSConnection if self.gateway.scheme == "https" else http.client.HTTPConnection
                conn = cls(self.gateway.hostname, self.gateway.port, timeout=6)
                with self.connection_lock:
                    self.connection = conn
                conn.request("GET", "/api/ccx/stream", headers={"Authorization": "Bearer " + self.gateway.token,
                             "Accept": "text/event-stream"})
                # HTTPResponse may take ownership of the socket when the server
                # says Connection: close; retain it so stop() can interrupt a read.
                with self.connection_lock:
                    self.stream_socket = conn.sock
                    if self.stop_event.is_set():
                        conn.close()
                        break
                response = conn.getresponse()
                if response.status in (404, 405):
                    with self.lock:
                        self.state, self.error = "unsupported", None
                    self.stop_event.wait(60)
                    continue
                if response.status != 200:
                    raise ConnectionError(f"CCX stream returned HTTP {response.status}")
                if "text/event-stream" not in (response.getheader("Content-Type") or ""):
                    raise ValueError("invalid CCX stream content type")
                with self.lock:
                    if self.connected_once:
                        self.reconnects += 1
                retry = 1
                self._consume(response)
            except (OSError, ConnectionError, ValueError, UnicodeDecodeError, http.client.HTTPException) as exc:
                if not self.stop_event.is_set():
                    with self.lock:
                        self.state, self.error = "disconnected", str(exc)
                    LOG.warning("CCX stream unavailable: %s", exc)
            finally:
                with self.connection_lock:
                    self.connection = None
                    self.stream_socket = None
                if conn:
                    conn.close()
            if not self.stop_event.wait(retry):
                retry = min(30, retry * 2)
