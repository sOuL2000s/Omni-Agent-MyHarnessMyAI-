# agent/safety.py
from pathlib import Path
import sys
import threading


class Denied(Exception):
    pass


_tl = threading.local()


def set_approver(fn, auto=None):
    """fn(action:str) -> bool asked instead of the terminal. auto=True/False overrides AGENT_AUTO_APPROVE."""
    _tl.approver = fn
    _tl.auto = auto


def resolve(cfg, path: str, write=False) -> Path:
    """Resolve a path. Relative paths are anchored at the workspace; absolute paths
    anywhere on the PC are allowed (with approval unless auto-approve is on)."""
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        p = cfg.workspace / p
    p = p.resolve()
    inside = cfg.workspace == p or cfg.workspace in p.parents
    if not inside:
        if not cfg.allow_outside:
            raise Denied(
                f"Path outside workspace blocked: {p}. "
                f"Set AGENT_ALLOW_OUTSIDE=1 to permit."
            )
        confirm(cfg, f"{'WRITE to' if write else 'Read'} outside workspace: {p}")
    return p


def confirm(cfg, action: str):
    auto = getattr(_tl, "auto", None)
    if auto is True or (auto is None and cfg.auto_approve):
        return
    approver = getattr(_tl, "approver", None)
    if approver is not None:
        if not approver(action):
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