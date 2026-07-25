# src/mindhive/paths.py
import os
import re
import subprocess
from pathlib import Path

# On Windows, a console child (e.g. git.exe) spawned from a window-less parent
# (the dashboard runs via pythonw / detached) pops a fresh console window for a
# split second. CREATE_NO_WINDOW suppresses it. No-op flag (0) elsewhere.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def store_home() -> Path:
    return Path(os.environ.get("MINDHIVE_HOME", str(Path.home() / ".mindhive")))


def write_text_atomic(path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + ``os.replace`` so a
    concurrent reader always sees either the whole old file or the whole new
    file, never a torn, half-written one.

    Creates the parent directory if missing (``mkdir -p``), so callers do not
    need their own mkdir. JSON callers serialize (``json.dumps`` / canonical
    JSON) themselves and pass the resulting string. ``path`` may be a str or a
    ``Path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
            creationflags=NO_WINDOW,
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
            creationflags=NO_WINDOW,
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


def runs_dir(project: str | None = None) -> Path:
    """Per-run output artifacts (runlog.py). Evidence, not a projection: unlike
    events/ these are not rebuildable, and losing one degrades every consumer to
    the bounded excerpt its ledger event carries."""
    return project_dir(project) / "runs"


def known_projects() -> set[str]:
    """Registered namespace names from the autosync registry (`autosync.json`) —
    the store's allow-list of 'real' projects. Read the file directly rather than
    importing autosync so paths stays dependency-free (autosync imports paths).
    Used to distinguish a deliberate namespace from one freshly minted by cwd
    detection."""
    import json
    reg = store_home() / "autosync.json"
    if not reg.exists():
        return set()
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {normalize(e["project"]) for e in data
            if isinstance(e, dict) and e.get("project")}


def list_namespaces() -> list[str]:
    """On-disk namespace directory names under the store home, sorted. A
    namespace is a directory carrying any of the projection artifacts a real
    namespace has (STATE.md / events / LEDGER), which filters out incidental
    dirs (downloads, dist) that share the store root."""
    home = store_home()
    if not home.exists():
        return []
    out = []
    for p in sorted(home.iterdir()):
        if not p.is_dir():
            continue
        if any((p / marker).exists() for marker in ("STATE.md", "events", "LEDGER")):
            out.append(p.name)
    return out


def is_freshly_minted(project: str | None = None) -> bool:
    """True when the resolved namespace was inferred from cwd (not pinned via
    MINDHIVE_PROJECT) and is neither registered nor already on disk — i.e. a read
    is about to land on / create a brand-new namespace the user likely did not
    intend."""
    if os.environ.get("MINDHIVE_PROJECT"):
        return False
    name = project or detect_project()
    if name in known_projects():
        return False
    return not (store_home() / name).exists()
