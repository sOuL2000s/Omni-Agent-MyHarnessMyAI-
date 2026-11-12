# agent/loop.py
"""Tool loop with parallel read-only tool calls, self-healing retries and rich events.

Adds an optional, conservative auto-save heuristic: when a user turn
contains durable-preference language ("remember", "always", "from now on",
"my name is", "I prefer", "don't … again"), the loop saves a short memory
entry after the answer succeeds. This is a heuristic, not an LLM call, so
it costs no extra tokens. Near-identical entries are deduped locally by
memory_save().

Unlimited by default
--------------------
`cfg.max_turns` and `cfg.max_total_tool_calls` are 0 by default, which
means "no limit". When they're positive they still act as a soft budget
with a warning event, not a hard stop. The only hard stop is the user
pressing Stop (the cancel event).
"""
import json
import os
import re
import sys
import threading
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import llm
from .tools import schemas, call, TOOLS
from .tools.memory import recent, get_all_facts_formatted, memory_save
from .tools.registry import set_cancel as _set_tool_cancel
from .tools import shell as _shell

SYSTEM = """You are Omni, a precise, autonomous assistant working on the user's real computer.
Current time: {datetime}
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
- DEV SERVERS (npm run dev, vite, next dev, flask run, uvicorn, python manage.py runserver,
  cargo watch, dotnet watch, etc.) MUST be launched with run_shell(..., background=true).
  The tool result often contains a DETECTED URL — if it does, report that URL and move on.
  If it does NOT, call wait_background(job_id=..., timeout=20) exactly ONCE.
  If wait_background reports no new output, STOP checking: the server is quiet, which is
  normal. NEVER call tail_background in a loop. NEVER call it more than twice for the same job.
- Make the smallest change that satisfies the request. Do not rewrite whole files unless asked.
- If a tool call fails, DIAGNOSE it: wrong path? missing arg? different tool needed? Fix and retry.
  Do NOT repeat the exact same failing call.
- For independent reads (multiple files, several URLs) you may batch them into one turn - they run
  in parallel.
- Use memory_save for durable user preferences or facts worth keeping across sessions. When the
  user says "remember", "always", "from now on", "I prefer", or "my name is", save it.
- Ask the user only when truly blocked.
- You can change Omni's own configuration when the user asks: admin_add_key,
  admin_set_pool_model, admin_add_mcp_server, admin_set_setting, etc. These
  always ask the user for approval before writing. Use them instead of telling
  the user to open the Keys panel.
- SAVED SKILLS: before improvising a multi-step workflow, call skill_list
  to see whether the user has saved one. If a skill matches the task, call
  skill_read(name) and follow it exactly. When you work out a good reusable
  recipe for the user, offer to save it with skill_save — but only after
  the task succeeds.
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
    facts = get_all_facts_formatted(cfg)
    now = datetime.datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")

    memory_section = ""
    if facts:
        memory_section += "\nUser Preferences & Facts:\n" + facts
    if mem:
        memory_section += "\nRecent Chat Context:\n" + mem

    return SYSTEM.format(ws=cfg.workspace,
                         datetime=now,
                         memory=memory_section)


def new_history(cfg):
    return [{"role": "system", "content": system_prompt(cfg)}]


def refresh_system_prompt(cfg, history):
    """Rewrite history[0] if the workspace (or memory) changed."""
    if not history or history[0].get("role") != "system":
        return False
    new_sp = system_prompt(cfg)
    if history[0].get("content") == new_sp:
        return False
    history[0]["content"] = new_sp
    return True


def _trim(history, limit=180_000, protect_from=None):
    """Drop the oldest tool outputs when the transcript gets too large."""
    total = 0
    for m in history:
        total += len(str(m.get("content") or ""))
        if m.get("tool_calls"):
            total += len(str(m["tool_calls"]))
    if total <= limit:
        return
    end = len(history) if protect_from is None else max(1, protect_from)
    for m in history[1:end]:
        if m.get("role") == "tool" and len(m.get("content") or "") > 400:
            m["content"] = m["content"][:300] + "\n...[old output trimmed]"
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            trimmed_calls = []
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                args = fn.get("arguments", "")
                if isinstance(args, str) and len(args) > 500:
                    fn = dict(fn, arguments=args[:500] + "…[trimmed]")
                    c = dict(c, function=fn)
                trimmed_calls.append(c)
            m["tool_calls"] = trimmed_calls


# ---------------------------------------------------------------- styling
# `_NO_COLOR` is decided at import time as a *conservative* default:
# piped output and NO_COLOR are the two conditions that always disable
# color. On Windows the parent module (`agent.__main__`) calls
# `SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)` and then calls
# `set_color(True)` so classic cmd.exe doesn't print raw `\033[…m`
# escapes. If that call never happens (e.g. tests import loop directly),
# the conservative default stands.
_NO_COLOR = (not sys.stdout.isatty()) or bool(os.environ.get("NO_COLOR"))


def set_color(enabled: bool) -> None:
    """Allow the parent module to override the color decision."""
    global _NO_COLOR
    _NO_COLOR = not enabled


def _w(text, code=""):
    if _NO_COLOR or not code:
        return str(text)
    return f"{code}{text}\033[0m"


def cli_event(kind, text="", **extra):
    ORANGE = "" if _NO_COLOR else "\033[38;5;208m"
    AMBER = "" if _NO_COLOR else "\033[38;5;214m"
    GREEN = "" if _NO_COLOR else "\033[38;5;42m"
    RED = "" if _NO_COLOR else "\033[38;5;203m"
    CYAN = "" if _NO_COLOR else "\033[38;5;51m"
    PURPLE = "" if _NO_COLOR else "\033[38;5;141m"
    GRAY = "" if _NO_COLOR else "\033[38;5;245m"
    BOLD = "" if _NO_COLOR else "\033[1m"

    if kind == "tool":
        name = extra.get("name") or "tool"
        args = (text or "").replace("\n", " ")[:110]
        print(f"  {_w('▸', CYAN)} {_w(name, BOLD + CYAN)}({_w(args, GRAY)})")
    elif kind == "tool_stream":
        sys.stdout.write(_w(text, GRAY))
        sys.stdout.flush()
    elif kind == "tool_result":
        ok = extra.get("ok", True)
        mark = _w("✓", GREEN + BOLD) if ok else _w("✗", RED + BOLD)
        first = (text or "").splitlines()[0][:110] if text else ""
        print(f"  {mark} {_w(first, GRAY)}")
    elif kind == "notice":
        print(f"  {_w('↪', AMBER)} {_w(text, AMBER)}")
    elif kind == "progress":
        title = extra.get("title") or text or "working"
        detail = extra.get("detail") or ""
        sys.stderr.write(f"\r  {_w('… ' + title + ' ' + detail, GRAY)}")
        sys.stderr.flush()
    elif kind == "route":
        print(_w(f"  ◆ route: {extra.get('provider','')} · {extra.get('model','')}", PURPLE))
    elif kind == "assistant" and text.strip():
        print()
        print(_simple_box("Omni", text.strip(), ORANGE))
    elif kind == "verify":
        print(_simple_box("⚠ Unverified claims", text, AMBER))
    elif kind == "error":
        print(_simple_box("✗ Error", text, RED), file=sys.stderr)


def _simple_box(title, body, color):
    """A minimal box used by the non-interactive cli_event."""
    import textwrap
    if _NO_COLOR:
        color = ""
    inner_w = 76
    top_label = f" {title} "
    top = f"{color}╭{top_label}{'─' * max(1, inner_w - len(top_label))}╮\033[0m" if color else \
          f"╭{top_label}{'─' * max(1, inner_w - len(top_label))}╮"
    out = [top]
    for para in (body or "").rstrip().split("\n"):
        if not para:
            out.append((color + "│" if color else "│") + " " * inner_w + (color + "│\033[0m" if color else "│"))
            continue
        for line in textwrap.wrap(para, width=inner_w - 2, replace_whitespace=False, drop_whitespace=False) or [""]:
            out.append((color + "│" if color else "│") + " " + line.ljust(inner_w - 2) + " " + (color + "│\033[0m" if color else "│"))
    out.append((color + "╰" + "─" * inner_w + "╯\033[0m") if color else ("╰" + "─" * inner_w + "╯"))
    return "\n".join(out)


# Tools that can safely run in parallel with each other.
PARALLEL_OK = {
    "read_file", "list_dir", "search_files",
    "web_fetch", "web_search",
    "docx_read", "xlsx_read", "pptx_read",
    "memory_search", "memory_get_fact", "memory_list_facts",
    "skill_list", "skill_read",
}
MAX_TOOL_RETRIES = 3
SOFT_CALL_WARNING_EVERY = 500

# Heuristic for "the user asked us to remember something". Deliberately
# conservative: a false positive only costs one memory entry, but it
# should not fire on every "always run the tests".
_SAVE_RX = re.compile(
    r"\b(remember|from now on|always|never|my name is|i prefer|i like|"
    r"i hate|don'?t (?:ever )?(?:do|use|run)|do not (?:ever )?(?:do|use|run))\b",
    re.I,
)


def _maybe_autosave(cfg, user_text, final_answer):
    """Save a short memory entry if the user turn looks durable."""
    if not user_text or not final_answer:
        return
    if len(user_text) > 400:
        return
    if not _SAVE_RX.search(user_text):
        return
    snippet = " ".join(user_text.split())
    try:
        memory_save(cfg, f"User preference: {snippet[:300]}")
    except Exception:
        pass


def run(cfg, history, user_text, on_event=cli_event, cancel=None):
    """Run the tool loop. Events:
       notice, progress, route, assistant, tool, tool_result, tool_stream, verify.
    """
    _set_context(on_event, cancel)
    try:
        return _run_inner(cfg, history, user_text, on_event, cancel)
    finally:
        _set_tool_cancel(None)
        try:
            _shell.set_emitter(None)
        except Exception:
            pass
        try:
            from . import safety
            safety.set_approver(None)
        except Exception:
            pass


def _run_inner(cfg, history, user_text, on_event, cancel):
    if refresh_system_prompt(cfg, history):
        try:
            on_event("notice", "workspace changed — system prompt updated")
        except Exception:
            pass

    turn_start = len(history)
    history.append({"role": "user", "content": user_text})
    last_route = None
    failures = {}
    tool_call_count = 0
    max_calls = getattr(cfg, "max_total_tool_calls", 0) or 0
    max_turns = getattr(cfg, "max_turns", 0) or 0
    warned_budget = False

    turn = 0
    while True:   # unlimited turns unless cancel or a *positive* cap says stop
        if cancel is not None and cancel.is_set():
            return "(stopped by you)"
        if max_turns and turn >= max_turns:
            return f"(stopped: reached your configured max_turns of {max_turns}. Say 'continue' to go on.)"
        turn += 1
        _trim(history, protect_from=turn_start)

        on_event("progress", "thinking…", title="Sending to model",
                 detail=f"{len(history)} message(s) in context")

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
            current_turn_tools = [m for m in history[turn_start:-1]
                                  if m.get("role") == "tool"]
            if cfg.strict and len(final) > 120 and current_turn_tools:
                try:
                    verify_msgs = history[turn_start:-1] if turn_start > 0 else history[:-1]
                    claims = llm.verify(cfg, verify_msgs, final)
                    unverified = [c for c in claims if not c.get("verified")]
                    if unverified:
                        on_event(
                            "verify",
                            "⚠ Unverified claim(s) in the answer above:\n" +
                            "\n".join(f"  • {c['claim']}" for c in unverified[:5]),
                        )
                except Exception:
                    pass
            _maybe_autosave(cfg, user_text, final)
            return final

        if entry["content"].strip():
            on_event("assistant", entry["content"])

        # Soft budget: warn once at the threshold, never stop.
        if max_calls and tool_call_count + len(calls) > max_calls and not warned_budget:
            warned_budget = True
            on_event(
                "notice",
                f"Reached your configured tool-call budget ({max_calls}). "
                f"Continuing anyway (unlimited by default). Set "
                f"AGENT_MAX_TOOL_CALLS=0 to silence this warning.",
            )
        tool_call_count += len(calls)
        if max_calls and tool_call_count % SOFT_CALL_WARNING_EVERY == 0:
            on_event("notice", f"…{tool_call_count} tool calls so far, still going.")

        results = {}

        def run_one(c):
            _set_tool_cancel(cancel)
            _shell.set_emitter(lambda kind, text, **kw: _emit(kind, text, **kw))
            fn = c["function"]
            name = fn["name"]
            on_event("tool", fn.get("arguments") or "", name=name, id=c["id"])
            if cancel is not None and cancel.is_set():
                res = "ERROR: stopped by user"
                on_event("tool_result", res, name=name, id=c["id"], ok=False)
                return c["id"], name, res

            _required = (TOOLS.get(name) or {}).get("required") or []
            try:
                _parsed = json.loads(fn.get("arguments") or "{}")
            except Exception:
                _parsed = {}
            if _required and isinstance(_parsed, dict) and not any(k in _parsed for k in _required):
                on_event("notice", f"{name} called without required args; nudging model")
                res = (
                    f"ERROR: `{name}` requires {', '.join(_required)}. "
                    f"You called it with {json.dumps(_parsed)[:120]}. "
                    f"Re-issue with all required arguments."
                )
                on_event("tool_result", res, name=name, id=c["id"], ok=False)
                return c["id"], name, res

            done = threading.Event()

            def heartbeat():
                i = 0
                while not done.wait(1.0):
                    i += 1
                    on_event("progress", f"{name} running ({i}s)…",
                             title=name, detail=f"{i}s elapsed")

            hb = threading.Thread(target=heartbeat, daemon=True)
            hb.start()
            try:
                res = call(cfg, name, fn.get("arguments"))
            finally:
                done.set()
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

        for c in calls:
            cid = c["id"]
            name, res = results.get(cid, (c["function"]["name"], "ERROR: cancelled"))
            history.append({"role": "tool", "tool_call_id": cid, "content": res})
            if res.startswith("ERROR"):
                failures[name] = failures.get(name, 0) + 1
                low = res.lower()
                if "missing required argument" in low or "could not parse arguments" in low \
                        or "rejected the arguments" in low:
                    history.append({
                        "role": "system",
                        "content": (
                            f"The last `{name}` call had a bad argument shape. "
                            "Look at the tool's schema (in the tools array) and "
                            "re-issue the call with the exact argument names and "
                            "types it expects. Do not reuse the same arguments."
                        ),
                    })
                elif failures[name] <= MAX_TOOL_RETRIES:
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
                failures.pop(name, None)

        if cancel is not None and cancel.is_set():
            for c in calls:
                if c["id"] not in results:
                    on_event("tool_result", "ERROR: cancelled",
                             name=c["function"]["name"], id=c["id"], ok=False)
            return "(stopped by you)"