# agent/tools/memory.py
import json
import time
from .registry import tool


def _f(cfg):
    return cfg.data_dir / "memory.jsonl"


def _read_rows(cfg):
    """Read all memory rows, tolerating a partially-written last line."""
    if not _f(cfg).exists():
        return []
    rows = []
    for line in _f(cfg).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A crash mid-write can leave a truncated last line. Skip it
            # rather than blowing up every subsequent memory operation.
            continue
    return rows


@tool("memory_save", "Save a durable fact/preference/note for future sessions.",
      {"text": {"type": "string"}}, ["text"])
def memory_save(cfg, text):
    with _f(cfg).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M"), "text": text},
                           ensure_ascii=False) + "\n")
    return "saved"


@tool("memory_search",
      "Search saved memory by keywords. Returns up to 20 matches, oldest first "
      "so you see the original preference, not just its latest restatement. "
      "Empty query = latest 20 entries.",
      {"query": {"type": "string"}}, ["query"])
def memory_search(cfg, query=""):
    rows = _read_rows(cfg)
    if not rows:
        return "(no memory yet)"
    words = query.lower().split()
    if not words:
        # Empty query -> latest 20, newest-first (most recent at the top)
        tail = rows[-20:]
        return "\n".join(f"[{r['t']}] {r['text']}" for r in reversed(tail))
    matches = [r for r in rows if all(w in r["text"].lower() for w in words)]
    if not matches:
        return "no matches"
    # Query matches -> oldest-first, capped at 20. The *original* preference
    # is the one that shaped every later restatement, so it's the one the
    # model should see first. If there are more than 20, tell the model.
    shown = matches[:20]
    lines = [f"[{r['t']}] {r['text']}" for r in shown]
    if len(matches) > 20:
        lines.append(f"...[{len(matches) - 20} more match(es); refine the query to narrow it down]")
    return "\n".join(lines)


def recent(cfg, n=15):
    """Return the latest n entries as bullet points, for the system prompt.

    Ordered oldest-first so the model sees the arc of what's been saved
    rather than the last 15 things in reverse.
    """
    rows = _read_rows(cfg)
    if not rows:
        return ""
    tail = rows[-n:]
    return "\n".join(f"- {r['text']}" for r in tail)