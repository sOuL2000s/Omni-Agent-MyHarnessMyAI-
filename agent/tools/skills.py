# agent/tools/skills.py
"""Reusable skill recipes the agent can save, list, read and delete.

A skill is a Markdown file under `data/skills/<name>.md`. The first
level-1 heading becomes the title; the rest of the file is the body the
model reads when it "loads" the skill.

Skills exist so the model can stop re-deriving the same multi-step
workflow on every session. Instead of "here's how I do a code review"
appearing in every prompt, the model calls skill_list, sees
`code_review: Review a project and write REVIEW.md`, calls
skill_read("code_review"), and gets the exact steps back.

Storage is intentionally trivial: one file per skill, no schema, no
database. It's greppable, git-friendly, and easy to back up.
"""
import re
from pathlib import Path

from .registry import tool
from ..safety import confirm


NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,64}$")


def _skills_dir(cfg) -> Path:
    d = cfg.data_dir / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path_for(cfg, name: str) -> Path:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("skill name must be 1-64 chars: letters, digits, _ . -")
    return _skills_dir(cfg) / f"{name}.md"


def _title_and_body(text: str):
    """Split a skill file into (title, body). Title is the first H1, else
    the first non-empty line, else the filename."""
    lines = text.splitlines()
    title = ""
    body_start = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("# "):
            title = s[2:].strip()
            body_start = i + 1
        else:
            title = s[:80]
            body_start = i
        break
    body = "\n".join(lines[body_start:]).strip()
    return title, body


def _one_line(body: str) -> str:
    for ln in body.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            return s[:120]
    return ""


def _list_files(cfg):
    d = _skills_dir(cfg)
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        title, body = _title_and_body(text)
        out.append({"name": p.stem, "title": title or p.stem,
                    "summary": _one_line(body), "size": len(text)})
    return out


# ---------------------------------------------------------------- tools

@tool(
    "skill_list",
    "List every saved skill (name, title, one-line summary). Use this "
    "BEFORE improvising a multi-step workflow — the user may have saved "
    "a skill that does exactly this.",
    {},
)
def skill_list(cfg):
    rows = _list_files(cfg)
    if not rows:
        return "No skills saved yet. You can create one with skill_save."
    lines = [f"{r['name']} — {r['title']}" + (f": {r['summary']}" if r["summary"] else "")
             for r in rows]
    return "\n".join(lines)


@tool(
    "skill_read",
    "Return the full instructions of a saved skill. Call this after "
    "skill_list to load a skill before executing it.",
    {"name": {"type": "string"}},
    ["name"],
)
def skill_read(cfg, name):
    p = _path_for(cfg, name)
    if not p.exists():
        return f"ERROR: no skill named '{name}'. Try skill_list."
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return f"ERROR: could not read {p}: {e}"
    title, body = _title_and_body(text)
    return f"# {title or name}\n\n{body}"


@tool(
    "skill_save",
    "Create or overwrite a skill. The body should be a complete, "
    "self-contained recipe the model can follow next time (imperative "
    "steps, concrete tool names, expected outputs). Asks for approval "
    "before writing.",
    {"name": {"type": "string"},
     "title": {"type": "string"},
     "body": {"type": "string"}},
    ["name", "body"],
)
def skill_save(cfg, name, body, title=None):
    p = _path_for(cfg, name)
    existed = p.exists()
    action = ("Overwrite" if existed else "Create") + f" skill '{name}'"
    confirm(cfg, action, tool="skill_save")
    title = (title or name).strip() or name
    text = f"# {title}\n\n{body.strip()}\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return f"{'Updated' if existed else 'Created'} skill '{name}' ({len(text)} chars)"


@tool(
    "skill_delete",
    "Delete a saved skill. Asks for approval first.",
    {"name": {"type": "string"}},
    ["name"],
)
def skill_delete(cfg, name):
    p = _path_for(cfg, name)
    if not p.exists():
        return f"ERROR: no skill named '{name}'"
    confirm(cfg, f"Delete skill '{name}'", tool="skill_delete")
    try:
        p.unlink()
    except OSError as e:
        return f"ERROR: could not delete {p}: {e}"
    return f"Deleted skill '{name}'"


@tool(
    "skill_run",
    "Load a skill and return its instructions so you can execute them "
    "now. Use this when the user says 'run the X skill' or when a "
    "skill_list entry exactly matches the task.",
    {"name": {"type": "string"}},
    ["name"],
)
def skill_run(cfg, name):
    return skill_read(cfg, name)