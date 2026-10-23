# agent/commands.py
"""Slash commands to manage keys/models/workspace without leaving the harness.

The web UI has the same powers (plus a folder picker).
"""
import getpass
import shlex
from pathlib import Path
from . import llm
from .providers import PRESETS, STRATEGIES, detect_provider

HELP = """Keys are grouped in POOLS: gemini-1, gemini-2 ... are the pool "gemini". The model is chosen per pool.
  /providers                         overview: one line per pool (model, fallbacks, ready/cooling/invalid keys)
  /keys [pool]                       every key with its health
  /add <name> <key> [model] [url]    add/replace one key. Presets: """ + ", ".join(PRESETS) + """
  /addkeys <pool> <key> <key> ...    add many keys at once (or  /addkeys <pool> @keys.txt  = one key per line)
  /key <name> [new_key]              change one key (hidden prompt if omitted)
  /model <model>                     model for the primary pool (all its keys)   |   /model <pool> <model>
  /models [pool] [filter]            list the model ids the service offers
  /fallback <pool> m1,m2 | clear     models to try when the chosen one is exhausted (quota is per model!)
  /strategy round_robin|failover     round_robin spreads calls over the keys; failover uses them in order
  /use <pool|key>                    make it the primary     /order a,b,c   pool priority
  /remove <key>   /removepool <pool>
  /test [key|pool|all]               real test call(s)      /revive   clear all cooldowns
  /refresh [key|pool|all]            re-fetch the model list for a key/pool/everything
  /keymodel <key-name> <model>       set the model for ONE key (overrides the pool default)
  /autodetect <key>                  guess the provider from the key format
  /workspace [path]                  show or change the agent's workspace (relative or absolute)
  /bgauto on|off                     when on, every run_shell call runs in the background
  /reset  /exit  /help"""


def _fmt_pool(pm, e):
    c = e["counts"]
    fb = ("  fallbacks: " + ", ".join(e["fallbacks"])) if e["fallbacks"] else ""
    return (f"{e['pool']:<12} model={e['model']}{fb}\n"
            f"{'':<12} keys: {len(e['keys'])} total = {c['ready']} ready, {c['cooling']} cooling, {c['invalid']} invalid   {e['base_url']}")


def handle(cfg, line):
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
            return "\n".join(rows) + f"\n(* = primary pool; strategy={pm.strategy}; next pools are used when it is exhausted)"
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
                    f"{len(p.models)} models cached, using {p.model}. "
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
            pm.set_fallbacks(pool, [] if args[1] == "clear" else " ".join(args[1:]).replace(",", " ").split())
            return f"{pool}: chosen model {pm.model_of(pool)}, fallbacks {pm.fallbacks(pool) or 'none'}"
        if c == "strategy":
            if not args:
                return f"strategy = {pm.strategy}   (options: {', '.join(STRATEGIES)})"
            pm.set_strategy(args[0])
            return f"strategy = {pm.strategy}"
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
            # Same guard as config.load(): refuse a filesystem root, because
            # that would silently disable the outside-workspace approval.
            if p == p.parent:
                return f"error: refusing a filesystem root ({p}) as the workspace"
            cfg.workspace = p
            # Rewrite the system prompt on the caller's history if any.
            try:
                from . import loop as _loop
                # If the caller has a live history, refresh it. We don't have
                # it here, but the loop refreshes on the next turn anyway.
                _ = _loop
            except Exception:
                pass
            return f"workspace -> {p}\n(the next turn will use this path; the web UI is updated too)"

        # ---- background-shell default -------------------------------------
        if c in ("bgauto", "bg-shell", "bgshell", "shell-bg"):
            from .tools import shell as _shell
            if not args:
                return f"background shell executions = {_shell.default_background()}"
            on = args[0].lower() in ("on", "1", "true", "yes")
            _shell.set_default_background(on)
            return f"background shell executions = {on}"

        return "unknown command. /help"
    except (ValueError, IndexError, OSError, llm.LLMError) as e:
        return f"error: {e}"


def test(cfg, target=None):
    """Real ping(s). target: key name, pool name, 'all' or None (one key of the primary pool)."""
    pm = cfg.providers
    if not pm.items:
        return "no keys"
    if target == "all":
        targets = list(pm.items)
    elif target and target in pm.pool_names():
        targets = pm.providers_in(target)[:1]
    elif target:
        targets = [pm.get(target)]
    else:
        targets = pm.providers_in(pm.pool_names()[0])[:1]
    out, bad = [], 0
    for p in targets:
        ok, kind, text = llm.probe(cfg, p)
        if ok:
            out.append(f"{p.name} OK ({p.model}): {text!r}")
        else:
            bad += 1
            out.append(f"{p.name} FAILED [{kind}]: {text[:200]}")
    if len(targets) > 6 or target == "all":
        out = [o for o in out if "FAILED" in o] + [f"{len(targets) - bad}/{len(targets)} keys OK"]
    return "\n".join(out)