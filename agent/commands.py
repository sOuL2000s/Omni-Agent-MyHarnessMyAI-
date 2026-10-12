"""Slash commands to manage keys/models without leaving the harness (terminal; the web UI has the same powers)."""
import getpass
import shlex
from pathlib import Path
from . import llm
from .providers import PRESETS, STRATEGIES

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
            if len(args) < 2:
                return "usage: /add <name> <key> [model] [base_url]"
            pm.add(args[0], args[1], args[2] if len(args) > 2 else None, args[3] if len(args) > 3 else None)
            return f"added '{args[0]}' to pool '{pm.get(args[0]).pool}'. /test {args[0]} to check it."
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
    if len(targets) > 6 or target == "all" or (target and target in pm.pool_names() and len(targets) > 1):
        out = [o for o in out if "FAILED" in o] + [f"{len(targets) - bad}/{len(targets)} keys OK"]
    return "\n".join(out)
