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

# Detect the local URL a dev server prints on startup, so run_shell can
# return it in the FIRST call instead of forcing the model to poll.
_URL_RX = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d{2,5})?(?:/\S*)?",
    re.I,
)
_READY_HINTS = re.compile(
    r"\b(ready|listening|running on|serving|started server|local:|network:|"
    r"development server|compiled successfully|you can now view)\b",
    re.I,
)


# Strength of each hint phrase, used to pick the *best* URL when a log
# line contains more than one. A URL right after "ready in 312 ms" or
# "Local:" beats a URL that just happens to be near the word "warning:".
_HINT_WEIGHTS = [
    (re.compile(r"\b(ready in|ready at|listening on|running on|serving at|"
                r"started server|compiled successfully|you can now view)\b", re.I), 3),
    (re.compile(r"\b(local:|network:|development server|local\s+->)\b", re.I), 2),
    (re.compile(r"\b(ready|listening|running|serving)\b", re.I), 1),
]


def _hint_score(window: str) -> int:
    for rx, score in _HINT_WEIGHTS:
        if rx.search(window):
            return score
    return 0


def _scan_for_url(text: str):
    """Return the *best-scored* localhost URL in `text`.

    When a log line contains more than one URL, the one nearest the
    strongest "ready" hint wins. Ties on score are broken by preferring
    the LAST matching URL: dev servers print a decoy "see help at ..."
    line before the real "Local:"/listening address, so the later URL
    is the one you actually want.
    """
    best_url = None
    best_score = -1
    for m in _URL_RX.finditer(text):
        url = m.group(0)
        before = text[max(0, m.start() - 120): m.start()]
        after = text[m.end(): m.end() + 120]
        score = max(_hint_score(before), _hint_score(after))
        if score >= best_score:          # >= so a later URL wins on a tie
            best_score = score
            best_url = url
            if score >= 3:
                break  # can't do better
    return best_url


# Foreground processes currently blocking this session. kill_all() kills these.
_ACTIVE: dict = {}
# Detached processes (background=True). kill_all() does NOT touch these.
_DETACHED: dict = {}     # id -> {proc, cmd, started, log_path, last_read, last_read_at}
_ACTIVE_LOCK = threading.RLock()

_tl = threading.local()

# Global default for "run everything in the background". Set by the UI
# toggle or /bgauto on. Defaults to OFF so a first-time user sees live
# output for short commands, and matches .env.example / README §6.2.
_DEFAULT_BG = False
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
            # Collapse \r\n to \n and turn bare \r into \n so progress bars
            # (npm, pip, cargo) render as distinct lines in the browser
            # instead of one concatenated blob.
            text = text.replace("\r\n", "\n").replace("\r", "\n")
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
    now = time.time()
    with _ACTIVE_LOCK:
        # Prune exited jobs older than 10 minutes so a long session's
        # _DETACHED dict doesn't grow without bound.
        for jid in list(_DETACHED):
            r = _DETACHED[jid]
            if r["proc"].poll() is not None and now - r["started"] > 600:
                del _DETACHED[jid]
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
    """Spawn `command` and return immediately. Output goes to a log file.

    Detects a localhost URL printed by dev servers in the first ~4s and
    returns it in the result, so the model rarely needs a second call.
    """
    job_id = uuid.uuid4().hex[:8]
    log_dir = cfg.data_dir / "jobs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{job_id}.log"
    log_f = log_path.open("wb")
    try:
        proc = subprocess.Popen(command, cwd=str(cwd), shell=True,
                                stdout=log_f, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    except Exception as e:
        log_f.close()
        return f"ERROR: could not start detached command: {e}"

    with _ACTIVE_LOCK:
        _DETACHED[job_id] = {
            "proc": proc, "cmd": command, "started": time.time(),
            "log_path": log_path, "last_read": 0, "last_read_at": 0.0,
        }

    # Wait briefly for a startup URL so we can return it right away.
    ready_url = None
    deadline = time.time() + 4.0
    while time.time() < deadline:
        try:
            if log_path.stat().st_size > 0:
                data = log_path.read_text(encoding="utf-8", errors="replace")
                ready_url = _scan_for_url(data)
                if ready_url:
                    break
        except OSError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.2)

    msg = (f"background job {job_id} started (pid {proc.pid}).\n"
           f"cmd: {command}\n"
           f"log: {log_path}\n")
    if ready_url:
        msg += (f"DETECTED URL: {ready_url}\n"
                f"The server appears to be up. Report this URL to the user; "
                f"you do NOT need to call tail_background unless you want more detail.\n")
    else:
        msg += (f"Use wait_background(job_id=\"{job_id}\", timeout=20) ONCE to read "
                f"startup output. Do NOT poll tail_background repeatedly.\n")
    msg += "This job will NOT be killed by Stop."
    return msg


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
    #   otherwise fall back to the global default (env-configured at startup)
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

    def _read_chunks(stream):
        """Yield text chunks as they arrive, splitting on \\n and \\r."""
        fd = stream.fileno()
        buf = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except (BlockingIOError, OSError):
                chunk = b""
            if chunk:
                buf += chunk
                while b"\n" in buf or b"\r" in buf:
                    i_n = buf.find(b"\n")
                    i_r = buf.find(b"\r")
                    i = min(x for x in (i_n, i_r) if x >= 0)
                    line, buf = buf[:i + 1], buf[i + 1:]
                    yield line.decode("utf-8", errors="replace")
            else:
                if buf:
                    yield buf.decode("utf-8", errors="replace")
                    buf = b""
                yield ""

    try:
        for text in _read_chunks(proc.stdout):
            if cancel is not None and cancel.is_set():
                _kill_tree(proc)
                return (f"job {job_id} CANCELLED by user after {time.time() - start:.1f}s\n"
                        f"--output so far--\n{''.join(lines)[-4000:]}")
            if time.time() - start > timeout:
                _kill_tree(proc)
                return (f"job {job_id} TIMED OUT after {timeout}s\n"
                        f"--output so far--\n{''.join(lines)[-4000:]}")
            if text:
                lines.append(text)
                _emit(text, id=job_id)
            elif proc.poll() is not None:
                break
            else:
                time.sleep(0.02)
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
    "background=true). Returns only NEW output since the last check, plus a "
    "one-line 'no new output' message if nothing changed. Use this to check "
    "whether a dev server is up, print its URL, read its error log, etc.",
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
        size = p.stat().st_size
    except OSError:
        return f"log for {job_id} is not readable"

    alive = rec["proc"].poll() is None
    new_bytes = size - rec.get("last_read", 0)

    if new_bytes <= 0:
        rec["last_read_at"] = time.time()
        return (f"job {job_id} ({'running' if alive else 'exited'}) "
                f"— no new output since last check. "
                f"{'Still booting.' if alive else 'Process has exited.'}")

    with p.open("rb") as f:
        f.seek(max(0, size - min(new_bytes, 50_000)))
        data = f.read().decode("utf-8", errors="replace")
    rec["last_read"] = size
    rec["last_read_at"] = time.time()

    tail = "\n".join(data.splitlines()[-max(1, min(int(lines or 200), 2000)):])
    return (f"job {job_id} ({'running' if alive else 'exited'}) "
            f"pid={rec['proc'].pid} cmd={rec['cmd']}\n"
            f"--- new output since last check ---\n{tail or '(no output)'}")


@tool(
    "wait_background",
    "Block until a background job produces new output or exits, up to `timeout` "
    "seconds. Returns immediately if there is already new output since the last "
    "check. Use this ONCE after starting a dev server, instead of polling "
    "tail_background repeatedly.",
    {"job_id": {"type": "string"}, "timeout": {"type": "integer"}, "lines": {"type": "integer"}},
    ["job_id"],
)
def wait_background(cfg, job_id, timeout=30, lines=200):
    with _ACTIVE_LOCK:
        rec = _DETACHED.get(job_id)
    if not rec:
        return f"no background job {job_id}"

    timeout = max(1, min(int(timeout or 30), 120))
    deadline = time.time() + timeout
    p = rec["log_path"]

    try:
        if p.stat().st_size > rec.get("last_read", 0):
            return tail_background(cfg, job_id, lines)
    except OSError:
        pass

    while time.time() < deadline:
        c = get_cancel()
        if c is not None and c.is_set():
            return f"job {job_id}: wait cancelled by user"
        try:
            size = p.stat().st_size
        except OSError:
            size = rec.get("last_read", 0)
        if size > rec.get("last_read", 0):
            return tail_background(cfg, job_id, lines)
        if rec["proc"].poll() is not None:
            return tail_background(cfg, job_id, lines)
        time.sleep(0.25)

    alive = rec["proc"].poll() is None
    return (f"job {job_id} ({'still running' if alive else 'exited'}) "
            f"— no new output after {timeout}s. "
            f"{'It may be waiting for input or just quiet.' if alive else ''}")


@tool(
    "stop_background",
    "Stop a background job started with run_shell background=true. "
    "This is how you shut down a dev server the user asked you to keep running.",
    {"job_id": {"type": "string"}},
    ["job_id"],
)
def stop_background(cfg, job_id):
    return kill_detached(job_id)