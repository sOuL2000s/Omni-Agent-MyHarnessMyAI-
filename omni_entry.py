# omni_entry.py
"""Entry point used by both `python omni_entry.py` and the Nuitka-built exe.

Nuitka sets sys.frozen. When frozen, bundled data lives in OMNI_ROOT
(the exe's directory, or the temp dir Nuitka extracts to for --onefile).

Any exception during startup is written to `omni_crash.log` next to the exe
(and to stderr) so silent crashes on Windows are diagnosable.
"""
import os
import sys
import traceback
from pathlib import Path


def _log_path() -> Path:
    """Where to write crash diagnostics."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent
    return base / "omni_crash.log"


def _write_crash(exc_text: str) -> None:
    try:
        with _log_path().open("a", encoding="utf-8") as f:
            import datetime
            f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            f.write(exc_text)
            f.write("\n")
    except Exception:
        pass
    # Also try to print to a console (does nothing if there's no console)
    try:
        sys.stderr.write(exc_text + "\n")
        sys.stderr.flush()
    except Exception:
        pass


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
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        _write_crash(traceback.format_exc())
        sys.exit(1)