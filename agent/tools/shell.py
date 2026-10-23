# agent/tools/shell.py
"""Cancellable, streaming shell execution.

Three ways to run:

* foreground (default)  — streams stdout live, cancellable with Stop.
* background=True       — returns immediately with a job id; the process
                          keeps running on its own after Omni's Stop and
                          after the session ends. Use this for dev servers
                          (`npm run dev`, `flask run`, `uvicorn ...`).
* background-by-default — set the global toggle (UI header or
                          `/bgauto on`) and every run_shell call that does
                          not explicitly say `background=false` will run
                          detached. Perfect for a "no foreground hang" mode.

Detached jobs are tracked in `_DETACHED` and are NEVER touched by
`kill_all()`, which is what `Session.stop()` calls. That means pressing
Stop does not kill a dev server that was started in the background.
Use the `stop_background` tool or `/kill <id>` to stop one explicitly.
"""
import os
import re
import signal
import subprocess
import threading
import time
import uuid

from .registry import tool, get_cancel
from ..safety import confirm, Denied

# This is a SPEED BUMP, not a guard. Approval prompts are the real safety net.
BLOCK = [
    r"rm\s+-rf\s+/(\s|$)",
    r"rm\s+-rf\s+/\*",
    r"rm\s+-rf\s+~\s*(&|;|$)",
    r"rm\s+-rf\s+\$HOME",
    r"--no-preserve-root",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:",
    r"dd\s+if=.*of=/dev/(sd|nvme|hd|vd|disk)",
    r"\bshred\b.*(/dev/|/etc/|/boot/)",
    r"chmod\s+-R\s+0+\s+/",
    r"chown\s+-R\s+.*\s+/",
    r">\s*/dev/(sd|nvme|hd|vd)",
    r"\bformat\s+[a-z]:",
    r"\bdel\s+/[sf]\s+/[sf]\s+[a-z]:\\",
]

# Foreground processes currently blocking this session. kill_all() kills these.
_ACTIVE: dict = {}
# Detached processes (background=True). kill_all() does NOT touch these.
_DETACHED: dict = {}     # id -> {proc, cmd, started, log_path}
_ACTIVE_LOCK = threading.RLock()

_tl = threading.local()

# Global default for "run everything in the background". Set by the UI
# toggle or /bgauto on. Defaults to off so a first-time user still sees
# live output for short commands.
_DEFAULT_BG = True
_DEFAULT_LOCK = threading.Lock()


def default_background() -> bool:
    with _DEFAULT_LOCK:
        return _DEFAULT_BG


def set_default_background(on: bool) -> None:
    global _DEFAULT_BG
    with _DEFAULT_LOCK:
        _DEFAULT_BG = bool(on)


def set_emitter(fn):
    _tl.emit = fn


def _emit(text, **extra):
    fn = getattr(_tl, "emit", None)
    if fn:
        try:
            fn("tool_stream", text, **extra)
        except Exception:
            pass


def _kill_tree(p: subprocess.Popen):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=5)
        else:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except ProcessLookupError:
                return
            time.sleep(0.4)
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:
        pass


def kill_all():
    """Called by Session.stop() and on session teardown.

    Kills ONLY foreground processes. Detached (background=True) processes
    are deliberately left alone — that's how a dev server survives Stop.
    """
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE.values())
    for p in procs:
        _kill_tree(p)
    with _ACTIVE_LOCK:
        _ACTIVE.clear()


def kill_detached(job_id: str) -> str:
    """Stop a backgrounded job by id. Returns a short human message."""
    with _ACTIVE_LOCK:
        rec = _DETACHED.pop(job_id, None)
    if not rec:
        return f"no detached job {job_id}"
    _kill_tree(rec["proc"])
    return f"stopped background job {job_id}"


def list_detached() -> list:
    with _ACTIVE_LOCK:
        return [{"id": jid, "cmd": r["cmd"], "started": r["started"],
                 "alive": r["proc"].poll() is None,
                 "log": str(r["log_path"]) if r["log_path"] else ""}
                for jid, r in _DETACHED.items()]


def _shell_kwargs(cwd):
    kw = dict(cwd=str(cwd), shell=True, text=True,
              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["preexec_fn"] = os.setsid
    return kw


def _run_detached(cfg, command, cwd):
    """Spawn `command` and return immediately. Output goes to a log file."""
    job_id = uuid.uuid4().hex[:8]
    log_dir = cfg.data_dir / "jobs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{job_id}.log"
    # We redirect the shell's own stdout/stderr to the log file *and* keep a
    # pipe so we can still peek at it, but the pipe is drained by a daemon
    # thread that writes to the file. This way the child is not blocked by an
    # unread pipe if the model stops reading.
    log_f = log_path.open("wb")
    try:
        proc = subprocess.Popen(command, cwd=str(cwd), shell=True,
                                stdout=log_f, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    except Exception as e:
        log_f.close()
        return f"ERROR: could not start detached command: {e}"

    with _ACTIVE_LOCK:
        _DETACHED[job_id] = {"proc": proc, "cmd": command,
                             "started": time.time(), "log_path": log_path}

    return (f"background job {job_id} started (pid {proc.pid}).\n"
            f"cmd: {command}\n"
            f"log: {log_path}\n"
            f"Alive check in ~2s, then use `tail_background(job_id=\"{job_id}\")` "
            f"to read output, or `stop_background(job_id=\"{job_id}\")` to stop it.\n"
            f"This job will NOT be killed by Stop.")


@tool(
    "run_shell",
    "Run a shell command in the workspace. "
    "For long-running servers use background=true: it returns immediately "
    "with a job id and the process keeps running (Stop will NOT kill it). "
    "Foreground commands stream output and can be cancelled with Stop. "
    "If the user has enabled the global background-shell toggle, a call "
    "with background omitted will run detached.",
    {"command": {"type": "string"},
     "timeout": {"type": "integer"},
     "background": {"type": "boolean"},
     "cwd": {"type": "string"}},
    ["command"],
)
def run_shell(cfg, command, timeout=120, background=None, cwd=None):
    for b in BLOCK:
        if re.search(b, command):
            raise Denied("Command blocked by safety rules.")

    # Resolve the effective background flag:
    #   explicit true/false wins
    #   otherwise fall back to the global default
    if background is None:
        background = default_background()

    confirm(cfg, f"Run shell: {command}", tool="run_shell")

    wd = cfg.workspace if not cwd else (cfg.workspace / cwd).resolve()

    if background:
        return _run_detached(cfg, command, wd)

    job_id = uuid.uuid4().hex[:8]
    try:
        proc = subprocess.Popen(command, **_shell_kwargs(wd))
    except Exception as e:
        return f"ERROR: could not start command: {e}"

    with _ACTIVE_LOCK:
        _ACTIVE[job_id] = proc

    cancel = get_cancel()
    lines = []
    start = time.time()

    try:
        while True:
            if cancel is not None and cancel.is_set():
                _kill_tree(proc)
                return (f"job {job_id} CANCELLED by user after {time.time() - start:.1f}s\n"
                        f"--output so far--\n{''.join(lines)[-4000:]}")
            if time.time() - start > timeout:
                _kill_tree(proc)
                return (f"job {job_id} TIMED OUT after {timeout}s\n"
                        f"--output so far--\n{''.join(lines)[-4000:]}")
            line = proc.stdout.readline()
            if line:
                lines.append(line)
                _emit(line, id=job_id)
                continue
            if proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    lines.append(rest)
                    _emit(rest, id=job_id)
                break
            time.sleep(0.05)
        code = proc.returncode
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(job_id, None)
        try:
            proc.stdout.close()
        except Exception:
            pass

    return f"exit={code}\n--output--\n{''.join(lines)}"


@tool(
    "tail_background",
    "Read the recent output of a background job (started with run_shell "
    "background=true). Use this to check whether a dev server is up, print "
    "its URL, read its error log, etc.",
    {"job_id": {"type": "string"}, "lines": {"type": "integer"}},
    ["job_id"],
)
def tail_background(cfg, job_id, lines=200):
    with _ACTIVE_LOCK:
        rec = _DETACHED.get(job_id)
    if not rec:
        # Also allow a prefix match so the model can type a short id.
        with _ACTIVE_LOCK:
            for jid, r in _DETACHED.items():
                if jid.startswith(job_id):
                    rec, job_id = r, jid
                    break
    if not rec:
        return f"no background job {job_id}"
    p = rec["log_path"]
    try:
        data = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"could not read log: {e}"
    tail = "\n".join(data.splitlines()[-max(1, min(int(lines or 200), 2000)):])
    alive = rec["proc"].poll() is None
    return (f"job {job_id} ({'running' if alive else 'exited'}) "
            f"pid={rec['proc'].pid} cmd={rec['cmd']}\n"
            f"--- last {lines} lines ---\n{tail or '(no output yet)'}")


@tool(
    "stop_background",
    "Stop a background job started with run_shell background=true. "
    "This is how you shut down a dev server the user asked you to keep running.",
    {"job_id": {"type": "string"}},
    ["job_id"],
)
def stop_background(cfg, job_id):
    return kill_detached(job_id)