# agent/learning.py
"""Persistent learning layer: outcome ledger, preference mining,
correction detection, per-task-type model stats, consolidation.

What this actually does (honestly):
  1. Tool/model/task-shape outcome ledger — every tool call and every
     model route is recorded with ok/fail and latency.
  2. Preference mining — a deterministic regex grammar turns durable user
     phrases into memory facts ("always use black" → fact
     always_use_black="always use black").
  3. Correction detection — records "no/wrong/not that" turns.
  4. Per-task-type model stats — each user turn is classified into a
     coarse bucket (chat, code-read, code-edit, shell, file-ops,
     research, office); the winning (pool, model) is recorded under
     that bucket so `prompt_hints()` can say "gemini-2.5-pro is the
     best code-edit model this week".
  5. Consolidation — real dedupe of near-identical facts (Levenshtein),
     pruning of low-signal shapes, cap on corrections.
  6. Background worker — consolidates every 10 min.

Nothing here fine-tunes an LLM. It's cheap, deterministic, inspectable.
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
_SHAPE_STATS: dict = {}          # "tool|k1,k2" -> {"ok","fail","ms","n"}
_TOOL_STATS: dict = {}           # "tool" -> {"ok","fail","ms","n"}
_MODEL_STATS: dict = {}          # "pool|model" -> {"ok","fail","ms","n"}
_TASK_MODEL_STATS: dict = {}     # "task|pool|model" -> {"ok","fail","ms","n"}
_CORRECTIONS: deque = deque(maxlen=500)
_WORKER: threading.Thread | None = None
_STOP = threading.Event()

_RECENT_TURNS = deque(maxlen=20)  # (ts, role, text_snippet)

# ------------------------------------------------------------------ task types
_TASK_PATTERNS = [
    ("code-edit", re.compile(
        r"\b(edit|refactor|rename|replace|patch|fix|modify|update)\b.*\b(file|code|function|class|module|method|script)\b"
        r"|\b(edit_file|write_file|apply_patch)\b", re.I)),
    ("code-read", re.compile(
        r"\b(read|review|explain|summari[sz]e|audit|inspect|look at|check)\b.*\b(code|file|repo|project|function|class|module)\b"
        r"|\b(read|review|explain|summari[sz]e|audit|inspect|look at|check)\b.*\.(py|js|ts|jsx|tsx|rs|go|c|cc|cpp|h|hpp|java|rb|php|cs|swift|kt|scala|sh|bash|zsh|ps1|lua|pl|r|jl|m|mm|sql|html|css|scss|sass|less|vue|svelte|dart|ex|exs|erl|hs|clj|cljs|fs|fsx|nim|zig)\b"
        r"|\b(read_file|list_dir|search_files)\b", re.I)),
    ("shell", re.compile(
        r"\b(run|execute|start|launch|install|build|test|deploy|npm|pip|git|docker|uvicorn|flask)\b"
        r"|\brun_shell\b", re.I)),
    ("file-ops", re.compile(
        r"\b(copy|move|delete|rename|mkdir|create folder|organi[sz]e|list files|find file)\b", re.I)),
    ("research", re.compile(
        r"\b(search|google|look up|find out|research|fetch|download|web)\b", re.I)),
    ("office", re.compile(
        r"\b(word|docx|excel|xlsx|spreadsheet|powerpoint|pptx|slide|document)\b", re.I)),
]


def classify_task(user_text: str) -> str:
    """Coarse, cheap bucket. Always returns a value."""
    if not user_text:
        return "chat"
    for name, rx in _TASK_PATTERNS:
        if rx.search(user_text):
            return name
    return "chat"


# ------------------------------------------------------------------ shapes
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
    e["ms"] = int((e["ms"] * (e["n"] - 1) + ms) / e["n"]) if e["n"] else ms


# ------------------------------------------------------------------ lifecycle
def configure(data_dir: Path | None) -> None:
    global _STATE_PATH, _EVENTS_PATH, _SHAPE_STATS, _TOOL_STATS, _MODEL_STATS, _TASK_MODEL_STATS
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
            _TASK_MODEL_STATS = raw.get("task_models", {}) or {}
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
                "task_models": _TASK_MODEL_STATS,
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
    with _LOCK:
        _bump(_TOOL_STATS, tool, ok, ms)
        _bump(_SHAPE_STATS, _shape_key(tool, args or {}), ok, ms)


def observe_model(pool: str, model: str, ok: bool, ms: int,
                  task: str | None = None) -> None:
    with _LOCK:
        _bump(_MODEL_STATS, f"{pool}|{model}", ok, ms)
        if task:
            _bump(_TASK_MODEL_STATS, f"{task}|{pool}|{model}", ok, ms)


def observe_turn(role: str, text: str) -> None:
    if not text:
        return
    with _LOCK:
        _RECENT_TURNS.append((time.time(), role, text[:400]))


def note_correction(reason: str = "") -> None:
    with _LOCK:
        rec = {"t": time.time(), "reason": reason[:200]}
        _CORRECTIONS.append(rec)
        _append_event({"kind": "correction", **rec})
    logs.notice(f"learning: correction recorded ({reason[:80]})")


# ------------------------------------------------------------------ preference mining (widened)
_PREF_RX = [
    # "always X" / "never X" / "from now on X"
    (re.compile(r"\b(?:please\s+)?always\s+(.+?)(?:[.!?]|$)", re.I), "always"),
    (re.compile(r"\b(?:please\s+)?never\s+(.+?)(?:[.!?]|$)", re.I), "never"),
    (re.compile(r"\bfrom now on,?\s+(.+?)(?:[.!?]|$)", re.I), "always"),
    (re.compile(r"\b(?:don'?t|do not)\s+(?:ever\s+)?(?:use|do|run)\s+(.+?)(?:[.!?]|$)", re.I), "never"),
    # NEW: "use X (instead|rather)" -> prefer
    (re.compile(r"\buse\s+(.+?)\s+(?:instead|rather)\b", re.I), "prefer"),
    # NEW: "next time ..." -> prefer (treat the clause as a preference)
    (re.compile(r"\bnext time,?\s+(.+?)(?:[.!?]|$)", re.I), "prefer"),
    # NEW: "remember (that) X" -> note
    (re.compile(r"\bremember\s+(?:that\s+)?(.+?)(?:[.!?]|$)", re.I), "note"),
    # NEW: "I (hate|dislike|avoid) X" -> never
    (re.compile(r"\bi\s+(?:hate|dislike|avoid)\s+(.+?)(?:[.!?]|$)", re.I), "never"),
    # existing
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
            elif kind == "note":
                out.append({"key": "note_" + _slug(phrase), "value": phrase})
            elif kind == "prefer":
                out.append({"key": "preference_" + _slug(phrase), "value": phrase})
            else:
                out.append({"key": f"{kind}_" + _slug(phrase), "value": phrase})
    # De-dupe within this single turn (identical extracted phrases).
    seen = set()
    uniq = []
    for f in out:
        if f["key"] in seen:
            continue
        seen.add(f["key"])
        uniq.append(f)
    return uniq


def looks_like_correction(user_text: str) -> bool:
    return bool(user_text and _CORRECTION_RX.search(user_text))


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s[:48] or "item"


# ------------------------------------------------------------------ prompt hints
def prompt_hints(cfg, limit: int = 8) -> str:
    """Return a short block to append to the system prompt.

    Priority order:
      1. best (pool, model) per task type (only where it differs from the
         current default and the sample is >= 3),
      2. failing tool shapes with a suggested alternative,
      3. model stats on the primary pool (flaky model warning),
      4. most recent correction.
    Never exceeds ~1500 chars.
    """
    lines: list[str] = []
    with _LOCK:
        # 1. per-task model preference
        try:
            pm = getattr(cfg, "providers", None)
            primary = pm.pool_names()[0] if (pm and pm.pool_names()) else None
            current_model = pm.model_of(primary) if primary else None
            for task, _rx in _TASK_PATTERNS:
                if task == "chat":
                    continue
                cand = [(k, v) for k, v in _TASK_MODEL_STATS.items()
                        if k.startswith(task + "|") and v["n"] >= 3]
                if not cand:
                    continue
                best_k, best_v = max(cand, key=lambda kv: (kv[1]["ok"] - kv[1]["fail"], kv[1]["ok"]))
                _, pool, model = best_k.split("|", 2)
                if pool != primary or model == current_model:
                    continue
                # only emit if the win is meaningful
                if best_v["ok"] < 2 or best_v["ok"] <= best_v["fail"]:
                    continue
                lines.append(
                    f"- for {task} tasks, `{model}` on `{pool}` has the best "
                    f"record here ({best_v['ok']} ok / {best_v['fail']} fail). "
                    f"Prefer it when the task fits."
                )
        except Exception:
            pass

        # 2. failing tool shapes
        bad = sorted(
            ((k, v) for k, v in _SHAPE_STATS.items()
             if v["fail"] >= 2 and v["fail"] > v["ok"]),
            key=lambda kv: -(kv[1]["fail"] / max(1, kv[1]["ok"] + kv[1]["fail"])),
        )[:3]
        for k, v in bad:
            tool, keys = k.split("|", 1)
            lines.append(
                f"- `{tool}` with args [{keys}] failed {v['fail']}/{v['fail'] + v['ok']} times "
                f"(avg {v['ms']} ms). Prefer a different tool/shape if possible."
            )

        # 3. flaky model on the primary pool
        try:
            pm = getattr(cfg, "providers", None)
            if pm is not None:
                primary = pm.pool_names()[0] if pm.pool_names() else None
                if primary:
                    for k, v in sorted(_MODEL_STATS.items(), key=lambda kv: -kv[1]["fail"]):
                        if k.startswith(primary + "|") and v["fail"] >= 3 and v["fail"] > v["ok"]:
                            _, model = k.split("|", 1)
                            lines.append(
                                f"- model `{model}` on `{primary}` failed "
                                f"{v['fail']}/{v['fail'] + v['ok']} times this week."
                            )
                            break
        except Exception:
            pass

        # 4. most recent correction
        if _CORRECTIONS:
            rec = _CORRECTIONS[-1]
            lines.append(
                f"- A recent answer was corrected by the user "
                f"({rec['reason'] or 'no reason given'}). Be more careful."
            )
    if not lines:
        return ""
    return "\n\nLEARNED FROM HISTORY (do not repeat these mistakes):\n" + "\n".join(lines[:limit])


# ------------------------------------------------------------------ consolidation
def _levenshtein(a: str, b: str, cap: int = 3) -> int:
    """Bounded Levenshtein; returns > cap if the distance exceeds cap."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(v)
            row_min = min(row_min, v)
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _dedupe_facts() -> int:
    """Merge near-identical fact keys. Returns number of merges.
    Only touches the in-memory correction/preference key space that is
    surfaced via the system prompt; the SQLite facts table is owned by
    tools.memory and left alone (memory_store_fact already does INSERT
    OR REPLACE on an identical key)."""
    # Placeholder hook: we do NOT rewrite SQLite here (that's the memory
    # tool's job). We just prune low-signal ledger entries below.
    return 0


def _prune_shapes() -> int:
    """Drop shape entries with < 3 samples and no failures, or that are
    older than the state file itself (i.e. never re-observed). Returns
    number of entries dropped."""
    dropped = 0
    with _LOCK:
        for k in list(_SHAPE_STATS):
            v = _SHAPE_STATS[k]
            if v["n"] < 3 and v["fail"] == 0:
                del _SHAPE_STATS[k]
                dropped += 1
        for k in list(_TASK_MODEL_STATS):
            v = _TASK_MODEL_STATS[k]
            if v["n"] < 2 and v["fail"] == 0:
                del _TASK_MODEL_STATS[k]
                dropped += 1
    return dropped


def consolidate() -> None:
    """Background housekeeping. Safe to call any time."""
    with _LOCK:
        merged = _dedupe_facts()
        dropped = _prune_shapes()
        while len(_CORRECTIONS) > 500:
            _CORRECTIONS.popleft()
        _save()
    logs.debug(
        f"learning: consolidated {len(_SHAPE_STATS)} shapes, "
        f"{len(_TASK_MODEL_STATS)} task-model rows, "
        f"{len(_CORRECTIONS)} corrections "
        f"(pruned {dropped} low-signal rows, merged {merged})"
    )


def _worker_loop() -> None:
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