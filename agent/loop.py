# agent/loop.py
"""Tool loop with parallel read-only tool calls, self-healing retries and rich events."""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import llm
from .tools import schemas, call, TOOLS
from .tools.memory import recent
from .tools.registry import set_cancel as _set_tool_cancel
from .tools import shell as _shell

SYSTEM = """You are Omni, a precise, autonomous assistant working on the user's real computer.
Workspace (default folder for relative paths): {ws}
You have full read/write access to the user's filesystem (subject to approval). You have tools for
files, code editing, shell, web, Word/Excel/PowerPoint editing, long-term memory, and (when configured)
MCP servers whose tools appear as mcp__<server>__<tool>.

CORE RULES - do not break these:
- NEVER invent file contents, command output, URLs, API responses, or facts. If you need to know
  something about the user's machine, you MUST call a tool to find out. If you cannot verify it,
  say so explicitly instead of guessing.
- Read before you edit. For code use edit_file with a unique `old` snippet; for Word files use
  docx_read then docx_edit_paragraph / docx_replace_text so formatting is preserved.
- Work step by step: plan briefly, use tools, verify results (re-read the file, run tests), then
  report concisely. When you run a command, quote its actual output.
- Make the smallest change that satisfies the request. Do not rewrite whole files unless asked.
- If a tool call fails, DIAGNOSE it: wrong path? missing arg? different tool needed? Fix and retry.
  Do NOT repeat the exact same failing call.
- For independent reads (multiple files, several URLs) you may batch them into one turn - they run
  in parallel.
- Use memory_save for durable user preferences or facts worth keeping across sessions.
- Ask the user only when truly blocked.
{memory}"""


# Per-thread context so shell/tools can see the cancel event and the emitter.
_tl = threading.local()


def _emit(kind, text="", **extra):
    fn = getattr(_tl, "on_event", None)
    if fn:
        try:
            fn(kind, text, **extra)
        except Exception:
            pass


def _set_context(on_event, cancel):
    _tl.on_event = on_event
    _tl.cancel = cancel
    _set_tool_cancel(cancel)
    _shell.set_emitter(lambda kind, text, **kw: _emit(kind, text, **kw))


def system_prompt(cfg):
    mem = recent(cfg)
    return SYSTEM.format(ws=cfg.workspace,
                         memory=("\nKnown about the user:\n" + mem) if mem else "")


def new_history(cfg):
    return [{"role": "system", "content": system_prompt(cfg)}]


def _trim(history, limit=180_000, protect_from=None):
    """Drop the oldest tool outputs when the transcript gets too large.

    `protect_from` is the history index where the CURRENT turn begins; we never
    trim messages at or after that index (the model may still be reasoning
    about them).
    """
    total = sum(len(str(m.get("content") or "")) for m in history)
    if total <= limit:
        return
    end = len(history) if protect_from is None else max(1, protect_from)
    for m in history[1:end]:
        if m.get("role") == "tool" and len(m["content"]) > 400:
            m["content"] = m["content"][:300] + "\n...[old output trimmed]"


def cli_event(kind, text="", **extra):
    if kind == "tool":
        print(f"  -> {extra.get('name')}({text[:120]})")
    elif kind == "tool_stream":
        print(text, end="")
    elif kind == "notice":
        import sys
        print(f"  ! {text}", file=sys.stderr)
    elif kind == "progress":
        import sys
        print(f"  ... {text}", file=sys.stderr)
    elif kind == "assistant" and text.strip():
        print(f"  · {text.strip()[:300]}")


# Tools that can safely run in parallel with each other (read-only / side-effect-free).
PARALLEL_OK = {
    "read_file", "list_dir", "search_files",
    "web_fetch", "web_search",
    "docx_read", "xlsx_read", "pptx_read",
    "memory_search",
}
# Retries per tool name before we stop trying the same thing.
MAX_TOOL_RETRIES = 3


def run(cfg, history, user_text, on_event=cli_event, cancel=None):
    """Run the tool loop. Events:
       notice, progress, route, assistant, tool, tool_result, tool_stream, verify.
    """
    _set_context(on_event, cancel)
    try:
        return _run_inner(cfg, history, user_text, on_event, cancel)
    finally:
        # Clear the thread-locals so a subsequent run on the same thread
        # (e.g. a fresh session) doesn't accidentally inherit an old cancel
        # event or emitter.
        _set_tool_cancel(None)
        try:
            _shell.set_emitter(None)
        except Exception:
            pass


def _run_inner(cfg, history, user_text, on_event, cancel):
    turn_start = len(history)  # protect this turn's tool outputs from trimming
    history.append({"role": "user", "content": user_text})
    last_route = None
    failures = {}

    for turn in range(cfg.max_turns):
        if cancel is not None and cancel.is_set():
            return "(stopped by you)"
        _trim(history, protect_from=turn_start)

        try:
            msg, route = llm.chat_ex(
                cfg, history, schemas(),
                notify=lambda t: on_event("notice", t),
                cancel=cancel,
                on_progress=lambda payload: on_event("progress",
                                                     payload.get("title", ""),
                                                     **payload),
            )
        except llm.LLMError as e:
            on_event("error", str(e))
            return f"[error] {e}"

        if route != last_route:
            on_event("route", f"{route['provider']} · {route['model']}",
                     **route)
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
            final = entry["content"]
            if cfg.strict and len(final) > 120 and any(m.get("role") == "tool" for m in history[:-1]):
                try:
                    claims = llm.verify(cfg, history[:-1], final)
                    unverified = [c for c in claims if not c.get("verified")]
                    if unverified:
                        on_event(
                            "verify",
                            "⚠ Unverified claim(s) in the answer above:\n" +
                            "\n".join(f"  • {c['claim']}" for c in unverified[:5]),
                        )
                except Exception:
                    pass
            return final

        if entry["content"].strip():
            on_event("assistant", entry["content"])

        # ---- run the tool calls (parallel where safe) -------------------
        results = {}

        def run_one(c):
            # Worker threads don't inherit the main thread's threading.local
            # state, so re-attach the cancel event and the event emitter here.
            _set_tool_cancel(cancel)
            _shell.set_emitter(lambda kind, text, **kw: _emit(kind, text, **kw))
            fn = c["function"]
            name = fn["name"]
            on_event("tool", fn.get("arguments") or "", name=name, id=c["id"])
            if cancel is not None and cancel.is_set():
                res = "ERROR: stopped by user"
            else:
                res = call(cfg, name, fn.get("arguments"))
            on_event("tool_result", res[:1500], name=name, id=c["id"],
                     ok=not res.startswith("ERROR"))
            return c["id"], name, res

        all_parallel = len(calls) > 1 and all(c["function"]["name"] in PARALLEL_OK for c in calls)
        if all_parallel:
            with ThreadPoolExecutor(max_workers=min(4, len(calls))) as ex:
                futs = [ex.submit(run_one, c) for c in calls]
                for f in as_completed(futs):
                    cid, name, res = f.result()
                    results[cid] = (name, res)
        else:
            for c in calls:
                if cancel is not None and cancel.is_set():
                    break
                cid, name, res = run_one(c)
                results[cid] = (name, res)

        # ---- append tool results in the original call order -------------
        for c in calls:
            cid = c["id"]
            name, res = results.get(cid, (c["function"]["name"], "ERROR: cancelled"))
            history.append({"role": "tool", "tool_call_id": cid, "content": res})
            # self-healing hint on errors
            if res.startswith("ERROR"):
                failures[name] = failures.get(name, 0) + 1
                if failures[name] <= MAX_TOOL_RETRIES:
                    history.append({
                        "role": "system",
                        "content": ("The last `" + name + "` call failed. Diagnose: "
                                    "(1) wrong path? (2) missing/wrong argument? "
                                    "(3) different tool needed? Fix it and try a "
                                    "DIFFERENT call - do not repeat the exact same one."),
                    })
                else:
                    history.append({
                        "role": "system",
                        "content": (f"`{name}` has failed {failures[name]} times. Stop retrying it "
                                    "and either use a different approach or tell the user you're stuck."),
                    })
            else:
                # A success clears the streak so we don't blacklist a tool
                # that failed 3 times 40 turns ago for unrelated reasons.
                failures.pop(name, None)

        if cancel is not None and cancel.is_set():
            return "(stopped by you)"

    return "(stopped: reached max turns. Say 'continue' to go on.)"