"""Offline test: mock OpenAI server drives a real tool-calling loop. Run: python tests/test_agent.py"""
import json, sys, threading, tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import config, loop

STEP = {"n": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        STEP["n"] += 1
        if STEP["n"] == 1:
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "hello.txt", "content": "hello world"})}}]}
        elif STEP["n"] == 2:
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "c2", "type": "function", "function": {
                "name": "edit_file", "arguments": json.dumps({"path": "hello.txt", "old": "world", "new": "omni"})}}]}
        else:
            msg = {"role": "assistant", "content": "done"}
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(out)


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
cfg = config.load()
from agent.providers import Provider
cfg.providers.items = [Provider("mock", f"http://127.0.0.1:{srv.server_port}/v1", "k", "m")]
cfg.providers.path = Path(tempfile.mkdtemp()) / "p.json"
cfg.workspace = Path(tempfile.mkdtemp()).resolve()
cfg.auto_approve = True
h = loop.new_history(cfg)
res = loop.run(cfg, h, "make a file")
assert res == "done", res
assert (cfg.workspace / "hello.txt").read_text() == "hello omni"
# docx round trip
from agent.tools import call
print(call(cfg, "docx_create", {"path": "a.docx", "blocks": [{"type": "heading", "text": "Title"}, {"type": "paragraph", "text": "Status: draft"}]}))
print(call(cfg, "docx_replace_text", {"path": "a.docx", "find": "draft", "replace": "final"}))
print(call(cfg, "docx_read", {"path": "a.docx"}))
print(call(cfg, "read_file", {"path": "../../etc/passwd"}))

# ---- failover: first provider always 429, second works
class Bad(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(429); self.end_headers(); self.wfile.write(b"rate limited")
bad = HTTPServer(("127.0.0.1", 0), Bad)
threading.Thread(target=bad.serve_forever, daemon=True).start()
STEP["n"] = 2  # mock replies "done"
from agent import commands, llm
good_url = f"http://127.0.0.1:{srv.server_port}/v1"
cfg.providers.items = []
print(commands.handle(cfg, f"/add bad KEY m http://127.0.0.1:{bad.server_port}/v1"))
print(commands.handle(cfg, f"/add good KEY m {good_url}"))
print(commands.handle(cfg, "/providers"))
out = llm.chat(cfg, [{"role": "user", "content": "hi"}])
assert out["content"] == "done", out
assert "cooling" in cfg.providers.status(cfg.providers.get("bad"))
print(commands.handle(cfg, "/use good"))
print(commands.handle(cfg, "/model good new-model"))
assert cfg.providers.items[0].name == "good" and cfg.providers.items[0].model == "new-model"
print("ALL OK")
