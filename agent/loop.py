# agent/loop.py
"""Tool loop with parallel read-only tool calls, self-healing retries and rich events."""
import threading
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import llm
from .tools import schemas, call, TOOLS
from .tools.memory import recent
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
- Use memory_save for durable user preferences or facts worth keeping across sessions.
- Ask the user only when truly blocked.
- You can change Omni's own configuration when the user asks: admin_add_key,
  admin_set_pool_model, admin_add_mcp_server, admin_set_setting, etc. These
  always ask the user for approval before writing. Use them instead of telling
  the user to open the Keys panel.
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
    now = datetime.datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")
    return SYSTEM.format(ws=cfg.workspace,
                         datetime=now,
                         memory=("\nKnown about the user:\n" + mem) if mem else "")


def new_history(cfg):
    return [{"role": "system", "content": system_prompt(cfg)}]


def refresh_system_prompt(cfg, history):
    """Rewrite history[0] if the workspace (or memory) changed since it was
    created. Cheap and idempotent — safe to call at the top of every turn.
    Returns True if it changed anything."""
    if not history or history[0].get("role") != "system":
        return False
    new_sp = system_prompt(cfg)
    if history[0].get("content") == new_sp:
        return False
    history[0]["content"] = new_sp
    return True


def _trim(history, limit=180_000, protect_from=None):
    """Drop the oldest tool outputs when the transcript gets too large.

    `protect_from` is the history index where the CURRENT turn begins; we never
    trim messages at or after that index (the model may still be reasoning
    about them).

    The size accounting includes `tool_calls`, not just `content`. A
    `write_file` call with a 50 KB `content` argument lives in the
    assistant's `tool_calls`, not in any `content` field — the old
    accounting missed it entirely, so a transcript that was 500 KB of
    tool-call payloads reported `total ≈ 0` and was never trimmed.
    """
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
        # Also drop any per-thread approver left over from a web run; the CLI
        # path will re-install its own on the next call.
        try:
            from . import safety
            safety.set_approver(None)
        except Exception:
            pass


def _run_inner(cfg, history, user_text, on_event, cancel):
    # If the workspace (or saved memory) changed since this session's system
    # prompt was built, rewrite it in place so the model isn't told to use a
    # stale path.
    if refresh_system_prompt(cfg, history):
        try:
            on_event("notice", "workspace changed — system prompt updated")
        except Exception:
            pass

    turn_start = len(history)  # protect this turn's tool outputs from trimming
    history.append({"role": "user", "content": user_text})
    last_route = None
    failures = {}
    # Budget counter for the whole task. A cheap/fast model can otherwise
    # burn hundreds of calls in one user turn — the heartbeat and the
    # "diagnose this error" hint don't help if the model just tries
    # different broken calls.
    tool_call_count = 0
    max_calls = getattr(cfg, "max_total_tool_calls", 200) or 200

    for turn in range(cfg.max_turns):
        if cancel is not None and cancel.is_set():
            return "(stopped by you)"
        _trim(history, protect_from=turn_start)

        # Emit a progress event *before* the LLM call so the UI has something
        # to show during the 1-3s it takes to reach the first token.
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
            # Strict mode is scoped to the CURRENT turn: only run the
            # verifier if *this* turn used a tool. A user turn that
            # answers a knowledge question shouldn't pay for a verify
            # call just because turn 3 of 40 used a tool.
            current_turn_tools = [m for m in history[turn_start:-1]
                                  if m.get("role") == "tool"]
            if cfg.strict and len(final) > 120 and current_turn_tools:
                try:
                    # Pass only the current turn's slice so the verifier
                    # isn't re-reading 40 turns of context.
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
            return final

        if entry["content"].strip():
            on_event("assistant", entry["content"])

        # ---- budget check (before running the calls) ------------------
        if tool_call_count + len(calls) > max_calls:
            on_event(
                "error",
                f"Reached the tool-call budget ({max_calls}). "
                f"Returning what I have; ask 'continue' to resume.",
            )
            return f"(stopped: exceeded tool-call budget of {max_calls})"
        tool_call_count += len(calls)

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
                on_event("tool_result", res, name=name, id=c["id"], ok=False)
                return c["id"], name, res

            # Heartbeat so slow tools don't look frozen in the UI.
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
            # Emit a synthetic tool_result for every call that never resolved
            # so the live UI doesn't leave amber "running" cards behind after
            # the user pressed Stop.
            for c in calls:
                if c["id"] not in results:
                    on_event("tool_result", "ERROR: cancelled",
                             name=c["function"]["name"], id=c["id"], ok=False)
            return "(stopped by you)"

    return "(stopped: reached max turns. Say 'continue' to go on.)"