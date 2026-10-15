"""Offline tests for key rotation, model choice/fallback and failover.
Mock gateways imitate the error bodies real services send (Gemini 429 + retryDelay, daily quota, API_KEY_INVALID,
404 model, OpenRouter 200-with-error-body).   Run:  python tests/test_failover.py
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["OMNI_NO_REFRESH"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import llm, commands                                    # noqa: E402
from agent.config import Config                                    # noqa: E402
from agent.providers import Manager, Provider, pool_of             # noqa: E402

llm.MIN_RATE_COOLDOWN = 1          # keep the "wait for a key" test fast


# ---------------------------------------------------------------- mock gateway
class Gateway:
    """handler(call:dict) -> (status, body_dict|str, headers)   call = {key, model, body}"""

    def __init__(self, handler):
        self.calls, self.handler = [], handler
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body, headers=()):
                data = body if isinstance(body, str) else json.dumps(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data.encode())))
                self.end_headers()
                self.wfile.write(data.encode())

            def do_GET(self):
                self._reply(200, {"data": [{"id": "models/m-good"}, {"id": "models/m-other"}, {"id": "models/text-embedding-004"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                call = {"key": (self.headers.get("Authorization") or "").replace("Bearer ", ""), "model": body.get("model"), "body": body}
                gw.calls.append(call)
                st, out, hd = gw.handler(call)
                self._reply(st, out, hd)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def ok(text="done", tool_calls=None):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg = {"role": "assistant", "content": None, "tool_calls": tool_calls}
    return 200, {"choices": [{"message": msg}]}, ()


def gem429(delay="30s", daily=False):
    qid = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if daily else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    return 429, [{"error": {"code": 429, "message": "You exceeded your current quota", "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": qid}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}}], ()


def make_cfg(tmp, timeout=5):
    tmp = Path(tmp)
    pm = Manager(tmp / "providers.json", None, tmp / "state.json", tmp / "settings.json")
    ws = tmp / "ws"
    ws.mkdir(exist_ok=True)
    pm.settings["max_wait"] = 3
    return Config(base_url="", api_key="", model="", workspace=ws.resolve(), allow_outside=False, auto_approve=True,
                  max_turns=10, temperature=0.2, timeout=timeout, data_dir=tmp, providers=pm)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.pm = self.cfg.providers
        self.gws = []
        self.notes = []
        llm.NO_TEMP.clear()

    def tearDown(self):
        for g in self.gws:
            g.close()
        self.tmp.cleanup()

    def gw(self, fn):
        g = Gateway(fn)
        self.gws.append(g)
        return g

    def chat(self, **kw):
        return llm.chat_ex(self.cfg, [{"role": "user", "content": "hi"}], notify=self.notes.append, **kw)


# ---------------------------------------------------------------- tests
class StorageTests(Base):
    def test_bom_file_is_read_and_rewritten_without_bom(self):
        p = Path(self.tmp.name) / "providers.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps([{"name": "gemini-1", "base_url": "http://x/v1", "api_key": "k1", "model": "m"}]).encode())
        pm = Manager(p, None)
        self.assertIsNone(pm.load_error)
        self.assertEqual([x.name for x in pm.items], ["gemini-1"])
        pm.save()
        self.assertFalse(p.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_corrupt_file_is_not_silently_lost(self):
        p = Path(self.tmp.name) / "providers.json"
        p.write_text("{ not json")
        pm = Manager(p, None)
        self.assertIn("could not be read", pm.load_error)
        self.assertTrue(list(Path(self.tmp.name).glob("providers.json.corrupt-*")))

    def test_pool_naming(self):
        self.assertEqual(pool_of("gemini-12"), "gemini")
        self.assertEqual(pool_of("openrouter"), "openrouter")
        self.assertEqual(pool_of("my_key_3"), "my_key")

    def test_bulk_add_numbering_duplicates_and_model_inheritance(self):
        a, d = self.pm.add_keys("gemini", ["AIza1", "AIza2", "AIza2", "", "# comment", ' "AIza3" '])
        self.assertEqual(a, ["gemini-1", "gemini-2", "gemini-3"])
        self.assertEqual(d, 1)
        self.pm.set_model("gemini", "gemini-2.5-pro")
        a, d = self.pm.add_keys("gemini", ["AIza4", "AIza1"])
        self.assertEqual(a, ["gemini-4"])
        self.assertEqual(d, 1)
        self.assertEqual({p.model for p in self.pm.providers_in("gemini")}, {"gemini-2.5-pro"})   # new key inherits pool model

    def test_model_change_applies_to_every_key_in_pool(self):
        self.pm.add_keys("gemini", [f"AIza{i}" for i in range(37)])
        self.pm.add_keys("openrouter", ["sk-or-1"])
        self.pm.set_model("gemini", "gemini-2.5-pro")
        self.assertEqual({p.model for p in self.pm.providers_in("gemini")}, {"gemini-2.5-pro"})
        self.assertNotEqual(self.pm.model_of("openrouter"), "gemini-2.5-pro")
        # survives a restart
        pm2 = Manager(self.pm.path, None, Path(self.tmp.name) / "state.json", Path(self.tmp.name) / "settings.json")
        self.assertEqual(pm2.model_of("gemini"), "gemini-2.5-pro")
        self.assertEqual(len(pm2.items), 38)

    def test_order_and_use(self):
        self.pm.add_keys("gemini", ["a1", "a2"])
        self.pm.add_keys("openrouter", ["o1"])
        self.pm.use("openrouter")
        self.assertEqual(self.pm.pool_names(), ["openrouter", "gemini"])
        self.pm.order_pools(["gemini", "openrouter"])
        self.assertEqual(self.pm.pool_names(), ["gemini", "openrouter"])


class FailoverTests(Base):
    def test_429_rotates_to_next_key_and_cools_the_bad_one(self):
        g = self.gw(lambda c: gem429("30s") if c["key"] == "k1" else ok())
        self.pm.add_keys("gem", ["k1", "k2", "k3"], g.url, "m-good")
        self.pm.settings["strategy"] = "failover"
        msg, route = self.chat()
        self.assertEqual(msg["content"], "done")
        self.assertEqual(route["provider"], "gem-2")
        self.assertEqual([c["key"] for c in g.calls], ["k1", "k2"])
        self.assertIn("cooling", self.pm.status(self.pm.get("gem-1")))
        # second request must not touch the cooling key again
        self.chat()
        self.assertEqual([c["key"] for c in g.calls][2:], ["k2"])

    def test_round_robin_spreads_calls(self):
        g = self.gw(lambda c: ok())
        self.pm.add_keys("gem", ["k1", "k2", "k3"], g.url, "m-good")
        for _ in range(6):
            self.chat()
        self.assertEqual([c["key"] for c in g.calls], ["k1", "k2", "k3", "k1", "k2", "k3"])

    def test_failover_strategy_sticks_to_first_key(self):
        g = self.gw(lambda c: ok())
        self.pm.add_keys("gem", ["k1", "k2"], g.url, "m-good")
        self.pm.set_strategy("failover")
        for _ in range(3):
            self.chat()
        self.assertEqual({c["key"] for c in g.calls}, {"k1"})

    def test_invalid_key_400_is_a_key_problem_not_a_request_problem(self):
        bad = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
                         "details": [{"reason": "API_KEY_INVALID"}]}}
        g = self.gw(lambda c: (400, bad, ()) if c["key"] in ("k1", "k2") else ok())
        self.pm.add_keys("gem", ["k1", "k2", "k3"], g.url, "m-good")
        self.pm.set_strategy("failover")
        _, route = self.chat()
        self.assertEqual(route["provider"], "gem-3")
        self.assertEqual(self.pm.status(self.pm.get("gem-1")), "INVALID KEY")
        self.assertEqual(self.pm.summary()[0]["counts"], {"ready": 1, "cooling": 0, "invalid": 2})

    def test_bad_request_400_does_not_burn_every_key(self):
        g = self.gw(lambda c: (400, {"error": {"message": "Request payload malformed: tools[3].parameters"}}, ()))
        self.pm.add_keys("gem", [f"k{i}" for i in range(10)], g.url, "m-good")
        with self.assertRaises(llm.LLMError) as cm:
            self.chat()
        self.assertIn("malformed", str(cm.exception))
        self.assertEqual(len(g.calls), 2)                                      # stopped at the first key (1 retry)
        self.assertEqual(self.pm.summary()[0]["counts"]["ready"], 10)          # nothing was put on cooldown

    def test_daily_quota_cools_for_hours_and_uses_model_fallback_on_same_keys(self):
        def h(c):
            if c["model"] == "m-main":
                return gem429("50s", daily=True)
            return ok("from-fallback")
        g = self.gw(h)
        self.pm.add_keys("gem", ["k1", "k2"], g.url, "m-main")
        self.pm.set_fallbacks("gem", ["m-lite"])
        msg, route = self.chat()
        self.assertEqual(msg["content"], "from-fallback")
        self.assertEqual((route["model"], route["fallback"]), ("m-lite", True))
        st = self.pm.health.state(self.pm.get("gem-1"), "m-main")
        self.assertEqual(st["kind"], "quota")
        self.assertGreater(st["remaining"], 50)                                # hours, not the 50s the body mentions
        self.assertIsNone(self.pm.health.state(self.pm.get("gem-1"), "m-lite"))  # quota is per MODEL: lite still usable
        n = len(g.calls)
        self.chat()
        self.assertTrue(all(c["model"] == "m-lite" for c in g.calls[n:]))      # main model not hammered again

    def test_missing_model_skips_whole_pool_quickly_and_falls_to_next_pool(self):
        g1 = self.gw(lambda c: (404, {"error": {"message": "models/nope is not found for API version v1beta"}}, ()))
        g2 = self.gw(lambda c: ok("from-pool-2"))
        self.pm.add_keys("a", [f"a{i}" for i in range(20)], g1.url, "nope")
        self.pm.add_keys("b", ["b1"], g2.url, "m-good")
        msg, route = self.chat()
        self.assertEqual(msg["content"], "from-pool-2")
        self.assertEqual(len(g1.calls), 1)                                     # 20 keys, ONE wasted call
        self.assertEqual(route["pool"], "b")

    def test_openrouter_style_200_with_error_body(self):
        g = self.gw(lambda c: (200, {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-min"}}, ()) if c["key"] == "k1" else ok())
        self.pm.add_keys("or", ["k1", "k2"], g.url, "m-good")
        self.pm.set_strategy("failover")
        _, route = self.chat()
        self.assertEqual(route["provider"], "or-2")

    def test_waits_out_short_rate_limit_instead_of_failing(self):
        state = {"n": 0}

        def h(c):
            state["n"] += 1
            return gem429("0.5s") if state["n"] <= 2 else ok("after-wait")
        g = self.gw(h)
        self.pm.add_keys("gem", ["k1", "k2"], g.url, "m-good")
        t = time.time()
        msg, _ = self.chat()
        self.assertEqual(msg["content"], "after-wait")
        self.assertTrue(any("waiting" in n for n in self.notes), self.notes)
        self.assertLess(time.time() - t, 8)

    def test_gives_up_with_readable_summary_when_nothing_works(self):
        g = self.gw(lambda c: (401, {"error": {"message": "Incorrect API key provided"}}, ()))
        self.pm.add_keys("gem", ["k1", "k2", "k3"], g.url, "m-good")
        with self.assertRaises(llm.LLMError) as cm:
            self.chat()
        txt = str(cm.exception)
        self.assertIn("3x auth", txt)
        self.assertIn("replace the invalid keys", txt)
        self.assertEqual(len(g.calls), 3)                                      # each tried once, no waiting for dead keys
        with self.assertRaises(llm.LLMError):
            self.chat()
        self.assertEqual(len(g.calls), 3)                                      # and not hammered again while cooling

    def test_network_down_pool_falls_through_without_trying_every_key(self):
        g2 = self.gw(lambda c: ok("backup"))
        self.pm.add_keys("a", [f"a{i}" for i in range(10)], "http://127.0.0.1:9/v1", "m")    # nothing listens on port 9
        self.pm.add_keys("b", ["b1"], g2.url, "m-good")
        t = time.time()
        msg, route = self.chat()
        self.assertEqual(route["pool"], "b")
        self.assertLess(time.time() - t, 4)

    def test_temperature_rejected_is_dropped_and_remembered(self):
        def h(c):
            if "temperature" in c["body"]:
                return 400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.2 with this model."}}, ()
            return ok()
        g = self.gw(h)
        self.pm.add_keys("o", ["k1"], g.url, "reasoner")
        self.chat()
        self.chat()
        self.assertEqual(["temperature" in c["body"] for c in g.calls], [True, False, False])

    def test_tool_call_arguments_object_is_normalised(self):
        tc = [{"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": {"path": "."}}}]
        g = self.gw(lambda c: ok(tool_calls=tc))
        self.pm.add_keys("g", ["k1"], g.url, "m")
        msg, _ = self.chat()
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"path": "."})

    def test_cooldowns_survive_restart(self):
        g = self.gw(lambda c: gem429("30s", daily=True) if c["key"] == "k1" else ok())
        self.pm.add_keys("gem", ["k1", "k2"], g.url, "m-good")
        self.pm.set_strategy("failover")
        self.chat()
        pm2 = Manager(self.pm.path, None, Path(self.tmp.name) / "state.json", Path(self.tmp.name) / "settings.json")
        self.assertEqual(pm2.status(pm2.get("gem-1")), "daily quota used")

    def test_cancel_during_wait(self):
        g = self.gw(lambda c: gem429("2s"))
        self.pm.settings["max_wait"] = 30
        self.pm.add_keys("gem", ["k1"], g.url, "m-good")
        ev = threading.Event()
        threading.Timer(0.5, ev.set).start()
        with self.assertRaises(llm.LLMError) as cm:
            self.chat(cancel=ev)
        self.assertIn("stopped", str(cm.exception))


class CommandTests(Base):
    def test_commands_end_to_end(self):
        g = self.gw(lambda c: ok("pong") if c["key"] != "bad" else (401, {"error": {"message": "Incorrect API key"}}, ()))
        cmd = lambda s: commands.handle(self.cfg, s)    # noqa: E731
        self.assertIn("added 3 key(s)", cmd("/addkeys gemini good1 good2 bad"))
        for p in self.pm.providers_in("gemini"):
            p.base_url, p.model = g.url, "m-good"
        self.pm.save()
        self.assertIn("3 total", cmd("/providers"))
        self.assertIn("now uses model m-other", cmd("/model gemini m-other"))
        self.assertIn("m-good", cmd("/models gemini"))
        self.assertNotIn("embedding", cmd("/models gemini"))
        self.assertIn("fallbacks ['m-good']", cmd("/fallback gemini m-good"))
        out = cmd("/test all")
        self.assertIn("gemini-3 FAILED [auth]", out)
        self.assertIn("2/3 keys OK", out)
        self.assertEqual(self.pm.status(self.pm.get("gemini-3")), "INVALID KEY")
        self.assertIn("cleared", cmd("/revive"))
        self.assertIn("round_robin", cmd("/strategy round_robin"))
        self.assertIn("removed gemini-3", cmd("/remove gemini-3"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
