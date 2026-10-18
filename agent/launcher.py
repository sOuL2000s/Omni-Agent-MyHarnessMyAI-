# agent/launcher.py
"""Tiny tkinter launcher shown when omni.exe is started with no arguments.

Two buttons: Terminal chat and Web UI. The launcher stays open after either
so the user can start the other as well. Everything else (run / check /
provider / schedule) is still available from the command line.

The window is deliberately plain: no images, no fonts beyond the defaults, so
it starts instantly and works on any Windows/macOS/Linux box Nuitka targets.
"""
import os
import subprocess
import sys
import threading
from pathlib import Path


# ---------------------------------------------------------------- helpers
def _python_cmd() -> list:
    """How to relaunch ourselves as a subprocess.

    * Frozen (Nuitka): re-exec the same exe with the desired subcommand.
    * Source tree:     re-exec sys.executable -m agent <subcommand>.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "agent"]


def _open_terminal_chat(cfg):
    """Open a *new console window* running `omni chat` so the launcher stays alive
    and can be reused. Falls back to running in this process if we can't spawn one.
    """
    args = _python_cmd() + ["chat"]
    try:
        if os.name == "nt":
            # CREATE_NEW_CONSOLE gives the child its own window
            subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE,
                             cwd=str(_cwd()))
        elif sys.platform == "darwin":
            # macOS: open Terminal.app with the command
            script = 'tell application "Terminal" to do script "%s"' % " ".join(
                _shell_quote(a) for a in args)
            subprocess.Popen(["osascript", "-e", script])
        else:
            # Linux: try the common terminal emulators in order
            for term in ("x-terminal-emulator", "gnome-terminal", "konsole",
                         "xfce4-terminal", "xterm"):
                if _which(term):
                    if term == "gnome-terminal":
                        subprocess.Popen([term, "--", *args], cwd=str(_cwd()))
                    else:
                        subprocess.Popen([term, "-e", " ".join(_shell_quote(a) for a in args)],
                                         cwd=str(_cwd()))
                    break
            else:
                # last resort: run it in this process (the GUI will close)
                _run_in_process(cfg, ["chat"])
    except Exception:
        # if anything above fails, just run it here
        _run_in_process(cfg, ["chat"])


def _open_web_ui(cfg, on_url=None):
    """Start the web UI server in a background thread and open the browser.

    `on_url` is called (from the server thread) with the real URL once the
    socket is bound, so the launcher can display the actual port.
    """
    from .web.server import serve
    t = threading.Thread(target=serve, kwargs=dict(
        cfg=cfg, host="127.0.0.1", port=8765, open_browser=True,
        on_url=on_url),
        daemon=True)
    t.start()
    return t


def _run_in_process(cfg, argv):
    """Fallback: run the CLI in this process (used only if we can't spawn a terminal)."""
    from .__main__ import main as cli_main
    old = sys.argv
    try:
        sys.argv = ["omni"] + argv
        cli_main()
    finally:
        sys.argv = old


def _cwd() -> Path:
    # When frozen, run from the exe's folder so data/ and .env sit next to it.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()


def _which(name: str):
    from shutil import which
    return which(name)


def _shell_quote(s: str) -> str:
    if not s:
        return "''"
    if all(c.isalnum() or c in "-_./=:@," for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- window
def run_launcher(cfg) -> int:
    """Show the launcher. Returns an exit code (0 = user closed the window)."""
    if os.environ.get("OMNI_NO_GUI") == "1":
        # user asked for the terminal; behave as if 'omni chat' was run
        from .__main__ import chat_repl
        chat_repl(cfg)
        return 0
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Omni")
    root.resizable(False, False)
    try:
        root.iconify()
        root.deiconify()
    except Exception:
        pass

    # ---- style: use the native theme, keep it minimal
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass

    frm = ttk.Frame(root, padding=20)
    frm.grid(row=0, column=0, sticky="nsew")

    ttk.Label(frm, text="Omni Agent",
              font=("Segoe UI", 16, "bold") if os.name == "nt" else ("Helvetica", 16, "bold")
              ).grid(row=0, column=0, columnspan=2, pady=(0, 4))
    ttk.Label(frm, text="Choose how to start",
              foreground="#666").grid(row=1, column=0, columnspan=2, pady=(0, 16))

    # ---- status label (updated when the web UI starts)
    status = tk.StringVar(value="")
    status_lbl = ttk.Label(frm, textvariable=status, foreground="#666",
                           wraplength=380, justify="left")
    status_lbl.grid(row=4, column=0, columnspan=2, pady=(14, 0))

    def on_terminal():
        status.set("Opened a terminal window. This launcher can stay open for "
                   "another launch, or you can close it.")
        root.update_idletasks()
        _open_terminal_chat(cfg)
        # Keep the launcher alive so the user can also start the web UI.
        # (Matches the docstring and the README.)

    def on_web():
        status.set("Starting the web UI…")
        root.update_idletasks()
        try:
            def _ready(url):
                # called from the server thread; hop back to the GUI thread
                try:
                    root.after(0, lambda: status.set(
                        f"Web UI running at {url}  —  close this launcher to stop it."))
                except Exception:
                    pass
            _open_web_ui(cfg, on_url=_ready)
        except Exception as e:
            status.set(f"Could not start the web UI: {e}")

    def on_quit():
        root.destroy()

    big = dict(width=22, padding=(8, 12))
    ttk.Button(frm, text="Terminal chat", command=on_terminal, **big
               ).grid(row=2, column=0, padx=(0, 8), sticky="ew")
    ttk.Button(frm, text="Web UI", command=on_web, **big
               ).grid(row=2, column=1, padx=(8, 0), sticky="ew")

    ttk.Separator(frm, orient="horizontal").grid(row=3, column=0, columnspan=2,
                                                 sticky="ew", pady=(16, 0))
    ttk.Button(frm, text="Quit", command=on_quit).grid(row=5, column=0, columnspan=2,
                                                       pady=(12, 0))

    # ---- workspace line (read-only, just so the user knows where files go)
    ws_text = f"Workspace: {cfg.workspace}"
    ttk.Label(frm, text=ws_text, foreground="#888",
              wraplength=380, justify="left").grid(row=6, column=0, columnspan=2,
                                                   pady=(10, 0))

    # center on screen
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    root.mainloop()
    return 0