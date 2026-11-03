# agent/launcher.py
"""Tiny tkinter launcher shown when omni.exe is started with no arguments.

Three buttons: Terminal chat, Web UI, and Single prompt. The launcher stays
open after either of the first two so the user can start the other as well.
"Single prompt" opens a modal where the user picks a workspace, types one
prompt, and gets the answer streamed back. That mode forces strict +
auto-approve + bg-shell on for the run (and restores the previous values
when the modal closes).

The window is deliberately plain: no images, no fonts beyond the defaults, so
it starts instantly and works on any Windows/macOS/Linux box Nuitka targets.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


# ---------------------------------------------------------------- helpers
def _python_cmd() -> list:
    """How to relaunch ourselves as a subprocess.

    * Frozen (Nuitka): re-exec the *bundled exe* itself, not the embedded
      python interpreter. On a `--standalone` build, `sys.executable` is
      `<dist>/python.exe`, not `<dist>/omni.exe`; re-execing python.exe
      with `chat` makes Python try to open a file named `chat`, which
      dies silently under CREATE_NEW_CONSOLE. `sys.argv[0]` is the real
      exe path Nuitka set for us.
    * Source tree:     re-exec sys.executable -m agent <subcommand>.
    """
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else sys.executable
        # Sanity: if argv[0] is not an existing file, fall back to the
        # bare executable (some launchers strip argv[0]).
        if not os.path.isfile(exe):
            exe = sys.executable
        return [exe]
    return [sys.executable, "-m", "agent"]


def _spawn_and_check(cmd: list, cwd: str, wait_ms: int = 700):
    """Popen `cmd` and return (proc, ok).

    `ok` is True if the child is still alive after `wait_ms`. If it died
    (missing file, wrong interpreter, CREATE_NEW_CONSOLE absorbed the
    console, etc.), the caller should fall back to in-process execution
    instead of pretending it worked.
    """
    try:
        if os.name == "nt":
            proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE,
                                    cwd=cwd)
        elif sys.platform == "darwin":
            script = 'tell application "Terminal" to do script "%s"' % " ".join(
                _shell_quote(a) for a in cmd)
            proc = subprocess.Popen(["osascript", "-e", script], cwd=cwd)
        else:
            for term in ("x-terminal-emulator", "gnome-terminal", "konsole",
                         "xfce4-terminal", "xterm"):
                if _which(term):
                    if term == "gnome-terminal":
                        proc = subprocess.Popen([term, "--", *cmd], cwd=cwd)
                    else:
                        proc = subprocess.Popen(
                            [term, "-e", " ".join(_shell_quote(a) for a in cmd)],
                            cwd=cwd)
                    break
            else:
                return None, False
        # Give the child a moment to fail fast (bad exe, missing module).
        for _ in range(wait_ms // 50):
            if proc.poll() is not None:
                return proc, False
            time.sleep(0.05)
        return proc, True
    except Exception:
        return None, False


def _open_terminal_chat(cfg):
    """Open a *new console window* running `omni chat` so the launcher stays
    alive. Falls back to running in this process if the child dies.

    Returns True if a new window was actually opened, False if we fell back.
    """
    args = _python_cmd() + ["chat"]
    proc, ok = _spawn_and_check(args, str(_cwd()))
    if ok:
        return True
    # Fallback: run the CLI in this process. The launcher window will
    # close as the CLI takes over stdin/stdout, which is better than the
    # user staring at a window that does nothing.
    _run_in_process(cfg, ["chat"])
    return False


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


def _open_single_prompt(cfg, parent):
    """Open the single-prompt window: workspace picker, prompt box,
    Run button. Strict + auto-approve + bg-shell are forced on for the run
    (and reverted when the window closes)."""

    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    win = tk.Toplevel(parent)
    win.title("Omni — single prompt")
    win.minsize(560, 480)
    win.transient(parent)

    # ---- workspace row -------------------------------------------------
    ws_var = tk.StringVar(value=str(cfg.workspace))
    ws_row = ttk.Frame(win, padding=(12, 12, 12, 4))
    ws_row.pack(fill="x")
    ttk.Label(ws_row, text="Workspace").pack(side="left")
    ws_entry = ttk.Entry(ws_row, textvariable=ws_var)
    ws_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))

    def pick_workspace():
        # tkinter's native picker — works because the launcher already
        # owns a Tk root. (No need to round-trip through the web server.)
        path = filedialog.askdirectory(
            initialdir=ws_var.get() or str(cfg.workspace),
            title="Choose the workspace for this prompt",
            mustexist=True)
        if path:
            ws_var.set(path)

    ttk.Button(ws_row, text="Browse…", command=pick_workspace).pack(side="left")

    # ---- mode summary --------------------------------------------------
    modes = ttk.Label(
        win, padding=(12, 0, 12, 8),
        foreground="#666",
        text="strict=on · auto-approve=on · bg-shell=on "
             "(forced for single-prompt runs)")
    modes.pack(fill="x")

    # ---- prompt box ----------------------------------------------------
    ttk.Label(win, text="Prompt", padding=(12, 0, 12, 2)).pack(anchor="w")
    prompt_frame = ttk.Frame(win, padding=(12, 0, 12, 8))
    prompt_frame.pack(fill="both", expand=True)
    prompt = tk.Text(prompt_frame, height=6, wrap="word",
                     font=("Consolas", 10) if os.name == "nt" else ("Menlo", 11))
    prompt.pack(fill="both", expand=True)
    prompt.insert("1.0", "")

    # ---- output --------------------------------------------------------
    ttk.Label(win, text="Output", padding=(12, 0, 12, 2)).pack(anchor="w")
    out_frame = ttk.Frame(win, padding=(12, 0, 12, 8))
    out_frame.pack(fill="both", expand=True)
    out = tk.Text(out_frame, height=10, wrap="word", state="disabled",
                  background="#0b1220", foreground="#e2e8f0",
                  font=("Consolas", 9) if os.name == "nt" else ("Menlo", 10))
    out.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(out_frame, command=out.yview)
    sb.pack(side="right", fill="y")
    out.configure(yscrollcommand=sb.set)

    def append_out(text):
        out.configure(state="normal")
        out.insert("end", text)
        out.see("end")
        out.configure(state="disabled")

    def clear_out():
        out.configure(state="normal")
        out.delete("1.0", "end")
        out.configure(state="disabled")

    # ---- buttons -------------------------------------------------------
    btns = ttk.Frame(win, padding=(12, 0, 12, 12))
    btns.pack(fill="x")
    run_btn = ttk.Button(btns, text="Run")
    run_btn.pack(side="right")
    cancel_btn = ttk.Button(btns, text="Close", command=win.destroy)
    cancel_btn.pack(side="right", padx=(0, 8))

    # ---- run ----------------------------------------------------------
    # Snapshot the config so we can restore it if the user runs a second
    # prompt later in the same launcher session.
    prev = {
        "workspace": cfg.workspace,
        "strict": cfg.strict,
        "auto_approve": cfg.auto_approve,
    }
    from .tools import shell as _shell
    prev["shell_bg"] = _shell.default_background()

    def restore_prev():
        cfg.workspace = prev["workspace"]
        cfg.strict = prev["strict"]
        cfg.auto_approve = prev["auto_approve"]
        _shell.set_default_background(prev["shell_bg"])
    win.protocol("WM_DELETE_WINDOW",
                 lambda: (restore_prev(), win.destroy()))

    running = {"flag": False}

    def do_run():
        if running["flag"]:
            return
        text = prompt.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Omni", "Enter a prompt first.", parent=win)
            return
        ws = ws_var.get().strip()
        if not ws:
            messagebox.showwarning("Omni", "Choose a workspace first.", parent=win)
            return
        from pathlib import Path
        try:
            ws_path = Path(ws).expanduser().resolve()
        except OSError as e:
            messagebox.showerror("Omni", f"Could not resolve workspace: {e}", parent=win)
            return
        if not ws_path.is_dir():
            messagebox.showerror("Omni", f"Not a directory: {ws_path}", parent=win)
            return
        if ws_path == ws_path.parent:
            messagebox.showerror(
                "Omni",
                "Refusing a filesystem root as the workspace "
                "(it would disable the approval guard).",
                parent=win)
            return

        # Apply the workspace + forced toggles.
        cfg.workspace = ws_path
        cfg.strict = True
        cfg.auto_approve = True
        _shell.set_default_background(True)

        # Persist the workspace to .env so the next launch remembers it.
        try:
            from .commands import _write_env_var
            _write_env_var("AGENT_WORKSPACE", str(ws_path))
            _write_env_var("AGENT_STRICT", "1")
            _write_env_var("AGENT_AUTO_APPROVE", "1")
            _write_env_var("AGENT_SHELL_BG", "1")
        except Exception:
            pass

        clear_out()
        append_out(f"[workspace] {ws_path}\n")
        append_out("[mode] strict=on  auto-approve=on  bg-shell=on\n\n")
        append_out("[running…]\n")

        run_btn.configure(state="disabled")
        running["flag"] = True

        # Run the loop in a background thread. Events are pushed to the
        # GUI thread via `win.after(0, …)` because Tk widgets must only be
        # touched from the main thread.
        def on_event(kind, text_="", **extra):
            if kind == "tool":
                name = extra.get("name") or "tool"
                win.after(0, append_out, f"  → {name}({str(text_)[:160]})\n")
            elif kind == "tool_result":
                marker = "✓" if extra.get("ok", True) else "✗"
                snippet = (text_ or "").splitlines()[0][:120] if text_ else ""
                win.after(0, append_out, f"  {marker} {snippet}\n")
            elif kind == "assistant" and text_:
                win.after(0, append_out, f"\n{text_}\n")
            elif kind == "notice":
                win.after(0, append_out, f"  ! {text_}\n")
            elif kind == "route":
                win.after(0, append_out,
                          f"[route] {extra.get('provider')} · "
                          f"{extra.get('model')}\n")
            elif kind == "verify":
                win.after(0, append_out, f"\n{text_}\n")
            elif kind == "error":
                win.after(0, append_out, f"\n[error] {text_}\n")
            # tool_stream is intentionally dropped: the shell tool can emit
            # thousands of lines and we don't want to freeze the GUI.

        def work():
            from . import loop as _loop, llm as _llm
            try:
                hist = _loop.new_history(cfg)
                final = _loop.run(cfg, hist, text,
                                  on_event=on_event, cancel=None)
                win.after(0, append_out, f"\n[done]\n{final}\n")
            except _llm.LLMError as e:
                win.after(0, append_out, f"\n[gateway error] {e}\n")
            except Exception as e:
                win.after(0, append_out, f"\n[unexpected] {type(e).__name__}: {e}\n")
            finally:
                def done():
                    running["flag"] = False
                    run_btn.configure(state="normal")
                win.after(0, done)

        threading.Thread(target=work, daemon=True,
                         name="omni-single-prompt").start()

    run_btn.configure(command=do_run)
    prompt.focus_set()
    return win


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
    status_lbl.grid(row=5, column=0, columnspan=2, pady=(14, 0))

    def on_terminal():
        status.set("Starting terminal chat…")
        root.update_idletasks()
        ok = _open_terminal_chat(cfg)
        if ok:
            status.set("Opened a terminal window. This launcher can stay open "
                       "for another launch, or you can close it.")
        else:
            # _open_terminal_chat ran the CLI inline; the launcher is
            # about to be replaced by the REPL. Nothing else to do.
            pass

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

    def on_single_prompt():
        _open_single_prompt(cfg, root)

    def on_quit():
        root.destroy()

    big = dict(width=22, padding=(8, 12))
    ttk.Button(frm, text="Terminal chat", command=on_terminal, **big
               ).grid(row=2, column=0, padx=(0, 8), sticky="ew")
    ttk.Button(frm, text="Web UI", command=on_web, **big
               ).grid(row=2, column=1, padx=(8, 0), sticky="ew")

    # Third button on its own row, full-width, so the window stays balanced.
    ttk.Button(frm, text="Single prompt (auto everything)",
               command=on_single_prompt, width=48, padding=(8, 12)
               ).grid(row=3, column=0, columnspan=2, pady=(8, 0), sticky="ew")

    ttk.Separator(frm, orient="horizontal").grid(row=4, column=0, columnspan=2,
                                                 sticky="ew", pady=(16, 0))
    ttk.Button(frm, text="Quit", command=on_quit).grid(row=6, column=0, columnspan=2,
                                                       pady=(12, 0))

    # ---- workspace line (read-only, just so the user knows where files go)
    ws_text = f"Workspace: {cfg.workspace}"
    ttk.Label(frm, text=ws_text, foreground="#888",
              wraplength=380, justify="left").grid(row=7, column=0, columnspan=2,
                                                   pady=(10, 0))

    # center on screen
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    root.mainloop()
    return 0