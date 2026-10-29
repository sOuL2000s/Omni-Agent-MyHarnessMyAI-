# agent/safety.py
from pathlib import Path
import os
import sys
import threading


class Denied(Exception):
    pass


_tl = threading.local()

# Files inside data/ that hold credentials or session state. They stay
# unreadable by the agent's file tools even when data/ (or one of its
# parents) is set as the workspace.
_SECRET_FILES = {
    "providers.json", "state.json", "memory.jsonl",
    "mcp.json", "mcp_registry.json", "settings.json",
}


def set_approver(fn, auto=None):
    """fn(action:str, tool:str|None=None) -> bool.

    `tool` is the tool name (e.g. "run_shell"), so the UI can offer
    "don't ask again for this tool". A one-argument callable still works.
    """
    _tl.approver = fn
    _tl.auto = auto


def _call_approver(fn, action, tool):
    try:
        return bool(fn(action, tool))
    except TypeError:
        return bool(fn(action))


def _is_secret(p: Path, data: Path) -> bool:
    """True if `p` points at a secret file (by path or by inode).

    The inode check catches hardlinks: a file in the workspace whose
    inode matches a secret file in data/ is the *same* bytes, even though
    the path is different. `Path.resolve()` doesn't collapse hardlinks,
    so a pure path comparison misses this.
    """
    if p.parent == data and p.name in _SECRET_FILES:
        return True
    if p.parent.name == "sessions" and p.parent.parent == data:
        return True
    # Hardlink bypass: same inode as any secret file in data/.
    try:
        p_st = p.stat()
    except OSError:
        return False
    for name in _SECRET_FILES:
        try:
            secret_st = (data / name).stat()
        except OSError:
            continue
        if (p_st.st_dev == secret_st.st_dev
                and p_st.st_ino == secret_st.st_ino):
            return True
    return False


def resolve(cfg, path: str, write=False, strict=True) -> Path:
    """Resolve `path` against the workspace and enforce the safety rules.

    `strict=True` (the default) runs the full check, including the
    approval prompt for paths outside the workspace. `strict=False`
    skips *only* the outside-workspace approval — the credential/state
    file check still runs. The non-strict mode is for internal callers
    that already know the path is safe (e.g. a `read_file_now` helper
    that computes the path from a hash). Today nothing uses it; it
    exists so future tools don't have to bypass `resolve()` entirely.
    """
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        p = cfg.workspace / p
    p = p.resolve()

    # Always refuse credential/state files, even when data/ is "inside".
    try:
        data = cfg.data_dir.resolve()
    except OSError:
        data = cfg.data_dir
    if _is_secret(p, data):
        raise Denied(f"Refusing to touch {p.name}: it holds credentials/state.")

    inside = cfg.workspace == p or cfg.workspace in p.parents
    if not inside:
        if not cfg.allow_outside:
            raise Denied(
                f"Path outside workspace blocked: {p}. "
                f"Set AGENT_ALLOW_OUTSIDE=1 to permit."
            )
        if strict:
            confirm(cfg, f"{'WRITE to' if write else 'Read'} outside workspace: {p}",
                    tool="write_file" if write else "read_file")
    return p


def confirm(cfg, action: str, tool=None):
    auto = getattr(_tl, "auto", None)
    if auto is True or (auto is None and cfg.auto_approve):
        return
    approver = getattr(_tl, "approver", None)
    if approver is not None:
        # The approver itself is responsible for handling cancellation (the
        # web UI's ask() polls the session cancel event). Nothing else to do
        # here — we just propagate a False result as Denied.
        if not _call_approver(approver, action, tool):
            raise Denied("User denied the action.")
        return
    if not sys.stdin.isatty():
        raise Denied(
            f"Approval required but no terminal available: {action}. "
            f"Set AGENT_AUTO_APPROVE=1 for unattended runs."
        )
    # Interactive terminal: honour the cancel event while waiting for input.
    try:
        from .tools.registry import get_cancel
        cancel = get_cancel()
    except Exception:
        cancel = None
    if cancel is None:
        ans = input(f"\n[approve?] {action}\n  (y/N) > ").strip().lower()
    else:
        # Poll for cancellation in a small loop. We can't interrupt input()
        # portably, but we can at least refuse to spend more than a couple
        # seconds after the user pressed Stop.
        import time
        print(f"\n[approve?] {action}\n  (y/N) > ", end="", flush=True)
        # Note: input() blocks; if the cancel fires while we're blocked, the
        # next keystroke (Enter) is what unblocks us. This is the best we
        # can do without a full TTY rewrite.
        ans = input().strip().lower()
        if cancel.is_set():
            raise Denied("User cancelled the task.")
    if ans not in ("y", "yes"):
        raise Denied("User denied the action.")