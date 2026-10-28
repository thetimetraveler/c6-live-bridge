import io
import json
from email.message import Message
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class StubGateway:
    def __init__(self): self.calls=[]; self.fail=False; self.endpoint_online=True
    def request(self, path, method="GET", body=None, content_type=None):
        self.calls.append((method,path,body,content_type))
        if self.fail: raise OSError("offline")
        if path.startswith("/api/snapshot"):
            status={"wifi_up":True,"endpoint_online":self.endpoint_online,"endpoint_uptime_s":60 if self.endpoint_online else 0}
            events={"boot_id":1,"after":0,"next":2,"oldest":1,"lost":0,"events":[{"id":1,"stage":"confirmed"}]}
            return 200,"application/json",json.dumps({"status":status,"devices":{"zones":[]},"events":events}).encode()
        if path == "/api/config":
            return 200,"application/json",b'{"generation":1,"hash":123,"zones":[]}'
        if path == "/api/status":
            return 200,"application/json",json.dumps({"wifi_up":True,"endpoint_online":self.endpoint_online,"endpoint_uptime_s":60 if self.endpoint_online else 0}).encode()
        if path == "/api/endpoint/update":
            self.endpoint_online=False
            return 202,"application/json",b'{"status":"rebooting"}'
        return 200,"application/json",json.dumps({"after":0,"next":2,"oldest":1,"lost":0,"events":[{"id":1,"stage":"confirmed"}]}).encode()


class StubHomeAssistant(app.HomeAssistant):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.calls=[];self.fail=False
    def request(self,method,path,body=None):
        self.calls.append((method,path,body))
        if self.fail: raise OSError("supervisor unreachable")
        if path=="/config": return json.dumps({"time_zone":"Europe/London","location_name":"Home"}).encode()
        return json.dumps({"sun":{"next_rising":"2026-10-26T06:52:00+00:00","next_setting":None},"lights":[
            {"entity_id":"light.kitchen","integration":"lutron_caseta","name":"Kitchen","state":"on","brightness":180,
             "color_temp_kelvin":3000,"area":"Kitchen","device_id":"abc","device":"Kitchen","serial":12345678,"zone_id":"1379"},
            {"entity_id":"light.bad","brightness":999,"color_temp_kelvin":"warm","serial":"","zone_id":"13 79"},"junk"]}).encode()


class AddonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.gateway=StubGateway()
        self.archive=app.EventArchive(Path(self.tmp.name)/"events")
        self.panel=app.Panel(self.gateway,self.archive)
        self.ha=StubHomeAssistant("supervisor-token",["lutron_caseta","bad name!"])
    def tearDown(self): self.tmp.cleanup()

    def test_gateway_origin_and_token_validation(self):
        for url in ("http://user:pass@example.test","http://example.test/arbitrary","http://example.test/?x=1"):
            with self.assertRaises(ValueError): app.Gateway(url,"secret")
        with self.assertRaises(ValueError): app.Gateway("https://example.test","")

    def test_ingress_peer_validation_and_relative_frontend(self):
        handler=object.__new__(app.Handler)
        handler.client_address=("172.30.32.2",1234)
        self.assertTrue(handler.authenticated())
        handler.client_address=("192.168.1.20",1234)
        self.assertFalse(handler.authenticated())
        html=Path(__file__).with_name("index.html").read_text()
        self.assertIn("new URL('.',location.href)",html)
        self.assertNotIn("gateway_token",html)

    def collect(self, panel=None):
        panel=panel or self.panel
        panel.next_poll=0;panel.config_due=float("inf");panel.timezone_due=float("inf")
        return panel.archive_poll_once()

    def test_events_get_wall_clock_time_from_gateway_uptime(self):
        gateway=StubGateway()
        def snapshot(path, method="GET", body=None, content_type=None):
            events={"boot_id":7,"after":0,"next":3,"oldest":1,"lost":0,
                    "events":[{"id":1,"ts_ms":40000,"stage":"requested"},{"id":2,"stage":"applied"},{"id":3,"ts_ms":True}]}
            return 200,"application/json",json.dumps({"status":{"gateway_uptime_ms":50000},"devices":{"zones":[]},"events":events}).encode()
        gateway.request=snapshot
        panel=app.Panel(gateway,self.archive)
        with patch.object(app.time,"time",return_value=1_000_000.0):
            self.collect(panel)
        self.assertEqual(panel.boot_epoch_ms,1_000_000_000-50000)
        _,_,raw=panel.proxy("events",[("after","0")])
        body=json.loads(raw)
        self.assertEqual(body["boot_epoch_ms"],1_000_000_000-50000)
        self.assertEqual(body["events"][0]["wall_ms"],1_000_000_000-10000)
        self.assertNotIn("wall_ms",body["events"][1])
        self.assertNotIn("wall_ms",body["events"][2])
        archived=[json.loads(l) for f in (Path(self.tmp.name)/"events").glob("*.jsonl") for l in f.read_text().splitlines()]
        self.assertEqual(archived[0]["wall_ms"],1_000_000_000-10000)

    def test_only_fixed_paths_forward_and_event_cursor(self):
        self.collect()
        calls=len(self.gateway.calls)
        code,_,raw=self.panel.proxy("events",[("after","0")])
        self.assertEqual(code,200)
        self.assertEqual(json.loads(raw)["events"][0]["id"],1)
        self.assertEqual(self.panel.cursor,1)
        with self.assertRaises(KeyError):self.panel.proxy("/api/evil")
        with self.assertRaises(ValueError):self.panel.proxy("events",[("after","-1")])
        self.assertEqual(len(self.gateway.calls),calls)

    def test_event_boot_reset_rewinds_cursor_and_archive_dedup_is_boot_scoped(self):
        class BootGateway(StubGateway):
            def __init__(self):super().__init__();self.boot=1
            def request(self,*args,**kwargs):
                code,ct,raw=super().request(*args,**kwargs)
                data=json.loads(raw)
                if "events" in data:data["events"]["boot_id"]=self.boot
                return code,ct,json.dumps(data).encode()
        gw=BootGateway();panel=app.Panel(gw,self.archive)
        self.collect(panel);self.assertEqual((panel.boot_id,panel.cursor),(1,1))
        gw.boot=2;self.collect(panel)
        self.assertEqual((panel.boot_id,panel.cursor),(2,1))
        data=json.loads(panel.proxy("events",[("after","1"),("boot_id","1")])[2])
        self.assertTrue(data["reset"]);self.assertEqual(len(data["events"]),1)
        rows=[json.loads(line) for f in (Path(self.tmp.name)/"events").glob("*.jsonl") for line in f.read_text().splitlines()]
        self.assertEqual(len(rows),2)
        self.assertEqual({r["boot_id"] for r in rows},{1,2})

    def test_background_archive_poll_runs_without_dashboard_proxy(self):
        payload=self.collect()
        self.assertEqual(payload["events"][0]["id"],1)
        self.assertEqual((self.panel.archive_cursor,self.panel.cursor),(1,1))
        self.assertEqual(len(self.gateway.calls),1)
        rows=[json.loads(line) for f in (Path(self.tmp.name)/"events").glob("*.jsonl") for line in f.read_text().splitlines()]
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]["ha_received_at"])

    def test_typed_report_fields_survive_snapshot_cache_and_archive(self):
        event={"schema_version":2,"id":1,"ts_ms":12000,"stage":"report-sent","zone":20,
               "requested":40,"confirmed":10000,"command_seq":0,"report_seq":40,
               "reported":10000,"reason":"command-settled","endpoint_ts_ms":0}
        class TypedGateway(StubGateway):
            def request(self,path,*args,**kwargs):
                code,ctype,raw=super().request(path,*args,**kwargs)
                data=json.loads(raw)
                if path.startswith("/api/snapshot"):
                    data["events"]["events"]=[event]
                return code,ctype,json.dumps(data).encode()
        self.panel.gateway=TypedGateway()
        self.collect()
        cached=json.loads(self.panel.proxy("events",[("after","0")])[2])["events"][0]
        self.assertEqual(cached,event)
        rows=[json.loads(line) for f in self.archive.root.glob("*.jsonl") for line in f.read_text().splitlines()]
        self.assertEqual(len(rows),1)
        self.assertEqual({k:rows[0][k] for k in event},event)
        self.assertEqual(rows[0]["boot_id"],1)

    def test_upload_limit_and_bytes_forward_exactly(self):
        self.assertEqual(app.MAX_FIRMWARE,786432)
        firmware=b"header\x00\xffimage"
        result=self.panel.proxy("update",body=firmware,content_type="application/octet-stream")
        self.assertEqual(result[0],202)
        self.assertEqual(self.gateway.calls[-1],("POST","/api/endpoint/update",firmware,"application/octet-stream"))
        handler=object.__new__(app.Handler)
        handler.headers={"Content-Length":str(app.MAX_FIRMWARE+1)}
        handler.rfile=io.BytesIO(b"x")
        with self.assertRaises(OverflowError): handler.read_body(app.MAX_FIRMWARE)

    def handler(self, peer="172.30.32.2", path="/"):
        h=object.__new__(app.Handler)
        h.client_address=(peer,1234);h.path=path;h.server=SimpleNamespace(panel=self.panel,html=b"ingress",ha=self.ha)
        h.headers=Message();h.rfile=io.BytesIO();h.results=[]
        h.reply=lambda code,value:h.results.append((code,value))
        h.send=lambda code,body,content_type="application/json":h.results.append((code,body,content_type))
        return h

    def test_handler_auth_route_and_cursor_validation(self):
        self.collect()
        h=self.handler(path="/api/status")
        h.do_GET()
        self.assertEqual(h.results[0][0],200)
        self.assertTrue(self.gateway.calls[-1][1].startswith("/api/snapshot"))
        denied=self.handler(peer="10.0.0.8",path="/api/status")
        denied.do_GET()
        self.assertEqual(denied.results[0][0],403)
        invalid=self.handler(path="/api/events?after=-1")
        invalid.do_GET()
        self.assertEqual(invalid.results[0][0],400)
        blocked=self.handler(path="/api/status?url=http://attacker")
        blocked.do_GET()
        self.assertEqual(blocked.results[0][0],404)

    def test_handler_upload_body_cap_and_binary_forward(self):
        binary=b"fw\x00\xff"
        h=self.handler(path="/api/endpoint/update")
        h.headers["Content-Type"]="application/octet-stream";h.headers["Content-Length"]=str(len(binary));h.rfile=io.BytesIO(binary)
        h.do_POST()
        self.assertEqual(h.results[0][0],202)
        self.assertEqual(self.gateway.calls[-1][2],binary)
        too_big=self.handler(path="/api/endpoint/update")
        too_big.headers["Content-Type"]="application/octet-stream";too_big.headers["Content-Length"]=str(app.MAX_FIRMWARE+1)
        too_big.do_POST()
        self.assertEqual(too_big.results[0][0],413)
        wrong_type=self.handler(path="/api/endpoint/update")
        wrong_type.headers["Content-Type"]="text/plain";wrong_type.headers["Content-Length"]="1"
        wrong_type.do_POST()
        self.assertEqual(wrong_type.results[0][0],415)

    def test_handler_transactional_config_put(self):
        doc={"generation":4,"hash":"abc123","zones":[{"zone_id":1,"name":"Kitchen","n4_group":"uuid","serial":"123","warm_dim_curve":1}]}
        body=json.dumps(doc).encode()
        h=self.handler("172.30.32.2","/api/config")
        h.command="PUT";h.headers["Content-Type"]="application/json";h.headers["Content-Length"]=str(len(body));h.rfile=io.BytesIO(body)
        h.do_PUT()
        self.assertEqual(h.results[0][0],200)
        method,path,forwarded,ctype=self.gateway.calls[-1]
        self.assertEqual((method,path,ctype),("PUT","/api/config","application/json"))
        self.assertEqual(json.loads(forwarded),doc)

    def test_timezone_proxy_validates_auth_and_writes(self):
        self.collect()
        with self.panel.lock: self.panel.cache["timezone"] = ({"timezone":"UTC0"}, 1)
        h=self.handler(path="/api/timezone");h.do_GET()
        self.assertEqual(json.loads(h.results[0][1])["timezone"],"UTC0")
        for zone, peer, expected in [("America/Los_Angeles","172.30.32.2",200),("No/Such_Zone","172.30.32.2",400),("America/Los_Angeles","10.0.0.8",403)]:
            body=json.dumps({"timezone":zone}).encode();h=self.handler(peer,path="/api/timezone")
            h.headers["Content-Type"]="application/json";h.headers["Content-Length"]=str(len(body));h.rfile=io.BytesIO(body);h.do_PUT()
            self.assertEqual(h.results[0][0],expected)
        self.assertEqual([c[1] for c in self.gateway.calls].count("/api/timezone"),1)
        self.assertEqual(self.panel.timezone_due,0)
        self.assertEqual(json.loads(self.panel.proxy("timezone")[2])["timezone"],"UTC0")

    def test_timezone_collector_gracefully_handles_old_gateway_and_retry(self):
        self.collect()
        original=self.gateway.request
        def respond(path, *args):
            if path == "/api/timezone": return 404,"application/json",b'{}'
            return original(path,*args)
        self.gateway.request=respond;self.panel.next_poll=0;self.panel.timezone_due=0;self.panel.config_due=float("inf")
        self.panel.archive_poll_once()
        self.assertTrue(json.loads(self.panel.proxy("timezone")[2])["unsupported"])
        self.assertTrue(self.panel.online)
        self.assertGreater(self.panel.timezone_due,app.time.monotonic())
        self.panel.next_poll=0;self.panel.archive_poll_once()
        self.assertTrue(self.gateway.calls[-1][1].startswith("/api/snapshot"))

    def test_gateway_adds_computed_firmware_digest_and_long_timeout(self):
        binary=b"abc\x00"
        captured={}
        class Response:
            status=202
            def read(self,n): return b'{"status":"rebooting"}'
            def getheader(self,*args): return "application/json"
        class Connection:
            def __init__(self,host,port,timeout): captured["timeout"]=timeout
            def request(self,method,path,body,headers): captured.update(method=method,path=path,body=body,headers=headers)
            def getresponse(self): return Response()
            def close(self): pass
        with patch("app.http.client.HTTPConnection",Connection):
            client=app.Gateway("http://localhost","token")
            self.assertEqual(client.request("/api/endpoint/update","POST",binary,"application/octet-stream")[0],202)
        import hashlib
        self.assertEqual(captured["headers"]["X-SHA256"],hashlib.sha256(binary).hexdigest())
        self.assertEqual(captured["timeout"],300)

    def test_reconnect_after_gateway_disconnect(self):
        self.collect()
        self.gateway.fail=True
        self.collect()
        self.assertFalse(self.panel.online)
        self.assertTrue(json.loads(self.panel.proxy("status")[2])["monitoring"]["delayed"])
        self.gateway.fail=False
        self.collect()
        self.assertTrue(self.panel.online)
        self.assertIsNone(self.panel.last_error)

    def test_firmware_reboot_status_and_endpoint_reconnect(self):
        self.collect()
        self.assertTrue(json.loads(self.panel.proxy("status")[2])["endpoint_online"])
        code,_,raw=self.panel.proxy("update",body=b"firmware",content_type="application/octet-stream")
        self.assertEqual(code,202)
        self.assertEqual(json.loads(raw)["status"],"rebooting")
        self.collect()
        self.assertFalse(json.loads(self.panel.proxy("status")[2])["endpoint_online"])
        self.gateway.endpoint_online=True;self.collect()
        self.assertTrue(json.loads(self.panel.proxy("status")[2])["endpoint_online"])

    def test_ten_browsers_never_add_upstream_requests(self):
        from concurrent.futures import ThreadPoolExecutor
        self.collect();calls=len(self.gateway.calls)
        def browse(_):
            for _ in range(20):
                for route in ("status","devices","events"):
                    self.assertEqual(self.panel.proxy(route)[0],200)
        with ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(browse,range(10)))
        self.assertEqual(len(self.gateway.calls),calls)

    def test_backoff_and_retry_after_cannot_be_bypassed_by_browser(self):
        self.gateway.fail=True;self.gateway.retry_after=0
        for n,delay in enumerate((2,4,8,16,30,30),1):
            with patch("app.time.monotonic",return_value=100):
                self.panel.next_poll=0;self.panel.archive_poll_once()
                self.assertEqual(self.panel.next_poll,100+delay)
                calls=len(self.gateway.calls)
                self.panel.archive_poll_once()
                self.panel.proxy("status");self.panel.proxy("events")
                self.assertEqual(len(self.gateway.calls),calls)
        self.gateway.retry_after=60
        with patch("app.time.monotonic",return_value=200):
            self.panel.next_poll=0;self.panel.archive_poll_once()
            self.assertEqual(self.panel.next_poll,260)

    def test_program_cache_busy_and_explicit_refresh(self):
        query=[("zone","0"),("page","0")]
        self.assertEqual(self.panel.proxy("program",query)[0],200)
        calls=len(self.gateway.calls)
        self.panel.proxy("program",query)
        self.assertEqual(len(self.gateway.calls),calls)
        self.panel.proxy("program",query+[("refresh","1")])
        self.assertEqual(len(self.gateway.calls),calls+1)
        with self.panel.upstream:
            self.assertEqual(self.panel.proxy("program",[("zone","1")])[0],503)
            self.assertIsNone(self.panel.archive_poll_once())
        self.assertEqual(len(self.gateway.calls),calls+1)

    def test_legacy_collector_is_serial_and_browser_reads_are_cached(self):
        class Legacy(StubGateway):
            def request(self,path,*args):
                if path.startswith("/api/snapshot"):
                    self.calls.append(("GET",path,None,None));return 404,"application/json",b'{}'
                return super().request(path,*args)
        self.panel.gateway=Legacy()
        self.collect();self.assertFalse(self.panel.snapshot_supported)
        self.collect();self.collect()
        calls=len(self.panel.gateway.calls)
        self.panel.proxy("status");self.panel.proxy("devices")
        self.assertEqual(len(self.panel.gateway.calls),calls)

    def test_gaps_inside_cached_event_history_are_reported(self):
        with self.panel.lock:
            self.panel._events({"boot_id":1,"events":[{"id":1},{"id":5}]})
        data=json.loads(self.panel.proxy("events",[("after","1"),("boot_id","1")])[2])
        self.assertEqual(data["lost"],3)
        self.assertEqual([e["id"] for e in data["events"]],[5])

    def test_optional_config_failure_does_not_starve_snapshots(self):
        self.collect()
        class DeferredConfig(StubGateway):
            retry_after=2
            def request(self,path,*args):
                if path=="/api/config":return 503,"application/json",b'{}'
                return super().request(path,*args)
        self.panel.gateway=DeferredConfig()
        self.panel.next_poll=self.panel.config_due=0
        self.panel.archive_poll_once()
        self.assertGreater(self.panel.config_due,0)
        self.assertTrue(self.panel.online)
        self.panel.next_poll=0
        self.panel.archive_poll_once()
        self.assertTrue(self.panel.gateway.calls[-1][1].startswith("/api/snapshot"))

    def test_legacy_gateway_upgrade_is_detected_without_app_restart(self):
        self.panel.snapshot_supported=False
        self.panel.snapshot_retry_at=50
        with patch("app.time.monotonic",return_value=100):
            self.collect()
        self.assertTrue(self.panel.snapshot_supported)
        self.assertTrue(self.gateway.calls[-1][1].startswith("/api/snapshot"))

    def test_inflight_collection_does_not_queue_browser_operations(self):
        import threading
        self.collect()
        entered=threading.Event();release=threading.Event()
        class SlowGateway(StubGateway):
            def request(self,*args):
                entered.set();release.wait(3)
                return super().request(*args)
        self.panel.gateway=SlowGateway();self.panel.next_poll=0
        worker=threading.Thread(target=self.panel.archive_poll_once)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            for _ in range(10):
                self.assertEqual(self.panel.proxy("status")[0],200)
                self.assertEqual(self.panel.proxy("program")[0],503)
                self.assertIsNone(self.panel.archive_poll_once())
            self.assertEqual(len(self.panel.gateway.calls),0)
        finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.panel.gateway.calls),1)

    def test_maintenance_is_explicit_and_can_end_during_monitoring_backoff(self):
        bad=self.handler(path="/api/maintenance")
        bad.headers["Content-Length"]="16";bad.rfile=io.BytesIO(b'{"enabled":"no"}')
        bad.do_POST();self.assertEqual(bad.results[0][0],400)
        self.panel.last_error="offline";self.panel.next_poll=float("inf")
        self.assertEqual(self.panel.proxy("maintenance",body=b'{"enabled":false}',content_type="application/json")[0],200)
        self.assertEqual(self.gateway.calls[-1][0:2],("POST","/api/maintenance"))

    def test_gateway_response_size_limit(self):
        class Response:
            status=200
            def read(self,n): return b"x"*(app.MAX_RESPONSE+1)
            def getheader(self,*args): return "application/json"
        class Connection:
            def __init__(self,*args,**kwargs): pass
            def request(self,*args,**kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch("app.http.client.HTTPConnection",Connection):
            client=app.Gateway("http://localhost","token")
            with self.assertRaises(ValueError): client.request("/api/status")

    def test_home_assistant_snapshot_normalises_and_caches(self):
        snap=self.ha.snapshot()
        self.assertTrue(snap["available"])
        self.assertEqual(snap["time_zone"],"Europe/London")
        self.assertEqual(snap["sun"],{"next_rising":"2026-10-26T06:52:00+00:00","next_setting":None})
        self.assertEqual([l["entity_id"] for l in snap["lights"]],["light.kitchen","light.bad"])
        self.assertEqual(snap["lights"][0]["serial"],"12345678")
        self.assertEqual((snap["lights"][0]["zone_id"],snap["lights"][1]["zone_id"]),("1379",None))
        self.assertEqual((snap["lights"][1]["brightness"],snap["lights"][1]["color_temp_kelvin"],snap["lights"][1]["serial"]),(None,None,None))
        method,path,body=self.ha.calls[-1]
        self.assertEqual((method,path),("POST","/template"))
        self.assertEqual(body["variables"],{"integrations":["lutron_caseta"]})
        self.assertEqual(body["template"],app.HA_TEMPLATE)
        calls=len(self.ha.calls);self.ha.snapshot();self.assertEqual(len(self.ha.calls),calls)

    def test_home_assistant_unavailable_without_token_or_on_error(self):
        self.assertFalse(app.HomeAssistant("",["lutron"]).snapshot()["available"])
        self.ha.fail=True
        snap=self.ha.snapshot()
        self.assertFalse(snap["available"])
        self.assertNotIn("supervisor-token",json.dumps(snap))

    def test_handler_home_assistant_route_is_fixed(self):
        h=self.handler(path="/api/ha");h.do_GET()
        self.assertEqual(h.results[0][0],200)
        self.assertTrue(h.results[0][1]["available"])
        for path in ("/api/ha?template=x","/api/ha/states"):
            blocked=self.handler(path=path);blocked.do_GET()
            self.assertEqual(blocked.results[0][0],404)
        denied=self.handler(peer="10.0.0.8",path="/api/ha");denied.do_GET()
        self.assertEqual(denied.results[0][0],403)


# Show 31779 as transferred to the Office A20: minute, 16-bit level, Kelvin, vibrancy.
SHOW_31779=[{"minute":m,"level":l,"cct":c,"field2":v} for m,l,c,v in
            ((240,19123,1800,25),(377,25717,2130,25),(600,65279,4000,35),(1020,65279,4000,35),(1206,25717,2130,25),(1320,19123,1800,25))]


def at(hour,minute=0): return (hour*60+minute)*60000


class FakeMqtt:
    def __init__(self): self.messages=[];self.closed=False;self.fail=False
    def publish(self,topic,payload,retain=False):
        if self.fail: raise ConnectionError("broker gone")
        self.messages.append((topic,payload.decode() if isinstance(payload,bytes) else payload,retain))
    def service(self):
        if self.fail: raise ConnectionError("broker closed the connection")
    def close(self): self.closed=True
    def topics(self,suffix): return [m for m in self.messages if m[0].endswith(suffix)]


class ShowSensorTests(unittest.TestCase):
    def test_show_evaluation_matches_endpoint_arithmetic(self):
        self.assertEqual([app.level16_to_x100(v) for v in (0,1,32640,65279,65535)],[0,100,5050,10000,10000])
        knot=app.show_state(SHOW_31779,at(4))
        self.assertEqual(knot,{"level_x100":app.level16_to_x100(19123),"cct":1800,"vibrancy":25})
        # 19:35 is 155 of the 186 minutes from 17:00 (4000 K) to 20:06 (2130 K): 2441.67 K rounds to 2442.
        evening=app.show_state(SHOW_31779,at(19,35))
        self.assertEqual((evening["cct"],evening["vibrancy"]),(2442,27))
        self.assertEqual(app.show_state(SHOW_31779,at(2))["cct"],1800)  # overnight segment wraps 22:00 → 04:00
        wrap=[{"minute":60,"level":0,"cct":2000,"field2":None},{"minute":1380,"level":65279,"cct":4000,"field2":None}]
        midnight=app.show_state(wrap,0)
        self.assertEqual((midnight["cct"],midnight["level_x100"],midnight["vibrancy"]),(3000,5050,None))
        self.assertEqual(app.show_state(wrap[:1],at(12))["cct"],2000)
        self.assertIsNone(app.show_state([],0));self.assertIsNone(app.show_state(SHOW_31779,app.DAY_MS))

    def test_programming_snapshot_validation(self):
        data={"selected":True,"show":31779,"programmed":True,"brightness_override_x100":2500,
              "shows":[{"id":31779,"knots":SHOW_31779},{"id":5,"knots":[{"minute":1440,"level":0,"cct":0,"field2":None}]},
                       {"id":"6","knots":SHOW_31779},"junk"]}
        program=app.show_program(data)
        self.assertEqual(sorted(program["shows"]),[31779])
        self.assertEqual((program["selected"],program["show"],program["override_x100"]),(True,31779,2500))
        self.assertIsNone(app.show_program({"selected":"yes"}))
        self.assertIsNone(app.show_program([]))
        self.assertIsNone(app.show_program(dict(data,brightness_override_x100=10001))["override_x100"])

    def test_zone_state_separates_target_requested_and_confirmed(self):
        program=app.show_program({"selected":True,"show":31779,"programmed":True,"brightness_override_x100":None,
                                  "shows":[{"id":31779,"knots":SHOW_31779}]})
        zone={"zone":0,"zone_id":30861,"serial":90130861,"name":"office",
              "requested":{"flags":0x0b,"level_x100":4950,"cct":2442,"x":4600,"y":4100,"vibrancy":27,"seq":9},
              "confirmed":{"level_x100":4949,"source":"observed","seq":9,"at_ms":5}}
        state=app.zone_show_state(zone,program,10,at(19,35))
        self.assertEqual((state["active_show"],state["target_cct"],state["requested_cct"]),("31779",2442,2442))
        self.assertEqual((state["requested_brightness"],state["confirmed_brightness"]),(49.5,49.49))
        self.assertEqual(state["target_brightness"],app.level16_to_x100(app._interpolate(65279,25717,at(2,35),at(3,6)))/100)
        self.assertEqual(state["attributes"]["target_vibrancy"],27)
        self.assertIsNone(state["attributes"]["requested_xy"])  # XY flag not set
        override=dict(program,override_x100=10000)
        self.assertEqual(app.zone_show_state(zone,override,10,at(19,35))["target_brightness"],100.0)
        off=dict(zone,requested={"flags":0x01,"level_x100":0,"cct":2442})
        self.assertEqual((app.zone_show_state(off,program,10,at(19,35))["requested_cct"],
                          app.zone_show_state(off,program,10,at(19,35))["requested_brightness"]),(None,0.0))
        xy=dict(zone,requested={"flags":0x05,"level_x100":5000,"x":5137,"y":4164})
        self.assertEqual(app.zone_show_state(xy,program,10,at(12))["attributes"]["requested_xy"],[0.5137,0.4164])
        self.assertEqual(app.zone_show_state(zone,dict(program,selected=False),10,at(12))["active_show"],"not_selected")
        stale=app.zone_show_state(zone,program,app.PROGRAM_STALE_S+1,at(12))
        self.assertEqual((stale["active_show"],stale["target_cct"],stale["requested_cct"]),(None,None,2442))
        self.assertIsNone(app.zone_show_state({"zone":0},None,None,at(12))["active_show"])

    def test_mqtt_packets_and_connect_handshake(self):
        import socket,threading
        self.assertEqual(app._mqtt_packet(0x30,b"x"*321)[:3],b"\x30\xc1\x02")
        listener=socket.socket();listener.bind(("127.0.0.1",0));listener.listen(1)
        received=[]
        def broker(code):
            conn,_=listener.accept()
            received.append(conn.recv(4096));conn.sendall(bytes([0x20,2,0,code]))
            received.append(conn.recv(4096));conn.close()
        thread=threading.Thread(target=broker,args=(0,));thread.start()
        client=app.MqttClient("127.0.0.1",listener.getsockname()[1],"addons","pw","cid","ccx_bridge/status").connect()
        client.publish("t/a","on",retain=True);client.close();thread.join(5)
        connect=received[0]
        self.assertEqual(connect[0],0x10)
        self.assertIn(b"\x00\x04MQTT\x04\xe6\x00\x3c",connect)  # v3.1.1, user+password+retained will+clean session
        for part in (b"cid",b"ccx_bridge/status",b"offline",b"addons",b"pw"):self.assertIn(part,connect)
        self.assertTrue(received[1].startswith(b"\x31\x07\x00\x03t/aon"))
        thread=threading.Thread(target=broker,args=(5,));thread.start()
        with self.assertRaises(ConnectionError):
            app.MqttClient("127.0.0.1",listener.getsockname()[1],None,None,"cid","s").connect()
        listener.close();thread.join(5)

    def test_supervisor_mqtt_service_lookup(self):
        class Conn:
            reply=(200,{"result":"ok","data":{"host":"core-mosquitto","port":1883,"ssl":False,"username":"addons","password":"pw"}})
            def __init__(self,*a,**k):pass
            def request(self,method,path,headers):Conn.seen=(method,path,headers["Authorization"])
            def getresponse(self):return SimpleNamespace(status=Conn.reply[0],read=lambda n:json.dumps(Conn.reply[1]).encode())
            def close(self):pass
        with patch.object(app.http.client,"HTTPConnection",Conn):
            self.assertEqual(app.supervisor_mqtt("tok"),{"host":"core-mosquitto","port":1883,"username":"addons","password":"pw"})
            self.assertEqual(Conn.seen,("GET","/services/mqtt","Bearer tok"))
            for reply in ((400,{}),(200,[]),(200,{"data":{"host":"h","ssl":True}})):
                Conn.reply=reply
                with self.assertRaises(ConnectionError):app.supervisor_mqtt("tok")


class ShowCollectorTests(unittest.TestCase):
    ZONES=[{"zone":0,"zone_id":30861,"serial":90130861,"name":"office","requested":{"flags":3,"level_x100":4950,"cct":2442},
            "confirmed":{"level_x100":4950,"source":"observed"}},
           {"zone":1,"zone_id":0,"serial":0,"name":"spare"}]
    PROGRAM={"serial":90130861,"programmed":True,"selected":True,"show":31779,"members":[30861],"presets":[],
             "shows":[{"id":31779,"knots":SHOW_31779}],"brightness_override_x100":None}

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        test=self
        class Gateway(StubGateway):
            program_code=200
            def request(self,path,method="GET",body=None,content_type=None,timeout=None):
                if path.startswith("/api/program"):
                    self.calls.append((method,path,timeout))
                    if self.fail:raise OSError("reset")
                    return self.program_code,"application/json",json.dumps(test.PROGRAM).encode()
                code,ct,raw=super().request(path,method,body,content_type)
                if path.startswith("/api/snapshot"):
                    data=json.loads(raw);data["devices"]={"zones":test.ZONES};raw=json.dumps(data).encode()
                return code,ct,raw
        self.gateway=Gateway()
        self.panel=app.Panel(self.gateway,app.EventArchive(Path(self.tmp.name)/"events"))
        self.panel.track_programs=True
        self.ha=StubHomeAssistant("supervisor-token",["lutron_caseta"])
        self.mqtt=FakeMqtt()
        self.sensors=app.ShowSensors(self.panel,self.ha,lambda:self.mqtt,
                                     now=lambda tz:app.datetime(2026,10,28,19,35,tzinfo=tz))
    def tearDown(self): self.tmp.cleanup()

    def collect(self):
        self.panel.next_poll=0;self.panel.config_due=float("inf");self.panel.timezone_due=float("inf")
        return self.panel.archive_poll_once()

    def test_programming_reads_take_bounded_slots_and_feed_the_dashboard_cache(self):
        self.collect();self.collect()
        self.assertEqual(self.gateway.calls[-1],("GET","/api/program?zone=0&page=0",app.PROGRAM_TIMEOUT_S))
        self.assertEqual(self.panel.zone_programs[0][0]["show"],31779)
        calls=len(self.gateway.calls)
        self.assertEqual(json.loads(self.panel.proxy("program",[("zone","0"),("page","0")])[2])["show"],31779)
        self.assertEqual(len(self.gateway.calls),calls)
        self.panel.program_due=0;self.collect()  # zone 0 was just read and zone 1 has no serial
        self.assertTrue(self.gateway.calls[-1][1].startswith("/api/snapshot"))
        self.panel.track_programs=False;self.panel.program_attempts.clear();self.collect()
        self.assertTrue(self.gateway.calls[-1][1].startswith("/api/snapshot"))

    def test_programming_failures_defer_without_marking_monitoring_offline(self):
        self.collect()
        for failure in ("code","exception"):
            self.panel.program_attempts.clear();self.panel.program_due=0
            self.gateway.program_code,self.gateway.fail=(503,False) if failure=="code" else (200,True)
            with patch("app.time.monotonic",return_value=1000.0):self.collect()
            self.assertTrue(self.gateway.calls[-1][1].startswith("/api/program"))
            self.assertGreaterEqual(self.panel.program_due,1000+app.PROGRAM_SLOT_S)
            self.assertTrue(self.panel.online);self.assertIsNone(self.panel.last_error)
            self.assertNotIn(0,self.panel.zone_programs)
        self.gateway.fail=False
        with patch("app.time.monotonic",return_value=1001.0):self.collect()
        self.assertTrue(self.gateway.calls[-1][1].startswith("/api/snapshot"))

    def test_new_boot_discards_programming(self):
        self.collect();self.collect();self.assertIn(0,self.panel.zone_programs)
        with self.panel.lock:self.panel._events({"boot_id":99,"events":[]})
        self.assertEqual((self.panel.zone_programs,self.panel.program_attempts),({},{}))

    def test_sensors_publish_discovery_state_and_availability(self):
        self.sensors.step()
        self.assertTrue(self.panel.track_programs)
        self.assertEqual(self.mqtt.topics("ccx_bridge/status"),[("ccx_bridge/status","offline",True)])
        self.collect();self.collect()
        self.mqtt.messages.clear();self.sensors.step()
        configs=self.mqtt.topics("/config")
        self.assertEqual(len(configs),len(app.SHOW_SENSORS))
        self.assertTrue(all(c[0].startswith("homeassistant/sensor/ccx_bridge_30861/") and c[2] for c in configs))
        show=json.loads(next(c[1] for c in configs if "/active_show/" in c[0]))
        self.assertEqual((show["unique_id"],show["device"]["name"],show["device"]["serial_number"]),
                         ("ccx_bridge_30861_active_show","CCX Bridge office","90130861"))
        self.assertEqual(show["availability_topic"],"ccx_bridge/status")
        self.assertEqual(json.loads(next(c[1] for c in configs if "/target_cct/" in c[0]))["unit_of_measurement"],"K")
        (topic,payload,retain),=self.mqtt.topics("/state")
        state=json.loads(payload)
        self.assertEqual((topic,retain),("ccx_bridge/zone/30861/state",True))
        self.assertEqual((state["active_show"],state["target_cct"],state["requested_cct"]),("31779",2442,2442))
        self.assertEqual(state["attributes"]["time_zone"],"Europe/London")
        self.assertEqual(self.mqtt.topics("ccx_bridge/status"),[("ccx_bridge/status","online",True)])
        self.mqtt.messages.clear();self.sensors.step();self.assertEqual(self.mqtt.messages,[])  # unchanged: silent
        with self.panel.lock:
            data,at_=self.panel.cache["devices"];self.panel.cache["devices"]=({"zones":[]},at_-app.SNAPSHOT_STALE_S-1)
        self.sensors.step()
        self.assertEqual(len(self.mqtt.topics("/config")),len(app.SHOW_SENSORS))
        self.assertTrue(all(m[1]=="" and m[2] for m in self.mqtt.topics("/config")+self.mqtt.topics("/state")))
        self.assertEqual(self.mqtt.topics("ccx_bridge/status")[-1][1],"offline")

    def test_gateway_timezone_overrides_home_assistant_for_show_targets(self):
        self.collect();self.collect();self.sensors.step()
        with self.panel.lock:
            self.panel.cache["timezone"] = ({"timezone":"UTC0"}, app.time.monotonic())
        self.mqtt.messages.clear();self.sensors.step()
        state=json.loads(self.mqtt.topics("/state")[-1][1])
        self.assertEqual(state["attributes"]["time_zone"],"UTC")
        with self.panel.lock:
            self.panel.cache["timezone"] = ({"timezone":"America/Los_Angeles"}, app.time.monotonic())
        self.mqtt.messages.clear();self.sensors.step()
        state=json.loads(self.mqtt.topics("/state")[-1][1])
        self.assertEqual(state["attributes"]["time_zone"],"America/Los_Angeles")
        with self.panel.lock:
            self.panel.cache["timezone"] = ({"timezone":"unsupported-posix"}, app.time.monotonic())
        self.mqtt.messages.clear();self.sensors.step()
        state=json.loads(self.mqtt.topics("/state")[-1][1])
        self.assertIsNone(state["attributes"]["time_zone"])
        self.assertIsNone(state["target_cct"])

    def test_sensors_reconnect_and_stop_program_reads_without_a_broker(self):
        self.sensors.step();self.mqtt.fail=True;self.sensors.step()
        self.assertIsNone(self.sensors.client);self.assertTrue(self.mqtt.closed)
        self.assertFalse(self.panel.track_programs)
        attempts=[]
        def refused():
            attempts.append(1);raise ConnectionError("no broker")
        self.sensors.connect=refused;self.sensors.retry_at=0
        with self.assertLogs("c6-management","WARNING"):self.sensors.step()
        self.sensors.step();self.assertEqual(len(attempts),1)  # backed off
        self.assertFalse(self.panel.track_programs)


# Sitting Room's show as transferred on 2026-10-28 (30% overnight, 100% / 3000 K by day).
SITTING_SHOW=[{"minute":240,"level":19123,"cct":1800,"field2":25},{"minute":377,"level":25717,"cct":1980,"field2":25},
              {"minute":600,"level":65279,"cct":3000,"field2":27},{"minute":1020,"level":65279,"cct":3000,"field2":27},
              {"minute":1206,"level":25717,"cct":1980,"field2":25},{"minute":1320,"level":19123,"cct":1800,"field2":25}]


def light(state,brightness=None,kelvin=None,zone_id="31467"):
    return {"entity_id":"light.lamp","name":"sitting room lamp","state":state,"brightness":brightness,
            "color_temp_kelvin":kelvin,"zone_id":zone_id,"serial":None,"area":"Sitting Room"}


def bridged(level,source="observed",seq=1,requested=None,at_ms=99_000,color=None,last_command_ms=50_000):
    confirmed={"level_x100":level,"source":source,"seq":seq,"at_ms":at_ms}
    if color: confirmed["color"]=color
    return {"zone":1,"zone_id":31467,"serial":90131467,"name":"Sitting Room A20","n4_group":"Sitting Room",
            "requested":requested,"confirmed":confirmed,"last_command_ms":last_command_ms,"last_confirmed_ms":at_ms}


class ZoneSummaryTests(unittest.TestCase):
    def program(self,selected=True,override=None):
        return app.show_program({"selected":selected,"show":31779 if selected else 0,"programmed":True,
                                 "brightness_override_x100":override,"shows":[{"id":31779,"knots":SITTING_SHOW}]})

    def statuses(self,summary):
        return {c["key"]:c["status"] for c in summary["checks"]}

    def test_lamp_on_while_lutron_off_is_out_of_sync(self):
        # Office after the 12:32 power cut: N4 at 100%, Lutron off, no command since boot.
        s=app.zone_summary(bridged(10000),self.program(False),10,at(14,28),light("off"),100_000)
        self.assertEqual(s["verdict"]["tone"],"bad")
        self.assertEqual(s["verdict"]["title"],"Lamp is on at 100%, Lutron shows off")
        self.assertEqual(self.statuses(s)["show"],"info")
        self.assertIsNone(s["sent"])
        both_off=app.zone_summary(bridged(0),self.program(False),10,at(14,28),light("off"),100_000)
        self.assertEqual((both_off["verdict"]["tone"],self.statuses(both_off)["lutron"]),("good","good"))
        lamp_off=app.zone_summary(bridged(0),self.program(False),10,at(14,28),light("on",255),100_000)
        self.assertEqual(lamp_off["verdict"]["title"],"Lutron shows on, lamp is off")

    def test_show_running_compares_lamp_with_the_show_not_lutrons_keyframe(self):
        # 09:16:52: the show calls for 88.40% / 2803 K; Lutron reports the 10:00 keyframe (100%, 3000 K).
        local=at(9,16)+52_000
        sent={"flags":0x0b,"level_x100":8840,"cct":2803,"vibrancy":27,"seq":16,"transition_ms":0,"delay_ms":0}
        zone=bridged(8840,"applied",16,sent,color={"power":True,"cct":2803,"x":0,"y":0,"vibrancy":27})
        s=app.zone_summary(zone,self.program(),10,local,light("on",255,3000),100_000)
        self.assertEqual(s["verdict"],{"tone":"good","title":"In sync · following Natural Show"})
        self.assertEqual(self.statuses(s),{"lutron":"info","hub":"good","show":"good"})
        self.assertEqual((s["show"]["target"]["level_x100"],s["show"]["target"]["cct"]),(8840,2803))
        self.assertEqual(s["show"]["next"],{"minute":600,"level_x100":10000,"cct":3000})
        self.assertEqual((s["show"]["curve"][0]["minute"],s["show"]["curve"][-1]["minute"],len(s["show"]["curve"])),(0,1440,97))
        self.assertEqual(s["show"]["curve"][16]["level_x100"],3000)  # 04:00, still the overnight level
        self.assertEqual(s["lamp"]["cct"],2803)
        drift=app.zone_summary(bridged(7000,"observed",16,sent),self.program(),10,local,light("on",255,3000),100_000)
        self.assertEqual(drift["verdict"]["title"],"Show calls for 88.4%, lamp at 70%")
        warm=app.zone_summary(bridged(8840,"applied",16,dict(sent,cct=2700)),self.program(),10,local,light("on",255),100_000)
        self.assertEqual(warm["verdict"]["title"],"Show calls for 2803 K, lamp at 2700 K")
        override=app.zone_summary(bridged(10000,"applied",16,dict(sent,level_x100=10000)),self.program(override=10000),10,local,light("on",255),100_000)
        self.assertEqual(self.statuses(override)["show"],"good")
        self.assertEqual(override["show"]["target"]["show_level_x100"],8840)

    def test_fixed_level_needs_lamp_and_lutron_to_agree(self):
        sent={"flags":0x05,"level_x100":7000,"x":3457,"y":3585,"seq":240,"transition_ms":0,"delay_ms":0}
        ok=app.zone_summary(bridged(7000,"applied",240,sent),self.program(False),10,at(9,20),light("on",179),100_000)
        self.assertEqual(ok["verdict"]["title"],"In sync")  # 179/255 is 70.2%
        self.assertEqual(ok["sent"]["xy"],[0.3457,0.3585])
        off_by=app.zone_summary(bridged(7000,"applied",240,sent),self.program(False),10,at(9,20),light("on",128),100_000)
        self.assertEqual(off_by["verdict"],{"tone":"warn","title":"Lamp at 70%, Lutron shows 50.2%"})
        moving=bridged(3000,"observed",240,dict(sent,transition_ms=10_000),last_command_ms=95_000)
        self.assertEqual(self.statuses(app.zone_summary(moving,self.program(False),10,at(9,20),light("on",179),100_000))["hub"],"wait")

    def test_unknowns_never_claim_sync(self):
        legacy=bridged(5000,requested=5000);legacy.pop("last_command_ms")
        s=app.zone_summary(legacy,None,None,at(12),None,100_000)
        self.assertEqual(s["sent"]["level_x100"],5000)
        self.assertEqual(self.statuses(s)["lutron"],"unknown")
        self.assertEqual(self.statuses(s)["show"],"unknown")
        self.assertEqual(s["verdict"]["tone"],"info")
        stale=app.zone_summary(bridged(5000,at_ms=1000),None,None,at(12),light("on",128),300_000)
        self.assertEqual(stale["verdict"],{"tone":"warn","title":"No recent reading from the hub"})
        empty=app.zone_summary({"zone":3,"confirmed":None},None,None,None,light("unavailable"),1)
        self.assertEqual(empty["verdict"]["tone"],"wait")

    def test_lights_match_by_zone_then_serial_then_name(self):
        lights=[light("on",1,zone_id="1"),dict(light("on",2,zone_id=None),serial="90131467",name="other"),
                dict(light("on",3,zone_id=None),name="Sitting Room A20")]
        self.assertEqual(app.match_light({"zone_id":1,"serial":90131467},lights)["brightness"],1)
        self.assertEqual(app.match_light({"zone_id":5,"serial":90131467},lights)["brightness"],2)
        self.assertEqual(app.match_light({"zone_id":5,"name":"sitting room a20"},lights)["brightness"],3)
        self.assertIsNone(app.match_light({"zone_id":5},lights))

    def test_zone_route_uses_cache_only_and_asks_for_programming(self):
        gateway=StubGateway();panel=app.Panel(gateway,app.EventArchive(tempfile.mkdtemp()))
        panel.cache["devices"]=({"zones":[bridged(10000)]},app.time.monotonic())
        panel.cache["status"]=({"gateway_uptime_ms":100_000},app.time.monotonic())
        result=panel.zone_summaries({"available":True,"lights":[light("off")]},lambda tz:(at(14,28),"America/Los_Angeles"))
        self.assertEqual(gateway.calls,[])
        self.assertGreater(panel.programs_wanted_until,app.time.monotonic())
        self.assertEqual((result["home_assistant"],result["time_zone"]),(True,"America/Los_Angeles"))
        self.assertEqual(result["zones"][0]["verdict"]["tone"],"bad")
        self.assertIsNone(panel.zone_summaries({"available":False},lambda tz:(None,None))["zones"][0]["lutron"])


if __name__=="__main__": unittest.main()
