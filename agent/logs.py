# agent/logs.py
"""Backend-only logging. The web UI never displays these lines; they go to
the terminal (stderr) and to data/logs/omni.log with daily rotation.

Why this module exists
----------------------
Before, `llm.chat_ex` called `notify(...)` which the loop forwarded straight
to the chat as a "notice" event. A rate-limited key produced 8 lines of
"gemini-3 [gemini-2.5-flash] rate: ..." in the user's face. That was the
"messy UI" complaint. Now:

  * `llm` calls `log.notice(...)` / `log.debug(...)` → backend only.
  * The loop emits a single soft event ("trying an alternative route") at
    most once per 20 s so the user knows something is happening.
  * The web UI can still open the full log via /api/logs/tail (a "Show
    diagnostics" link) for power users.

Design
------
* Thread-safe.
* Never raises (logging must not break a request).
* Redacts common secret shapes before writing.
* Rotation: 5 MB per file, 3 files, in data/logs/.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys
import threading
from collections import deque
from pathlib import Path

_LOCK = threading.RLock()
_RECENT = deque(maxlen=2000)          # in-memory ring for /api/logs/tail
_MAX_BYTES = 5 * 1024 * 1024
_KEEP_FILES = 3
_LOG_DIR: Path | None = None

# Redaction patterns — conservative; we'd rather over-redact than leak.
_REDACT = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"), "sk-***"),
    (re.compile(r"(AIza[0-9A-Za-z_\-]{20,})"), "AIza***"),
    (re.compile(r"(gsk_[A-Za-z0-9]{20,})"), "gsk_***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{12,}", re.I), r"\1***"),
    (re.compile(r"(api[_-]?key[\"'\s:=]+)[A-Za-z0-9._\-]{8,}", re.I), r"\1***"),
]


def _redact(text: str) -> str:
    if not text:
        return text
    for rx, repl in _REDACT:
        text = rx.sub(repl, text)
    return text


def configure(data_dir: Path | None) -> None:
    """Called once by config.load(). Safe to call with None (terminal only)."""
    global _LOG_DIR
    if data_dir is None:
        _LOG_DIR = None
        return
    try:
        d = Path(data_dir) / "logs"
        d.mkdir(parents=True, exist_ok=True)
        _LOG_DIR = d
    except OSError:
        _LOG_DIR = None


def _path() -> Path | None:
    if _LOG_DIR is None:
        return None
    try:
        return _LOG_DIR / "omni.log"
    except Exception:
        return None


def _rotate_if_needed(p: Path) -> None:
    try:
        if not p.exists() or p.stat().st_size < _MAX_BYTES:
            return
    except OSError:
        return
    for i in range(_KEEP_FILES - 1, 0, -1):
        src = p.with_suffix(f".{i}.log")
        dst = p.with_suffix(f".{i + 1}.log")
        try:
            if src.exists():
                os.replace(src, dst)
        except OSError:
            pass
    try:
        os.replace(p, p.with_suffix(".1.log"))
    except OSError:
        pass


def _write(level: str, text: str) -> None:
    text = _redact(text)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {level:<7} {text}\n"
    with _LOCK:
        _RECENT.append(line.rstrip("\n"))
        # Terminal first — visible to whoever ran `omni ui`.
        try:
            stream = sys.stderr if level in ("WARN", "ERROR") else sys.stdout
            stream.write(line)
            stream.flush()
        except Exception:
            pass
        p = _path()
        if p is not None:
            try:
                _rotate_if_needed(p)
                with p.open("a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass


def info(text: str) -> None:
    _write("INFO", text)


def notice(text: str) -> None:
    _write("NOTICE", text)


def warn(text: str) -> None:
    _write("WARN", text)


def error(text: str) -> None:
    _write("ERROR", text)


def debug(text: str) -> None:
    if os.environ.get("OMNI_DEBUG") == "1":
        _write("DEBUG", text)


def recent(n: int = 200) -> list[str]:
    with _LOCK:
        return list(_RECENT)[-max(1, min(int(n or 200), 2000)):]