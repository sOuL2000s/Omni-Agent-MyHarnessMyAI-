# agent/tools/__init__.py
from . import files, shell, web, memory, office, admin, skills  # noqa: F401
from . import parallel, automation  # noqa: F401 (new in 2.4)
try:
    from . import mcp_bridge  # noqa: F401
except Exception:
    pass
from .registry import schemas, call, TOOLS, register_external, set_cancel, get_cancel  # noqa: F401
