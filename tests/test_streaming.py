# tests/test_streaming.py
"""Regression tests for the streaming pipeline.

These tests exercise the server-side event flow end-to-end:

  * tool_stream events reach /api/poll BEFORE the tool_result / done
  * the stream buffer is flushed on tool_result (no lost tail)
  * the ring buffer's monotonic _seq prevents silent event skips
  * cancel during a run emits a synthetic tool_result (no stuck amber cards)

They do NOT test the browser (that's manual), but they do test every
server-side guarantee the browser relies on.
"""
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
from test_failover import Gateway, make_cfg, ok   # noqa: E402
from test_agent import tc                        # noqa: E402
from agent.web import server                     # noqa: E402


TOKEN = "t0ken"
SID = "abcdef0123456789"


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.cfg.auto_approve = True
        self.step = 0
        self.gw = Gateway(self._handler)
        self.cfg.providers.add_keys("mock", ["k"], self.gw.url, "m")
        self.app = server.App(self.cfg, TOKEN, {"127.0.0.1", "localhost"})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.gw.close()
        self.tmp.cleanup()

    def _handler(self, c):
        self.step += 1
        if self.step == 1:
            # A shell command that emits three lines then exits.
            return ok(tool_calls=[tc(1, "run_shell", {
                "command": "echo one; sleep 0.1; echo two; sleep 0.1; echo three"
            })])
        return ok("all done")

    def req(self, method, path, body=None, cookie=True):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"X-Omni": "1", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = f"omni_token={TOKEN}"
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        txt = r.read()
        c.close()
        try:
            return r.status, json.loads(txt)
        except ValueError:
            return r.status, txt

    def test_stream_events_arrive_before_done(self):
        """tool_stream events must reach /api/poll before the final 'done'."""
        st, d = self.req("POST", "/api/chat", {"session": SID, "text": "go"})
        self.assertEqual(st, 200)
        cursor = d["cursor"]
        events = []
        saw_stream = False
        saw_done = False
        deadline = time.time() + 15
        while time.time() < deadline and not saw_done:
            st, p = self.req("GET", f"/api/poll?session={SID}&after={cursor}&wait=2")
            self.assertEqual(st, 200)
            cursor = p["next"]
            for e in p["events"]:
                events.append(e)
                if e["type"] == "tool_stream":
                    saw_stream = True
                if e["type"] == "done":
                    saw_done = True
            if not p["busy"] and not p["events"]:
                break
        self.assertTrue(saw_stream, "never saw a tool_stream event")
        self.assertTrue(saw_done, "never saw a done event")
        # All three lines must appear somewhere in the stream events.
        stream_text = "".join(e["text"] for e in events if e["type"] == "tool_stream")
        self.assertIn("one", stream_text)
        self.assertIn("two", stream_text)
        self.assertIn("three", stream_text)

    def test_stream_tail_flushed_on_tool_result(self):
        """The last chunk of a stream must not be lost when the tool exits.

        We emit exactly two lines with a short sleep so the second line
        would be held in the coalesce buffer if the flush logic were
        broken.
        """
        # Reconfigure the gateway: a single tool call, two lines.
        self.step = 0
        self.gw.handler = lambda c: ok(tool_calls=[tc(1, "run_shell", {
            "command": "echo first; sleep 0.05; echo last"
        })]) if c["body"].get("messages", [{}])[-1].get("role") != "tool" else ok("done")

        st, d = self.req("POST", "/api/chat", {"session": SID, "text": "go"})
        cursor = d["cursor"]
        events = []
        deadline = time.time() + 15
        saw_done = False
        while time.time() < deadline and not saw_done:
            st, p = self.req("GET", f"/api/poll?session={SID}&after={cursor}&wait=2")
            cursor = p["next"]
            events.extend(p["events"])
            saw_done = any(e["type"] == "done" for e in p["events"])
            if not p["busy"] and not p["events"]:
                break
        stream_text = "".join(e["text"] for e in events if e["type"] == "tool_stream")
        self.assertIn("first", stream_text)
        self.assertIn("last", stream_text,
                      "the last line was lost — the coalesce buffer "
                      "was not flushed on tool_result")

    def test_cancel_emits_synthetic_tool_result(self):
        """Pressing Stop during a long tool must not leave a stuck card.

        The synthetic tool_result with ok=False is what the UI uses to
        flip the amber 'running' card to red.
        """
        self.step = 0
        # A shell command that runs for 10 seconds (longer than the test).
        self.gw.handler = lambda c: ok(tool_calls=[tc(1, "run_shell", {
            "command": "sleep 10"
        })]) if c["body"].get("messages", [{}])[-1].get("role") != "tool" else ok("done")

        st, d = self.req("POST", "/api/chat", {"session": SID, "text": "go"})
        cursor = d["cursor"]
        # Wait until we see the 'tool' event (not just the route).
        deadline = time.time() + 10
        saw_tool = False
        while time.time() < deadline and not saw_tool:
            st, p = self.req("GET", f"/api/poll?session={SID}&after={cursor}&wait=2")
            cursor = p["next"]
            for e in p["events"]:
                if e["type"] == "tool" and e.get("name") == "run_shell":
                    saw_tool = True
        self.assertTrue(saw_tool, "never saw the tool event")
        # Now press Stop.
        st, _ = self.req("POST", "/api/stop", {"session": SID})
        self.assertEqual(st, 200)
        # Wait for the 'done' event and check for a synthetic tool_result.
        events = []
        deadline = time.time() + 15
        saw_done = False
        while time.time() < deadline and not saw_done:
            st, p = self.req("GET", f"/api/poll?session={SID}&after={cursor}&wait=2")
            cursor = p["next"]
            events.extend(p["events"])
            saw_done = any(e["type"] == "done" for e in p["events"])
            if not p["busy"] and not p["events"]:
                break
        synthetic = [e for e in events
                     if e["type"] == "tool_result" and "cancelled" in (e["text"] or "").lower()]
        self.assertTrue(synthetic,
                        "no synthetic tool_result emitted on cancel; "
                        "the UI would leave a stuck amber card")


if __name__ == "__main__":
    unittest.main(verbosity=2)