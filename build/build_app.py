# build/build_app.py
"""Freeze the MIND HIVE Graph desktop app into a single binary.

Usage:
    pip install -e .[build]
    python build/build_app.py

Produces dist/MindHive (.exe on Windows). PyInstaller builds for the OS it
runs on, so run this on the target OS.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "build" / "app_entry.py"
ICON = ROOT / "build" / "icon.ico"


def main() -> int:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--name", "MindHive",
        "--paths", str(ROOT / "src"),
    ]
    if ICON.exists():
        args += ["--icon", str(ICON)]
    args.append(str(ENTRY))
    print("Running:", " ".join(args))
    return subprocess.call(args, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
