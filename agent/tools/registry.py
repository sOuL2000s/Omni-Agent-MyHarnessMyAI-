import json
import traceback

TOOLS = {}


def tool(name, desc, props, required=()):
    def deco(fn):
        TOOLS[name] = {
            "fn": fn,
            "schema": {"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": list(required)}}},
        }
        return fn
    return deco


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
        if not isinstance(e, (ValueError, FileNotFoundError, PermissionError)) and e.__class__.__name__ != "Denied":
            out += "\n" + traceback.format_exc(limit=2)
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + f"\n...[truncated {len(out) - MAX_OUT} chars]"
    return out
