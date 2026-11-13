# agent/tools/parallel.py
"""parallel_tasks: run several tool calls concurrently, respecting deps."""
import json
from .registry import tool, call as _call, get_cancel
from ..concurrency import Task, run_graph
from .. import learning


@tool(
    "parallel_tasks",
    "Run several tool calls concurrently. Each task has its own `id`. "
    "Use `needs` to depend on another task's output. Returns each task's "
    "output, ok flag, and elapsed ms. Use this when subtasks are independent "
    "(read 3 files, fetch 2 URLs, run a grep and a list in parallel).",
    {"tasks": {"type": "array", "items": {"type": "object",
        "properties": {
            "id": {"type": "string", "description": "unique id for this task"},
            "tool": {"type": "string", "description": "tool name to call"},
            # Gemini rejects an `object` with no `properties`; give it an
            # empty properties map AND an explicit description.
            "args": {"type": "object", "properties": {},
                     "description": "arguments object for the tool"},
            "needs": {"type": "array", "items": {"type": "string"},
                      "description": "ids of tasks that must finish first"},
            "retries": {"type": "integer",
                        "description": "retry count on failure (default 1)"},
        },
        "required": ["id", "tool", "args"]}},
     "max_workers": {"type": "integer", "description": "threads to use (default 4)"}},
    ["tasks"],
)
def parallel_tasks(cfg, tasks, max_workers=4):
    if not isinstance(tasks, list) or not tasks:
        return "ERROR: `tasks` must be a non-empty list"
    parsed = []
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            return f"ERROR: task {i} is not an object"
        parsed.append(Task(
            id=str(t.get("id") or f"t{i + 1}"),
            tool=str(t.get("tool") or ""),
            args=t.get("args") or {},
            needs=[str(x) for x in (t.get("needs") or [])],
            retries=int(t.get("retries") or 1),
        ))
    if any(not t.tool for t in parsed):
        return "ERROR: every task needs a `tool`"
    ids = [t.id for t in parsed]
    if len(set(ids)) != len(ids):
        return "ERROR: task ids must be unique"
    for t in parsed:
        for n in t.needs:
            if n not in ids:
                return f"ERROR: task {t.id} needs unknown task '{n}'"
    cancel = get_cancel()
    results = run_graph(cfg, _call, parsed, cancel=cancel, max_workers=max_workers)
    lines = [f"{len(results)} task(s) finished:"]
    for r in results:
        mark = "ok" if r.ok else "FAIL"
        snippet = (r.output or "").replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:280] + "…"
        lines.append(f"[{mark}] {r.id} ({r.ms} ms, {r.attempts} attempt(s)): {snippet}")
        learning.observe_tool(f"parallel:{r.id}", {}, r.ok, r.ms)
    # full outputs are long; include them if the total is reasonable
    if sum(len(r.output or "") for r in results) < 6000:
        lines.append("\n--- full outputs ---")
        for r in results:
            lines.append(f"### {r.id}\n{r.output}")
    return "\n".join(lines)