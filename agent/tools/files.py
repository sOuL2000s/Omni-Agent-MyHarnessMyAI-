# agent/tools/files.py
import fnmatch
import re
from .registry import tool
from ..safety import resolve, confirm

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}


@tool("read_file", "Read a text file with line numbers. Use start/end (1-based, inclusive) for big files.",
      {"path": S, "start": I, "end": I}, ["path"])
def read_file(cfg, path, start=1, end=None):
    p = resolve(cfg, path)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    end = end or len(lines)
    start = max(1, start)
    body = "\n".join(f"{i}\t{l}" for i, l in enumerate(lines[start - 1:end], start))
    return f"{p} ({len(lines)} lines)\n{body}"


@tool("write_file", "Create or overwrite a text file with the full content.",
      {"path": S, "content": S}, ["path", "content"])
def write_file(cfg, path, content):
    p = resolve(cfg, path, write=True)
    if p.exists():
        if not _in_ws(cfg, p):
            confirm(cfg, f"Overwrite {p}", tool="write_file")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {p}"


def _in_ws(cfg, p):
    return cfg.workspace == p or cfg.workspace in p.parents


@tool("edit_file",
      "Precise edit: replace exact text `old` with `new` in a file. `old` must match exactly once unless replace_all=true. "
      "Always read the file first and include enough context in `old` to be unique.",
      {"path": S, "old": S, "new": S, "replace_all": B}, ["path", "old", "new"])
def edit_file(cfg, path, old, new, replace_all=False):
    p = resolve(cfg, path, write=True)
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        raise ValueError("`old` text not found (check whitespace/indentation).")
    if n > 1 and not replace_all:
        raise ValueError(f"`old` matches {n} places; add context or set replace_all=true.")
    p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1), encoding="utf-8")
    return f"Edited {p}: {n if replace_all else 1} replacement(s)"


@tool("list_dir", "List files in a directory (recursive up to depth).", {"path": S, "depth": I})
def list_dir(cfg, path=".", depth=2):
    root = resolve(cfg, path)
    out = []

    def walk(d, lvl):
        if lvl > depth:
            return
        for c in sorted(d.iterdir()):
            if c.name.startswith(".git") or c.name in ("node_modules", "__pycache__", ".venv"):
                continue
            out.append("  " * lvl + c.name + ("/" if c.is_dir() else ""))
            if c.is_dir():
                walk(c, lvl + 1)
    walk(root, 0)
    return "\n".join(out[:500]) or "(empty)"


@tool("search_files", "Regex search (grep) across files. Optional glob filter like '*.py'.",
      {"pattern": S, "path": S, "glob": S}, ["pattern"])
def search_files(cfg, pattern, path=".", glob="*"):
    root = resolve(cfg, path)
    rx = re.compile(pattern)
    hits = []
    for f in root.rglob("*"):
        if not f.is_file() or not fnmatch.fnmatch(f.name, glob):
            continue
        if any(x in f.parts for x in (".git", "node_modules", "__pycache__", ".venv")):
            continue
        try:
            for i, l in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(l):
                    hits.append(f"{f.relative_to(root)}:{i}: {l.strip()[:200]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n...[200 hit limit]"
        except Exception:
            continue
    return "\n".join(hits) or "No matches."