# agent/mcp_admin.py
"""Shared MCP admin operations used by both the web UI and the AI tools.

The web UI calls these via `App.mcp_add` etc. (which delegate here); the
AI calls them via `agent.tools.admin` (which also delegate here). Keeping
the logic in one place means the two call sites can't drift on validation
or on the file-write path.
"""
import json
import os
import re
import threading
from pathlib import Path


NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")


def read_config(cfg) -> dict:
    """Read data/mcp.json, tolerating missing / corrupt files."""
    p = cfg.data_dir / "mcp.json"
    if not p.exists():
        return {"servers": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"servers": {}}
    if not isinstance(data, dict):
        return {"servers": {}}
    data.setdefault("servers", {})
    return data


def write_config(cfg, data: dict) -> None:
    """Write data/mcp.json atomically (tmp + os.replace)."""
    p = cfg.data_dir / "mcp.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def validate_name(name: str) -> None:
    if not NAME_RE.match(name or ""):
        raise ValueError(
            "name must start with a letter and contain only letters, digits, _, ., -"
        )


def validate_transport(command: str, url: str) -> None:
    if not command and not url:
        raise ValueError("either command (local stdio) or url (remote HTTP) is required")


def reload_tools(cfg, registry=None) -> None:
    """Force the MCP bridge to re-register tools from the live registry."""
    from .tools import mcp_bridge
    mcp_bridge._registered_for = None
    try:
        mcp_bridge.register(cfg)
    except Exception:
        pass


def add_server(cfg, registry, spec: dict, lock=None) -> dict:
    """Add a server: validate, start it, persist, register tools.

    `registry` is the MCPRegistry (may be None; created if so).
    `lock` is the shared mcp_lock from App; used to serialize writes.
    Returns {"name": str, "tools": int}.
    """
    name = (spec.get("name") or "").strip()
    command = (spec.get("command") or "").strip()
    url = (spec.get("url") or "").strip()
    if not name:
        raise ValueError("name is required")
    validate_name(name)
    validate_transport(command, url)

    args = list(spec.get("args") or [])
    env = dict(spec.get("env") or {})
    headers = dict(spec.get("headers") or {})
    cwd = spec.get("cwd") or None

    from .mcp import MCPServer
    srv = MCPServer(name, command or None, args, env, cwd,
                    url=url or None, headers=headers)
    try:
        srv.start()
    except Exception as e:
        raise ValueError(f"could not start '{name}': {e}")

    cm = lock or _null_lock()
    with cm:
        data = read_config(cfg)
        entry = {
            "command": command or None,
            "url": url or None,
            "args": args,
            "env": env,
            "headers": headers,
            "enabled": True,
        }
        if cwd:
            entry["cwd"] = cwd
        data["servers"][name] = entry
        write_config(cfg, data)
        if registry is None:
            from .mcp import MCPRegistry
            registry = MCPRegistry()
            cfg.mcp = registry
        registry.servers[name] = srv
        reload_tools(cfg, registry)
    return {"name": name, "tools": len(srv.tools or [])}


def update_server(cfg, registry, old_name: str, spec: dict, lock=None) -> dict:
    """Stop the old server, remove its config, then add the new one.

    `spec.get("masked_secrets")` is honored: `"***"` values keep the
    on-disk value from the *old* entry.
    """
    old_name = (old_name or "").strip()
    new_name = (spec.get("name") or old_name).strip()
    if not old_name:
        raise ValueError("old_name is required")
    validate_name(new_name)
    command = (spec.get("command") or "").strip()
    url = (spec.get("url") or "").strip()
    validate_transport(command, url)

    args = list(spec.get("args") or [])
    env = dict(spec.get("env") or {})
    headers = dict(spec.get("headers") or {})
    cwd = spec.get("cwd") or None

    if spec.get("masked_secrets"):
        old_spec = (read_config(cfg).get("servers", {}).get(old_name, {}) or {})
        old_env = old_spec.get("env") or {}
        old_headers = old_spec.get("headers") or {}
        env = {k: (old_env.get(k, "") if v == "***" else v) for k, v in env.items()}
        headers = {k: (old_headers.get(k, "") if v == "***" else v) for k, v in headers.items()}

    cm = lock or _null_lock()
    with cm:
        if registry and old_name in registry.servers:
            srv = registry.servers.pop(old_name)
            try:
                srv.stop()
            except Exception:
                pass
        data = read_config(cfg)
        data["servers"].pop(old_name, None)
        write_config(cfg, data)

    return add_server(cfg, registry, {
        "name": new_name, "command": command, "url": url,
        "args": args, "env": env, "headers": headers, "cwd": cwd,
    }, lock=lock)


def delete_server(cfg, registry, name: str, lock=None) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    cm = lock or _null_lock()
    with cm:
        if registry and name in registry.servers:
            srv = registry.servers.pop(name)
            try:
                srv.stop()
            except Exception:
                pass
        data = read_config(cfg)
        data["servers"].pop(name, None)
        write_config(cfg, data)
        reload_tools(cfg, registry)


def start_server(cfg, registry, name: str) -> dict:
    """Start a server (build it from config if it wasn't loaded)."""
    name = (name or "").strip()
    srv = registry.servers.get(name) if registry else None
    if not srv:
        data = read_config(cfg)
        spec = data.get("servers", {}).get(name)
        if not spec:
            raise ValueError(f"no such MCP server: {name}")
        from .mcp import MCPServer
        srv = MCPServer(name, spec.get("command"), list(spec.get("args") or []),
                        dict(spec.get("env") or {}), spec.get("cwd"),
                        url=spec.get("url"), headers=dict(spec.get("headers") or {}))
        if registry is None:
            from .mcp import MCPRegistry
            registry = MCPRegistry()
            cfg.mcp = registry
        registry.servers[name] = srv
    srv.stop()
    srv.start()
    reload_tools(cfg, registry)
    return {"name": name, "tools": len(srv.tools or [])}


def stop_server(cfg, registry, name: str) -> None:
    srv = registry.servers.get(name) if registry else None
    if srv:
        srv.stop()


class _null_lock:
    def __enter__(self): return self
    def __exit__(self, *a): return False