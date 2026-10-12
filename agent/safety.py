from pathlib import Path
import sys


class Denied(Exception):
    pass


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
    if cfg.auto_approve:
        return
    if not sys.stdin.isatty():
        raise Denied(f"Approval required but no terminal available: {action}. Use AGENT_AUTO_APPROVE=1 for unattended runs.")
    ans = input(f"\n[approve?] {action}\n  (y/N) > ").strip().lower()
    if ans not in ("y", "yes"):
        raise Denied("User denied the action.")
