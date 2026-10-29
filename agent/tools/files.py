# agent/tools/files.py
import fnmatch
import os
import re
import time
from pathlib import Path
from .registry import tool
from ..safety import resolve, confirm

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

# Directories we never want to walk into. Both `list_dir` and
# `search_files` use this; `_prune` is called with a Path and returns True
# if the directory should be skipped entirely.
_NOISE_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".venv", "venv", "env", ".env",
    ".next", ".nuxt", "dist", "build", "target", ".cache",
    "Pods", "DerivedData",
    "AppData", "Library",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode",
}


def _prune(path) -> bool:
    """True if this path should be skipped by the directory walkers."""
    name = path.name
    if name in _NOISE_DIRS:
        return True
    # Hidden dot-dirs (but allow the workspace root itself, which is
    # passed as the argument, not discovered during the walk).
    if name.startswith(".") and name not in (".gitkeep",):
        return True
    return False


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


@tool("list_dir", "List files in a directory (recursive up to depth). Truncates at ~12000 chars.",
      {"path": S, "depth": I})
def list_dir(cfg, path=".", depth=2):
    root = resolve(cfg, path)
    out = []

    def walk(d, lvl):
        if lvl > depth:
            return
        try:
            children = sorted(d.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
        except (PermissionError, OSError):
            return
        for c in children:
            if _prune(c):
                continue
            out.append("  " * lvl + c.name + ("/" if c.is_dir() else ""))
            if c.is_dir():
                walk(c, lvl + 1)

    walk(root, 0)
    if not out:
        return "(empty)"
    # The model reads this as text, not lines. A char cap is the honest
    # limit — 12000 chars is roughly 3k tokens, which is safe to inline.
    body = "\n".join(out)
    if len(body) > 12000:
        head = body[:12000]
        return head + f"\n...[truncated at 12000 chars; {len(out)} total entries]"
    return body


@tool("search_files",
      "Regex search (grep) across files. Optional glob filter like '*.py'. "
      "Stops after 30 seconds or 200 hits, whichever comes first.",
      {"pattern": S, "path": S, "glob": S, "timeout": I}, ["pattern"])
def search_files(cfg, pattern, path=".", glob="*", timeout=30):
    root = resolve(cfg, path)
    rx = re.compile(pattern)
    hits = []
    start = time.time()
    hit_limit = 200
    char_limit = 12000

    def _timed_out():
        return (time.time() - start) > timeout

    def _files():
        # Prune noise directories *before* descending, using os.walk
        # instead of rglob so we don't enter directories we'll skip
        # anyway. `rglob` has no way to prune mid-walk.
        for dirpath, dirnames, filenames in os.walk(root):
            # In-place filter so os.walk doesn't descend into them.
            dirnames[:] = [d for d in dirnames
                           if not _prune((root / d)) and d not in _NOISE_DIRS]
            for name in filenames:
                if _prune(Path(os.path.join(dirpath, name))):
                    continue
                if not fnmatch.fnmatch(name, glob):
                    continue
                yield root / os.path.relpath(os.path.join(dirpath, name), root)

    for f in _files():
        if _timed_out():
            hits.append(f"...[search timed out after {timeout}s; {len(hits)} hits so far]")
            break
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError):
            continue
        for i, l in enumerate(text.splitlines(), 1):
            if rx.search(l):
                rel = f.relative_to(root) if f.is_relative_to(root) else f
                hits.append(f"{rel}:{i}: {l.strip()[:200]}")
                if len(hits) >= hit_limit:
                    hits.append(f"...[{hit_limit} hit limit reached]")
                    return _format_hits(hits, char_limit)
                if sum(len(h) + 1 for h in hits) > char_limit:
                    hits.append(f"...[truncated at {char_limit} chars]")
                    return _format_hits(hits, char_limit)
    return _format_hits(hits, char_limit) if hits else "No matches."


def _format_hits(hits, char_limit):
    if not hits:
        return "No matches."
    body = "\n".join(hits)
    if len(body) > char_limit:
        return body[:char_limit] + f"\n...[truncated at {char_limit} chars]"
    return body