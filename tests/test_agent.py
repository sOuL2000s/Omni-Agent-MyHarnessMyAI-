"""Offline test: a mock OpenAI server drives the real tool-calling loop (isolated temp config - never touches your data/).
Run: python tests/test_agent.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_failover import Gateway, make_cfg, ok   # noqa: E402
from agent import loop, safety                     # noqa: E402
from agent.tools import call                       # noqa: E402


def tc(i, name, args):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class AgentLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.step = {"n": 0}

        def handler(c):
            self.step["n"] += 1
            n = self.step["n"]
            if n == 1:
                return ok(tool_calls=[tc(1, "write_file", {"path": "hello.txt", "content": "hello world"})])
            if n == 2:
                return ok(tool_calls=[tc(2, "edit_file", {"path": "hello.txt", "old": "world", "new": "omni"})])
            return ok("done")
        self.gw = Gateway(handler)
        self.cfg.providers.add_keys("mock", ["k"], self.gw.url, "m")

    def tearDown(self):
        # Reset thread-local approver so the next test starts clean.
        safety.set_approver(None)
        self.gw.close()
        self.tmp.cleanup()

    def test_tool_loop_writes_and_edits_a_file(self):
        events = []
        res = loop.run(self.cfg, loop.new_history(self.cfg), "make a file", on_event=lambda k, t="", **e: events.append((k, e.get("name"))))
        self.assertEqual(res, "done")
        self.assertEqual((self.cfg.workspace / "hello.txt").read_text(), "hello omni")
        kinds = [k for k, _ in events]
        self.assertIn("route", kinds)
        self.assertEqual([n for k, n in events if k == "tool"], ["write_file", "edit_file"])
        self.assertEqual(kinds.count("tool_result"), 2)

    def test_workspace_is_enforced(self):
        self.assertIn("outside workspace", call(self.cfg, "read_file", {"path": "../../etc/passwd"}))

    def test_docx_roundtrip(self):
        try:
            import docx  # noqa: F401
        except ImportError:
            self.skipTest("python-docx not installed")
        call(self.cfg, "docx_create", {"path": "a.docx", "blocks": [{"type": "heading", "text": "Title"}, {"type": "paragraph", "text": "Status: draft"}]})
        call(self.cfg, "docx_replace_text", {"path": "a.docx", "find": "draft", "replace": "final"})
        self.assertIn("Status: final", call(self.cfg, "docx_read", {"path": "a.docx"}))

    def test_denied_approval_blocks_shell(self):
        self.cfg.auto_approve = False
        safety.set_approver(lambda action: False)
        try:
            out = call(self.cfg, "run_shell", {"command": "echo hi"})
        finally:
            safety.set_approver(None)
        self.assertIn("denied", out.lower())

    def test_tool_schemas_have_no_empty_required(self):
        from agent.tools import schemas
        for s in schemas():
            self.assertNotEqual(s["function"]["parameters"].get("required"), [], s["function"]["name"])


if __name__ == "__main__":
    unittest.main(verbosity=2)