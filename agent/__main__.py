import argparse
import sys
import time
from . import config, loop, llm, commands


def chat_repl(cfg):
    pm = cfg.providers
    pools = pm.pool_names()
    print(f"Omni agent  |  primary pool={pools[0] if pools else 'none'}  |  keys={len(pm.items)}  |  strategy={pm.strategy}\n"
          f"workspace={cfg.workspace}\n"
          "Type /help for key/model commands (key rotation + failover is automatic).  Prefer a browser?  run:  omni ui\n")
    if pm.load_error:
        print("WARNING:", pm.load_error, "\n")
    hist = loop.new_history(cfg)
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not q:
            continue
        if q in ("/exit", "/quit"):
            return
        if q == "/reset":
            hist = loop.new_history(cfg)
            print("history cleared")
            continue
        if q.startswith("/"):
            print(commands.handle(cfg, q))
            continue
        try:
            print("\n" + loop.run(cfg, hist, q) + "\n")
        except llm.LLMError as e:
            print(f"[gateway error] {e}")
        except KeyboardInterrupt:
            print("\n(interrupted)")


def main():
    ap = argparse.ArgumentParser(prog="omni-agent")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="interactive terminal session (default when no GUI)")
    u = sub.add_parser("ui", help="open the web UI (chat + keys + models)")
    u.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 unless you know why not")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true")
    r = sub.add_parser("run", help="one-shot task, then exit")
    r.add_argument("prompt", nargs="?")
    r.add_argument("-f", "--file", help="read the task from a file (e.g. workflows/daily.md)")
    s = sub.add_parser("schedule", help="repeat a task on an interval")
    s.add_argument("-f", "--file", required=True)
    s.add_argument("--every", type=int, required=True, help="seconds between runs")
    c = sub.add_parser("check", help="test the primary pool (--all = every key)")
    c.add_argument("--all", action="store_true")
    m = sub.add_parser("models", help="list models a pool offers: omni models [pool] [filter]")
    m.add_argument("rest", nargs="*")
    pv = sub.add_parser("provider", help="manage keys: list | add | addkeys | model | models | fallback | strategy | use | order | remove | test | revive")
    pv.add_argument("rest", nargs="*")
    # Launcher controls (only meaningful when no subcommand is given)
    ap.add_argument("--terminal", action="store_true",
                    help="skip the launcher and go straight to the terminal chat")
    ap.add_argument("--no-gui", action="store_true",
                    help="same as --terminal (alias, for scripts)")
    a = ap.parse_args()
    cfg = config.load()

    if cfg.providers.load_error:
        print("WARNING:", cfg.providers.load_error, file=sys.stderr)

    # No subcommand → show the launcher (or fall back to the terminal if asked / if tkinter is missing)
    if a.cmd is None:
        if a.terminal or a.no_gui or not _tk_available():
            return chat_repl(cfg)
        try:
            from .launcher import run_launcher
            return run_launcher(cfg)
        except Exception as e:
            # Launcher failed for any reason — never leave the user stranded.
            print(f"[launcher unavailable: {e}] falling back to terminal chat\n", file=sys.stderr)
            return chat_repl(cfg)

    if a.cmd == "chat":
        return chat_repl(cfg)
    if a.cmd == "ui":
        from .web.server import serve
        return serve(cfg, a.host, a.port, open_browser=not a.no_browser)
    if a.cmd == "check":
        out = commands.test(cfg, "all" if a.all else None)
        print(out)
        for e in cfg.providers.summary()[:1] if not a.all else cfg.providers.summary():
            ok = llm.model_listed(cfg, e["pool"], e["model"])
            if ok is False:
                print(f"WARNING: model '{e['model']}' is not in {e['pool']}'s model list. Run: omni models {e['pool']}")
        if "FAILED" in out and not a.all:
            sys.exit(1)
        return
    if a.cmd == "models":
        print(commands.handle(cfg, "/models " + " ".join(a.rest)))
        return
    if a.cmd == "provider":
        import shlex
        r = a.rest or ["list"]
        print(commands.handle(cfg, "/" + " ".join(shlex.quote(x) for x in r)))
        return
    if a.cmd == "run":
        task = open(a.file, encoding="utf-8").read() if a.file else a.prompt
        if not task:
            ap.error("give a prompt or -f file")
        try:
            print(loop.run(cfg, loop.new_history(cfg), task))
        except llm.LLMError as e:
            print(f"[gateway error] {e}", file=sys.stderr)
            sys.exit(1)
        return
    if a.cmd == "schedule":
        task = open(a.file, encoding="utf-8").read()
        while True:
            print(f"\n[{time.strftime('%F %T')}] running {a.file}")
            try:
                print(loop.run(cfg, loop.new_history(cfg), task))
            except Exception as e:
                print("run failed:", e)
            time.sleep(a.every)


def _tk_available() -> bool:
    """True if we can import tkinter (bundled by Nuitka when the build includes it)."""
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


if __name__ == "__main__":
    main()