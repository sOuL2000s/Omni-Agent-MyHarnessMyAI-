import json
import time
from .registry import tool


def _f(cfg):
    return cfg.data_dir / "memory.jsonl"


@tool("memory_save", "Save a durable fact/preference/note for future sessions.", {"text": {"type": "string"}}, ["text"])
def memory_save(cfg, text):
    with _f(cfg).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M"), "text": text}, ensure_ascii=False) + "\n")
    return "saved"


@tool("memory_search", "Search saved memory by keywords (empty query = latest 20).", {"query": {"type": "string"}})
def memory_search(cfg, query=""):
    if not _f(cfg).exists():
        return "(no memory yet)"
    rows = [json.loads(l) for l in _f(cfg).read_text(encoding="utf-8").splitlines() if l.strip()]
    words = query.lower().split()
    if words:
        rows = [r for r in rows if all(w in r["text"].lower() for w in words)]
    return "\n".join(f"[{r['t']}] {r['text']}" for r in rows[-20:]) or "no matches"


def recent(cfg, n=15):
    if not _f(cfg).exists():
        return ""
    rows = [json.loads(l) for l in _f(cfg).read_text(encoding="utf-8").splitlines() if l.strip()]
    return "\n".join(f"- {r['text']}" for r in rows[-n:])
