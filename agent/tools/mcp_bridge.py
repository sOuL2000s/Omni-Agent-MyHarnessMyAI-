# agent/tools/mcp_bridge.py
"""Registers MCP tools into the global registry if a registry has been attached.

`agent.config.load()` builds an MCPRegistry and stores it on `cfg.mcp`.
This module pulls tools from that registry into the tool table when the
config calls `register(cfg)`.
"""
_registered_for = None


def register(cfg):
    global _registered_for
    reg = getattr(cfg, "mcp", None)
    if not reg or reg is _registered_for:
        return 0
    try:
        n = reg.register_tools()
        _registered_for = reg
        return n
    except Exception:
        return 0