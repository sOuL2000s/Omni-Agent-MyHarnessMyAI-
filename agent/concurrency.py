# agent/concurrency.py
"""Concurrent task graph for the `parallel_tasks` tool.

A Task is a callable that returns a string (same contract as a tool). A
TaskGraph is a list of Tasks with optional `needs=[id,...]` dependencies.
We run them on a bounded thread pool, honor cancellation, retry transient
failures, and return a merged, human-readable report.

Why a custom graph and not asyncio?
  * Every tool in Omni is synchronous (urllib, subprocess, docx). Wrapping
    them in `loop.run_in_executor` gains nothing over ThreadPoolExecutor.
  * Threads share the existing thread-local cancel event and shell emitter
    correctly, so Stop works in parallel tasks for free.
  * asyncio would require rewriting llm.py, shell.py, web.py — high risk,
    low reward.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from . import logs


@dataclass
class Task:
    id: str
    tool: str
    args: dict
    needs: list[str] = field(default_factory=list)
    retries: int = 1
    timeout: int = 0            # 0 = no per-task cap (shell enforces its own)

    def to_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool, "args": self.args,
                "needs": self.needs, "retries": self.retries}


@dataclass
class Result:
    id: str
    ok: bool
    output: str
    ms: int
    attempts: int = 1


def _run_one(cfg, call_fn: Callable, task: Task, cancel, emitter) -> Result:
    attempts = 0
    last = ""
    t0 = time.time()
    while attempts <= task.retries:
        attempts += 1
        if cancel is not None and cancel.is_set():
            return Result(task.id, False, "ERROR: cancelled by user", int((time.time() - t0) * 1000), attempts)
        try:
            out = call_fn(cfg, task.tool, json.dumps(task.args))
            ok = not (isinstance(out, str) and out.startswith("ERROR"))
            if ok:
                return Result(task.id, True, out, int((time.time() - t0) * 1000), attempts)
            last = out
        except Exception as e:
            last = f"ERROR: {type(e).__name__}: {e}"
            logs.warn(f"parallel task {task.id} ({task.tool}) error: {last[:200]}")
        # small backoff before retry
        if attempts <= task.retries:
            if cancel is not None and cancel.is_set():
                return Result(task.id, False, "ERROR: cancelled by user", int((time.time() - t0) * 1000), attempts)
            time.sleep(min(0.5 * attempts, 2.0))
    return Result(task.id, False, last or "ERROR: failed", int((time.time() - t0) * 1000), attempts)


def run_graph(cfg, call_fn, tasks: list[Task], cancel=None, max_workers: int = 4,
              emitter=None) -> list[Result]:
    """Execute the graph. Returns results in the original task order."""
    by_id = {t.id: t for t in tasks}
    done: dict[str, Result] = {}
    emitted = {"n": 0}

    def emit_progress():
        if emitter is None:
            return
        emitted["n"] += 1
        try:
            total = len(tasks)
            finished = len(done)
            pending = total - finished
            emitter("progress", f"parallel: {finished}/{total} done, {pending} running",
                    title="Parallel tasks",
                    detail=f"{finished}/{total} complete")
        except Exception:
            pass

    def wait_for_needs(t: Task) -> bool:
        for n in t.needs:
            while n not in done:
                if cancel is not None and cancel.is_set():
                    return False
                time.sleep(0.05)
        return True

    # Wave scheduling: loop until everything has been dispatched or cancelled.
    pending = list(tasks)
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 8)), thread_name_prefix="omni-task") as ex:
        futures = {}
        while pending or futures:
            if cancel is not None and cancel.is_set():
                break
            # dispatch anything whose deps are satisfied
            still = []
            for t in pending:
                if all(n in done for n in t.needs):
                    futures[ex.submit(_run_one, cfg, call_fn, t, cancel, emitter)] = t
                else:
                    still.append(t)
            pending = still
            if not futures:
                if pending:
                    # dependency cycle or missing dep
                    for t in pending:
                        done[t.id] = Result(t.id, False,
                                            f"ERROR: dependency not met: {', '.join(t.needs)}", 0)
                    pending = []
                break
            # block for at least one
            for fut in as_completed(list(futures), timeout=None):
                t = futures.pop(fut)
                try:
                    r = fut.result()
                except Exception as e:
                    r = Result(t.id, False, f"ERROR: {e}", 0)
                done[t.id] = r
                emit_progress()
                break  # loop back to dispatch more
        # cancel remaining futures' scheduling
        for fut, t in futures.items():
            fut.cancel()
            done.setdefault(t.id, Result(t.id, False, "ERROR: cancelled by user", 0))

    return [done.get(t.id, Result(t.id, False, "ERROR: not run", 0)) for t in tasks]