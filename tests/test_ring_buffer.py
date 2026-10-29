# tests/test_ring_buffer.py
"""Regression tests for the ring buffer + monotonic _seq cursor.

The bug this guards against: the old code used an array index as the
cursor and deleted events from the middle when over MAX_EVENTS, which
caused the client to silently skip events. The new code uses a deque
with maxlen, and the cursor is a monotonically increasing _seq.

These tests exercise Session.poll and Session.emit directly (no HTTP),
so they run in milliseconds.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.web.server import Session, MAX_EVENTS   # noqa: E402


class _StubCfg:
    """Minimal Config stub — Session only reads data_dir, auto_approve."""
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.workspace = Path(data_dir)
        self.auto_approve = False
        self.strict = True


class RingBufferTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _StubCfg(self.tmp.name)
        self.s = Session(self.cfg, "abcdef0123456789")

    def tearDown(self):
        self.tmp.cleanup()

    def test_seq_is_monotonic(self):
        for i in range(10):
            self.s.emit("assistant", f"m{i}")
        seqs = [e["_seq"] for e in self.s.events]
        self.assertEqual(seqs, list(range(1, 11)))

    def test_poll_returns_events_after_cursor(self):
        for i in range(5):
            self.s.emit("assistant", f"m{i}")
        events, nxt = self.s.poll(0, wait=0)
        self.assertEqual(len(events), 5)
        self.assertEqual(nxt, 5)
        self.assertEqual(events[0]["text"], "m0")
        self.assertEqual(events[-1]["text"], "m4")
        # Polling again with the same cursor should return nothing.
        events2, nxt2 = self.s.poll(nxt, wait=0)
        self.assertEqual(events2, [])
        self.assertEqual(nxt2, nxt)

    def test_ring_buffer_evicts_oldest_but_cursor_stays_valid(self):
        """After more than MAX_EVENTS events, polling with an old cursor
        must not skip events — the cursor is a sequence number, not an
        index, so the client just sees the events that are still in the
        deque after its cursor."""
        # Emit more than the buffer can hold.
        n = MAX_EVENTS + 100
        for i in range(n):
            self.s.emit("assistant", f"m{i}")
        # Buffer holds only the last MAX_EVENTS.
        self.assertEqual(len(self.s.events), MAX_EVENTS)
        # The oldest event is _seq = 101 (since n - MAX_EVENTS = 100).
        oldest_seq = next(iter(self.s.events))["_seq"]
        self.assertEqual(oldest_seq, n - MAX_EVENTS + 1)
        # Polling from cursor 0 returns only what's in the buffer — no
        # crash, no silent skip past the client's cursor. Because
        # MAX_POLL_EVENTS caps each response at 200 events, we have to
        # paginate to reach the end.
        cursor = 0
        last_seq = 0
        first_seq = None
        total_events = 0
        while True:
            events, nxt = self.s.poll(cursor, wait=0)
            if not events:
                break
            for e in events:
                self.assertGreater(e["_seq"], cursor,
                                   "poll returned an event at or before the cursor")
                self.assertGreaterEqual(e["_seq"], last_seq,
                                        "sequence numbers are not monotonic")
                last_seq = e["_seq"]
                if first_seq is None:
                    first_seq = e["_seq"]
            self.assertGreater(nxt, cursor, "cursor did not advance")
            cursor = nxt
            total_events += len(events)
        self.assertEqual(first_seq, oldest_seq,
                         "the first event should be the oldest retained one")
        self.assertEqual(cursor, n,
                         "pagination should reach the most recent event")
        # The buffer retained exactly MAX_EVENTS events, so a full
        # pagination should have returned exactly MAX_EVENTS events
        # (not n = MAX_EVENTS + 100).
        self.assertEqual(total_events, MAX_EVENTS)

        # Polling from a cursor inside the buffer returns only the tail.
        events, nxt = self.s.poll(oldest_seq + 10, wait=0)
        self.assertEqual(events[0]["_seq"], oldest_seq + 11)
        # The first batch is capped at MAX_POLL_EVENTS, so `nxt` is
        # the last seq of the first page, not the last seq of the buffer.
        self.assertEqual(nxt, oldest_seq + 10 + len(events))

    def test_tool_stream_coalesces_in_place(self):
        """Two tool_stream chunks for the same id within the coalesce
        window must merge into one event, not two."""
        self.s.emit("tool_stream", "a", id="x")
        self.s.emit("tool_stream", "b", id="x")   # within 100ms
        # Only one tool_stream event should exist.
        stream_events = [e for e in self.s.events if e["type"] == "tool_stream"]
        self.assertEqual(len(stream_events), 1)
        self.assertEqual(stream_events[0]["text"], "ab")

    def test_tool_stream_flushes_on_tool_result(self):
        """When a tool_result arrives, any pending stream buffer must be
        flushed so the last chunk isn't lost from the live view."""
        # Emit a stream chunk, then a tool_result for the same id.
        # The stream chunk was merged into events[-1] by the coalesce,
        # but if the buffer had a pending piece (which would happen if
        # the second chunk arrived *just* under the coalesce window and
        # was held), the tool_result must flush it.
        self.s.emit("tool_stream", "a", id="x")
        # Manually stuff the buffer to simulate a chunk that arrived
        # within the coalesce window and was held for the next chunk.
        self.s._stream_buf["x"] = ["b"]
        self.s._stream_last["x"] = 0.0
        self.s.emit("tool_result", "done", id="x", ok=True)
        stream_events = [e for e in self.s.events if e["type"] == "tool_stream"]
        # The original "a" is one event; the flush added "b" as another.
        all_stream_text = "".join(e["text"] for e in stream_events)
        self.assertIn("a", all_stream_text)
        self.assertIn("b", all_stream_text,
                      "the flushed stream chunk 'b' was lost")

    def test_poll_does_not_return_events_at_or_before_cursor(self):
        self.s.emit("assistant", "x")
        self.s.emit("assistant", "y")
        events, nxt = self.s.poll(1, wait=0)   # cursor 1 => only the 2nd event
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "y")
        self.assertEqual(nxt, 2)


class SeqCursorTests(unittest.TestCase):
    """Tests for the wider property that the cursor is monotonic even
    when the buffer evicts entries."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _StubCfg(self.tmp.name)
        self.s = Session(self.cfg, "abcdef0123456789")

    def tearDown(self):
        self.tmp.cleanup()

    def test_seq_never_decreases_across_evictions(self):
        # Emit enough events to trigger eviction several times over.
        n = MAX_EVENTS * 3
        for i in range(n):
            self.s.emit("assistant", f"m{i}")
        # The final event must have _seq == n.
        self.assertEqual(self.s.events[-1]["_seq"], n)
        # The oldest event must have a positive _seq.
        self.assertGreater(self.s.events[0]["_seq"], 0)
        # The number of events retained must be exactly MAX_EVENTS.
        self.assertEqual(len(self.s.events), MAX_EVENTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)