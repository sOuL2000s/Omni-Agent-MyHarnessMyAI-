import re
import subprocess
from .registry import tool
from ..safety import confirm, Denied

BLOCK = [r"rm\s+-rf\s+/(\s|$)", r"mkfs", r":\(\)\s*\{", r"dd\s+if=.*of=/dev/"]


@tool("run_shell", "Run a shell command in the workspace (tests, git, pip, scripts...). Asks the user to approve.",
      {"command": {"type": "string"}, "timeout": {"type": "integer"}}, ["command"])
def run_shell(cfg, command, timeout=120):
    for b in BLOCK:
        if re.search(b, command):
            raise Denied("Command blocked by safety rules.")
    confirm(cfg, f"Run shell: {command}")
    r = subprocess.run(command, shell=True, cwd=cfg.workspace, capture_output=True, text=True, timeout=timeout)
    return f"exit={r.returncode}\n--stdout--\n{r.stdout}\n--stderr--\n{r.stderr}"
