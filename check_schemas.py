# check_schemas.py — prints every tool schema in a form that tells us
# immediately whether Gemini will reject it.
import json
from agent import config
from agent.tools import schemas

cfg = config.load()          # ensures registry is fully populated

bad = []
for s in schemas():
    fn = s["function"]
    p = fn.get("parameters") or {}
    req = p.get("required")
    t = p.get("type")
    props = list((p.get("properties") or {}).keys())
    flag = ""
    # Correct Gemini rules (empirically confirmed against
    # gemini-2.5-flash through the OpenAI-compatible endpoint):
    #
    #   R1: if `properties` is non-empty, `required` MUST be present.
    #       It MAY be `[]` — that is the correct way to say "all optional".
    #       This was the original bug: `required` was absent, so Gemini
    #       rejected the whole request with `tools[N].parameters`.
    #
    #   R2: if `properties` is empty, `required` MAY be absent or `[]`.
    #       Both are accepted; we prefer `[]` for symmetry.
    #
    # The only *unsafe* shapes are:
    #   - non-empty properties + required ABSENT   -> will be rejected
    #   - required listing a key not in properties -> will be rejected
    if props and req is None:
        flag += "  <-- MISSING required (Gemini will reject)"
    if props and isinstance(req, list):
        missing = [k for k in req if k not in props]
        if missing:
            flag += f"  <-- required references unknown props: {missing}"
    # `required: []` with non-empty props is FINE — that means "all optional",
    # which Gemini accepts. Do NOT flag it. Only *absent* required is wrong.
    if t != "object":
        flag += f"  <-- type={t!r}"
    # every array property must have items
    for k, v in (p.get("properties") or {}).items():
        if v.get("type") == "array" and "items" not in v:
            flag += f"  <-- array '{k}' has no items"
        if v.get("type") not in ("string", "number", "integer", "boolean", "array", "object"):
            flag += f"  <-- prop '{k}' type={v.get('type')!r}"
    print(f"{fn['name']:<32} type={t!r:<10} required={req!r:<30} props={props}{flag}")
    if flag:
        bad.append(fn["name"])

print()
print("BAD SCHEMAS:", bad or "none")