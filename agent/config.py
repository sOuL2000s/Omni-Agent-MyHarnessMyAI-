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
    for p in (ROOT / ".env", Path.cwd() / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = re.sub(r"\s+#.*$", "", line).strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


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


def _default_workspace() -> Path:
    env = os.environ.get("AGENT_WORKSPACE", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else (ROOT / p).resolve()
    return Path.home().resolve()


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
        auto_approve=g("AGENT_AUTO_APPROVE", "0") == "1",
        max_turns=int(g("AGENT_MAX_TURNS", "60")),
        temperature=float(g("AGENT_TEMPERATURE", "0.2")),
        timeout=int(g("AGENT_TIMEOUT", "180")),
        data_dir=data,
        strict=g("AGENT_STRICT", "1") == "1",
        home=Path.home().resolve(),
    )

    # Normalise the workspace once so safety.resolve() comparisons are exact
    # even when AGENT_WORKSPACE pointed at a symlink.
    try:
        cfg.workspace = cfg.workspace.resolve()
    except OSError:
        pass
    if cfg.home:
        try:
            cfg.home = cfg.home.resolve()
        except OSError:
            pass

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

    # ---- MCP servers (optional) ----------------------------------------
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