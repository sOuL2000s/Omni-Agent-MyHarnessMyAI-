"""Slash commands to manage providers/keys/models without leaving the harness."""
import getpass
import shlex
from . import llm
from .providers import PRESETS

HELP = """Commands:
  /providers                         list providers (first = primary, rest = automatic fallbacks)
  /add <name> <key> [model] [url]    add/replace a provider. Presets: """ + ", ".join(PRESETS) + """
                                     e.g.  /add openrouter sk-or-...     /add gemini AIza...
  /key <name> [new_key]              change a key (prompts hidden if key omitted)
  /model <model>                     change the primary's model   |   /model <name> <model>
  /use <name>                        make a provider the primary
  /order a,b,c                       set failover priority
  /remove <name>                     delete a provider
  /test [name]                       send a ping to check a provider
  /reset  /exit  /help"""


def handle(cfg, line):
    pm = cfg.providers
    try:
        a = shlex.split(line)
    except ValueError as e:
        return f"parse error: {e}"
    c, args = a[0].lstrip("/"), a[1:]
    try:
        if c in ("providers", "list", "ls"):
            if not pm.items:
                return "No providers yet. Try:  /add openrouter YOUR_KEY"
            rows = [f"{'*' if i == 0 else ' '} {i + 1}. {p.name:<12} {p.model:<34} key={p.masked():<12} {pm.status(p)}\n       {p.base_url}"
                    for i, p in enumerate(pm.items)]
            return "\n".join(rows) + "\n(* = primary; others are tried in order if it fails)"
        if c == "add":
            if len(args) < 2:
                return "usage: /add <name> <key> [model] [base_url]"
            pm.add(args[0], args[1], args[2] if len(args) > 2 else None, args[3] if len(args) > 3 else None)
            return f"added '{args[0]}'. Use /use {args[0]} to make it primary, /test {args[0]} to check it."
        if c == "key":
            if not args:
                return "usage: /key <name> [new_key]"
            p = pm.get(args[0])
            p.api_key = args[1] if len(args) > 1 else getpass.getpass(f"New key for {p.name}: ").strip()
            pm.save()
            pm.cooldown.pop(p.name, None)
            return f"key updated for {p.name}"
        if c == "model":
            if not args:
                return f"current: {pm.items[0].name} -> {pm.items[0].model}" if pm.items else "no providers"
            p = pm.get(args[0]) if len(args) > 1 else pm.items[0]
            p.model = args[-1]
            pm.save()
            return f"{p.name} now uses model {p.model}"
        if c == "use":
            pm.use(args[0])
            return f"{args[0]} is now primary"
        if c == "order":
            pm.order_by([x for x in " ".join(args).replace(",", " ").split()])
            return "order: " + " > ".join(p.name for p in pm.items)
        if c in ("remove", "rm", "delete"):
            pm.remove(args[0])
            return f"removed {args[0]}"
        if c == "test":
            p = pm.get(args[0]) if args else (pm.items[0] if pm.items else None)
            if not p:
                return "no providers"
            try:
                return f"{p.name} OK: {llm.ping(p, cfg).get('content')!r}"
            except Exception as e:
                return f"{p.name} FAILED: {e}"
        if c == "help":
            return HELP
        return "unknown command. /help"
    except (ValueError, IndexError) as e:
        return f"error: {e}"
