from pathlib import Path
import sys
import threading


class Denied(Exception):
    pass


# Per-thread hooks so the web UI can answer approvals for each chat session separately.
_tl = threading.local()


def set_approver(fn, auto=None):
    """fn(action:str) -> bool is asked instead of the terminal. auto=True/False overrides AGENT_AUTO_APPROVE."""
    _tl.approver = fn
    _tl.auto = auto


def resolve(cfg, path: str, write=False) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = cfg.workspace / p
    p = p.resolve()
    inside = cfg.workspace == p or cfg.workspace in p.parents
    if not inside:
        if not cfg.allow_outside:
            raise Denied(f"Path outside workspace blocked: {p}. Set AGENT_ALLOW_OUTSIDE=1 to permit.")
        confirm(cfg, f"Access outside workspace: {p}")
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
        raise Denied(f"Approval required but no terminal available: {action}. Use AGENT_AUTO_APPROVE=1 for unattended runs.")
    ans = input(f"\n[approve?] {action}\n  (y/N) > ").strip().lower()
    if ans not in ("y", "yes"):
        raise Denied("User denied the action.")
