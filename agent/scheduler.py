# agent/scheduler.py
"""Durable, restart-safe scheduler for recurring agent tasks.

Jobs live in data/schedules.json and are executed by a daemon thread
started from config.load(). A job is:

    {"id": "abc123", "name": "daily-notes", "prompt": "...", "every": 86400,
     "next_at": 1712345678.0, "created_at": ..., "last_run": null,
     "last_status": null, "runs": 0}

The executor calls `loop.run(cfg, new_history, prompt)`. Auto-approve is
honored (if the process runs with AGENT_AUTO_APPROVE=1, jobs run
unattended). Otherwise approvals block until the 120 s timeout, which is
correct: an unattended schedule must not silently do dangerous things.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from . import logs

_LOCK = threading.RLock()
_PATH: Path | None = None
_STOP = threading.Event()
_WORKER: threading.Thread | None = None
_jobs: dict[str, dict] = {}


def configure(data_dir: Path | None) -> None:
    global _PATH
    if data_dir is None:
        return
    _PATH = Path(data_dir) / "schedules.json"
    _load()


def _load() -> None:
    global _jobs
    if _PATH is None or not _PATH.exists():
        return
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            _jobs = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except Exception as e:
        logs.warn(f"scheduler: could not load schedules: {e}")


def _save() -> None:
    if _PATH is None:
        return
    with _LOCK:
        try:
            tmp = _PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(_jobs, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _PATH)
        except OSError as e:
            logs.warn(f"scheduler: could not save schedules: {e}")


def add(name: str, prompt: str, every: int, first_delay: int = 0) -> dict:
    every = max(30, int(every or 0))
    jid = uuid.uuid4().hex[:8]
    now = time.time()
    job = {
        "id": jid, "name": (name or "job")[:80], "prompt": prompt,
        "every": every, "next_at": now + max(0, int(first_delay or 0)),
        "created_at": now, "last_run": None, "last_status": None, "runs": 0,
    }
    with _LOCK:
        _jobs[jid] = job
        _save()
    logs.notice(f"scheduler: added '{job['name']}' every {every}s (id {jid})")
    return job


def remove(jid: str) -> str:
    with _LOCK:
        j = _jobs.pop(jid, None)
        _save()
    return f"removed {jid}" if j else f"no job {jid}"


def list_jobs() -> list[dict]:
    with _LOCK:
        return sorted(_jobs.values(), key=lambda j: j.get("next_at", 0))


def _worker_loop(cfg) -> None:
    from . import loop as _loop
    while not _STOP.wait(5):
        now = time.time()
        due = []
        with _LOCK:
            for j in _jobs.values():
                if j.get("next_at", now + 1) <= now:
                    due.append(j)
        for j in due:
            try:
                logs.notice(f"scheduler: running '{j['name']}'")
                final = _loop.run(cfg, _loop.new_history(cfg), j["prompt"])
                status = "ok"
                logs.info(f"scheduler: '{j['name']}' -> {str(final)[:160]}")
            except Exception as e:
                status = f"error: {e}"
                logs.warn(f"scheduler: '{j['name']}' failed: {e}")
            with _LOCK:
                j["last_run"] = now
                j["last_status"] = status
                j["runs"] = int(j.get("runs") or 0) + 1
                j["next_at"] = now + int(j["every"])
                _save()


def start_worker(cfg) -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _STOP.clear()
        _WORKER = threading.Thread(target=_worker_loop, args=(cfg,),
                                   daemon=True, name="omni-scheduler")
        _WORKER.start()


def stop_worker() -> None:
    _STOP.set()
    if _WORKER is not None:
        _WORKER.join(timeout=2.0)