# agent/tools/registry.py
"""Tool registry with thread-local cancel support and external (MCP) registration."""
import json
import threading
import traceback

TOOLS = {}

_tl = threading.local()


def set_cancel(ev):
    """Attach a threading.Event to the current thread; tools poll it."""
    _tl.cancel = ev


def get_cancel():
    return getattr(_tl, "cancel", None)


def tool(name, desc, props, required=()):
    def deco(fn):
        TOOLS[name] = {
            "fn": fn,
            "schema": {"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props,
                               **({"required": list(required)} if required else {})}}},
        }
        return fn
    return deco


def register_external(name, description, parameters, fn, required=None):
    """Register a tool from outside this package (used by the MCP bridge).

    parameters: JSON-schema 'properties' dict OR a full JSON schema object
                (if it already has type/properties keys we take it as-is).
    """
    if isinstance(parameters, dict) and ("properties" in parameters or "type" in parameters):
        params = parameters
    else:
        params = {"type": "object", "properties": parameters or {}}
    if required:
        params.setdefault("required", list(required))
    TOOLS[name] = {
        "fn": fn,
        "schema": {"type": "function", "function": {
            "name": name, "description": description, "parameters": params}},
    }


def schemas():
    return [t["schema"] for t in TOOLS.values()]


MAX_OUT = 20000


def call(cfg, name, args):
    if name not in TOOLS:
        return f"ERROR: unknown tool {name}"
    try:
        if isinstance(args, str):
            args = json.loads(args or "{}")
        out = TOOLS[name]["fn"](cfg, **args)
        out = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1)
    except Exception as e:
        out = f"ERROR: {type(e).__name__}: {e}"
        if not isinstance(e, (ValueError, FileNotFoundError, PermissionError)) \
                and e.__class__.__name__ != "Denied":
            out += "\n" + traceback.format_exc(limit=2)
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + f"\n...[truncated {len(out) - MAX_OUT} chars]"
    return out