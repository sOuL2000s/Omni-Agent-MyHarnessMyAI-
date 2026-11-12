# agent/launcher.py
"""Tiny tkinter launcher shown when omni.exe is started with no arguments.

Three buttons: Terminal chat, Web UI, and Single prompt. The launcher stays
open after either of the first two so the user can start the other as well.
"Single prompt" opens a modal where the user picks a workspace, types one
prompt, and gets the answer streamed back. Strict, auto-approve and
bg-shell start ON for the run but the user can toggle each of them off
before running, and there is a Stop button that cancels a running prompt.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


# ---------------------------------------------------------------- helpers
def _python_cmd() -> list:
    """How to relaunch ourselves as a subprocess."""
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else sys.executable
        if not os.path.isfile(exe):
            exe = sys.executable
        return [exe]
    return [sys.executable, "-m", "agent"]


def _spawn_and_check(cmd: list, cwd: str, wait_ms: int = 700):
    """Popen `cmd` and return (proc, ok)."""
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
        for _ in range(wait_ms // 50):
            if proc.poll() is not None:
                return proc, False
            time.sleep(0.05)
        return proc, True
    except Exception:
        return None, False


def _open_terminal_chat(cfg):
    args = _python_cmd() + ["chat"]
    proc, ok = _spawn_and_check(args, str(_cwd()))
    if ok:
        return True
    _run_in_process(cfg, ["chat"])
    return False


def _open_web_ui(cfg, on_url=None):
    from .web.server import serve
    t = threading.Thread(target=serve, kwargs=dict(
        cfg=cfg, host="127.0.0.1", port=8765, open_browser=True,
        on_url=on_url),
        daemon=True)
    t.start()
    return t


def _open_single_prompt(cfg, parent):
    """Open the single-prompt window: workspace picker, prompt box,
    toggles, Run/Stop buttons. Strict + auto-approve + bg-shell start ON
    for the run but can be toggled off before starting."""

    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    win = tk.Toplevel(parent)
    win.title("Omni — single prompt")
    win.minsize(560, 540)
    win.transient(parent)

    # ---- workspace row -------------------------------------------------
    ws_var = tk.StringVar(value=str(cfg.workspace))
    ws_row = ttk.Frame(win, padding=(12, 12, 12, 4))
    ws_row.pack(fill="x")
    ttk.Label(ws_row, text="Workspace").pack(side="left")
    ws_entry = ttk.Entry(ws_row, textvariable=ws_var)
    ws_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))

    def pick_workspace():
        path = filedialog.askdirectory(
            initialdir=ws_var.get() or str(cfg.workspace),
            title="Choose the workspace for this prompt",
            mustexist=True)
        if path:
            ws_var.set(path)

    ttk.Button(ws_row, text="Browse…", command=pick_workspace).pack(side="left")

    # ---- mode toggles (editable) --------------------------------------
    toggles = ttk.Frame(win, padding=(12, 0, 12, 6))
    toggles.pack(fill="x")
    strict_v = tk.BooleanVar(value=True)
    auto_v = tk.BooleanVar(value=True)
    bg_v = tk.BooleanVar(value=True)
    ttk.Checkbutton(toggles, text="strict", variable=strict_v).pack(side="left")
    ttk.Checkbutton(toggles, text="auto-approve", variable=auto_v).pack(side="left", padx=(10, 0))
    ttk.Checkbutton(toggles, text="bg-shell", variable=bg_v).pack(side="left", padx=(10, 0))
    ttk.Label(toggles, text="(all on by default for single-prompt runs)",
              foreground="#666").pack(side="left", padx=(10, 0))

    # ---- prompt box ----------------------------------------------------
    ttk.Label(win, text="Prompt", padding=(12, 0, 12, 2)).pack(anchor="w")
    prompt_frame = ttk.Frame(win, padding=(12, 0, 12, 8))
    prompt_frame.pack(fill="both", expand=True)
    prompt = tk.Text(prompt_frame, height=6, wrap="word",
                     font=("Consolas", 10) if os.name == "nt" else ("Menlo", 11))
    prompt.pack(fill="both", expand=True)

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
    stop_btn = ttk.Button(btns, text="Stop", state="disabled")
    stop_btn.pack(side="right", padx=(0, 8))
    close_btn = ttk.Button(btns, text="Close", command=win.destroy)
    close_btn.pack(side="right", padx=(0, 8))

    # ---- run ----------------------------------------------------------
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

    state = {"running": False, "cancel": None, "thread": None}

    def do_stop():
        if state["cancel"] is not None:
            state["cancel"].set()
            append_out("\n[stop requested]\n")

    stop_btn.configure(command=do_stop)
    win.protocol("WM_DELETE_WINDOW",
                 lambda: (restore_prev(), win.destroy()))

    def do_run():
        if state["running"]:
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

        cfg.workspace = ws_path
        cfg.strict = bool(strict_v.get())
        cfg.auto_approve = bool(auto_v.get())
        _shell.set_default_background(bool(bg_v.get()))

        clear_out()
        append_out(f"[workspace] {ws_path}\n")
        append_out(f"[mode] strict={'on' if cfg.strict else 'off'}  "
                   f"auto-approve={'on' if cfg.auto_approve else 'off'}  "
                   f"bg-shell={'on' if _shell.default_background() else 'off'}\n\n")
        append_out("[running…]\n")

        run_btn.configure(state="disabled")
        stop_btn.configure(state="normal")
        state["running"] = True
        cancel = threading.Event()
        state["cancel"] = cancel

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

        def work():
            from . import loop as _loop, llm as _llm
            try:
                hist = _loop.new_history(cfg)
                final = _loop.run(cfg, hist, text,
                                  on_event=on_event, cancel=cancel)
                win.after(0, append_out, f"\n[done]\n{final}\n")
            except _llm.LLMError as e:
                win.after(0, append_out, f"\n[gateway error] {e}\n")
            except Exception as e:
                win.after(0, append_out, f"\n[unexpected] {type(e).__name__}: {e}\n")
            finally:
                def done():
                    state["running"] = False
                    state["cancel"] = None
                    run_btn.configure(state="normal")
                    stop_btn.configure(state="disabled")
                win.after(0, done)

        t = threading.Thread(target=work, daemon=True, name="omni-single-prompt")
        state["thread"] = t
        t.start()

    run_btn.configure(command=do_run)
    prompt.focus_set()
    return win


def _run_in_process(cfg, argv):
    from .__main__ import main as cli_main
    old = sys.argv
    try:
        sys.argv = ["omni"] + argv
        cli_main()
    finally:
        sys.argv = old


def _cwd() -> Path:
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
            pass

    def on_web():
        status.set("Starting the web UI…")
        root.update_idletasks()
        try:
            def _ready(url):
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

    ttk.Button(frm, text="Single prompt (auto everything)",
               command=on_single_prompt, width=48, padding=(8, 12)
               ).grid(row=3, column=0, columnspan=2, pady=(8, 0), sticky="ew")

    ttk.Separator(frm, orient="horizontal").grid(row=4, column=0, columnspan=2,
                                                 sticky="ew", pady=(16, 0))
    ttk.Button(frm, text="Quit", command=on_quit).grid(row=6, column=0, columnspan=2,
                                                       pady=(12, 0))

    ws_text = f"Workspace: {cfg.workspace}"
    ttk.Label(frm, text=ws_text, foreground="#888",
              wraplength=380, justify="left").grid(row=7, column=0, columnspan=2,
                                                   pady=(10, 0))

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    root.mainloop()
    return 0