# agent/tools/__init__.py
from . import files, shell, web, memory, office, admin  # noqa: F401  (registers built-ins)
try:
    from . import mcp_bridge  # noqa: F401  (registers MCP tools if any are configured)
except Exception:
    pass
from .registry import schemas, call, TOOLS, register_external, set_cancel, get_cancel  # noqa: F401
