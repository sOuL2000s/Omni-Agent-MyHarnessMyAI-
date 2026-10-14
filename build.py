# build.py
"""Build a standalone Omni (no Python needed for end users).

  python build.py                 -> dist/omni.dist/   (folder, fast startup)  [DEFAULT]
  python build.py --onefile       -> dist/omni.exe     (single file, slower startup)
  python build.py --clean         -> wipe dist/ and the Nuitka cache first

Both outputs are fully self-contained: the end user does NOT need Python,
pip, or any of the project's dependencies installed.

Modes supported by the built binary:
  omni            GUI launcher (Terminal chat / Web UI buttons)
  omni chat       terminal chat
  omni ui         web UI (opens browser)
  omni run "..."  one-shot task
  omni check      test keys
  omni provider   key management
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ensure_nuitka():
    try:
        import nuitka  # noqa: F401
    except ImportError:
        print("Installing Nuitka and its helpers…")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "nuitka", "ordered-set", "zstandard"])


def ensure_requirements():
    req = ROOT / "requirements.txt"
    if req.exists():
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req)])


def have_compiler():
    if os.name == "nt":
        return shutil.which("cl.exe") is not None or shutil.which("gcc.exe") is not None
    return shutil.which("gcc") is not None or shutil.which("clang") is not None


def clean():
    for p in (ROOT / "dist", ROOT / "omni.build", ROOT / "omni.dist",
              ROOT / "omni.onefile-build"):
        if p.exists():
            print("removing", p)
            shutil.rmtree(p, ignore_errors=True)


def build(onefile=False):
    entry = ROOT / "omni_entry.py"
    if not entry.exists():
        sys.exit("omni_entry.py not found at project root")

    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",                       # ALWAYS standalone (self-contained)
        "--assume-yes-for-downloads",
        "--include-package=agent",
        "--include-package=docx",
        "--include-package=openpyxl",
        "--include-package=pptx",
        "--include-package=lxml",
        "--include-package=tzdata",
        "--include-package=selenium",
        "--include-package=webdriver_manager",
        # --- GUI launcher needs tkinter. Nuitka requires the plugin flag for
        #     tkinter to work at runtime; the --include-module lines are not enough.
        "--enable-plugin=tk-inter",
        "--include-module=tkinter",
        "--include-module=tkinter.ttk",
        "--include-module=tkinter.font",
        "--include-module=tkinter.messagebox",
        "--include-module=_tkinter",
        # --- static data
        "--include-data-files=agent/web/index.html=agent/web/index.html",
        "--include-data-dir=config=config",
        "--include-data-dir=workflows=workflows",
        "--output-filename=omni",
        "--output-dir=dist",
        "--nofollow-import-to=tests",
        "--nofollow-import-to=scripts",
        "--remove-output",
        "--company-name=Omni",
        "--product-name=Omni Agent",
        "--file-version=2.0.0.0",
        "--product-version=2.0.0.0",
    ]
    if onefile:
        cmd.append("--onefile")               # optional: also pack into one file
    if os.name == "nt":
        # Console mode: keep a console window attached to the process. On Windows
        # a console-less build would still work (the GUI has its own window), but
        # the terminal-chat mode spawns its own console anyway and the CLI modes
        # (run/check/provider) print to stdout, so we keep the console for those.
        cmd.append("--windows-console-mode=force")
    cmd.append(str(entry))

    print("\nNuitka command:\n  " + " ".join(cmd) + "\n")
    print("Building (this takes a few minutes)…\n")
    subprocess.check_call(cmd, cwd=ROOT)

    # Nuitka names the .dist folder after the entry script (omni_entry.py).
    # Rename both the folder and the exe inside it to something clean.
    if not onefile:
        old_dir = ROOT / "dist" / "omni_entry.dist"
        new_dir = ROOT / "dist" / "omni.dist"
        if old_dir.exists() and not new_dir.exists():
            old_dir.rename(new_dir)
        for old_exe in ("omni_entry.exe", "omni.exe"):
            src = new_dir / old_exe
            dst = new_dir / "omni.exe" if os.name == "nt" else new_dir / "omni"
            if src.exists() and src != dst:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
                break

    if onefile:
        exe = ROOT / "dist" / ("omni.exe" if os.name == "nt" else "omni")
        print(f"\nDone.  Single-file build:\n  {exe}\n")
        print(f"  {exe}           launcher GUI (Terminal chat / Web UI)")
        print(f"  {exe} chat      terminal chat (no GUI)")
        print(f"  {exe} ui        web UI")
    else:
        d = ROOT / "dist" / "omni.dist"
        exe = d / ("omni.exe" if os.name == "nt" else "omni")
        print(f"\nDone.  Folder build (fast startup):\n  {d}\n")
        print(f"  {exe}           launcher GUI (Terminal chat / Web UI)")
        print(f"  {exe} chat      terminal chat (no GUI)")
        print(f"  {exe} ui        web UI")
        print("\nShip the whole 'omni.dist' folder (zip it). The user runs omni.exe inside it.")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean()
    onefile = "--onefile" in sys.argv
    if not have_compiler():
        print("WARNING: no C compiler detected.")
        if os.name == "nt":
            print("  Install 'Visual Studio Build Tools' with the 'Desktop development with C++'")
            print("  workload, or install MinGW-w64. Nuitka needs one of them.")
        else:
            print("  Install gcc/clang (build-essential / xcode-select --install).")
        if input("Continue anyway? [y/N] ").strip().lower() != "y":
            sys.exit(1)
    ensure_nuitka()
    ensure_requirements()
    build(onefile=onefile)