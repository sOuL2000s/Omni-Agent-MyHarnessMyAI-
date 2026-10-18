# agent/tools/shell.py
"""Cancellable, streaming shell execution.

* Runs each command in its own process group, so killing it kills children too.
* Streams stdout line-by-line through a per-thread emitter so the UI can show
  progress as it happens.
* Polls a per-thread threading.Event every 50 ms so the Stop button works even
  while a long command is running.
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
# It will NOT catch things like `mv ~/Documents /tmp/x` or base64-encoded
# payloads; it only stops the obvious catastrophic typos.
BLOCK = [
    r"rm\s+-rf\s+/(\s|$)",                       # rm -rf /
    r"rm\s+-rf\s+/\*",                           # rm -rf /*
    r"rm\s+-rf\s+~\s*(&|;|$)",                   # rm -rf ~
    r"rm\s+-rf\s+\$HOME",                        # rm -rf $HOME
    r"--no-preserve-root",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:", # fork bomb
    r"dd\s+if=.*of=/dev/(sd|nvme|hd|vd|disk)",
    r"\bshred\b.*(/dev/|/etc/|/boot/)",
    r"chmod\s+-R\s+0+\s+/",                      # chmod -R 000 /
    r"chown\s+-R\s+.*\s+/",                      # chown -R ... /
    r">\s*/dev/(sd|nvme|hd|vd)",                 # clobber a disk
    r"\bformat\s+[a-z]:",                        # Windows format C:
    r"\bdel\s+/[sf]\s+/[sf]\s+[a-z]:\\",         # Windows del /s /q C:\
]

_ACTIVE: dict = {}
_ACTIVE_LOCK = threading.RLock()

# Set by loop.run() to a callable(kind, text, **extra) that fans out to the UI.
_tl = threading.local()


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
    """Called by Stop and on session teardown."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE.values())
    for p in procs:
        _kill_tree(p)
    with _ACTIVE_LOCK:
        _ACTIVE.clear()


def active_jobs() -> list:
    with _ACTIVE_LOCK:
        return list(_ACTIVE.keys())


@tool(
    "run_shell",
    "Run a shell command in the workspace. Long commands stream output and "
    "can be cancelled with Stop. NOTE: the blocklist only stops obvious "
    "catastrophes; approval prompts are the real guard.",
    {"command": {"type": "string"},
     "timeout": {"type": "integer"},
     "background": {"type": "boolean"},
     "cwd": {"type": "string"}},
    ["command"],
)
def run_shell(cfg, command, timeout=120, background=False, cwd=None):
    for b in BLOCK:
        if re.search(b, command):
            raise Denied("Command blocked by safety rules.")

    confirm(cfg, f"Run shell: {command}", tool="run_shell")

    wd = cfg.workspace if not cwd else (cfg.workspace / cwd).resolve()

    kwargs = dict(cwd=str(wd), shell=True, text=True,
                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  bufsize=1)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["preexec_fn"] = os.setsid

    job_id = uuid.uuid4().hex[:8]
    try:
        proc = subprocess.Popen(command, **kwargs)
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

    out = "".join(lines)
    if background:
        return f"job {job_id} started in background; output so far:\n{out[-2000:]}"
    return f"exit={code}\n--output--\n{out}"