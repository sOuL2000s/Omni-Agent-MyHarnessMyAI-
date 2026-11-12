# agent/commands.py
"""Slash commands to manage keys/models/workspace without leaving the harness.

The web UI has the same powers (plus a folder picker). New in 2.3:

  * `handle(cfg, line, interactive=None)` — the caller may pass an
    `interactive(question, options, default)` callback. Commands like
    `/auto`, `/modelmode`, `/strategy` use it to ask the user (on/off,
    manual/auto, round_robin/failover) instead of requiring a full
    argument. In the terminal this is wired to input(); elsewhere it's
    None and the command falls back to printing the current value.
  * `/automodel [pool]` — probe candidates and pick a main + fallback
    that actually answer (same logic as the UI's Auto button).
"""
import getpass
import os
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from . import llm
from .providers import PRESETS, STRATEGIES, detect_provider

HELP = """Keys are grouped in POOLS: gemini-1, gemini-2 ... are the pool "gemini". The model is chosen per pool.
  /providers                         overview: one line per pool (model, chain, ready/cooling/invalid keys)
  /keys [pool]                       every key with its health
  /add <name> <key> [model] [url]    add/replace one key. Presets: """ + ", ".join(PRESETS) + """
  /addkeys <pool> <key> <key> ...    add many keys at once (or  /addkeys <pool> @keys.txt)
  /key <name> [new_key]              change one key (hidden prompt if omitted)
  /model <model>                     model for the primary pool (all its keys)   |   /model <pool> <model>
  /models [pool] [filter]            list the model ids the service offers
  /fallback <pool> m1,m2 | clear     models to try when the chosen one is exhausted (manual mode)
  /strategy [round_robin|failover]   ask (or set) the rotation strategy
  /modelmode [auto|manual]           ask (or set) the model-selection mode
  /preference [m1,m2,...|clear]      ordered model preference for auto mode
  /automodel [pool]                  probe candidates, pick a working main+fallback, save them
  /use <pool|key>                    make it the primary     /order a,b,c   pool priority
  /remove <key>   /removepool <pool>
  /test [key|pool|all]               real test call(s)      /revive   clear all cooldowns
  /refresh [key|pool|all]            re-fetch the model list for a key/pool/everything
  /keymodel <key-name> <model>       set the model for ONE key (overrides the pool default)
  /autodetect <key>                  guess the provider from the key format
  /workspace [path]                  show or change the agent's workspace (relative or absolute)
  /bgauto [on|off]                   ask (or set) background-by-default for run_shell
  /auto [on|off]                     ask (or set) per-session auto-approve
  /killall                           stop every running background shell
  /memory [...]  /files  /skills [...]  /mcp [...]   (same as before)
  /config [key|key=value]            show every runtime setting, or set one (written to .env)
  /reset  /exit  /help"""


def _fmt_pool(pm, e):
    c = e["counts"]
    fb = ("  fallbacks: " + ", ".join(e["fallbacks"])) if e["fallbacks"] else ""
    chain = e.get("chain") or []
    ch = ("  auto-chain: " + " > ".join(chain[:5])) if e.get("model_mode") == "auto" and len(chain) > 1 else ""
    return (f"{e['pool']:<12} model={e['model']}{fb}{ch}\n"
            f"{'':<12} keys: {len(e['keys'])} total = {c['ready']} ready, {c['cooling']} cooling, {c['invalid']} invalid   {e['base_url']}")


def _fmt_config(cfg):
    rows = [
        ("AGENT_BASE_URL", cfg.base_url),
        ("AGENT_MODEL", cfg.model),
        ("AGENT_WORKSPACE", str(cfg.workspace)),
        ("AGENT_ALLOW_OUTSIDE", "1" if cfg.allow_outside else "0"),
        ("AGENT_AUTO_APPROVE", "1" if cfg.auto_approve else "0"),
        ("AGENT_STRICT", "1" if cfg.strict else "0"),
        ("AGENT_SHELL_BG", "1" if cfg.shell_bg else "0"),
        ("AGENT_MODEL_MODE", cfg.providers.model_mode if cfg.providers else "?"),
        ("AGENT_MODEL_PREFERENCE", ",".join(cfg.providers.settings.get("model_preference") or []) or "(none)"),
        ("AGENT_MAX_TURNS", str(cfg.max_turns) if cfg.max_turns else "0 (unlimited)"),
        ("AGENT_MAX_TOOL_CALLS", str(getattr(cfg, "max_total_tool_calls", 0)) if getattr(cfg, "max_total_tool_calls", 0) else "0 (unlimited)"),
        ("AGENT_TEMPERATURE", str(cfg.temperature)),
        ("AGENT_TIMEOUT", str(cfg.timeout)),
        ("AGENT_STRATEGY", cfg.providers.strategy if cfg.providers else "?"),
        ("AGENT_MAX_WAIT", str(cfg.providers.max_wait) if cfg.providers else "?"),
        ("AGENT_API_KEY", "(hidden)" if cfg.api_key else "(empty)"),
    ]
    return "\n".join(f"  {k:<24} = {v}" for k, v in rows)


def _write_env_var(key, value):
    """Write or update KEY=value in .env, preserving comments and order."""
    from .config import ROOT
    env_path = ROOT / ".env"
    line = f"{key}={value}\n"
    if not env_path.exists():
        env_path.write_text(line, encoding="utf-8")
        return env_path
    text = env_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    replaced = False
    out = []
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            indent = ln[:len(ln) - len(stripped)]
            out.append(f"{indent}{key}={value}\n")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(line)
    env_path.write_text("".join(out), encoding="utf-8")
    return env_path


def _ask(interactive, question, options, default=None):
    """Ask via the interactive callback, or return None if unavailable."""
    if interactive is None:
        return None
    try:
        return interactive(question, options, default)
    except Exception:
        return None


def handle(cfg, line, interactive=None):
    pm = cfg.providers
    try:
        a = shlex.split(line)
    except ValueError as e:
        return f"parse error: {e}"
    if not a:
        return ""
    c, args = a[0].lstrip("/"), a[1:]
    try:
        if c in ("providers", "list", "ls", "pools", "status"):
            s = pm.summary()
            if not s:
                return "No keys yet. Try:  /add openrouter YOUR_KEY   or   /addkeys gemini KEY1 KEY2 ..."
            rows = [("* " if i == 0 else "  ") + _fmt_pool(pm, e) for i, e in enumerate(s)]
            return ("\n".join(rows)
                    + f"\n(* = primary pool; strategy={pm.strategy}; model_mode={pm.model_mode}; "
                      f"next pools are used when it is exhausted)")
        if c == "keys":
            pool = pm.resolve_pool(args[0]) if args else None
            rows = []
            for e in pm.summary():
                if pool and e["pool"] != pool:
                    continue
                for k in e["keys"]:
                    rows.append(f"  {k['name']:<14} {k['masked']:<12} {k['status']:<8} ok={k['ok']} fail={k['fail']}"
                                + (f"  ({k['remaining']}s) {k['reason'][:70]}" if k["status"] != "ready" else ""))
            return "\n".join(rows) or "no keys"
        if c == "add":
            if not args:
                return "usage: /add [name] <key> [model] [base_url]   (name is optional; auto-detected)"
            if len(args) == 1:
                key = args[0]
                det = detect_provider(key) or "custom"
                args = [det, key]
            pm.add(args[0], args[1],
                   args[2] if len(args) > 2 else None,
                   args[3] if len(args) > 3 else None)
            p = pm.get(args[0])
            return (f"added '{p.name}' to pool '{p.pool}'. "
                    f"Model list is fetching in the background; "
                    f"/test {p.name} to verify.")
        if c in ("addkeys", "keysadd"):
            if len(args) < 2:
                return "usage: /addkeys <pool> <key> <key> ...   or   /addkeys <pool> @keys.txt"
            keys = []
            for x in args[1:]:
                if x.startswith("@"):
                    keys += Path(x[1:]).read_text(encoding="utf-8-sig").splitlines()
                else:
                    keys.append(x)
            added, dup = pm.add_keys(args[0], keys)
            return f"added {len(added)} key(s) to pool '{args[0]}'" + (f", skipped {dup} duplicate(s)" if dup else "")
        if c == "key":
            if not args:
                return "usage: /key <name> [new_key]"
            pm.get(args[0])
            pm.set_key(args[0], args[1] if len(args) > 1 else getpass.getpass(f"New key for {args[0]}: "))
            return f"key updated for {args[0]}"
        if c == "model":
            if not args:
                return "\n".join(f"{e['pool']}: {e['model']}" for e in pm.summary()) or "no keys"
            if len(args) == 1:
                pool = pm.pool_names()[0]
                model = args[0]
            else:
                pool, model = pm.resolve_pool(args[0]), args[-1]
            pm.set_model(pool, model)
            n = len(pm.providers_in(pool))
            ok = llm.model_listed(cfg, pool, model)
            warn = "" if ok in (True, None) else f"\n  warning: '{model}' is not in {pool}'s model list - see /models {pool}"
            return f"pool '{pool}' ({n} keys) now uses model {model}{warn}"
        if c == "models":
            pool = pm.resolve_pool(args[0]) if args else pm.pool_names()[0]
            flt = args[1].lower() if len(args) > 1 else ""
            ids = [m for m in llm.list_models(cfg, pool, force=True) if flt in m.lower()]
            return f"{len(ids)} model(s) for {pool} (current: {pm.model_of(pool)}):\n  " + "\n  ".join(ids[:80])
        if c == "fallback":
            if len(args) < 2:
                return "usage: /fallback <pool> model1,model2   |   /fallback <pool> clear"
            pool = pm.resolve_pool(args[0])
            models = [] if args[1] == "clear" else " ".join(args[1:]).replace(",", " ").split()
            kept, dropped = pm.set_fallbacks(pool, models)
            msg = f"{pool}: chosen model {pm.model_of(pool)}, fallbacks {kept or 'none'}"
            if dropped:
                msg += f"\n  ignored {len(dropped)} entr(ies) equal to the main model: {', '.join(dropped)}"
            return msg
        if c == "strategy":
            if not args:
                cur = pm.strategy
                ans = _ask(interactive, "Rotation strategy?",
                           ["round_robin", "failover"], cur)
                if ans is None:
                    return f"strategy = {cur}   (options: {', '.join(STRATEGIES)}; use /strategy round_robin|failover)"
                args = [ans]
            pm.set_strategy(args[0])
            return f"strategy = {pm.strategy}"
        if c == "modelmode":
            if not args:
                cur = pm.model_mode
                ans = _ask(interactive, "Model-selection mode?",
                           ["manual", "auto"], cur)
                if ans is None:
                    return (f"model_mode = {cur}\n"
                            f"  auto = rank the discovered model list per request and walk it\n"
                            f"  manual = pool default + explicit /fallback list")
                args = [ans]
            pm.set_model_mode(args[0])
            cfg.model_mode = pm.model_mode
            cfg.providers.settings["model_mode"] = pm.model_mode
            try:
                _write_env_var("AGENT_MODEL_MODE", pm.model_mode)
            except Exception:
                pass
            return f"model_mode = {pm.model_mode} (saved to .env)"
        if c == "preference":
            if not args:
                pref = pm.settings.get("model_preference") or []
                return "preference: " + (", ".join(pref) if pref else "(none)")
            if args[0].lower() == "clear":
                pm.set_model_preference([])
                try:
                    _write_env_var("AGENT_MODEL_PREFERENCE", "")
                except Exception:
                    pass
                return "preference cleared"
            models = " ".join(args).replace(",", " ").split()
            kept = pm.set_model_preference(models)
            try:
                _write_env_var("AGENT_MODEL_PREFERENCE", ",".join(kept))
            except Exception:
                pass
            return "preference: " + ", ".join(kept)
        if c in ("automodel", "auto-model"):
            pool = pm.resolve_pool(args[0]) if args else pm.pool_names()[0]
            try:
                main, fb = pm.pick_auto_models(pool, cfg=cfg)
            except Exception as e:
                return f"error while probing: {e}"
            if not main:
                return (f"could not find a model that answers on '{pool}'. "
                        f"Add keys, or pick a model manually with /model {pool} <id>.")
            pm.set_model(pool, main)
            if fb:
                pm.set_fallbacks(pool, [fb])
            else:
                pm.set_fallbacks(pool, [])
            return (f"{pool}: auto-picked main={main}"
                    + (f", fallback={fb}" if fb else " (no distinct fallback found)")
                    + "  — both were probed with a tiny real call.")
        if c == "use":
            pm.use(args[0])
            return f"{args[0]} is now primary"
        if c == "order":
            pm.order_pools(" ".join(args).replace(",", " ").split())
            return "order: " + " > ".join(pm.pool_names())
        if c in ("remove", "rm", "delete"):
            pm.remove(args[0])
            return f"removed {args[0]}"
        if c == "removepool":
            n = len(pm.providers_in(args[0]))
            pm.remove_pool(args[0])
            return f"removed pool {args[0]} ({n} keys)"
        if c in ("revive", "clear"):
            pm.health.clear()
            return "all cooldowns cleared"
        if c in ("killall", "kill-all"):
            from .tools import shell as _sh
            jobs = [j for j in _sh.list_detached() if j.get("alive")]
            if not jobs:
                return "no running background shells"
            for j in jobs:
                _sh.kill_detached(j["id"])
            return f"stopped {len(jobs)} background shell(s)"
        if c == "test":
            return test(cfg, args[0] if args else None)
        if c == "help":
            return HELP
        if c in ("refresh", "refetch"):
            target = args[0] if args else "all"
            if target == "all":
                out = pm.refresh_all_models()
                lines = []
                for k, v in out.items():
                    lines.append(f"{k}: {len(v)} models" if isinstance(v, list) else f"{k}: {v}")
                return "\n".join(lines) or "no keys"
            if target in pm.pool_names():
                names = [p.name for p in pm.providers_in(target)]
            else:
                names = [target]
            lines = []
            for n in names:
                try:
                    ids = pm.refresh_models(n)
                    lines.append(f"{n}: {len(ids)} models")
                except Exception as e:
                    lines.append(f"{n}: ERROR {e}")
            return "\n".join(lines)
        if c == "autodetect":
            if not args:
                return "usage: /autodetect <key>   — guesses the provider from the key format"
            name = detect_provider(args[0])
            return f"detected: {name}" if name else "could not detect (unknown key format)"
        if c == "keymodel":
            if len(args) < 2:
                return "usage: /keymodel <key-name> <model>   — set the model for one key only"
            pm.set_model_for_key(args[0], " ".join(args[1:]))
            return f"{args[0]} now uses {pm.get(args[0]).model}"

        # ---- workspace -----------------------------------------------------
        if c in ("workspace", "ws", "cd", "cwd"):
            if not args:
                return f"workspace: {cfg.workspace}"
            raw = " ".join(args).strip().strip('"').strip("'")
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = (cfg.workspace / p)
            try:
                p = p.resolve()
            except OSError:
                return f"error: cannot resolve {raw}"
            if not p.is_dir():
                return f"error: not a directory: {p}"
            if p == p.parent:
                return f"error: refusing a filesystem root ({p}) as the workspace"
            cfg.workspace = p
            return f"workspace -> {p}\n(the next turn will use this path; the web UI is updated too)"

        # ---- background-shell default -------------------------------------
        if c in ("bgauto", "bg-shell", "bgshell", "shell-bg"):
            from .tools import shell as _shell
            if not args:
                cur = _shell.default_background()
                ans = _ask(interactive, "Run every run_shell in the background by default?",
                           ["on", "off"], "on" if cur else "off")
                if ans is None:
                    return f"background shell executions = {cur}  (use /bgauto on|off)"
                args = [ans]
            on = args[0].lower() in ("on", "1", "true", "yes")
            _shell.set_default_background(on)
            cfg.shell_bg = on
            try:
                _write_env_var("AGENT_SHELL_BG", "1" if on else "0")
            except Exception:
                pass
            return f"background shell executions = {on} (saved to .env)"

        # ---- per-session auto-approve (terminal) --------------------------
        if c == "auto":
            if not args:
                cur = cfg.auto_approve
                ans = _ask(interactive, "Skip all Allow/Deny prompts (auto-approve)?",
                           ["on", "off"], "on" if cur else "off")
                if ans is None:
                    return f"auto-approve = {cur}  (this process; use /auto on|off or /config AGENT_AUTO_APPROVE=...)"
                args = [ans]
            on = args[0].lower() in ("on", "1", "true", "yes")
            cfg.auto_approve = on
            return f"auto-approve = {on}  (this process; use /config AGENT_AUTO_APPROVE=... to persist)"

        # ---- memory -------------------------------------------------------
        if c == "memory":
            return _memory_cmd(cfg, args)

        # ---- files --------------------------------------------------------
        if c == "files":
            return _files_cmd(cfg, args)

        # ---- skills -------------------------------------------------------
        if c == "skills":
            return _skills_cmd(cfg, args)

        # ---- MCP ----------------------------------------------------------
        if c == "mcp":
            return _mcp_cmd(cfg, args)

        # ---- config -------------------------------------------------------
        if c == "config":
            if not args:
                return _fmt_config(cfg)
            if "=" in args[0]:
                key, _, value = args[0].partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
            else:
                key = args[0].strip()
                if len(args) < 2:
                    return f"usage: /config            (show all)\n       /config KEY=value  (set and persist)"
                value = " ".join(args[1:]).strip().strip('"').strip("'")
            if not key.startswith("AGENT_") and not key.startswith("OMNI_"):
                return f"error: {key} is not an AGENT_/OMNI_ setting"
            try:
                _write_env_var(key, value)
            except Exception as e:
                return f"error: could not write .env: {e}"
            applied = ""
            try:
                if key == "AGENT_AUTO_APPROVE":
                    cfg.auto_approve = value.lower() not in ("0", "false", "no", "off", "n", "")
                    applied = f" (auto_approve = {cfg.auto_approve})"
                elif key == "AGENT_STRICT":
                    cfg.strict = value.lower() not in ("0", "false", "no", "off", "n", "")
                    applied = f" (strict = {cfg.strict})"
                elif key == "AGENT_SHELL_BG":
                    from .tools import shell as _shell
                    on = value.lower() not in ("0", "false", "no", "off", "n", "")
                    _shell.set_default_background(on)
                    cfg.shell_bg = on
                    applied = f" (shell_bg = {on})"
                elif key == "AGENT_MODEL_MODE":
                    pm.set_model_mode(value)
                    cfg.model_mode = pm.model_mode
                    applied = f" (model_mode = {pm.model_mode})"
                elif key == "AGENT_MODEL_PREFERENCE":
                    kept = pm.set_model_preference(value.replace(",", " ").split())
                    cfg.model_preference = kept
                    applied = f" (preference = {', '.join(kept) or '(none)'})"
                elif key == "AGENT_MAX_TURNS":
                    cfg.max_turns = int(value)
                    applied = f" (max_turns = {cfg.max_turns or 'unlimited'})"
                elif key == "AGENT_MAX_TOOL_CALLS":
                    cfg.max_total_tool_calls = int(value)
                    applied = f" (max_total_tool_calls = {cfg.max_total_tool_calls or 'unlimited'})"
                elif key == "AGENT_TEMPERATURE":
                    cfg.temperature = float(value)
                    applied = f" (temperature = {cfg.temperature})"
                elif key == "AGENT_TIMEOUT":
                    cfg.timeout = int(value)
                    applied = f" (timeout = {cfg.timeout})"
                elif key == "AGENT_WORKSPACE":
                    p = Path(value).expanduser().resolve()
                    if not p.is_dir():
                        return f"error: not a directory: {p}"
                    if p == p.parent:
                        return f"error: refusing a filesystem root ({p}) as the workspace"
                    cfg.workspace = p
                    applied = f" (workspace = {p})"
            except Exception as e:
                return f"wrote .env, but could not apply live: {e}"
            return f"saved {key}={value} to .env{applied}"

        return "unknown command. /help"
    except (ValueError, IndexError, OSError, llm.LLMError) as e:
        return f"error: {e}"


# ---------------------------------------------------------------- sub-commands

def _memory_cmd(cfg, args):
    from .tools import memory as _mem
    sub = (args[0].lower() if args else "list")
    if sub in ("list", "ls"):
        with _mem._get_db(cfg) as conn:
            mems = conn.execute(
                "SELECT id, timestamp, content FROM memories ORDER BY id DESC LIMIT 200"
            ).fetchall()
            facts = conn.execute(
                "SELECT key, value FROM facts ORDER BY key ASC"
            ).fetchall()
        lines = []
        if mems:
            lines.append(f"Memories ({len(mems)}):")
            for r in mems:
                lines.append(f"  [{r['id']}] {r['timestamp']}  {r['content'][:120]}")
        else:
            lines.append("No memories stored.")
        if facts:
            lines.append(f"Facts ({len(facts)}):")
            for r in facts:
                lines.append(f"  {r['key']}: {r['value']}")
        return "\n".join(lines)
    if sub == "delete" and len(args) >= 2:
        q = " ".join(args[1:]).strip()
        return _mem.memory_delete(cfg, q)
    if sub == "facts":
        return _mem.memory_list_facts(cfg)
    if sub == "deletefact" and len(args) >= 2:
        return _mem.memory_delete_fact(cfg, " ".join(args[1:]).strip())
    if sub in ("clear", "clearfacts"):
        if sub == "clearfacts":
            return _mem.memory_clear_all(cfg)  # clears facts + events
        return _mem.memory_clear_all(cfg)
    return ("usage: /memory list | delete <id|text> | facts | deletefact <key> | clear\n"
            "  Tip: /memory list shows numeric ids; /memory delete <id> removes one entry.")


def _files_cmd(cfg, args):
    ws = cfg.workspace
    out = []
    try:
        for p in sorted(ws.rglob("*")):
            rel = p.relative_to(ws)
            if p.is_file() and not any(x.startswith(".") or x in ("__pycache__", "node_modules")
                                       for x in rel.parts):
                out.append(f"  {rel.as_posix()}  ({p.stat().st_size} B)")
            if len(out) >= 500:
                out.append("  …(truncated at 500 files)")
                break
    except Exception as e:
        return f"error: {e}"
    return "\n".join(out) or "(workspace is empty)"


def _skills_cmd(cfg, args):
    from .tools import skills as _sk
    sub = (args[0].lower() if args else "list")
    if sub in ("list", "ls"):
        rows = _sk._list_files(cfg)
        if not rows:
            return "No skills saved."
        return "\n".join(f"  {r['name']} — {r['title']}" + (f": {r['summary']}" if r['summary'] else "")
                         for r in rows)
    if sub == "read" and len(args) >= 2:
        return _sk.skill_read(cfg, args[1])
    if sub == "delete" and len(args) >= 2:
        return _sk.skill_delete(cfg, args[1])
    if sub == "save":
        return "Use the web UI's Skills tab, or the model can call skill_save. (Body text is long for the CLI.)"
    return "usage: /skills list | read <name> | delete <name>"


def _mcp_cmd(cfg, args):
    from . import mcp_admin
    reg = getattr(cfg, "mcp", None)
    sub = (args[0].lower() if args else "list")
    if sub in ("list", "ls"):
        if not reg or not reg.servers:
            return "No MCP servers configured."
        lines = []
        for name, srv in reg.servers.items():
            status = "running" if srv.ready else "stopped"
            lines.append(f"  {name}: {status}, {len(srv.tools or [])} tool(s)"
                         + (f" — {srv.last_error}" if srv.last_error else ""))
        return "\n".join(lines)
    if sub == "start" and len(args) >= 2:
        try:
            r = mcp_admin.start_server(cfg, reg, args[1])
            return f"{r['name']}: {r['tools']} tools"
        except Exception as e:
            return f"error: {e}"
    if sub == "stop" and len(args) >= 2:
        mcp_admin.stop_server(cfg, reg, args[1])
        return f"stopped {args[1]}"
    if sub == "restart" and len(args) >= 2:
        try:
            mcp_admin.stop_server(cfg, reg, args[1])
            r = mcp_admin.start_server(cfg, reg, args[1])
            return f"{r['name']}: {r['tools']} tools"
        except Exception as e:
            return f"error: {e}"
    if sub == "remove" and len(args) >= 2:
        try:
            mcp_admin.delete_server(cfg, reg, args[1])
            return f"removed {args[1]}"
        except Exception as e:
            return f"error: {e}"
    if sub == "add":
        return ("Use the web UI's MCP tab (or the model can call admin_add_mcp_server). "
                "Adding a server needs command/args/url/env/headers, which is awkward in one line.")
    return "usage: /mcp list | start <name> | stop <name> | restart <name> | remove <name>"


def test(cfg, target=None):
    """Real ping(s). target: key name, pool name, 'all' or None."""
    pm = cfg.providers
    if not pm.items:
        return "no keys"
    if target == "all":
        targets = list(pm.items)
        parallel = True
    elif target and target in pm.pool_names():
        targets = pm.providers_in(target)
        parallel = True
    elif target:
        targets = [pm.get(target)]
        parallel = False
    else:
        targets = pm.providers_in(pm.pool_names()[0])[:1]
        parallel = False

    if not parallel or len(targets) <= 2:
        out = []
        for p in targets:
            ok, kind, text = llm.probe(cfg, p)
            out.append(f"{p.name} OK ({p.model}): {text!r}" if ok
                       else f"{p.name} FAILED [{kind}]: {text[:200]}")
        return "\n".join(out)

    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(llm.probe, cfg, p): p for p in targets}
        for f in as_completed(futs):
            p = futs[f]
            try:
                results[p.name] = f.result()
            except Exception as e:
                results[p.name] = (False, "error", str(e))
    out, bad = [], 0
    for p in targets:
        ok, kind, text = results.get(p.name, (False, "error", "no result"))
        if ok:
            out.append(f"{p.name} OK ({p.model}): {text!r}")
        else:
            bad += 1
            out.append(f"{p.name} FAILED [{kind}]: {text[:200]}")
    out = [o for o in out if "FAILED" in o] + [f"{len(targets) - bad}/{len(targets)} keys OK"]
    return "\n".join(out)