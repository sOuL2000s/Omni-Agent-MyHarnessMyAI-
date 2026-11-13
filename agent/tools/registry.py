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


# ------------------------------------------------------------------ schema normalisation
# Gemini's function-calling validator is stricter than OpenAI's. It rejects:
#   * `"required": []`                     (empty array)
#   * a `properties` map with no `required` key (some gateway versions)
#   * `parameters` without type:"object"
#   * an `array` with no `items`
#   * anyOf / oneOf / allOf / $ref
#   * additionalProperties
#   * description > 1024 chars
# We normalise every schema at registration time so we never send an
# unsupported shape, regardless of which tool defines it.
_ALLOWED_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def _normalise_prop(spec: dict) -> dict:
    if not isinstance(spec, dict):
        return {"type": "string"}
    t = spec.get("type")
    if t not in _ALLOWED_TYPES:
        t = "object" if "properties" in spec else "string"
    out = {"type": t}
    desc = spec.get("description")
    if isinstance(desc, str) and desc:
        out["description"] = desc[:1000]
    if t == "object":
        props = spec.get("properties") or {}
        out["properties"] = {k: _normalise_prop(v) for k, v in props.items()}
        req = [k for k in (spec.get("required") or []) if k in out["properties"]]
        if req:
            out["required"] = req
    elif t == "array":
        items = spec.get("items") or {"type": "string"}
        if not isinstance(items, dict):
            items = {"type": "string"}
        if items.get("type") not in _ALLOWED_TYPES:
            items = {"type": "string"}
        out["items"] = _normalise_prop(items)
    if isinstance(spec.get("enum"), list) and spec["enum"]:
        out["enum"] = [str(x) for x in spec["enum"]]
    return out


def _normalise_parameters(props: dict, required: list) -> dict:
    props = props or {}
    norm_props = {k: _normalise_prop(v) for k, v in props.items()}
    # Keep only required keys that actually exist, in stable order.
    req = [k for k in (required or []) if k in norm_props]
    # Gemini REQUIRES the `required` key whenever `properties` is non-empty.
    # Even an empty list is fine. When `properties` is empty we still emit
    # `required: []` for symmetry (harmless, and avoids a second code path).
    return {"type": "object", "properties": norm_props, "required": req}


def tool(name, desc, props, required=()):
    def deco(fn):
        params = _normalise_parameters(props, list(required))
        TOOLS[name] = {
            "fn": fn,
            "schema": {"type": "function", "function": {
                "name": name,
                "description": (desc or "")[:1000],
                "parameters": params}},
            "required": list(params.get("required") or []),
        }
        return fn
    return deco


def register_external(name, description, parameters, fn, required=None):
    """Register a tool from outside this package (used by the MCP bridge).

    `parameters` may be:
      * a bare properties dict,
      * or a full JSON-Schema object (it has `type` / `properties` keys).
    Either way it's passed through `_normalise_parameters`, so an MCP
    server that ships a sloppy schema can't break the whole request.
    """
    if isinstance(parameters, dict) and ("properties" in parameters or "type" in parameters):
        raw = parameters
    else:
        raw = {"type": "object", "properties": parameters or {}}
    req = list(required or raw.get("required") or [])
    params = _normalise_parameters(raw.get("properties") or {}, req)
    TOOLS[name] = {
        "fn": fn,
        "schema": {"type": "function", "function": {
            "name": name,
            "description": (description or "")[:1000],
            "parameters": params}},
        "required": list(params.get("required") or []),
    }


def schemas():
    """Return every tool schema, Gemini-safe.

    Two extra guards on top of `_normalise_parameters`:
      * tools with no parameters still get a full `{"type":"object","properties":{}}`
        (some Gemini gateways reject a missing `parameters` key),
      * descriptions are capped at 1024 chars so the total request stays
        under provider limits.
    """
    out = []
    for t in TOOLS.values():
        s = t["schema"]
        params = s["function"].get("parameters")
        if not isinstance(params, dict) or params.get("type") != "object":
            s["function"]["parameters"] = {"type": "object", "properties": {}}
        elif not params.get("properties"):
            params["properties"] = {}
        out.append(s)
    return out


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