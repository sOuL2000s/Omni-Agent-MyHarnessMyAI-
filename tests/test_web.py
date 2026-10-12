"""Tests for the web UI server: auth, chat run with tool calls + approval, key/model management.
Run: python tests/test_web.py"""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_failover import Gateway, make_cfg, ok                      # noqa: E402
from test_agent import tc                                           # noqa: E402
from agent.web import server                                        # noqa: E402

TOKEN = "t0ken"
SID = "abcdef0123456789"


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.cfg.auto_approve = False
        self.step = 0

        def handler(c):
            self.step += 1
            if self.step == 1:
                return ok(tool_calls=[tc(1, "run_shell", {"command": "echo hello-from-shell"})])
            return ok("all done")
        self.gw = Gateway(handler)
        self.cfg.providers.add_keys("mock", ["secret-key-123456"], self.gw.url, "m-good")
        self.app = server.App(self.cfg, TOKEN, {"127.0.0.1", "localhost"})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.gw.close()
        self.tmp.cleanup()

    # -- tiny client
    def req(self, method, path, body=None, cookie=True, headers=None, raw=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"X-Omni": "1", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = f"omni_token={TOKEN}"
        if host:
            h["Host"] = host
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        txt = r.read()
        c.close()
        try:
            return r.status, json.loads(txt), r
        except ValueError:
            return r.status, txt, r

    def test_auth(self):
        self.assertEqual(self.req("GET", "/api/state", cookie=False)[0], 401)
        self.assertEqual(self.req("GET", "/", cookie=False)[0], 401)
        st, _, r = self.req("GET", f"/?token={TOKEN}", cookie=False)
        self.assertEqual(st, 302)
        self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
        self.assertIn("SameSite=Strict", r.getheader("Set-Cookie"))
        self.assertEqual(self.req("GET", "/?token=wrong", cookie=False)[0], 401)
        self.assertEqual(self.req("GET", "/")[0], 200)
        self.assertEqual(self.req("POST", "/api/stop", {"session": SID}, headers={"X-Omni": "0"})[0], 403)   # CSRF header
        self.assertEqual(self.req("GET", "/api/state", host="evil.example.com")[0], 403)                     # DNS rebinding

    def test_api_keys_never_leave_the_server(self):
        st, d, _ = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertNotIn("secret-key-123456", json.dumps(d))
        self.assertEqual(d["pools"][0]["keys"][0]["masked"], "secr...456")

    def test_manage_keys_and_models(self):
        self.assertEqual(self.req("POST", "/api/keys/add", {"pool": "mock", "keys": "k2\nk3, k4 k2"})[1], {"added": 3, "duplicates": 1})
        st, d, _ = self.req("GET", "/api/state")
        self.assertEqual(len(d["pools"][0]["keys"]), 4)
        st, d, _ = self.req("POST", "/api/pool/model", {"pool": "mock", "model": "m-other"})
        self.assertTrue(d["listed"])
        self.assertEqual({p.model for p in self.cfg.providers.providers_in("mock")}, {"m-other"})
        st, d, _ = self.req("POST", "/api/pool/model", {"pool": "mock", "model": "nonexistent"})
        self.assertIs(d["listed"], False)
        self.assertEqual(self.req("GET", "/api/models?pool=mock")[1]["models"], ["m-good", "m-other"])
        self.req("POST", "/api/pool/fallbacks", {"pool": "mock", "models": "m-good, m-other"})
        self.assertEqual(self.cfg.providers.fallbacks("mock"), ["m-good", "m-other"])
        self.assertEqual(self.req("POST", "/api/strategy", {"strategy": "failover"})[0], 200)
        self.assertEqual(self.req("POST", "/api/strategy", {"strategy": "bogus"})[0], 400)
        res = self.req("POST", "/api/keys/test", {"pool": "mock"})[1]["results"]
        self.assertEqual(len(res), 4)
        self.assertEqual(self.req("POST", "/api/keys/add", {"pool": "bad name!", "keys": "x"})[0], 400)
        self.assertEqual(self.req("POST", "/api/keys/remove", {"name": "mock-2"})[0], 200)

    def test_chat_with_approval_flow(self):
        st, d, _ = self.req("POST", "/api/chat", {"session": SID, "text": "run echo"})
        self.assertEqual(st, 200)
        # shell needs approval -> an approval event shows up
        after, approval, deadline, first = 0, None, time.time() + 15, []
        while time.time() < deadline and not approval:
            _, p, _ = self.req("GET", f"/api/poll?session={SID}&after={after}&wait=2")
            after = p["next"]
            first += p["events"]
            approval = next((e for e in p["events"] if e["type"] == "approval"), None)
        self.assertIsNotNone(approval, "no approval requested")
        self.assertIn("echo hello-from-shell", approval["text"])
        self.assertEqual(self.req("POST", "/api/chat", {"session": SID, "text": "again"})[0], 409)      # busy
        self.req("POST", "/api/approve", {"session": SID, "id": approval["id"], "allow": True})
        events, deadline = list(first), time.time() + 15
        while time.time() < deadline and not any(e["type"] == "done" for e in events):
            _, p, _ = self.req("GET", f"/api/poll?session={SID}&after={after}&wait=2")
            after = p["next"]
            events += p["events"]
        types = [e["type"] for e in events]
        self.assertIn("route", types)
        self.assertIn("tool_result", types)
        self.assertIn("hello-from-shell", next(e["text"] for e in events if e["type"] == "tool_result"))
        self.assertEqual(next(e["text"] for e in events if e["type"] == "final"), "all done")
        # history survives (and is persisted for restarts)
        _, h, _ = self.req("GET", f"/api/history?session={SID}")
        self.assertEqual([i["type"] for i in h["items"]], ["user", "tool", "tool_result", "final"])
        self.assertTrue((Path(self.tmp.name) / "sessions" / f"{SID}.json").exists())
        self.assertEqual(self.req("GET", "/api/sessions")[1][0]["title"], "run echo")

    def test_denied_approval(self):
        self.req("POST", "/api/chat", {"session": SID, "text": "run echo"})
        after, approval, deadline = 0, None, time.time() + 15
        while time.time() < deadline and not approval:
            _, p, _ = self.req("GET", f"/api/poll?session={SID}&after={after}&wait=2")
            after = p["next"]
            approval = next((e for e in p["events"] if e["type"] == "approval"), None)
        self.req("POST", "/api/approve", {"session": SID, "id": approval["id"], "allow": False})
        deadline = time.time() + 15
        got = []
        while time.time() < deadline and not any(e["type"] == "done" for e in got):
            _, p, _ = self.req("GET", f"/api/poll?session={SID}&after={after}&wait=2")
            after = p["next"]
            got += p["events"]
        self.assertIn("denied", next(e["text"] for e in got if e["type"] == "tool_result").lower())

    def test_stop_releases_pending_approval(self):
        self.req("POST", "/api/chat", {"session": SID, "text": "run echo"})
        time.sleep(1.0)
        self.req("POST", "/api/stop", {"session": SID})
        deadline = time.time() + 10
        while time.time() < deadline and self.app.session(SID).busy:
            time.sleep(0.2)
        self.assertFalse(self.app.session(SID).busy)

    def test_auto_approve_toggle_runs_without_asking(self):
        self.req("POST", "/api/auto", {"session": SID, "on": True})
        self.req("POST", "/api/chat", {"session": SID, "text": "run echo"})
        after, got, deadline = 0, [], time.time() + 15
        while time.time() < deadline and not any(e["type"] == "done" for e in got):
            _, p, _ = self.req("GET", f"/api/poll?session={SID}&after={after}&wait=2")
            after = p["next"]
            got += p["events"]
        self.assertFalse(any(e["type"] == "approval" for e in got))
        self.assertIn("hello-from-shell", next(e["text"] for e in got if e["type"] == "tool_result"))

    def test_chat_without_keys_gives_clear_error(self):
        self.cfg.providers.remove_pool("mock")
        st, d, _ = self.req("POST", "/api/chat", {"session": SID, "text": "hi"})
        self.assertEqual(st, 400)
        self.assertIn("API key", d["error"])

    def test_files_upload_download_and_path_traversal(self):
        self.assertEqual(self.req("POST", "/api/upload?name=../../evil.txt", raw=b"hello", headers={"Content-Type": "application/octet-stream"})[1]["path"], "evil.txt")
        self.assertTrue((self.cfg.workspace / "evil.txt").exists())
        self.assertEqual(self.req("GET", "/api/files")[1][0]["path"], "evil.txt")
        st, body, _ = self.req("GET", "/api/file?path=evil.txt")
        self.assertEqual((st, body), (200, b"hello"))
        self.assertEqual(self.req("GET", "/api/file?path=../providers.json")[0], 404)
        self.assertEqual(self.req("GET", "/api/poll?session=../../etc&after=0&wait=0")[0], 400)

    def test_index_page_served(self):
        st, body, _ = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"<title>Omni</title>", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
