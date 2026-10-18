# omni_entry.py
"""Entry point used by both `python omni_entry.py` and the Nuitka-built exe.

Nuitka sets sys.frozen. When frozen, bundled data lives in OMNI_ROOT
(the exe's directory for --standalone, or a temp dir for --onefile).

Any exception during startup is written to `omni_crash.log` next to the exe,
or in %TEMP% if the install location is read-only, so silent crashes on
Windows are diagnosable.
"""
import os
import sys
import tempfile
import traceback
from pathlib import Path


def _log_path() -> Path:
    """Where to write crash diagnostics. Prefers the exe's folder, falls back
    to %TEMP% (or $TMPDIR) when the install location is read-only."""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent)
    candidates.append(Path(__file__).resolve().parent)
    candidates.append(Path(tempfile.gettempdir()))
    for base in candidates:
        try:
            p = base / "omni_crash.log"
            if p.exists() or base.is_dir():
                with p.open("a", encoding="utf-8"):
                    pass
                return p
        except OSError:
            continue
    return Path("omni_crash.log")  # last resort; will fail silently if cwd is unwritable


def _write_crash(exc_text: str) -> None:
    try:
        with _log_path().open("a", encoding="utf-8") as f:
            import datetime
            f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            f.write(exc_text)
            f.write("\n")
    except Exception:
        pass
    try:
        sys.stderr.write(exc_text + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _fix_paths():
    if getattr(sys, "frozen", False):
        # Nuitka --onefile extracts to a temp dir and points sys.argv[0] there.
        # --standalone keeps everything next to the exe.
        # PyInstaller sets _MEIPASS; Nuitka does not.
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        # If the data files aren't where we expect, try the exe's own folder.
        if not (base / "agent" / "web" / "index.html").exists():
            base = Path(sys.executable).parent
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