"""Offline tests for the learning layer: widened grammar, task
classification, per-task model stats, consolidation prune."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import learning


class GrammarTests(unittest.TestCase):
    def test_always_never_prefer_still_work(self):
        for text, kind in [
            ("always use black for formatting", "always_"),
            ("never use tabs", "never_"),
            ("I prefer spaces", "preference_"),
            ("from now on use 4 spaces", "always_"),
            ("don't use semicolons", "never_"),
        ]:
            facts = learning.mine_preference(text)
            self.assertTrue(
                any(f["key"].startswith(kind) for f in facts),
                f"{text!r} -> {facts}",
            )

    def test_new_patterns(self):
        for text, kind in [
            ("use JSON instead", "preference_"),
            ("use YAML rather than XML", "preference_"),
            ("next time, use 2-space indent", "preference_"),
            ("remember that I hate YAML", "note_"),
            ("I hate tabs", "never_"),
            ("I dislike semicolons", "never_"),
        ]:
            facts = learning.mine_preference(text)
            self.assertTrue(
                any(f["key"].startswith(kind) for f in facts),
                f"{text!r} -> {facts}",
            )

    def test_duplicate_keys_collapsed_in_one_turn(self):
        facts = learning.mine_preference(
            "always use black. always use black for formatting."
        )
        keys = [f["key"] for f in facts]
        self.assertEqual(len(keys), len(set(keys)))


class TaskClassifyTests(unittest.TestCase):
    def test_buckets(self):
        cases = {
            "refactor the auth module": "code-edit",
            "explain what utils.py does": "code-read",
            "review main.rs please": "code-read",
            "run npm test": "shell",
            "move report.docx into archive": "file-ops",
            "search the web for python 3.13 changes": "research",
            "make a powerpoint from these slides": "office",
            "hello there": "chat",
        }
        for text, want in cases.items():
            self.assertEqual(learning.classify_task(text), want, text)


class PerTaskModelStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        learning.configure(Path(self.tmp.name))
        with learning._LOCK:
            learning._TASK_MODEL_STATS.clear()
            learning._SHAPE_STATS.clear()
            learning._CORRECTIONS.clear()

    def tearDown(self):
        with learning._LOCK:
            learning._TASK_MODEL_STATS.clear()
            learning._SHAPE_STATS.clear()
            learning._CORRECTIONS.clear()
        self.tmp.cleanup()

    def test_observe_model_records_task_row(self):
        learning.observe_model("gemini", "pro", True, 100, task="code-edit")
        learning.observe_model("gemini", "pro", True, 100, task="code-edit")
        learning.observe_model("gemini", "pro", True, 100, task="code-edit")
        row = learning._TASK_MODEL_STATS["code-edit|gemini|pro"]
        self.assertEqual(row["ok"], 3)
        self.assertEqual(row["n"], 3)

    def test_prompt_hints_surfaces_best_task_model(self):
        for _ in range(3):
            learning.observe_model("gemini", "pro", True, 100, task="code-edit")
        for _ in range(3):
            learning.observe_model("gemini", "flash", False, 100, task="code-edit")
        class _PM:
            def pool_names(self): return ["gemini"]
            def model_of(self, p): return "flash"
        class _Cfg:
            providers = _PM()
        hints = learning.prompt_hints(_Cfg())
        self.assertIn("code-edit", hints)
        self.assertIn("pro", hints)

    def test_consolidate_prunes_low_signal(self):
        with learning._LOCK:
            learning._SHAPE_STATS["read_file|path"] = {"ok": 1, "fail": 0, "ms": 10, "n": 1}
            learning._SHAPE_STATS["write_file|path,content"] = {"ok": 5, "fail": 2, "ms": 10, "n": 7}
        learning.consolidate()
        self.assertNotIn("read_file|path", learning._SHAPE_STATS)
        self.assertIn("write_file|path,content", learning._SHAPE_STATS)


if __name__ == "__main__":
    unittest.main(verbosity=2)