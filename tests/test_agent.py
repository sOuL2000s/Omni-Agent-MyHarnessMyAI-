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
cfg.base_url = f"http://127.0.0.1:{srv.server_port}/v1"
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
print("ALL OK")
