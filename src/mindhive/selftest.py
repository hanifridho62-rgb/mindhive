# src/mindhive/selftest.py
import subprocess
import sys
from pathlib import Path


def run() -> int:
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(root / "tests")],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return r.returncode
