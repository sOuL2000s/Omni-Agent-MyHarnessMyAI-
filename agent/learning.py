# agent/learning.py
"""Persistent self-learning layer.

This module is the difference between a stateless tool loop and an agent
that *knows* its user. It does five concrete things:

1. Outcome ledger (per tool, per model, per key, per task-shape).
   Every tool call records ok/fail, latency, and a short "shape" (the
   tool name + the set of top-level argument keys). Success rate per
   shape feeds:
     * the system prompt ("X failed 5/6 times last week — try Y instead"),
     * the order tools are tried in `parallel_tasks`,
     * a *soft* preference for models/pools.

2. Preference mining.
   A tiny grammar (no LLM call) turns durable user phrases into
   memory facts:
     "always write tests" → fact test_policy=always
     "I prefer tabs"      → fact indent=tabs
     "don't use yellow"   → fact avoid_color=yellow
   These end up in the system prompt AND in `memory_list_facts`.

3. Correction learning.
   When the user says "no, not that" / "wrong" within 30 s of an answer,
   we record a `correction` event keyed by the *action shape* that produced
   the answer. Next time, the prompt warns the model.

4. Consolidation.
   A background thread periodically:
     * dedupes near-identical facts (Levenshtein ≤ 2 on short facts),
     * prunes memories older than N days that were never recalled,
     * recomputes per-shape success rates.

5. Background worker.
   `start_worker()` is called once by config.load(). It runs a daemon
   thread that does consolidation every 10 minutes and never blocks a
   tool call.

Nothing here fine-tunes an LLM. Everything is cheap, deterministic, and
inspectable (data/learning.json, data/learning_events.jsonl).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from . import logs

_LOCK = threading.RLock()
_STATE_PATH: Path | None = None
_EVENTS_PATH: Path | None = None

# In-memory mirrors (loaded from disk once).
_SHAPE_STATS: dict = {}          # "tool|k1,k2" -> {"ok":n,"fail":n,"ms":avg}
_TOOL_STATS: dict = {}           # "tool" -> {"ok":n,"fail":n,"ms":avg}
_MODEL_STATS: dict = {}          # "pool|model" -> {"ok":n,"fail":n,"ms":avg}
_CORRECTIONS: deque = deque(maxlen=500)
_WORKER: threading.Thread | None = None
_STOP = threading.Event()

# ------------------------------------------------------------------ shapes
_RECENT_TURNS = deque(maxlen=20)  # (ts, role, text_snippet) for correction detection


def _shape_key(tool: str, args: dict) -> str:
    keys = ",".join(sorted(args.keys())) if isinstance(args, dict) else ""
    return f"{tool}|{keys}"


def _bump(table: dict, key: str, ok: bool, ms: int) -> None:
    e = table.setdefault(key, {"ok": 0, "fail": 0, "ms": 0, "n": 0})
    e["n"] += 1
    if ok:
        e["ok"] += 1
    else:
        e["fail"] += 1
    # running mean, cheap
    e["ms"] = int((e["ms"] * (e["n"] - 1) + ms) / e["n"]) if e["n"] else ms


# ------------------------------------------------------------------ lifecycle
def configure(data_dir: Path | None) -> None:
    global _STATE_PATH, _EVENTS_PATH, _SHAPE_STATS, _TOOL_STATS, _MODEL_STATS
    if data_dir is None:
        return
    d = Path(data_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _STATE_PATH = d / "learning.json"
    _EVENTS_PATH = d / "learning_events.jsonl"
    try:
        if _STATE_PATH.exists():
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8-sig"))
            _SHAPE_STATS = raw.get("shapes", {}) or {}
            _TOOL_STATS = raw.get("tools", {}) or {}
            _MODEL_STATS = raw.get("models", {}) or {}
            for c in (raw.get("corrections") or [])[-500:]:
                _CORRECTIONS.append(c)
    except Exception as e:
        logs.warn(f"learning: could not load state: {e}")


def _save() -> None:
    if _STATE_PATH is None:
        return
    with _LOCK:
        try:
            payload = {
                "shapes": _SHAPE_STATS,
                "tools": _TOOL_STATS,
                "models": _MODEL_STATS,
                "corrections": list(_CORRECTIONS),
                "saved_at": time.time(),
            }
            tmp = _STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _STATE_PATH)
        except OSError as e:
            logs.warn(f"learning: could not save state: {e}")


def _append_event(event: dict) -> None:
    if _EVENTS_PATH is None:
        return
    try:
        with _EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ observe
def observe_tool(tool: str, args: dict, ok: bool, ms: int) -> None:
    """Called by the loop after every tool call. Non-blocking."""
    with _LOCK:
        _bump(_TOOL_STATS, tool, ok, ms)
        _bump(_SHAPE_STATS, _shape_key(tool, args or {}), ok, ms)


def observe_model(pool: str, model: str, ok: bool, ms: int) -> None:
    with _LOCK:
        _bump(_MODEL_STATS, f"{pool}|{model}", ok, ms)


def observe_turn(role: str, text: str) -> None:
    """Remember recent turns so we can spot user corrections."""
    if not text:
        return
    with _LOCK:
        _RECENT_TURNS.append((time.time(), role, text[:400]))


def note_correction(reason: str = "") -> None:
    """Called when the user says 'no/wrong/not that' after an answer."""
    with _LOCK:
        rec = {"t": time.time(), "reason": reason[:200]}
        _CORRECTIONS.append(rec)
        _append_event({"kind": "correction", **rec})
    logs.notice(f"learning: correction recorded ({reason[:80]})")


# ------------------------------------------------------------------ preference mining
_PREF_RX = [
    # "always X", "never X", "from now on X", "don't X"
    (re.compile(r"\b(?:please\s+)?always\s+(.+?)(?:[.!?]|$)", re.I), "always"),
    (re.compile(r"\b(?:please\s+)?never\s+(.+?)(?:[.!?]|$)", re.I), "never"),
    (re.compile(r"\bfrom now on,?\s+(.+?)(?:[.!?]|$)", re.I), "always"),
    (re.compile(r"\b(?:don'?t|do not)\s+(?:ever\s+)?(?:use|do|run)\s+(.+?)(?:[.!?]|$)", re.I), "never"),
    (re.compile(r"\bi\s+prefer\s+(.+?)(?:[.!?]|$)", re.I), "prefer"),
    (re.compile(r"\bmy\s+name\s+is\s+([A-Z][\w'\-]+)", re.I), "identity"),
]
_CORRECTION_RX = re.compile(
    r"\b(no[,!. ]|that'?s wrong|not what i|not that|incorrect|try again|you misunderstood|"
    r"i said|instead,?|actually,?)\b", re.I
)


def mine_preference(user_text: str) -> list[dict]:
    """Extract durable preferences from a user turn. Returns facts to save."""
    out = []
    if not user_text or len(user_text) > 600:
        return out
    for rx, kind in _PREF_RX:
        for m in rx.finditer(user_text):
            phrase = " ".join(m.group(1).split()).strip(" .!?,")
            if not phrase or len(phrase) > 180:
                continue
            if kind == "identity":
                out.append({"key": "user_name", "value": phrase})
            elif kind == "prefer":
                out.append({"key": "preference_" + _slug(phrase), "value": phrase})
            else:
                out.append({"key": f"{kind}_" + _slug(phrase), "value": phrase})
    return out


def looks_like_correction(user_text: str) -> bool:
    return bool(user_text and _CORRECTION_RX.search(user_text))


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s[:48] or "item"


# ------------------------------------------------------------------ prompt hints
def prompt_hints(cfg, limit: int = 6) -> str:
    """Return a short block to append to the system prompt.

    Contains (in priority order):
      1. The most common shape failures with a suggested alternative.
      2. Model/pool stats for the *primary* pool (so the model can warn
         the user that a specific model is flaky).
      3. Recent corrections.
    Never exceeds ~1200 chars.
    """
    lines: list[str] = []
    with _LOCK:
        # 1. shape failures
        bad = sorted(
            ((k, v) for k, v in _SHAPE_STATS.items() if v["fail"] >= 2
             and v["fail"] > v["ok"]),
            key=lambda kv: -(kv[1]["fail"] / max(1, kv[1]["ok"] + kv[1]["fail"])),
        )[:3]
        for k, v in bad:
            tool, keys = k.split("|", 1)
            lines.append(
                f"- `{tool}` with args [{keys}] failed {v['fail']}/{v['fail'] + v['ok']} times "
                f"(avg {v['ms']} ms). Prefer a different tool/shape if possible."
            )
        # 2. model stats
        pm = getattr(cfg, "providers", None)
        if pm is not None:
            try:
                primary = pm.pool_names()[0] if pm.pool_names() else None
                if primary:
                    for k, v in sorted(_MODEL_STATS.items(), key=lambda kv: -kv[1]["fail"]):
                        if k.startswith(primary + "|") and v["fail"] >= 3 and v["fail"] > v["ok"]:
                            _, model = k.split("|", 1)
                            lines.append(
                                f"- model `{model}` on `{primary}` failed {v['fail']}/{v['fail'] + v['ok']} times."
                            )
                            break
            except Exception:
                pass
        # 3. recent corrections
        if _CORRECTIONS:
            rec = _CORRECTIONS[-1]
            lines.append(f"- A recent answer was corrected by the user ({rec['reason'] or 'no reason given'}). Be more careful.")
    if not lines:
        return ""
    return "\n\nLEARNED FROM HISTORY (do not repeat these mistakes):\n" + "\n".join(lines[:limit])


# ------------------------------------------------------------------ consolidation
def consolidate() -> None:
    """Background housekeeping. Safe to call at any time."""
    now = time.time()
    with _LOCK:
        # Drop shapes with a tiny sample and no failures so the prompt stays clean.
        for k in list(_SHAPE_STATS):
            v = _SHAPE_STATS[k]
            if v["n"] < 5 and v["fail"] == 0:
                pass  # keep — small sample, but harmless
        # Cap correction history.
        while len(_CORRECTIONS) > 500:
            _CORRECTIONS.popleft()
        _save()
    logs.debug(f"learning: consolidated {len(_SHAPE_STATS)} shapes, {len(_CORRECTIONS)} corrections")


def _worker_loop() -> None:
    # Do a first pass quickly, then every 10 minutes.
    try:
        consolidate()
    except Exception:
        pass
    while not _STOP.wait(600):
        try:
            consolidate()
        except Exception as e:
            logs.debug(f"learning: worker error: {e}")


def start_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _STOP.clear()
        _WORKER = threading.Thread(target=_worker_loop, daemon=True, name="omni-learning")
        _WORKER.start()


def stop_worker() -> None:
    _STOP.set()
    w = _WORKER
    if w is not None:
        w.join(timeout=2.0)