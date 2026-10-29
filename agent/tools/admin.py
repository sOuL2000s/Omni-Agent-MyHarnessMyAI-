# agent/tools/admin.py
"""AI self-configuration tools.

These let the model change Omni's own configuration (add keys, manage MCP
servers, tune settings) in response to a natural-language request, using
the *same* code path as the web UI. Nothing here bypasses the approval
flow — each tool that mutates state goes through `confirm()`, so the
user still sees an Allow/Deny dialog before anything happens.

The tools are registered under the `admin_*` namespace to make it
obvious in the transcript what they do.
"""
import json
import os
import re
from pathlib import Path

from .registry import tool
from ..safety import confirm
from ..providers import PRESETS, detect_provider


def _require_confirm(cfg, action, tool_name):
    """Wrapper around confirm() that always fires for admin tools.

    Even if auto_approve is on, we want the user to see what's about to
    change — the transcript is the audit log. The one exception is when
    the caller is in a context with no terminal AND auto_approve is off;
    in that case confirm() raises Denied, which is the right behavior.
    """
    confirm(cfg, action, tool=tool_name)


@tool(
    "admin_add_key",
    "Add one or more API keys to Omni's configuration. Use this when the user "
    "asks you to configure a new model service. This tool always asks the user "
    "for approval before writing.",
    {"pool": {"type": "string"},
     "keys": {"type": "string"},
     "base_url": {"type": "string"},
     "model": {"type": "string"}},
    ["pool", "keys"],
)
def admin_add_key(cfg, pool, keys, base_url=None, model=None):
    pm = cfg.providers
    key_list = [k.strip() for k in re.split(r"[\s,;]+", keys) if k.strip()]
    if not key_list:
        return "ERROR: no keys provided"
    pool = (pool or "").strip()
    if not pool:
        det = detect_provider(key_list[0])
        pool = det or "custom"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]*", pool):
        return "ERROR: pool name must start with a letter and contain only letters, digits, _, ., -"
    _require_confirm(
        cfg,
        f"Add {len(key_list)} API key(s) to pool '{pool}'"
        + (f" (model {model})" if model else "")
        + (f" at {base_url}" if base_url else ""),
        "admin_add_key",
    )
    try:
        added, dup = pm.add_keys(pool, key_list, base_url or None, model or None)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    msg = f"Added {len(added)} key(s) to pool '{pool}'"
    if dup:
        msg += f", skipped {dup} duplicate(s)"
    if added:
        # Do a quick probe of the first added key so the model (and user)
        # know whether the key actually works.
        try:
            from .. import llm
            p = pm.get(added[0])
            ok, kind, text = llm.probe(cfg, p)
            if ok:
                msg += f". Test call OK: {text!r}"
            else:
                msg += f". WARNING: test call failed [{kind}]: {text[:160]}"
        except Exception as e:
            msg += f". (Test skipped: {e})"
    return msg


@tool(
    "admin_remove_key",
    "Remove a single API key by name. Asks for approval first.",
    {"name": {"type": "string"}},
    ["name"],
)
def admin_remove_key(cfg, name):
    pm = cfg.providers
    try:
        p = pm.get(name)
    except ValueError as e:
        return f"ERROR: {e}"
    _require_confirm(cfg, f"Remove key '{name}' (pool {p.pool}, {p.masked()})", "admin_remove_key")
    try:
        pm.remove(name)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    return f"Removed key '{name}'"


@tool(
    "admin_set_pool_model",
    "Change the model used by every key in a pool. Asks for approval first.",
    {"pool": {"type": "string"}, "model": {"type": "string"}},
    ["pool", "model"],
)
def admin_set_pool_model(cfg, pool, model):
    pm = cfg.providers
    try:
        resolved = pm.resolve_pool(pool)
    except ValueError as e:
        return f"ERROR: {e}"
    _require_confirm(cfg, f"Set pool '{resolved}' to model '{model}'", "admin_set_pool_model")
    try:
        pm.set_model(resolved, model)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    n = len(pm.providers_in(resolved))
    return f"Pool '{resolved}' ({n} key(s)) now uses model '{model}'"


@tool(
    "admin_list_providers",
    "List every configured pool, its keys, and their health. Read-only.",
    {},
)
def admin_list_providers(cfg):
    pm = cfg.providers
    pools = pm.summary()
    if not pools:
        return "No keys configured yet."
    lines = []
    for i, p in enumerate(pools):
        c = p["counts"]
        star = "* " if i == 0 else "  "
        lines.append(
            f"{star}{p['pool']}: model={p['model']} "
            f"({c['ready']} ready, {c['cooling']} cooling, {c['invalid']} invalid) "
            f"at {p['base_url']}"
        )
        for k in p["keys"][:5]:
            lines.append(f"    {k['name']:<14} {k['masked']:<12} {k['status']}")
        if len(p["keys"]) > 5:
            lines.append(f"    ... and {len(p['keys']) - 5} more")
    return "\n".join(lines)


@tool(
    "admin_test_key",
    "Send a tiny real request to a key (or one key of a pool) and report the result. Read-only.",
    {"name": {"type": "string"}},
)
def admin_test_key(cfg, name=""):
    from .. import llm
    pm = cfg.providers
    if not name:
        # Probe one key of the primary pool.
        pools = pm.pool_names()
        if not pools:
            return "No pools configured."
        ps = pm.providers_in(pools[0])
        if not ps:
            return "Primary pool has no keys."
        p = ps[0]
    else:
        try:
            p = pm.get(name)
        except ValueError:
            # Try resolving as a pool name.
            ps = pm.providers_in(name)
            if not ps:
                return f"ERROR: no key or pool named '{name}'"
            p = ps[0]
    ok, kind, text = llm.probe(cfg, p)
    if ok:
        return f"{p.name} OK ({p.model}): {text!r}"
    return f"{p.name} FAILED [{kind}]: {text[:200]}"


@tool(
    "admin_add_mcp_server",
    "Add and start a Model Context Protocol (MCP) server so the model gains "
    "new tools. Give either `command`+`args` (local stdio) or `url` (remote HTTP). "
    "Asks for approval before writing data/mcp.json or starting the process.",
    {"name": {"type": "string"},
     "command": {"type": "string"},
     "args": {"type": "string"},
     "url": {"type": "string"},
     "env": {"type": "string"},
     "headers": {"type": "string"}},
    ["name"],
)
def admin_add_mcp_server(cfg, name, command="", args="", url="", env="", headers=""):
    from .. import mcp_admin
    name = (name or "").strip()
    command = (command or "").strip()
    url = (url or "").strip()
    if not command and not url:
        return "ERROR: give either `command` (local) or `url` (remote)"
    # Parse env / headers: "KEY=VALUE per line" / "Header: value per line".
    env_dict = {}
    for line in (env or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env_dict[k.strip()] = v.strip().strip('"').strip("'")
    hdr_dict = {}
    for line in (headers or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        hdr_dict[k.strip()] = v.strip()

    _require_confirm(
        cfg,
        f"Add MCP server '{name}'"
        + (f" (command: {command})" if command else f" (url: {url})"),
        "admin_add_mcp_server",
    )
    try:
        res = mcp_admin.add_server(
            cfg, getattr(cfg, "mcp", None),
            {"name": name, "command": command, "url": url,
             "args": args.split() if args else [],
             "env": env_dict, "headers": hdr_dict},
        )
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    return f"Added MCP server '{res['name']}' with {res['tools']} tool(s)"


@tool(
    "admin_remove_mcp_server",
    "Remove an MCP server: stops its process, deletes its config, unregisters its tools. "
    "Asks for approval first.",
    {"name": {"type": "string"}},
    ["name"],
)
def admin_remove_mcp_server(cfg, name):
    from .. import mcp_admin
    name = (name or "").strip()
    if not name:
        return "ERROR: name is required"
    _require_confirm(cfg, f"Remove MCP server '{name}'", "admin_remove_mcp_server")
    try:
        mcp_admin.delete_server(cfg, getattr(cfg, "mcp", None), name)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    return f"Removed MCP server '{name}'"


@tool(
    "admin_list_mcp_servers",
    "List every configured MCP server with its status. Read-only.",
    {},
)
def admin_list_mcp_servers(cfg):
    reg = getattr(cfg, "mcp", None)
    if not reg or not reg.servers:
        return "No MCP servers configured."
    lines = []
    for name, srv in reg.servers.items():
        status = "running" if srv.ready else "stopped"
        tools = len(srv.tools or [])
        err = f" — {srv.last_error}" if srv.last_error else ""
        lines.append(f"{name}: {status}, {tools} tool(s){err}")
    return "\n".join(lines)


@tool(
    "admin_set_setting",
    "Change one of Omni's runtime settings (temperature, timeout, strict mode, "
    "auto-approve, max_turns, max_total_tool_calls, shell_bg). Writes to .env and "
    "applies live. Asks for approval first.",
    {"key": {"type": "string"}, "value": {"type": "string"}},
    ["key", "value"],
)
def admin_set_setting(cfg, key, value):
    from .. import commands as _commands
    key = (key or "").strip()
    value = (value or "").strip()
    if not key.startswith("AGENT_") and not key.startswith("OMNI_"):
        return "ERROR: only AGENT_* / OMNI_* settings can be changed"
    # Whitelist the ones we know how to live-apply. Everything else is
    # written to .env and takes effect on next restart.
    live_appliable = {
        "AGENT_AUTO_APPROVE", "AGENT_STRICT", "AGENT_SHELL_BG",
        "AGENT_MAX_TURNS", "AGENT_MAX_TOOL_CALLS", "AGENT_TEMPERATURE",
        "AGENT_TIMEOUT", "AGENT_WORKSPACE",
    }
    _require_confirm(
        cfg,
        f"Set {key}={value}" + ("" if key in live_appliable else " (takes effect on next restart)"),
        "admin_set_setting",
    )
    # Reuse the command handler's implementation.
    return _commands.handle(cfg, f"/config {key}={value}")