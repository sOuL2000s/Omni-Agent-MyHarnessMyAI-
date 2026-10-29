# agent/config.py
import json
import os
import re
import sys as _sys
from dataclasses import dataclass
from pathlib import Path


def _find_root() -> Path:
    if getattr(_sys, "frozen", False) or os.environ.get("OMNI_ROOT"):
        return Path(os.environ.get("OMNI_ROOT") or Path(_sys.executable).parent)
    return Path(__file__).resolve().parent.parent


ROOT = _find_root()


def _load_env():
    """Load .env without letting a stale `KEY=` value silently win.

    Precedence (highest to lowest):
      1. A real shell export (`os.environ` already has the key).
      2. The first .env file that has the key.
      3. The code default in load().

    A line that just says `AGENT_AUTO_APPROVE=` (no value) is treated as
    "not set" so the code default applies — this is the difference from
    the old version, which treated it as the empty string.
    """
    for p in (ROOT / ".env", Path.cwd() / ".env"):
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = re.sub(r"\s+#.*$", "", line).strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not k or not v:
                # empty value -> ignore so the code default wins
                continue
            os.environ.setdefault(k, v)


@dataclass
class Config:
    base_url: str
    api_key: str
    model: str
    workspace: Path
    allow_outside: bool
    auto_approve: bool
    max_turns: int
    temperature: float
    timeout: int
    data_dir: Path
    providers: object = None
    strict: bool = True
    home: Path = None
    mcp: object = None
    # Runtime mode flags (see §6.2 / §7.3 in the README).
    shell_bg: bool = False
    # New: hard cap on total tool calls per task, so a runaway loop on a
    # cheap/fast model can't burn 500 calls before max_turns trips.
    max_total_tool_calls: int = 200


def _default_workspace() -> Path:
    env = os.environ.get("AGENT_WORKSPACE", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else (ROOT / p).resolve()
    return Path.home().resolve()


def _is_fs_root(p: Path) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        rp = p
    return rp == rp.parent


def _is_ancestor_of(a: Path, b: Path) -> bool:
    """True if `a` is `b` or a parent of `b` (both resolved)."""
    try:
        a = a.resolve()
        b = b.resolve()
    except OSError:
        pass
    return a == b or a in b.parents


def load() -> Config:
    _load_env()
    g = os.environ.get
    ws = _default_workspace()
    ws.mkdir(parents=True, exist_ok=True)
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    try:
        os.chmod(data, 0o700)  # owner-only on Unix; no-op on Windows
    except OSError:
        pass

    cfg = Config(
        base_url=g("AGENT_BASE_URL", "http://localhost:20128/v1").rstrip("/"),
        api_key=g("AGENT_API_KEY", ""),
        model=g("AGENT_MODEL", "auto"),
        workspace=ws,
        allow_outside=g("AGENT_ALLOW_OUTSIDE", "1") == "1",
        # OFF by default — matches .env.example and README §11.2. Set
        # AGENT_AUTO_APPROVE=1 in .env to skip all Allow/Deny prompts.
        # The web UI shows a persistent red banner when it is active, and
        # the header toggle turns it off for a single session.
        auto_approve=_env_bool("AGENT_AUTO_APPROVE", default=False),
        max_turns=int(g("AGENT_MAX_TURNS", "60")),
        temperature=float(g("AGENT_TEMPERATURE", "0.2")),
        timeout=int(g("AGENT_TIMEOUT", "180")),
        data_dir=data,
        strict=g("AGENT_STRICT", "1") == "1",
        home=Path.home().resolve(),
        shell_bg=_env_bool("AGENT_SHELL_BG", default=False),
        max_total_tool_calls=int(g("AGENT_MAX_TOOL_CALLS", "200")),
    )

    try:
        cfg.workspace = cfg.workspace.resolve()
    except OSError:
        pass
    if cfg.home:
        try:
            cfg.home = cfg.home.resolve()
        except OSError:
            pass

    if _is_fs_root(cfg.workspace):
        print(
            f"ERROR: AGENT_WORKSPACE points at a filesystem root ({cfg.workspace}).\n"
            f"  That disables the outside-workspace approval guard.\n"
            f"  Set AGENT_WORKSPACE to a normal folder (your home or a project dir).",
            file=_sys.stderr,
        )
        raise SystemExit(2)

    from .providers import Manager, Provider
    default = Provider("default", cfg.base_url, cfg.api_key, cfg.model)
    cfg.providers = Manager(data / "providers.json", default,
                            state_path=data / "state.json",
                            settings_path=data / "settings.json")
    if cfg.providers.load_error and not cfg.providers.items:
        print("WARNING:", cfg.providers.load_error, file=_sys.stderr)
        print("  Fix or delete data/providers.json, then restart.", file=_sys.stderr)
    if g("AGENT_MAX_WAIT"):
        cfg.providers.settings["max_wait"] = int(g("AGENT_MAX_WAIT"))
    if g("AGENT_STRATEGY") in ("round_robin", "failover"):
        cfg.providers.settings["strategy"] = g("AGENT_STRATEGY")

    try:
        from .mcp import MCPRegistry
        mcp = MCPRegistry()
        mcp.load(data / "mcp.json")
        cfg.mcp = mcp
        from .tools import mcp_bridge
        mcp_bridge.register(cfg)
    except Exception:
        cfg.mcp = None

    return cfg


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var. Missing or empty -> `default`."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "n", "")