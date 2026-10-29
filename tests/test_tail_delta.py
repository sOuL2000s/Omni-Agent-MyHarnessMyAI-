# tests/test_tail_delta.py
"""Regression tests for the delta-aware tail_background.

The bug: the old tail_background always returned the last N lines,
even if the log hadn't grown. That made the model call it repeatedly
on a quiet dev server, which burned API calls. The new one returns a
one-line "no new output" message when nothing has changed, and only
the delta when there is.

These tests exercise the tool functions directly with a fake
_DETACHED record, so they run without spawning a real process.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.tools import shell as sh   # noqa: E402


class _FakeProc:
    """Minimal subprocess.Popen stand-in for a detached record."""
    def __init__(self, pid=12345, alive=True):
        self.pid = pid
        self._alive = alive
    def poll(self):
        return None if self._alive else 0


class _Cfg:
    """Minimal Config stub — tail_background only reads data_dir."""
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)


class TailDeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _Cfg(self.tmp.name)
        self.log = Path(self.tmp.name) / "job.log"
        self.job_id = "abc12345"
        # Install a fake detached record.
        with sh._ACTIVE_LOCK:
            sh._DETACHED[self.job_id] = {
                "proc": _FakeProc(),
                "cmd": "npm run dev",
                "started": time.time(),
                "log_path": self.log,
                "last_read": 0,
                "last_read_at": 0.0,
            }

    def tearDown(self):
        with sh._ACTIVE_LOCK:
            sh._DETACHED.pop(self.job_id, None)
        self.tmp.cleanup()

    def _append(self, text):
        with self.log.open("ab") as f:
            f.write(text.encode("utf-8"))

    def test_first_call_returns_content_and_advances_cursor(self):
        self._append("Server started\nListening on :5173\n")
        out = sh.tail_background(self.cfg, self.job_id)
        self.assertIn("Server started", out)
        self.assertIn("Listening on :5173", out)
        # The record's last_read must now be the file size.
        rec = sh._DETACHED[self.job_id]
        self.assertEqual(rec["last_read"], self.log.stat().st_size)

    def test_second_call_with_no_new_output_returns_short_message(self):
        self._append("one\n")
        sh.tail_background(self.cfg, self.job_id)
        out = sh.tail_background(self.cfg, self.job_id)
        self.assertIn("no new output", out)
        # It must be short (one line), not a re-dump of "one".
        self.assertLess(len(out), 200)
        self.assertNotIn("one\n", out)

    def test_new_output_after_silence_returns_only_delta(self):
        self._append("first\n")
        sh.tail_background(self.cfg, self.job_id)
        self._append("second\n")
        out = sh.tail_background(self.cfg, self.job_id)
        self.assertIn("second", out)
        # The full log's "first" line should NOT be re-emitted.
        # (It was already read, so the delta is just "second".)
        self.assertNotIn("first\n", out)

    def test_wait_background_returns_immediately_if_output_exists(self):
        self._append("ready on :3000\n")
        out = sh.wait_background(self.cfg, self.job_id, timeout=5)
        self.assertIn("ready on :3000", out)

    def test_wait_background_times_out_on_silence(self):
        # No output at all. wait_background should block for ~timeout
        # then return the timeout message.
        start = time.time()
        out = sh.wait_background(self.cfg, self.job_id, timeout=1)
        elapsed = time.time() - start
        self.assertLess(elapsed, 3.0, "wait_background blocked too long")
        self.assertIn("no new output", out)

    def test_wait_background_returns_when_process_exits(self):
        self._append("booting\n")
        sh.tail_background(self.cfg, self.job_id)
        # Simulate the process exiting without new output.
        with sh._ACTIVE_LOCK:
            sh._DETACHED[self.job_id]["proc"] = _FakeProc(alive=False)
        out = sh.wait_background(self.cfg, self.job_id, timeout=5)
        # Must return promptly (the process died) with the current state.
        self.assertTrue("exited" in out.lower() or "booting" in out)


class DetachedPruningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _Cfg(self.tmp.name)

    def tearDown(self):
        with sh._ACTIVE_LOCK:
            sh._DETACHED.clear()
        self.tmp.cleanup()

    def test_list_detached_prunes_old_exited_jobs(self):
        """A job that exited more than 10 minutes ago should be removed
        from _DETACHED by list_detached()."""
        old_id = "old00000"
        new_id = "new00000"
        with sh._ACTIVE_LOCK:
            sh._DETACHED[old_id] = {
                "proc": _FakeProc(alive=False),
                "cmd": "echo done",
                "started": time.time() - 700,   # 11 min ago
                "log_path": None,
                "last_read": 0, "last_read_at": 0.0,
            }
            sh._DETACHED[new_id] = {
                "proc": _FakeProc(alive=False),
                "cmd": "echo done",
                "started": time.time() - 30,    # 30s ago
                "log_path": None,
                "last_read": 0, "last_read_at": 0.0,
            }
        rows = sh.list_detached()
        ids = [r["id"] for r in rows]
        self.assertNotIn(old_id, ids)
        self.assertIn(new_id, ids)


class URLDetectionTests(unittest.TestCase):
    """Tests for the URL detection that run_shell uses to skip the
    second round-trip on dev servers."""

    def test_vite_style_output(self):
        text = "  VITE v5.0.0  ready in 312 ms\n\n  ➜  Local:   http://localhost:5173/\n"
        self.assertEqual(sh._scan_for_url(text), "http://localhost:5173/")

    def test_nextjs_style_output(self):
        text = "- ready started server on 0.0.0.0:3000, url: http://localhost:3000\n"
        self.assertEqual(sh._scan_for_url(text), "http://localhost:3000")

    def test_django_style_output(self):
        text = "Starting development server at http://127.0.0.1:8000/\nQuit the server with CONTROL-C.\n"
        self.assertEqual(sh._scan_for_url(text), "http://127.0.0.1:8000/")

    def test_no_url_returns_none(self):
        text = "Compiling...\nDone in 4.5s\n"
        self.assertIsNone(sh._scan_for_url(text))

    def test_url_without_hint_still_found(self):
        # Even without "ready"/"listening", a localhost URL is returned.
        text = "some random log line http://localhost:8080/path\n"
        self.assertEqual(sh._scan_for_url(text), "http://localhost:8080/path")

    def test_prefers_url_near_ready_hint(self):
        # Two URLs; the one after "Local:" should win.
        text = "warning: see http://localhost:9999/help\n  Local: http://localhost:5173/\n"
        self.assertEqual(sh._scan_for_url(text), "http://localhost:5173/")


if __name__ == "__main__":
    unittest.main(verbosity=2)