import argparse
import os
import sys
import threading
import time
from pathlib import Path
from . import config, loop, llm, commands


def _setup_readline():
    """Enable ↑ history, Ctrl+R search, and persistent history across runs.

    Uses the stdlib `readline` on Unix/macOS (which ships with Python) and
    `pyreadline3` on Windows if it's installed. If neither is available,
    the REPL still works — it just doesn't remember history.
    """
    try:
        import readline  # noqa: F401
    except ImportError:
        try:
            import pyreadline3 as readline  # noqa: F401
        except ImportError:
            return  # no readline available; bare input() still works
    try:
        histfile = Path.home() / ".omni_history"
        if histfile.exists():
            readline.read_history_file(str(histfile))
        readline.set_history_length(1000)
        import atexit
        def _save():
            try:
                readline.write_history_file(str(histfile))
            except Exception:
                pass
        atexit.register(_save)
    except Exception:
        pass


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "unknown"


def chat_repl(cfg):
    _setup_readline()
    pm = cfg.providers
    pools = pm.pool_names()
    print(f"Omni agent  |  primary pool={pools[0] if pools else 'none'}  |  keys={len(pm.items)}  |  strategy={pm.strategy}\n"
          f"workspace={cfg.workspace}\n"
          "Type /help for key/model commands (key rotation + failover is automatic).  Prefer a browser?  run:  omni ui\n"
          "Long task?  prefix it with /bg to run in the background and keep typing.\n"
          "Backgrounded shell jobs survive /exit and Ctrl+C. "
          "Use /bg <task> for agent jobs and background=true for shell servers.\n")
    if pm.load_error:
        print("WARNING:", pm.load_error, "\n")
    hist = loop.new_history(cfg)
    jobs = {}       # id -> {"stop": Event, "out": list, "thread": Thread, "label": str, "done": Event}

    def launch_bg(text):
        job_id = str(len(jobs) + 1)
        while job_id in jobs:
            job_id = str(int(job_id) + 1)
        stop = threading.Event()
        done = threading.Event()
        out = []

        def run():
            try:
                result = loop.run(cfg, loop.new_history(cfg), text,
                                  on_event=lambda k, t="", **e: out.append(t) if t else None,
                                  cancel=stop)
                out.append(result)
            except Exception as e:
                out.append(f"[error] {e}")
            finally:
                out.append("[done]")
                done.set()

        t = threading.Thread(target=run, daemon=True, name=f"omni-bg-{job_id}")
        jobs[job_id] = {"stop": stop, "out": out, "thread": t, "label": text[:60], "done": done}
        t.start()
        return job_id

    def _bg_monitor(job_id, rec):
        """Print a one-line completion notice when a background agent job finishes."""
        rec["done"].wait()
        # Give the final append a moment to land.
        time.sleep(0.05)
        tail = [x for x in rec["out"] if x and x != "[done]"]
        first = (tail[-1] if tail else "")[:100].replace("\n", " ")
        # Only print if we're still attached to a terminal (a /reset or a
        # new task shouldn't spam the prompt).
        try:
            sys.stderr.write(f"\n[bg {job_id} done] {first}\n")
            sys.stderr.flush()
        except Exception:
            pass

    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not q:
            continue
        if q in ("/exit", "/quit"):
            running_agents = any(j["thread"].is_alive() for j in jobs.values())
            from .tools import shell as _shell_mod
            running_shells = [d for d in _shell_mod.list_detached() if d["alive"]]
            if running_agents or running_shells:
                print("(some background jobs are still running - use /kill N first if you want them gone)")
            return
        if q == "/reset":
            hist = loop.new_history(cfg)
            print("history cleared")
            continue
        if q == "/jobs":
            from .tools import shell as _shell_mod
            detached = _shell_mod.list_detached()
            if not jobs and not detached:
                print("no background jobs")
            for jid, j in jobs.items():
                status = "running" if j["thread"].is_alive() else "done"
                tail = "".join(j["out"])[-300:].replace("\n", " ")
                print(f"  agent {jid}: {status}  {j['label']}  {tail}")
            for d in detached:
                status = "running" if d["alive"] else "exited"
                started = time.strftime("%H:%M:%S", time.localtime(d["started"]))
                print(f"  shell {d['id']}: {status}  started {started}  {d['cmd'][:80]}  log={d['log']}")
            continue
        if q.startswith("/kill "):
            jid = q[6:].strip()
            if jid in jobs:
                jobs[jid]["stop"].set()
                print(f"kill signal sent to agent job {jid}")
                continue
            from .tools import shell as _shell_mod
            print(_shell_mod.kill_detached(jid))
            continue
        if q.startswith("/bg "):
            text = q[4:].strip()
            if not text:
                print("usage: /bg <task>")
                continue
            jid = launch_bg(text)
            # Start a monitor thread that prints when it finishes.
            threading.Thread(target=_bg_monitor, args=(jid, jobs[jid]),
                             daemon=True, name=f"omni-bg-monitor-{jid}").start()
            print(f"started job {jid} in background; /jobs to check, /kill {jid} to stop")
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
    ap.add_argument("--version", action="version",
                    version=f"omni-agent {_version()}",
                    help="print the version and exit")
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
            # Live events: the user sees `-> tool(...)` lines while the
            # task runs, instead of a silent 30-second wait for the final
            # answer.
            print(loop.run(cfg, loop.new_history(cfg), task, on_event=loop.cli_event))
        except llm.LLMError as e:
            print(f"[gateway error] {e}", file=sys.stderr)
            sys.exit(1)
        return
    if a.cmd == "schedule":
        task = open(a.file, encoding="utf-8").read()
        while True:
            print(f"\n[{time.strftime('%F %T')}] running {a.file}")
            try:
                print(loop.run(cfg, loop.new_history(cfg), task, on_event=loop.cli_event))
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