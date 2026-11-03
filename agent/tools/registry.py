# agent/tools/registry.py
"""Tool registry with thread-local cancel support and external (MCP) registration."""
import json
import re
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
            "required": list(required),
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
        "required": list(required or (params.get("required") or [])),
    }


def schemas():
    return [t["schema"] for t in TOOLS.values()]


MAX_OUT = 20000


def _repair_json(text):
    """Best-effort repair of malformed JSON arguments.

    Handles the shapes models actually emit:
      * trailing commas
      * single quotes instead of double
      * unquoted keys
      * a leading "arguments: " or "```json" fence
      * bare strings that were meant to be objects
    Returns a parsed object (dict/list) or raises ValueError.
    """
    if isinstance(text, (dict, list)):
        return text
    s = (text or "").strip()
    if not s:
        return {}
    # Strip code fences and leading label.
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    s = re.sub(r"^arguments?\s*[:=]\s*", "", s, flags=re.I)
    # Fast path.
    try:
        return json.loads(s)
    except ValueError:
        pass
    # Extract first balanced {…} or […] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(s)):
            if s[i] == opener:
                depth += 1
            elif s[i] == closer:
                depth -= 1
                if depth == 0:
                    candidate = s[start:i + 1]
                    # Trailing commas.
                    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
                    # Single quotes -> double, if no double quotes present.
                    if "'" in candidate and '"' not in candidate:
                        candidate = candidate.replace("'", '"')
                    try:
                        return json.loads(candidate)
                    except ValueError:
                        break
    raise ValueError("not valid JSON")


def call(cfg, name, args):
    if name not in TOOLS:
        return f"ERROR: unknown tool {name}"
    try:
        # 1. Parse arguments (with repair).
        if isinstance(args, str):
            try:
                parsed = _repair_json(args)
            except ValueError:
                required = TOOLS[name].get("required") or []
                return (
                    f"ERROR: could not parse arguments for `{name}`: "
                    f"not valid JSON. You sent: {args[:200]!r}. "
                    f"Expected a JSON object"
                    + (f" with keys: {', '.join(required)}" if required else "")
                    + ". Please re-issue the call with valid JSON."
                )
        else:
            parsed = args or {}

        # 2. Check required arguments up front so the model gets a clear
        #    error instead of a Python TypeError.
        required = TOOLS[name].get("required") or []
        missing = [k for k in required if k not in parsed]
        if missing:
            return (
                f"ERROR: `{name}` is missing required argument(s): "
                f"{', '.join(missing)}. You sent: {json.dumps(parsed)[:200]}. "
                f"Please re-issue the call with all required arguments."
            )

        # 3. Dispatch.
        out = TOOLS[name]["fn"](cfg, **parsed)
        out = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1)
    except TypeError as e:
        # Model called with unexpected kwargs (e.g. `password` instead of
        # `api_key`). Give it the exact signature so it can fix itself.
        required = TOOLS[name].get("required") or []
        props = (TOOLS[name]["schema"]["function"]["parameters"].get("properties") or {})
        allowed = ", ".join(props.keys()) or "(none)"
        out = (
            f"ERROR: `{name}` rejected the arguments: {e}. "
            f"Allowed arguments: {allowed}. "
            + (f"Required: {', '.join(required)}. " if required else "")
            + "Please re-issue the call with valid argument names."
        )
    except Exception as e:
        out = f"ERROR: {type(e).__name__}: {e}"
        if not isinstance(e, (ValueError, FileNotFoundError, PermissionError)) \
                and e.__class__.__name__ != "Denied":
            out += "\n" + traceback.format_exc(limit=2)
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + f"\n...[truncated {len(out) - MAX_OUT} chars]"
    return out