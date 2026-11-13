# agent/tools/automation.py
"""Automation tools: schedule_task, list_schedules, cancel_schedule."""
from .registry import tool
from .. import scheduler, learning
from ..safety import confirm


@tool(
    "schedule_task",
    "Schedule a natural-language prompt to run repeatedly (e.g. daily). "
    "The job survives restarts. Use this when the user says 'every day', "
    "'every hour', 'run this again tomorrow', etc. Asks for approval.",
    {"name": {"type": "string"},
     "prompt": {"type": "string"},
     "every_seconds": {"type": "integer"},
     "first_delay_seconds": {"type": "integer"}},
    ["name", "prompt", "every_seconds"],
)
def schedule_task(cfg, name, prompt, every_seconds, first_delay_seconds=0):
    confirm(cfg, f"Schedule '{name}' every {every_seconds}s: {prompt[:200]}",
            tool="schedule_task")
    job = scheduler.add(name, prompt, int(every_seconds), int(first_delay_seconds or 0))
    return (f"Scheduled '{job['name']}' (id {job['id']}) every {job['every']}s. "
            f"Next run in ~{max(0, int(job['next_at'] - __import__('time').time()))}s. "
            f"Use cancel_schedule(id) to remove it.")


@tool(
    "list_schedules",
    "List every scheduled recurring task with its next run time and last status.",
    {},
    [],
)
def list_schedules(cfg):
    jobs = scheduler.list_jobs()
    if not jobs:
        return "No scheduled jobs."
    import time as _t
    lines = []
    for j in jobs:
        nxt = max(0, int(j.get("next_at", 0) - _t.time()))
        lines.append(
            f"{j['id']}  {j['name']}  every {j['every']}s  next in {nxt}s  "
            f"runs={j.get('runs', 0)}  last={j.get('last_status') or 'never'}"
        )
    return "\n".join(lines)


@tool(
    "cancel_schedule",
    "Cancel a scheduled recurring task by id. Asks for approval.",
    {"id": {"type": "string"}},
    ["id"],
)
def cancel_schedule(cfg, id):
    confirm(cfg, f"Cancel scheduled job {id}", tool="cancel_schedule")
    return scheduler.remove(id)


@tool(
    "note_correction",
    "Record that the previous answer was wrong or rejected. Use this when "
    "the user says 'no', 'wrong', 'not that', or corrects you, so the agent "
    "learns the shape of what failed. Does not ask.",
    {"reason": {"type": "string"}},
    ["reason"],
)
def note_correction(cfg, reason):
    learning.note_correction(reason)
    return "correction recorded"