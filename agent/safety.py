# agent/safety.py
from pathlib import Path
import sys
import threading


class Denied(Exception):
    pass


_tl = threading.local()

# Files inside data/ that hold credentials or session state. They stay
# unreadable by the agent's file tools even when data/ (or one of its
# parents) is set as the workspace.
_SECRET_FILES = {"providers.json", "state.json", "memory.jsonl"}


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


def resolve(cfg, path: str, write=False) -> Path:
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        p = cfg.workspace / p
    p = p.resolve()

    # Always refuse the credential/state files, even when data/ is "inside".
    try:
        data = cfg.data_dir.resolve()
    except OSError:
        data = cfg.data_dir
    if p.parent == data and p.name in _SECRET_FILES:
        raise Denied(f"Refusing to touch {p.name}: it holds credentials/state.")

    inside = cfg.workspace == p or cfg.workspace in p.parents
    if not inside:
        if not cfg.allow_outside:
            raise Denied(
                f"Path outside workspace blocked: {p}. "
                f"Set AGENT_ALLOW_OUTSIDE=1 to permit."
            )
        confirm(cfg, f"{'WRITE to' if write else 'Read'} outside workspace: {p}",
                tool="write_file" if write else "read_file")
    return p


def confirm(cfg, action: str, tool=None):
    auto = getattr(_tl, "auto", None)
    if auto is True or (auto is None and cfg.auto_approve):
        return
    approver = getattr(_tl, "approver", None)
    if approver is not None:
        if not _call_approver(approver, action, tool):
            raise Denied("User denied the action.")
        return
    if not sys.stdin.isatty():
        raise Denied(
            f"Approval required but no terminal available: {action}. "
            f"Set AGENT_AUTO_APPROVE=1 for unattended runs."
        )
    ans = input(f"\n[approve?] {action}\n  (y/N) > ").strip().lower()
    if ans not in ("y", "yes"):
        raise Denied("User denied the action.")