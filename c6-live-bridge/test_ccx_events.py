import io
import http.client
import json
import os
import threading
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

import app
from ccx_events import CcxEventPublisher, CcxFeed


class Sink:
    def __init__(self):
        self.events = []
    def offer(self, event):
        self.events.append(event)
    def snapshot(self):
        return {"online": True, "sent_to_socket": len(self.events), "dropped": 0}


def envelope(boot=7, next_id=10, producer_next=None, events=(), **extra):
    producer_next = next_id + 1 if producer_next is None else producer_next
    value = {"schema_version": 1, "boot_id": boot, "next": producer_next, "cursor": next_id,
             "producer_next": producer_next,
             "oldest": 1, "lost": 0, "events": list(events)}
    value.update(extra)
    return value


class CcxFeedTests(unittest.TestCase):
    def setUp(self):
        self.sink = Sink()
        self.feed = CcxFeed(None, self.sink, capacity=2)

    def test_baseline_live_cache_gap_restart_and_no_replay(self):
        self.feed.accept(envelope(baseline=True), require_baseline=True)
        self.assertEqual(self.feed.read(0)["cursor"], 10)
        self.assertEqual(self.feed.read(0)["lost"], 0)
        self.assertEqual(self.sink.events, [])
        event = {"id": 11, "kind": "ccx.local_level", "source_event": {"boot_id": 2, "sequence": 9},
                 "source": {"serial": 71839249}, "timing": {"source_rf_sfd_us": 100}}
        self.feed.accept(envelope(next_id=11, events=[event]))
        self.feed.accept(envelope(next_id=11, events=[event]))
        self.assertEqual(len(self.sink.events), 1)
        self.assertEqual(self.sink.events[0]["gateway_boot_id"], 7)
        self.assertEqual(self.sink.events[0]["source_event"], event["source_event"])
        self.feed.accept(envelope(next_id=15, baseline=True), require_baseline=True)
        self.assertEqual(self.feed.read(11, 7)["lost"], 4)
        self.assertEqual(self.feed.read(11, 7)["stream"]["missed"], 4)
        self.assertEqual((self.sink.events[-1]["kind"], self.sink.events[-1]["lost"]), ("ccx.stream_gap", 4))
        self.feed.accept(envelope(next_id=16, events=[{"id": 16, "kind": "ccx.message"}]))
        self.assertEqual([e["id"] for e in self.feed.read(11, 7)["events"]], [16])
        self.feed.accept(envelope(boot=8, next_id=0, baseline=True), require_baseline=True)
        self.assertTrue(self.feed.read(16, 7)["reset"])
        self.assertEqual(self.feed.read(0, 8)["events"], [])

    def test_ring_overflow_and_reported_gap(self):
        self.feed.accept(envelope(next_id=0, baseline=True))
        self.feed.accept(envelope(next_id=4, events=[{"id": 1}, {"id": 3}, {"id": 4}], lost=1))
        self.assertEqual(self.feed.read(0)["lost"], 2)
        self.assertEqual(self.feed.read(0)["stream"]["missed"], 1)
        self.assertEqual([e["id"] for e in self.feed.read(0)["events"]], [3, 4])

    def test_limited_pages_follow_delivered_cursor_not_producer_next(self):
        self.feed.accept(envelope(next_id=0, baseline=True))
        self.feed.accept(envelope(next_id=1, producer_next=50, events=[{"id": 1}]))
        self.assertEqual(self.feed.read(0)["cursor"], 1)
        self.feed.accept(envelope(next_id=2, producer_next=50, events=[{"id": 2}]))
        self.assertEqual([e["id"] for e in self.feed.read(0)["events"]], [1, 2])
        self.assertEqual(self.feed.read(0)["lost"], 0)

    def test_sse_parser_accepts_baseline_and_multiline_data(self):
        start = envelope(next_id=0, baseline=True)
        later = envelope(next_id=1, events=[{"id": 1, "kind": "ccx.message"}])
        source = (": keepalive\n\nevent: ccx_events\ndata: " + json.dumps(start) + "\n\n"
                  "event: ccx_events\ndata: " + json.dumps(later) + "\n\n").encode()
        with self.assertRaises(ConnectionError):
            self.feed._consume(io.BytesIO(source))
        self.assertEqual(self.sink.events[0]["kind"], "ccx.message")
        with self.assertRaises(ValueError):
            self.feed.accept(envelope(next_id=1, events=[{"id": 1}]), require_baseline=True)

    def test_chunked_http_sse_is_read_incrementally(self):
        first = ("event: ccx_events\ndata: " + json.dumps(envelope(next_id=0, baseline=True)) + "\n\n").encode()
        second = ("event: ccx_events\ndata: " + json.dumps(envelope(next_id=1, events=[{"id": 1, "kind": "ccx.message"}])) + "\n\n").encode()
        chunks = [first[:11], first[11:], second[:19], second[19:]]
        body = b"".join(("%x\r\n" % len(chunk)).encode() + chunk + b"\r\n" for chunk in chunks) + b"0\r\n\r\n"
        wire = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + body
        class Socket:
            def makefile(self, mode): return io.BufferedReader(io.BytesIO(wire))
        response = http.client.HTTPResponse(Socket())
        response.begin()
        with self.assertRaises(ConnectionError):
            self.feed._consume(response)
        self.assertEqual([e["kind"] for e in self.sink.events], ["ccx.message"])

    def test_invalid_event_envelopes_do_not_become_messages(self):
        for bad in ({}, {"schema_version": 1, "boot_id": True, "next": 0, "events": []},
                    envelope(baseline=True, events=[{"id": 1}])):
            with self.assertRaises(ValueError):
                self.feed.accept(bad)
        self.assertEqual(self.sink.events, [])

    def test_old_gateway_is_unsupported_and_worker_stops(self):
        connected = threading.Event()
        class Connection:
            sock = None
            def __init__(self, *args, **kwargs): pass
            def request(self, *args, **kwargs): pass
            def getresponse(self):
                connected.set()
                return SimpleNamespace(status=404)
            def close(self): pass
        gateway = SimpleNamespace(scheme="http", hostname="gateway", port=80, token="not-logged")
        feed = CcxFeed(gateway)
        with patch("ccx_events.http.client.HTTPConnection", Connection):
            feed.start()
            self.assertTrue(connected.wait(2))
            for _ in range(100):
                if feed.read(0)["stream"]["state"] == "unsupported": break
                threading.Event().wait(.001)
            self.assertEqual(feed.read(0)["stream"]["state"], "unsupported")
            feed.stop()
        self.assertFalse(feed.thread.is_alive())

    def test_stop_interrupts_stream_even_when_response_owns_socket(self):
        reading, interrupted = threading.Event(), threading.Event()
        class Sock:
            def shutdown(self, how): interrupted.set()
        class Response:
            status = 200
            def getheader(self, name): return "text/event-stream"
            def readline(self, limit):
                reading.set()
                interrupted.wait(3)
                return b""
        class Connection:
            def __init__(self, *args, **kwargs): self.sock = Sock()
            def request(self, *args, **kwargs): pass
            def getresponse(self):
                self.sock = None  # http.client may transfer the socket to HTTPResponse
                return Response()
            def close(self): pass
        gateway = SimpleNamespace(scheme="http", hostname="gateway", port=80, token="not-logged")
        feed = CcxFeed(gateway)
        with patch("ccx_events.http.client.HTTPConnection", Connection):
            feed.start()
            self.assertTrue(reading.wait(2))
            feed.stop()
        self.assertTrue(interrupted.is_set())
        self.assertFalse(feed.thread.is_alive())

    def test_sidecar_upload_closes_sse_and_resumes_once_after_uncertain_reconciliation(self):
        readers = [threading.Event(), threading.Event()]
        connections, calls = [], []
        class Socket:
            def __init__(self): self.closed = threading.Event()
            def shutdown(self, how): self.closed.set(); calls.append("sse-closed")
        class Response:
            status = 200
            def __init__(self, sock, index):
                self.sock, self.index = sock, index
                baseline=envelope(next_id=10 if index==0 else 15,baseline=True)
                self.lines=iter([b"event: ccx_events\n", b"data: "+json.dumps(baseline).encode()+b"\n", b"\n"])
            def getheader(self, name): return "text/event-stream"
            def readline(self, limit):
                try:return next(self.lines)
                except StopIteration:
                    readers[self.index].set();self.sock.closed.wait(3);return b""
        class Connection:
            def __init__(self, *args, **kwargs):
                self.sock=Socket();self.index=len(connections);connections.append(self)
            def request(self, *args, **kwargs): pass
            def getresponse(self):
                sock=self.sock;self.sock=None
                return Response(sock,self.index)
            def close(self): pass
        data={"schema_version":1,"supported":True,"online":True,"mac":app.SIDECAR_MAC,"antenna":"external",
              "boot_id":12,"slot":0,"elf_sha256":"a"*64,"pending_verify":False,"confirmed":True,
              "update":{"state":"idle","busy":False,"retry_allowed":True}}
        class Gateway:
            scheme,hostname,port,token="http","gateway",80,"not-logged"
            def __init__(self):self.posted=False;self.probes=0
            def request(self,path,method="GET",body=None,content_type=None,timeout=None):
                if path=="/api/sidecar/status":
                    calls.append("status")
                    if connections and connections[0].sock is None and "sse-closed" in calls and not self.posted:
                        self.probes+=1
                        if self.probes==1:return 503,"application/json",b'{}'
                    return 200,"application/json",json.dumps(data).encode()
                if path=="/api/sidecar/update":
                    calls.append("post");self.posted=True
                    self.assert_closed()
                    data["update"]={"state":"reconciling","busy":True,"retry_allowed":False}
                    raise OSError("response lost")
                raise AssertionError(path)
            def assert_closed(self):
                if not connections[0].index==0 or "sse-closed" not in calls or self.probes<2 or feed.thread.is_alive():
                    raise AssertionError("POST began before SSE closure, thread exit and gateway probe")
        gateway=Gateway();feed=CcxFeed(gateway,self.sink)
        panel=app.Panel(gateway,SimpleNamespace(program_events=lambda:{}));panel.sidecar.feed=feed
        panel.sidecar.poll_once()
        with patch("ccx_events.http.client.HTTPConnection",Connection):
            feed.start();self.assertTrue(readers[0].wait(2))
            try:
                with self.assertRaises(OSError):panel.proxy("sidecar_update",body=b"\xe9signed")
                self.assertEqual(calls.count("post"),1)
                self.assertEqual(feed.read(0)["stream"]["state"],"paused")
                self.assertTrue(json.loads(panel.proxy("sidecar_status")[2])["monitoring"]["event_feed_paused"])
                panel.sidecar.poll_once()  # S3 still owns the update; keep the stream closed.
                self.assertEqual(len(connections),1)
                data["update"]={"state":"uncertain","busy":False,"retry_allowed":False}
                panel.sidecar.poll_once()
                self.assertTrue(readers[1].wait(2))
                for _ in range(3):panel.sidecar.poll_once()
                self.assertEqual(len(connections),2)
                self.assertFalse(json.loads(panel.proxy("sidecar_status")[2])["monitoring"]["event_feed_paused"])
                self.assertEqual([e["kind"] for e in self.sink.events],["ccx.stream_gap"])
                feed.accept(envelope(next_id=16,events=[{"id":16,"kind":"ccx.message"}]))
                self.assertEqual([e["kind"] for e in self.sink.events],["ccx.stream_gap","ccx.message"])
                self.assertNotIn("maintenance",calls)
            finally:
                panel.stop.set();feed.stop()

    def test_feed_start_cannot_clear_stop_event_while_old_reader_runs(self):
        release=threading.Event()
        self.feed.thread=threading.Thread(target=lambda:release.wait(2));self.feed.thread.start()
        self.feed.stop_event.set()
        try:
            self.assertFalse(self.feed.start())
            self.assertTrue(self.feed.stop_event.is_set())
        finally:
            release.set();self.feed.thread.join(2)


class PublisherTests(unittest.TestCase):
    def test_mqtt_wire_message_is_not_retained(self):
        client = app.MqttClient("host", 1883, None, None, "event-client", "status")
        sent = []
        client._send = sent.append
        client.publish(CcxEventPublisher.TOPIC, "{}", retain=False)
        self.assertEqual(sent[0][0], 0x30)  # PUBLISH, QoS 0, retain clear

    def test_bounded_nonblocking_queue_and_offline_drop(self):
        pub = CcxEventPublisher(None, capacity=1)
        self.assertFalse(pub.offer({"id": 1}))
        with pub.lock:
            pub.online = True
        self.assertTrue(pub.offer({"id": 2}))
        self.assertFalse(pub.offer({"id": 3}))
        self.assertEqual(pub.snapshot()["dropped"], 2)
        pub._discard()
        self.assertEqual(pub.snapshot()["dropped"], 3)
        self.assertEqual(pub.snapshot()["queued"], 0)

    def test_expired_queued_event_is_dropped_before_publish(self):
        messages = []
        class Client:
            def publish(self, topic, payload, retain=False):
                if topic == CcxEventPublisher.TOPIC:
                    messages.append(json.loads(payload))
                    pub.stop_event.set()
            def service(self): pass
            def close(self): pass
        pub = CcxEventPublisher(Client)
        pub.online = True
        samples = iter((0, 1_100_000_000))
        def now(): return next(samples, 1_100_000_000)
        with patch("ccx_events.time.monotonic_ns", side_effect=now):
            self.assertTrue(pub.offer({"id": 1}))
            self.assertTrue(pub.offer({"id": 2}))
            pub.run()
        self.assertEqual([e["id"] for e in messages], [2])
        self.assertEqual(messages[0]["app_mqtt_dropped"], 1)
        self.assertEqual(pub.snapshot()["dropped"], 1)
        self.assertEqual(pub.snapshot()["sent_to_socket"], 1)

    def test_worker_publishes_unretained_and_reports_socket_send(self):
        online, sent = threading.Event(), threading.Event()
        class Client:
            def __init__(self): self.calls = []
            def publish(self, topic, payload, retain=False):
                self.calls.append((topic, payload, retain))
                if topic.endswith("/status") and payload == "online": online.set()
                if topic.endswith("/events"): sent.set()
            def service(self): pass
            def close(self): pass
        client = Client()
        pub = CcxEventPublisher(lambda: client)
        pub.start()
        self.assertTrue(online.wait(2))
        # Wait until the worker has marked the connection usable.
        for _ in range(100):
            if pub.snapshot()["online"]: break
            threading.Event().wait(.001)
        self.assertTrue(pub.offer({"gateway_boot_id": 4, "id": 12}))
        self.assertTrue(sent.wait(2))
        pub.stop()
        messages = [c for c in client.calls if c[0] == pub.TOPIC]
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0][2])
        self.assertEqual(json.loads(messages[0][1])["id"], 12)
        self.assertEqual(pub.snapshot()["sent_to_socket"], 1)
        self.assertTrue(any(c == (pub.AVAILABILITY, "offline", True) for c in client.calls))

    def test_disconnect_discards_queued_events_instead_of_replaying(self):
        sending, release = threading.Event(), threading.Event()
        class Client:
            def publish(self, topic, payload, retain=False):
                if topic == CcxEventPublisher.TOPIC:
                    sending.set(); release.wait(2); raise OSError("broker disconnected")
            def service(self): pass
            def close(self): pass
        pub = CcxEventPublisher(Client)
        pub.start()
        for _ in range(500):
            if pub.snapshot()["online"]: break
            threading.Event().wait(.001)
        self.assertTrue(pub.offer({"id": 1}))
        self.assertTrue(sending.wait(2))
        self.assertTrue(pub.offer({"id": 2}))
        release.set()
        for _ in range(500):
            if pub.snapshot()["dropped"] >= 2: break
            threading.Event().wait(.001)
        pub.stop()
        self.assertEqual(pub.snapshot()["dropped"], 2)
        self.assertEqual(pub.snapshot()["queued"], 0)
        self.assertEqual(pub.snapshot()["sent_to_socket"], 0)

    def test_main_starts_ccx_publisher_with_show_sensors_disabled(self):
        calls = []
        class Publisher:
            AVAILABILITY = "status"
            def __init__(self, connect): calls.append("publisher-created")
            def start(self): calls.append("publisher-started")
            def stop(self): calls.append("publisher-stopped")
        class Feed:
            def __init__(self, gateway, publisher): calls.append("feed-created")
            def start(self): calls.append("feed-started")
            def stop(self): calls.append("feed-stopped")
        class Server:
            def __init__(self, *args): pass
            def serve_forever(self): pass
            def server_close(self): pass
        panel = SimpleNamespace(run_event_archive=lambda: None, stop=threading.Event(), sidecar=SimpleNamespace(run=lambda: None))
        options = {"gateway_host": "http://gateway", "gateway_token": "token", "show_sensors": False}
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "test"}), \
             patch.object(app, "Path", return_value=SimpleNamespace(read_text=lambda: json.dumps(options))), \
             patch.object(app, "Gateway"), patch.object(app, "EventArchive"), \
             patch.object(app, "Panel", return_value=panel), patch.object(app, "HomeAssistant"), \
             patch.object(app, "CcxEventPublisher", Publisher), patch.object(app, "CcxFeed", Feed), \
             patch.object(app, "ShowSensors") as sensors, patch.object(app, "Server", Server), \
             patch.object(app.threading, "Thread"):
            app.main()
        self.assertEqual(calls, ["publisher-created", "feed-created", "publisher-started", "feed-started", "feed-stopped", "publisher-stopped"])
        sensors.assert_not_called()


class RulesTests(unittest.TestCase):
    def test_listener_health_is_collected_once_and_served_from_cache(self):
        class Gateway:
            def __init__(self): self.calls = []
            def request(self, path, *args, **kwargs):
                self.calls.append(path)
                if path == "/api/ccx/status":
                    return 200, "application/json", b'{"listener":{"online":true}}'
                if path == "/api/ccx/clock":
                    return 404, "application/json", b'{}'
                raise AssertionError(path)
        gateway = Gateway()
        archive = SimpleNamespace(program_events=lambda: {}, record=lambda events: None)
        panel = app.Panel(gateway, archive)
        panel.cache["status"] = ({}, 1)
        panel.config_due = panel.timezone_due = panel.ketra_due = float("inf")
        panel.ccx_status_due = 0; panel.ccx_clock_due = float("inf"); panel.next_poll = 0
        panel.archive_poll_once()
        self.assertTrue(json.loads(panel.proxy("ccx_status")[2])["listener"]["online"])
        panel.proxy("ccx_status")
        self.assertEqual(gateway.calls, ["/api/ccx/status"])
        panel.ccx_status_due = float("inf"); panel.ccx_clock_due = 0; panel.next_poll = 0
        panel.archive_poll_once()
        self.assertTrue(json.loads(panel.proxy("ccx_clock")[2])["unsupported"])
        self.assertEqual(gateway.calls[-1], "/api/ccx/clock")

    def test_validation_supports_event_levels_constant_off_and_shows(self):
        rules = {"rules": [
            {"id": "dining", "enabled": False, "match": {"serial": 71839249, "kind": "ccx.local_level"},
             "action": {"type": "set_level", "target_zone": 38138, "level": "event"}},
            {"id": "off", "enabled": True, "match": {},
             "action": {"type": "set_level", "target_zone": 38138, "level": 0}},
            {"id": "show", "enabled": False, "match": {"message_type": 3},
             "action": {"type": "select_show", "target_zone": 38138, "show_id": 31779}},
        ]}
        self.assertEqual(app.validate_ccx_rules(rules), rules)
        for action in ({"type": "set_level", "target_zone": 38138},
                       {"type": "set_level", "target_zone": 38138, "level": True},
                       {"type": "select_show", "target_zone": 38138, "show_id": 0}):
            with self.assertRaises(ValueError):
                app.validate_ccx_rules({"rules": [{"id": "x", "enabled": True, "match": {}, "action": action}]})

    def test_handler_exposes_only_fixed_routes_and_cache(self):
        class Panel:
            def __init__(self): self.calls = []
            def proxy(self, route, query=(), body=None, content_type=None):
                self.calls.append((route, body, content_type))
                return 200, "application/json", b'{}'
        panel, feed = Panel(), CcxFeed(None)
        feed.accept(envelope(next_id=0, baseline=True))
        h = object.__new__(app.Handler)
        h.client_address = ("172.30.32.2", 1)
        h.server = SimpleNamespace(panel=panel, ccx=feed)
        h.results = []
        h.reply = lambda code, value: h.results.append((code, value))
        h.send = lambda code, body, ctype="application/json": h.results.append((code, body))
        h.path = "/api/ccx/events?after=0&boot_id=7"
        h.do_GET()
        self.assertEqual(h.results[-1][0], 200)
        self.assertEqual(panel.calls, [])
        h.path = "/api/ccx/events?after=-1"
        h.do_GET()
        self.assertEqual(h.results[-1][0], 400)
        h.path = "/api/ccx/rules"
        body = json.dumps({"rules": [{"id": "off", "enabled": False, "match": {},
                  "action": {"type": "set_level", "target_zone": 38138, "level": 0}}]}).encode()
        h.headers = Message(); h.headers["Content-Type"] = "application/json"; h.headers["Content-Length"] = str(len(body))
        h.rfile = io.BytesIO(body)
        h.do_PUT()
        self.assertEqual(h.results[-1][0], 200)
        self.assertEqual(panel.calls[-1][0], "ccx_rules_put")
        h.path = "/api/ccx/rules?url=http://elsewhere"
        h.do_GET()
        self.assertEqual(h.results[-1][0], 404)


if __name__ == "__main__":
    unittest.main()
