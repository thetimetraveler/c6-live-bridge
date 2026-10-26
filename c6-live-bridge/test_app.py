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
             "color_temp_kelvin":3000,"area":"Kitchen","device_id":"abc","device":"Kitchen","serial":12345678},
            {"entity_id":"light.bad","brightness":999,"color_temp_kelvin":"warm","serial":""},"junk"]}).encode()


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
        panel.next_poll=0;panel.config_due=float("inf")
        return panel.archive_poll_once()

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


if __name__=="__main__": unittest.main()
