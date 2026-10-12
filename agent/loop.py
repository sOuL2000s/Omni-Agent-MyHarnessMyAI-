import threading
from . import llm
from .tools import schemas, call, TOOLS
from .tools.memory import recent

SYSTEM = """You are Omni, a precise, autonomous assistant working on the user's machine.
Workspace: {ws}
You have tools for files, code editing, shell, web, Word/Excel/PowerPoint editing and long-term memory.

Rules:
- Read before you edit. For code use edit_file with a unique `old` snippet; for Word files use docx_read then docx_edit_paragraph / docx_replace_text so formatting is preserved.
- Work step by step: plan briefly, use tools, verify results (re-read the file, run tests), then report concisely.
- Never invent file contents or command output. If a tool errors, read the error and fix your call.
- Make the smallest change that satisfies the request. Do not rewrite whole files unless asked.
- Use memory_save for durable user preferences or facts worth keeping across sessions.
- Ask the user only when truly blocked.
{memory}"""


def system_prompt(cfg):
    mem = recent(cfg)
    return SYSTEM.format(ws=cfg.workspace, memory=("\nKnown about the user:\n" + mem) if mem else "")


def new_history(cfg):
    return [{"role": "system", "content": system_prompt(cfg)}]


def _trim(history, limit=180_000):
    """Cheap context control: shrink old tool outputs when history gets big."""
    total = sum(len(str(m.get("content") or "")) for m in history)
    if total <= limit:
        return
    for m in history[1:-6]:
        if m.get("role") == "tool" and len(m["content"]) > 400:
            m["content"] = m["content"][:300] + "\n...[old output trimmed]"


def cli_event(kind, text="", **extra):
    """Default event printer for the terminal."""
    if kind == "tool":
        print(f"  → {extra.get('name')}({text[:120]})")
    elif kind == "notice":
        import sys
        print(f"  ! {text}", file=sys.stderr)
    elif kind == "assistant" and text.strip():
        print(f"  · {text.strip()[:300]}")


def run(cfg, history, user_text, on_event=cli_event, cancel=None):
    """Run the tool loop. on_event(kind, text, **extra) kinds: notice, route, assistant, tool, tool_result."""
    history.append({"role": "user", "content": user_text})
    last_route = None
    for turn in range(cfg.max_turns):
        if cancel is not None and cancel.is_set():
            return "(stopped by you)"
        _trim(history)
        msg, route = llm.chat_ex(cfg, history, schemas(),
                                 notify=lambda t: on_event("notice", t), cancel=cancel)
        if route != last_route:
            on_event("route", f"{route['provider']} · {route['model']}", **route)
            last_route = route
        calls = msg.get("tool_calls") or []
        entry = {"role": "assistant", "content": msg.get("content") or ""}
        if calls:
            for i, c in enumerate(calls):
                c.setdefault("type", "function")
                if not c.get("id"):
                    c["id"] = f"call_{turn}_{i}"
            entry["tool_calls"] = calls
        history.append(entry)
        if not calls:
            return entry["content"]
        if entry["content"].strip():
            on_event("assistant", entry["content"])
        for c in calls:
            fn = c["function"]
            on_event("tool", fn.get("arguments") or "", name=fn["name"], id=c["id"])
            if cancel is not None and cancel.is_set():
                result = "ERROR: stopped by user"
            else:
                result = call(cfg, fn["name"], fn.get("arguments"))
            history.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            on_event("tool_result", result[:1500], name=fn["name"], id=c["id"], ok=not result.startswith("ERROR"))
    return "(stopped: reached max turns. Say 'continue' to go on.)"
