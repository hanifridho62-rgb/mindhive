# src/mindhive/paths.py
import os
import re
import subprocess
from pathlib import Path


def store_home() -> Path:
    return Path(os.environ.get("MINDHIVE_HOME", str(Path.home() / ".mindhive")))


def normalize(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "default"


def detect_project(cwd: Path | None = None) -> str:
    env = os.environ.get("MINDHIVE_PROJECT")
    if env:
        return normalize(env)
    cwd = cwd or Path.cwd()
    # Collapse git worktrees onto their MAIN repo so every worktree of a repo
    # shares one memory namespace — the linked-worktree folder name is ephemeral
    # (it's deleted when the worktree is removed). `--git-common-dir` points at
    # the main repo's .git even from inside a linked worktree.
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(cwd), capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            gcd = Path(r.stdout.strip())
            if not gcd.is_absolute():
                gcd = Path(cwd) / gcd
            gcd = gcd.resolve()
            if gcd.name == ".git":          # normal repo / linked-worktree layout
                return normalize(gcd.parent.name)
    except Exception:
        pass
    # Fallbacks: plain working-tree root, then the cwd name.
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd), capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            return normalize(Path(r.stdout.strip()).name)
    except Exception:
        pass
    return normalize(cwd.name)


def project_dir(project: str | None = None) -> Path:
    return store_home() / (project or detect_project())


def events_dir(project: str | None = None) -> Path:
    return project_dir(project) / "events"
