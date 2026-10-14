# omni_entry.py
"""Entry point used by both `python omni_entry.py` and the Nuitka-built exe.

Nuitka sets sys.frozen. When frozen, bundled data lives in OMNI_ROOT
(the exe's directory, or the temp dir Nuitka extracts to for --onefile).
"""
import os
import sys
from pathlib import Path


def _fix_paths():
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        os.environ.setdefault("OMNI_ROOT", str(base))
        for sub in ("agent", "config", "workflows"):
            d = base / sub
            if d.exists():
                os.environ.setdefault("OMNI_" + sub.upper() + "_DIR", str(d))


def main():
    _fix_paths()
    from agent.__main__ import main as agent_main
    agent_main()


if __name__ == "__main__":
    main()