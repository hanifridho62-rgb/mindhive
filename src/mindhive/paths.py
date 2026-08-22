# src/mindhive/paths.py
import itertools
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


# Per-process, per-call temp-file discriminator. See write_text_atomic.
_TMP_SEQ = itertools.count(1)


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
    # A temp name PRIVATE to this writer. It used to be `<target>.tmp` for
    # every writer of that target, and on this machine every hook is its own
    # process: two concurrent writers of one path wrote the same temp file
    # and `os.replace` published whichever bytes happened to be there, so one
    # process's snapshot landed under the other's name. That is the mechanism
    # under the sidecar clobbering measured in `tasktool` -- a whole 25-task
    # session gone from `task_tools.json`. 19 call sites share this function,
    # so the fix belongs here rather than in any one of them. The pid keeps
    # the name unique across processes, the counter across threads and repeat
    # calls inside one.
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(),
                                           next(_TMP_SEQ)))
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        # A shared name self-healed: the next writer overwrote the litter. A
        # unique one never will, so a failed write has to clean up after
        # itself or leave a file per failure behind forever.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def normalize(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "default"


# --- path equivalence ---------------------------------------------------------
# Three modules ask the same question — "are these two strings the same file?" —
# and get it wrong in the same way when they answer it locally. It lives here
# rather than in any one of them because `changelog` must not depend on the
# injector to compare paths, and both already import this module.

def normalise_path(text) -> str:
    """One path, forward-slashed, with any leading `./` segments removed.

    A single function because the alternative was `lstrip("./")` repeated at five
    call sites — and `lstrip` takes a CHARACTER SET, not a prefix, so it ate the
    leading dot of every hidden path: `.hidden/x.py` became `hidden/x.py`, and
    `...` became "". Two spellings of one path that no longer match are two paths
    as far as every fold is concerned, which silently breaks the "has this been
    logged / addressed" comparisons that are the point of normalising."""
    out = str(text or "").replace("\\", "/")
    while out.startswith("./"):
        out = out[2:]
    return out


def path_spellings(normalised) -> set:
    """Every spelling of one path a ledger entry should match.

    The ledger holds worktree-absolute paths written by every past checkout while
    git reports repo-relative ones, so comparing them literally calls everything
    unlogged. Measured on the live store: 3716 `file_touched` paths are
    worktree-absolute against 3 relative, while 1026 `code_change` entries are
    repo-relative against 17 absolute — the two sets essentially never intersect,
    so a literal comparison subtracts nothing and the pending-changelog marker
    grows without bound."""
    normalised = normalise_path(normalised)
    out = {normalised}
    for marker, prefix in (("/src/mindhive/", "src/mindhive/"),
                           ("/tests/", "tests/")):
        if marker in normalised:
            out.add(prefix + normalised.split(marker, 1)[1])
    return out


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


def worktree_root(cwd: Path | None = None) -> Path | None:
    """Absolute root of the git WORKTREE containing *cwd*, or ``None`` outside
    one. Total: any failure (no git, not a repo, timeout) answers ``None``.

    Deliberately DISTINCT from `detect_project`, which resolves
    ``--git-common-dir`` so every linked worktree collapses onto the main repo's
    memory namespace (SOP 10). Both are correct for their own axis and
    conflating them is a real bug: **evidence paths are per-checkout, the ledger
    namespace is per-repo**. A plan file that exists only on one branch is
    legitimately absent from another checkout, so a satisfaction recorded here
    must remember which root resolved it rather than being re-stat'd against
    whichever worktree happens to be reading."""
    cwd = cwd or Path.cwd()
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd), capture_output=True, text=True, timeout=2,
            creationflags=NO_WINDOW,
        )
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except Exception:
        pass
    return None


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
