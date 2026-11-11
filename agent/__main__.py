import argparse
import os
import sys
import threading
import time
from pathlib import Path
from . import config, loop, llm, commands


# ---------------------------------------------------------------- terminal
def _enable_windows_vt() -> bool:
    """Turn on ANSI/VT escape processing for the current console.

    Returns True if ANSI sequences will be interpreted (so color is safe
    to emit), False if this is a legacy console that can't do it (so the
    caller must fall back to plain text).

    - Windows: call SetConsoleMode with ENABLE_VIRTUAL_TERMINAL_PROCESSING
      on both stdout and stderr. Classic cmd.exe does NOT enable this by
      default, which is why escape codes were showing up literally
      (`←[1m←[38;5;208m`) instead of coloring the text.
    - POSIX: True when stdout is a tty (the shell already understands ANSI).
    """
    if os.name != "nt":
        return sys.stdout.isatty()
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Constants from the Windows console API.
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        STD_OUTPUT_HANDLE = -11
        STD_ERROR_HANDLE = -12

        ok = True
        for std_handle in (STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
            handle = kernel32.GetStdHandle(std_handle)
            if handle in (0, -1):
                # No console attached (e.g. output piped to a file). Don't
                # try to enable VT — treat it as "no color".
                ok = False
                continue
            mode = wintypes.DWORD()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ok = False
                continue
            new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            if not kernel32.SetConsoleMode(handle, new_mode):
                ok = False
        return ok
    except Exception:
        return False


# Resolve once, before any colored output is emitted. `_VT_OK` is also
# imported by loop.py so the non-interactive modes (`omni run`) agree
# with the REPL about whether color is safe.
_VT_OK = _enable_windows_vt()

# Color is on only when the terminal will interpret the codes AND the
# user hasn't opted out with NO_COLOR (https://no-color.org).
_NO_COLOR = (not _VT_OK) or bool(os.environ.get("NO_COLOR"))
# Tell loop.py's cli_event the same thing (it computes its own flag at
# import time; we overwrite it here so `omni run` matches the REPL).
try:
    loop._NO_COLOR = _NO_COLOR
except Exception:
    pass


class C:
    """ANSI color helpers. Empty strings when color is disabled."""
    RESET = "" if _NO_COLOR else "\033[0m"
    BOLD = "" if _NO_COLOR else "\033[1m"
    DIM = "" if _NO_COLOR else "\033[2m"
    ITALIC = "" if _NO_COLOR else "\033[3m"
    ORANGE = "" if _NO_COLOR else "\033[38;5;208m"   # matches the web accent
    AMBER = "" if _NO_COLOR else "\033[38;5;214m"
    GREEN = "" if _NO_COLOR else "\033[38;5;42m"
    RED = "" if _NO_COLOR else "\033[38;5;203m"
    BLUE = "" if _NO_COLOR else "\033[38;5;75m"
    PURPLE = "" if _NO_COLOR else "\033[38;5;141m"
    CYAN = "" if _NO_COLOR else "\033[38;5;51m"
    GRAY = "" if _NO_COLOR else "\033[38;5;245m"


def _w(text, color="", bold=False):
    """Wrap `text` in a color (and optional bold). No-op when color is off."""
    if _NO_COLOR or not (color or bold):
        return str(text)
    return f"{C.BOLD if bold else ''}{color}{text}{C.RESET}"


def _ljust_visible(text, width):
    """Left-justify `text` to `width` *visible* chars.

    ANSI escape sequences have zero visible width, so we strip them for
    the length calculation but keep them in the output. Without this,
    a colored string inside a box shifts the right border by the length
    of the escape codes.
    """
    import re
    visible = re.sub(r"\033\[[0-9;]*m", "", text)
    pad = max(0, width - len(visible))
    return text + " " * pad


def _box(title, body, color=C.ORANGE):
    """Draw a rounded box around `body` with `title` on the top edge.

    Lines are wrapped at 78 columns so the box never gets absurdly wide.
    Empty bodies render a single blank line so the box isn't a sliver.
    """
    import textwrap
    inner_w = 76
    top_title = f" {title} " if title else ""
    top = _w("╭", color) + _w(top_title, color, bold=True) + _w("─" * max(1, inner_w - len(top_title)), color) + _w("╮", color)
    out = [top]
    body = (body or "").rstrip()
    if not body:
        out.append(_w("│", color) + " " * inner_w + _w("│", color))
    else:
        for para in body.split("\n"):
            if not para:
                out.append(_w("│", color) + " " * inner_w + _w("│", color))
                continue
            for line in textwrap.wrap(para, width=inner_w - 2, replace_whitespace=False, drop_whitespace=False) or [""]:
                out.append(_w("│", color) + " " + _ljust_visible(line, inner_w - 2) + " " + _w("│", color))
    out.append(_w("╰", color) + _w("─" * inner_w, color) + _w("╯", color))
    return "\n".join(out)


def _rule(label="", width=78, color=C.GRAY):
    """A horizontal rule with an optional centered label."""
    if not label:
        return _w("─" * width, color)
    label = f" {label} "
    side = max(0, (width - len(label)) // 2)
    return _w("─" * side, color) + _w(label, C.BOLD + color) + _w("─" * (width - side - len(label)), color)


# ---------------------------------------------------------------- banner
BANNER = r"""
   ██████╗ ███╗   ███╗███╗   ██╗██╗
  ██╔═══██╗████╗ ████║████╗  ██║██║
  ██║   ██║██╔████╔██║██╔██╗ ██║██║
  ██║   ██║██║╚██╔╝██║██║╚██╗██║██║
  ╚██████╔╝██║ ╚═╝ ██║██║ ╚████║██║
   ╚═════╝ ╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝

   █████╗  ██████╗ ███████╗███╗   ██╗████████╗
  ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
  ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║
  ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║
  ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║
  ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝
"""


def _print_banner(pm, pools):
    """Print the ASCII banner + a boxed welcome panel.

    The banner is plain ASCII (not ANSI) so it renders the same on every
    terminal. Only the welcome panel uses color, and `_w` no-ops when
    color was disabled (legacy console / NO_COLOR).
    """
    print(_w(BANNER, C.ORANGE, bold=True))
    primary = pools[0] if pools else "none"
    lines = [
        f"{_w('primary pool', C.GRAY)}   {_w(primary, C.ORANGE, bold=True)}",
        f"{_w('keys', C.GRAY)}           {len(pm.items)}",
        f"{_w('strategy', C.GRAY)}       {pm.strategy}",
        f"{_w('model mode', C.GRAY)}     {pm.model_mode}",
        f"{_w('workspace', C.GRAY)}      {cfg_workspace_short()}",
        "",
        _w("Type a task, or /help for commands.", C.GRAY),
        _w("Prefer a browser?  run:  omni ui", C.GRAY),
    ]
    print(_box("Omni Agent", "\n".join(lines), color=C.ORANGE))


def cfg_workspace_short(max_len=52):
    """Workspace path, shortened with an ellipsis so the box stays tidy."""
    ws = str(config.ROOT)
    try:
        from . import config as _cfg
        ws = str(_cfg.load_workspace_for_display() if hasattr(_cfg, "load_workspace_for_display") else _current_cfg.workspace)
    except Exception:
        pass
    if len(ws) <= max_len:
        return ws
    return "…" + ws[-(max_len - 1):]


_current_cfg = None


# ---------------------------------------------------------------- readline
def _setup_readline():
    try:
        import readline  # noqa: F401
    except ImportError:
        try:
            import pyreadline3 as readline  # noqa: F401
        except ImportError:
            return
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


def _setup_completion():
    """Tab-completion for slash commands (readline)."""
    try:
        import readline
    except ImportError:
        try:
            import pyreadline3 as readline  # noqa: F401
        except ImportError:
            return
    commands_list = [
        "/help", "/providers", "/keys", "/add", "/addkeys", "/key", "/model",
        "/models", "/fallback", "/strategy", "/modelmode", "/preference",
        "/use", "/order", "/remove", "/removepool", "/test", "/revive",
        "/refresh", "/keymodel", "/autodetect", "/workspace", "/bgauto",
        "/bg", "/jobs", "/kill", "/killall", "/auto", "/config",
        "/memory", "/files", "/skills", "/mcp", "/reset", "/exit",
    ]
    def completer(text, state):
        opts = [c for c in commands_list if c.startswith(text)]
        return opts[state] if state < len(opts) else None
    try:
        readline.set_completer(completer)
        readline.parse_and_bind("tab: complete")
    except Exception:
        pass


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- REPL
def chat_repl(cfg):
    global _current_cfg
    _current_cfg = cfg
    _setup_readline()
    _setup_completion()
    pm = cfg.providers
    pools = pm.pool_names()

    _print_banner(pm, pools)
    if pm.load_error:
        print(_box("Warning", pm.load_error, color=C.AMBER) + "\n")
    if _NO_COLOR:
        # One-line hint so the user knows why there is no color. Only on
        # Windows (where this is actionable); on POSIX it just means the
        # output was piped, and a hint would be noise.
        if os.name == "nt":
            print(_w("  (colors disabled — this console cannot handle ANSI escapes; "
                     "use Windows Terminal or PowerShell for the full look)", C.GRAY))

    hist = loop.new_history(cfg)
    jobs = {}

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
        rec["done"].wait()
        time.sleep(0.05)
        tail = [x for x in rec["out"] if x and x != "[done]"]
        first = (tail[-1] if tail else "")[:100].replace("\n", " ")
        try:
            sys.stderr.write("\n" + _w(f"[bg {job_id} done] {first}", C.GREEN) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    # On a no-color console the braille spinners look like tofu; use a
    # plain rotating ASCII spinner instead so the status line is legible.
    if _NO_COLOR:
        spinner_frames = ["|", "/", "-", "\\"]
    spin = {"i": 0, "line": ""}

    def _clear_status():
        if spin["line"]:
            try:
                sys.stderr.write("\r" + " " * len(spin["line"]) + "\r")
                sys.stderr.flush()
            except Exception:
                pass
            spin["line"] = ""

    def _draw_status(text):
        _clear_status()
        line = f"  {_w(spinner_frames[spin['i'] % len(spinner_frames)], C.ORANGE)} {_w(text, C.GRAY)}"
        spin["i"] += 1
        spin["line"] = line
        try:
            sys.stderr.write("\r" + line)
            sys.stderr.flush()
        except Exception:
            pass

    def _cli_event(kind, text="", **extra):
        try:
            if kind == "tool":
                _clear_status()
                name = extra.get("name") or "tool"
                args = (text or "").replace("\n", " ")[:110]
                print(f"  {_w('▸', C.CYAN)} {_w(name, C.BOLD + C.CYAN)}({_w(args, C.GRAY)})")
            elif kind == "tool_stream":
                _clear_status()
                sys.stdout.write(_w(text, C.GRAY))
                sys.stdout.flush()
            elif kind == "tool_result":
                _clear_status()
                ok = extra.get("ok", True)
                mark = _w("✓", C.GREEN, bold=True) if ok else _w("✗", C.RED, bold=True)
                first = (text or "").splitlines()[0][:110] if text else ""
                print(f"  {mark} {_w(first, C.GRAY)}")
            elif kind == "notice":
                _clear_status()
                print(f"  {_w('↪', C.AMBER)} {_w(text, C.AMBER)}")
            elif kind == "progress":
                title = extra.get("title") or text or "working"
                detail = extra.get("detail") or ""
                _draw_status(f"{title}  {_w(detail, C.DIM)}".strip())
            elif kind == "route":
                _clear_status()
                prov = extra.get("provider", "")
                model = extra.get("model", "")
                fb = extra.get("fallback", False)
                tag = " (fallback)" if fb else ""
                print(_w(f"  ◆ route: {prov} · {model}{tag}", C.PURPLE))
            elif kind == "assistant" and text.strip():
                _clear_status()
                print()
                print(_box("Omni", text.strip(), color=C.ORANGE))
            elif kind == "verify":
                _clear_status()
                print()
                print(_box("⚠ Unverified claims", text, color=C.AMBER))
            elif kind == "error":
                _clear_status()
                print()
                print(_box("✗ Error", text, color=C.RED), file=sys.stderr)
        except Exception:
            try:
                print(text)
            except Exception:
                pass

    while True:
        try:
            q = input(_w("omni ❯ ", C.ORANGE, bold=True)).strip()
        except (EOFError, KeyboardInterrupt):
            _clear_status()
            print()
            return
        if not q:
            continue

        if q in ("/exit", "/quit"):
            running_agents = any(j["thread"].is_alive() for j in jobs.values())
            from .tools import shell as _shell_mod
            running_shells = [d for d in _shell_mod.list_detached() if d["alive"]]
            if running_agents or running_shells:
                print(_w("(some background jobs are still running - use /kill N first if you want them gone)", C.GRAY))
            return

        if q == "/reset":
            hist = loop.new_history(cfg)
            print(_w("history cleared", C.GREEN))
            continue

        if q == "/jobs":
            from .tools import shell as _shell_mod
            detached = _shell_mod.list_detached()
            if not jobs and not detached:
                print(_w("no background jobs", C.GRAY))
                continue
            lines = []
            for jid, j in jobs.items():
                status = "running" if j["thread"].is_alive() else "done"
                tail = "".join(j["out"])[-200:].replace("\n", " ")
                lines.append(f"agent {jid}: {status}  {j['label']}  {tail}")
            for d in detached:
                status = "running" if d["alive"] else "exited"
                started = time.strftime("%H:%M:%S", time.localtime(d["started"]))
                lines.append(f"shell {d['id']}: {status}  started {started}  {d['cmd'][:70]}  log={d['log']}")
            print(_box("Background jobs", "\n".join(lines), color=C.CYAN))
            continue

        if q.startswith("/kill "):
            jid = q[6:].strip()
            if jid in jobs:
                jobs[jid]["stop"].set()
                print(_w(f"kill signal sent to agent job {jid}", C.GREEN))
                continue
            from .tools import shell as _shell_mod
            print(_w(_shell_mod.kill_detached(jid), C.GREEN))
            continue

        if q.startswith("/bg "):
            text = q[4:].strip()
            if not text:
                print(_w("usage: /bg <task>", C.AMBER))
                continue
            jid = launch_bg(text)
            threading.Thread(target=_bg_monitor, args=(jid, jobs[jid]),
                             daemon=True, name=f"omni-bg-monitor-{jid}").start()
            print(_w(f"started job {jid} in background; /jobs to check, /kill {jid} to stop", C.GREEN))
            continue

        if q.startswith("/"):
            out = commands.handle(cfg, q)
            if out:
                title = q.split()[0].lstrip("/").capitalize() or "Command"
                print(_box(title, out, color=C.BLUE))
            continue

        try:
            _clear_status()
            print()
            t0 = time.time()
            final = loop.run(cfg, hist, q, on_event=_cli_event)
            _clear_status()
            elapsed = time.time() - t0
            print()
            print(_box(f"Omni  ·  {elapsed:.1f}s", final, color=C.ORANGE))
        except llm.LLMError as e:
            _clear_status()
            print()
            print(_box("✗ Gateway error", str(e), color=C.RED), file=sys.stderr)
        except KeyboardInterrupt:
            _clear_status()
            print(_w("\n(interrupted)", C.AMBER))


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
    ap.add_argument("--terminal", action="store_true",
                    help="skip the launcher and go straight to the terminal chat")
    ap.add_argument("--no-gui", action="store_true",
                    help="same as --terminal (alias, for scripts)")
    a = ap.parse_args()
    cfg = config.load()

    if cfg.providers.load_error:
        print(_w("WARNING: " + cfg.providers.load_error, C.AMBER), file=sys.stderr)

    if a.cmd is None:
        if a.terminal or a.no_gui or not _tk_available():
            return chat_repl(cfg)
        try:
            from .launcher import run_launcher
            return run_launcher(cfg)
        except Exception as e:
            print(_w(f"[launcher unavailable: {e}] falling back to terminal chat", C.AMBER) + "\n", file=sys.stderr)
            return chat_repl(cfg)

    if a.cmd == "chat":
        return chat_repl(cfg)
    if a.cmd == "ui":
        from .web.server import serve
        return serve(cfg, a.host, a.port, open_browser=not a.no_browser)
    if a.cmd == "check":
        out = commands.test(cfg, "all" if a.all else None)
        print(_box("Key check", out, color=C.BLUE))
        for e in cfg.providers.summary()[:1] if not a.all else cfg.providers.summary():
            ok = llm.model_listed(cfg, e["pool"], e["model"])
            if ok is False:
                print(_w(f"WARNING: model '{e['model']}' is not in {e['pool']}'s model list. Run: omni models {e['pool']}", C.AMBER))
        if "FAILED" in out and not a.all:
            sys.exit(1)
        return
    if a.cmd == "models":
        out = commands.handle(cfg, "/models " + " ".join(a.rest))
        print(_box("Models", out, color=C.BLUE))
        return
    if a.cmd == "provider":
        import shlex
        r = a.rest or ["list"]
        out = commands.handle(cfg, "/" + " ".join(shlex.quote(x) for x in r))
        print(_box("Providers", out, color=C.BLUE))
        return
    if a.cmd == "run":
        task = open(a.file, encoding="utf-8").read() if a.file else a.prompt
        if not task:
            ap.error("give a prompt or -f file")
        try:
            print(_box("Omni", loop.run(cfg, loop.new_history(cfg), task, on_event=_cli_event_run), color=C.ORANGE))
        except llm.LLMError as e:
            print(_w(f"[gateway error] {e}", C.RED), file=sys.stderr)
            sys.exit(1)
        return
    if a.cmd == "schedule":
        task = open(a.file, encoding="utf-8").read()
        while True:
            print("\n" + _rule(time.strftime("%F %T") + "  running " + a.file))
            try:
                print(_box("Omni", loop.run(cfg, loop.new_history(cfg), task, on_event=_cli_event_run), color=C.ORANGE))
            except Exception as e:
                print(_w("run failed: " + str(e), C.RED))
            time.sleep(a.every)


def _cli_event_run(kind, text="", **extra):
    if kind == "tool":
        name = extra.get("name") or "tool"
        args = (text or "").replace("\n", " ")[:110]
        print(f"  {_w('▸', C.CYAN)} {_w(name, C.CYAN)}({_w(args, C.GRAY)})")
    elif kind == "tool_stream":
        sys.stdout.write(_w(text, C.GRAY))
        sys.stdout.flush()
    elif kind == "tool_result":
        ok = extra.get("ok", True)
        mark = _w("✓", C.GREEN, bold=True) if ok else _w("✗", C.RED, bold=True)
        first = (text or "").splitlines()[0][:110] if text else ""
        print(f"  {mark} {_w(first, C.GRAY)}")
    elif kind == "notice":
        print(f"  {_w('↪', C.AMBER)} {_w(text, C.AMBER)}")
    elif kind == "route":
        print(_w(f"  ◆ route: {extra.get('provider','')} · {extra.get('model','')}", C.PURPLE))
    elif kind == "assistant" and text.strip():
        print()
        print(_box("Omni", text.strip(), color=C.ORANGE))
    elif kind == "verify":
        print(_box("⚠ Unverified claims", text, color=C.AMBER))
    elif kind == "error":
        print(_box("✗ Error", text, color=C.RED), file=sys.stderr)


def _tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


if __name__ == "__main__":
    main()