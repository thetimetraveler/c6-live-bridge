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
        if path == "/api/status":
            return 200,"application/json",json.dumps({"wifi_up":True,"endpoint_online":self.endpoint_online,"endpoint_uptime_s":60 if self.endpoint_online else 0}).encode()
        if path == "/api/endpoint/update":
            self.endpoint_online=False
            return 202,"application/json",b'{"status":"rebooting"}'
        return 200,"application/json",json.dumps({"after":0,"next":2,"oldest":1,"lost":0,"events":[{"id":1,"stage":"confirmed"}]}).encode()


class AddonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.gateway=StubGateway()
        self.archive=app.EventArchive(Path(self.tmp.name)/"events")
        self.panel=app.Panel(self.gateway,self.archive)
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

    def test_only_fixed_paths_forward_and_event_cursor(self):
        code,_,raw=self.panel.proxy("events",[("after","0")])
        self.assertEqual(code,200)
        self.assertEqual(self.gateway.calls[-1][:2],("GET","/api/events?after=0"))
        self.assertEqual(self.panel.cursor,1)
        with self.assertRaises(KeyError): self.panel.proxy("/api/evil")
        self.assertEqual(self.gateway.calls[-1][1],"/api/events?after=0")
        with self.assertRaises(ValueError): self.panel.proxy("events",[("after","-1")])
        files=list((Path(self.tmp.name)/"events").glob("*.jsonl"))
        self.assertEqual(len(files),1)
        self.assertIn('"id":1',files[0].read_text())

    def test_event_boot_reset_rewinds_cursor_and_archive_dedup_is_boot_scoped(self):
        class BootGateway:
            def __init__(self): self.boot="A";self.calls=[]
            def request(self,path,method="GET",body=None,content_type=None):
                self.calls.append(path)
                after=int(path.split("after=",1)[1]) if "after=" in path else 0
                events=[] if after and self.boot=="B" else [{"id":1,"ts_ms":2500,"stage":"confirmed"}]
                body=json.dumps({"boot_id":self.boot,"after":after,"next_sequence":2,"oldest":1,"lost":0,"events":events}).encode()
                return 200,"application/json",body
        gw=BootGateway();panel=app.Panel(gw,self.archive)
        panel.proxy("events",[("after","0")]);self.assertEqual((panel.boot_id,panel.cursor),("A",1))
        gw.boot="B";panel.proxy("events",[("after","1")])
        self.assertEqual(gw.calls[-2:], ["/api/events?after=1","/api/events?after=0"])
        self.assertEqual((panel.boot_id,panel.cursor),("B",1))
        panel.archive.record({"boot_id":"B","events":[{"id":1,"ts_ms":2500,"stage":"confirmed"}]})
        rows=[json.loads(line) for f in (Path(self.tmp.name)/"events").glob("*.jsonl") for line in f.read_text().splitlines()]
        self.assertEqual(len(rows),2)
        self.assertEqual({r["boot_id"] for r in rows},{"A","B"})
        self.assertTrue(all(r.get("ha_received_at") for r in rows))

    def test_background_archive_poll_runs_without_dashboard_proxy(self):
        payload=self.panel.archive_poll_once()
        self.assertEqual(payload["events"][0]["id"],1)
        self.assertEqual(self.panel.archive_cursor,1)
        self.assertEqual(self.panel.cursor,0)
        self.assertEqual(self.gateway.calls[-1][1],"/api/events?after=0")
        rows=[json.loads(line) for f in (Path(self.tmp.name)/"events").glob("*.jsonl") for line in f.read_text().splitlines()]
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]["ha_received_at"])

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
        h.client_address=(peer,1234);h.path=path;h.server=SimpleNamespace(panel=self.panel,html=b"ingress")
        h.headers=Message();h.rfile=io.BytesIO();h.results=[]
        h.reply=lambda code,value:h.results.append((code,value))
        h.send=lambda code,body,content_type="application/json":h.results.append((code,body,content_type))
        return h

    def test_handler_auth_route_and_cursor_validation(self):
        h=self.handler(path="/api/status")
        h.do_GET()
        self.assertEqual(h.results[0][0],200)
        self.assertEqual(self.gateway.calls[-1][1],"/api/status")
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
        self.gateway.fail=True
        with self.assertRaises(OSError): self.panel.proxy("status")
        self.assertFalse(self.panel.online)
        self.gateway.fail=False
        self.assertEqual(self.panel.proxy("status")[0],200)
        self.assertTrue(self.panel.online)
        self.assertIsNone(self.panel.last_error)

    def test_firmware_reboot_status_and_endpoint_reconnect(self):
        self.assertTrue(json.loads(self.panel.proxy("status")[2])["endpoint_online"])
        code,_,raw=self.panel.proxy("update",body=b"firmware",content_type="application/octet-stream")
        self.assertEqual(code,202)
        self.assertEqual(json.loads(raw)["status"],"rebooting")
        self.assertFalse(json.loads(self.panel.proxy("status")[2])["endpoint_online"])
        self.gateway.endpoint_online=True
        self.assertTrue(json.loads(self.panel.proxy("status")[2])["endpoint_online"])

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


if __name__=="__main__": unittest.main()
