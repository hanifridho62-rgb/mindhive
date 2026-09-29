# SPDX-License-Identifier: Apache-2.0
# src/mindhive/paths.py
import hashlib
import itertools
import os
import shutil
import sys
import time
import re
import subprocess
from pathlib import Path

# On Windows, a console child (e.g. git.exe) spawned from a window-less parent
# (the dashboard runs via pythonw / detached) pops a fresh console window for a
# split second. CREATE_NO_WINDOW suppresses it. No-op flag (0) elsewhere.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# A Python child that writes `__pycache__` into a lane or a checkout under test
# leaves litter the merge-back reads as work (MEB operational risk 7). Every
# environment this package builds for a child carries this variable.
NO_BYTECODE_ENV = "PYTHONDONTWRITEBYTECODE"

# Variables this process set for ITS OWN native libraries and never means for
# a child: name -> the value it inserted. `vectors.apply_thread_env` registers
# the BLAS/OpenMP thread caps it inserted here, and `no_bytecode_env` -- the
# one builder of child environments -- strips each one while it still holds
# that value, so a `claude`, a `pytest` or a `python -m mindhive` spawned from
# a process that merely imported `vectors` does not inherit a two-thread cap
# nothing there asked for. A value the operator set, before the insert or over
# it, is never stripped: it propagates the way it would without mindhive. A
# child that needs the cap applies it itself, at import, as `vectors` does.
PROCESS_ONLY_ENV: dict = {}


def no_bytecode_env(env=None) -> dict:
    """A copy of *env* (``None``: this process's) for a CHILD: every variable
    in `PROCESS_ONLY_ENV` that still holds the value this process inserted
    removed, and `NO_BYTECODE_ENV` set to ``"1"`` unless it already carries a
    value -- the operator's choice wins, even an empty one. The name is
    matched the way the platform matches it: case-blind on Windows, where a
    second spelling would be a duplicate key."""
    merged = dict(os.environ if env is None else env)
    for name, inserted in PROCESS_ONLY_ENV.items():
        if merged.get(name) == inserted:
            del merged[name]
    names = {str(k).upper() for k in merged} if os.name == "nt" else set(merged)
    if NO_BYTECODE_ENV not in names:
        merged[NO_BYTECODE_ENV] = "1"
    return merged


def store_home() -> Path:
    return Path(os.environ.get("MINDHIVE_HOME", str(Path.home() / ".mindhive")))


# Per-process, per-call temp-file discriminator. See write_text_atomic.
_TMP_SEQ = itertools.count(1)


# A publish that loses a race with a READER retries this many times, doubling
# the wait each time from this base: 5, 10, 20 ... 320ms, ~635ms before it gives
# up. `workspace.load_board` retries the READER side of the identical violation
# with a flat 4 x 20ms; the writer side is measurably harder and needed its own
# numbers rather than a matching pair that loses.
_REPLACE_TRIES = 8
_REPLACE_BACKOFF = 0.005


def _replace_with_retry(tmp, path) -> None:
    """`os.replace` is atomic on Windows; it is not always ALLOWED.

    Windows refuses to replace a destination that another process holds open
    without FILE_SHARE_DELETE, and Python's `open()` does not ask for it. So
    every reader of a published file is, for the instant it holds the handle, a
    reason for the publish to raise PermissionError (WinError 5) -- against the
    DESTINATION, a file that exists and is perfectly intact.

    That is not hypothetical and it is not rare. A reader looping on one file
    made the unguarded version raise inside 0.25s, every run. Nineteen call
    sites reach this function, and `chain.write_chain` is one of them with no
    try/except of its own, so a dashboard poll landing on `chain.json` at the
    wrong microsecond could take `hive project` down with it.

    The backoff was measured, not guessed. 600 publishes against one reader,
    counting publishes that never landed::

        reader           none   4x20ms   8x5ms-exp   12x10ms
        tight loop        264       38           0         0
        1ms between        67        0           0         0
        5ms between        19        0           0         0

    Doubling wins twice over a flat wait: it is the only bounded policy that
    survives a reader holding the handle continuously, AND it is faster there
    (9.1s against 11.0s for those 600), because the common case clears on the
    first 5ms sleep instead of always paying 20.

    Past the last try the error is RAISED, not swallowed. A publish that never
    happened must not look like one that did -- that is the failure this whole
    function exists to prevent, and eating the exception here would reintroduce
    it one layer up.
    """
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt + 1 >= _REPLACE_TRIES:
                raise
            time.sleep(_REPLACE_BACKOFF * (2 ** attempt))


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
        _replace_with_retry(tmp, path)
    except BaseException:
        # A shared name self-healed: the next writer overwrote the litter. A
        # unique one never will, so a failed write has to clean up after
        # itself or leave a file per failure behind forever.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_text_retry(path) -> str:
    """Read a file that a writer may be publishing over, without being denied.

    The mirror of `_replace_with_retry`, and the reason it is needed: making
    the write atomic removes torn CONTENT entirely (measured, 6218 torn reads
    in 15977 became 0) but leaves a SHARING VIOLATION -- for the instant
    `os.replace` holds the destination, a reader's `open()` is refused. 300
    refusals in 56483 reads, against a writer publishing flat out.

    Callers turn that refusal into a wrong answer rather than an error, which
    is why it matters: `chain.verify` catches OSError and reports
    `corrupt_chain_file`, showing the whole LEDGER as corrupt when only the
    checkpoint was being replaced; `harvest._load_state` catches everything and
    answers `{}`, a cursor reset wearing a first run's costume.

    A MISSING file raises. Absence is a real answer, retrying it cannot change
    it, and the common first-run path must not pay eight sleeps to learn so --
    `workspace.load_board` draws the line in exactly the same place. Only
    PermissionError is transient, and only now that torn content is impossible:
    a JSON error from this file means the file is really damaged, so retrying
    it would be waiting for a bug to fix itself.
    """
    for attempt in range(_REPLACE_TRIES):
        try:
            return Path(path).read_text(encoding="utf-8")
        except PermissionError:
            if attempt + 1 >= _REPLACE_TRIES:
                raise
            time.sleep(_REPLACE_BACKOFF * (2 ** attempt))
    raise AssertionError("unreachable")          # pragma: no cover


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


# The characters a namespace may contain. It names a DIRECTORY under
# `store_home()`, so the rule is `events.SESSION_ID_RE`'s with one addition:
# a leading `_`, because `__hivemind__` is a real namespace this code writes.
# Measured against the live store before it was tightened -- 81 directories,
# every one of them a match -- so nothing on disk moves.
PROJECT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")

# The component-name namespace the quarantine reserves for itself. OWNED here,
# not in `events`: `events` imports this module and re-exports the constant,
# while `paths` stays dependency-free so `autosync` can import it. One authority
# rather than two spellings; `tests/test_events_segment_path.py` pins the
# re-export.
QUARANTINE_PREFIX = "unsafe-"

# The names Windows resolves to a DEVICE rather than a file. The stem before the
# first dot is what counts, and case is ignored. Measured on this repo's
# Windows 11 dev box: `mkdir NUL/events` raised FileNotFoundError, so
# `append_event(project="NUL")` threw from inside the append, while
# `NUL.jsonl` and a bare `AUX` were created as ordinary files. Which members
# are reserved, and whether an extension frees them, has moved between Windows
# builds, so the whole family is refused rather than the members one build
# happens to reserve. Zero of 90 namespaces and 2547 segments in the live store
# use one, so nothing on disk moves.
WINDOWS_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(10)]
    + ["lpt%d" % i for i in range(10)])


def text_of(value):
    """*value* as the text a path component is named or hashed by, or None.

    `str`, then `repr`, then the type's name. Each fallback is the cheapest fact
    that still tells two values apart, so an object whose `__str__` raises is
    not merged with every other such object under one constant -- the merge the
    quarantine exists to prevent, reached without anybody choosing it. None
    only when all three raise, the one input with nothing left to name it by.
    Shared by `namespace_name` and `events.segment_name`, which used to carry
    two versions of this chain and only one of them was the full one."""
    try:
        return value if isinstance(value, str) else str(value)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        return repr(value)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        return "unstringifiable:" + type(value).__name__
    except Exception:                                   # noqa: BLE001
        return None


def is_reserved_component(name: str, os_name: str | None = None) -> bool:
    """Whether *name*, a COMPLETE path component that already matched its
    whitelist, must still not reach the filesystem verbatim.

    Both reasons are the filesystem storing something OTHER than the name it
    was handed:

    * **The quarantine prefix, in any case.** A conforming `unsafe-<16 hex>`
      would land in the quarantine slot of whichever hostile value hashes to
      it: a chosen merge. The test ignores case because NTFS does.
      `UNSAFE-<hex>.jsonl` IS `unsafe-<hex>.jsonl` there, and the first,
      case-sensitive reservation left that merge one Shift key away (observed:
      three spellings appended to one segment). Refused on every platform, so
      `unsafe-*` means "refused" wherever the store lives.
    * **On Windows only, a name the OS aliases**: a device stem (see
      `WINDOWS_DEVICE_NAMES`), or a trailing dot, which Win32 strips, making
      `abc.` and `abc` one directory. Only on `nt`, deliberately. On POSIX
      `aux` and `abc.` are ordinary names a store may already hold, and
      quarantining them there would silently point an existing caller at a
      different, empty namespace. `namespace_name` refuses `normalize` for
      exactly that failure.

    *name* is the whole component, suffix included, because that is what the
    filesystem sees: a session id `abc.` is the segment `abc..jsonl`, which
    ends in no dot and needs no quarantine. *os_name* is injected so each
    platform's answer is testable on the other's runner, the way
    `rotation.install` takes it."""
    if name[:len(QUARANTINE_PREFIX)].casefold() == QUARANTINE_PREFIX:
        return True
    if (os.name if os_name is None else os_name) != "nt":
        return False
    return (name.endswith(".")
            or name.split(".", 1)[0].casefold() in WINDOWS_DEVICE_NAMES)


def quarantine_name(raw: str) -> str:
    """The quarantine component for a refused *raw* value: deterministic, one
    legal component, and distinct per value (a hash cannot collide by
    accident, and `is_reserved_component` stops anyone choosing one)."""
    digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()
    return QUARANTINE_PREFIX + digest[:16]


# The FILES the store keeps at its own root, beside the namespace directories.
#
# A namespace is a directory at that same level, so a project named after one of
# these stood where the store expects its own file, and neither half failed
# loudly. `append_event(project="LIVE_LOCK")` minted `LIVE_LOCK/events/`, and
# `guard.live_lock_active` -- an `exists()` -- read the directory as a held lock:
# every `hive gc --prune` and `reflect` refused from then on. A project named
# `autosync.json` made the registry a directory, so `known_projects` read an
# empty allow-list and `autosync.register` raised.
#
# Every root file is reached through `store_file`, which refuses a name not
# declared here, and `tests/test_store_root_files.py` fails on a join onto
# `store_home()` that goes around it -- so the next root file is declared by the
# first run of the suite, not by the next collision.
STORE_ROOT_FILES = frozenset({
    "LIVE_LOCK", "autosync.json", "consolidate_marker.json", "dashboard.token",
    "harvest.lock", "hooks_errors.log", "mandatory_sops.json",
    "research_runs.json", "workspace_board.json", "workspace_profiles.json",
})
# Root files named per research run: `research.leaderboard_path(run_id)` writes
# `research_run_<run_id>.json`, and companions derived from a leaderboard's name
# follow it (`_skills.json`, `_learnings.md`, `_prds.json` in the live store).
# `research_runs_` holds the same place for companions of `research_runs.json`.
STORE_ROOT_PREFIXES = ("research_run_", "research_runs_")
_STORE_ROOT_FOLDED = frozenset(n.casefold() for n in STORE_ROOT_FILES)
_STORE_ROOT_PREFIXES_FOLDED = tuple(p.casefold() for p in STORE_ROOT_PREFIXES)
# The shape of a declared root file name: one component, no leading dot.
_ROOT_FILE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


def is_store_root_name(name) -> bool:
    """Whether *name*, in any case, is one of the store root's own files.

    Case is ignored because NTFS and APFS ignore it: `Live_Lock` IS `LIVE_LOCK`
    there. Refused on every platform, so a store that moves between machines
    means the same thing on each -- `is_reserved_component`'s reasoning for the
    quarantine prefix."""
    if not isinstance(name, str):
        return False
    folded = name.casefold()
    return (folded in _STORE_ROOT_FOLDED
            or folded.startswith(_STORE_ROOT_PREFIXES_FOLDED))


def store_file(name: str) -> Path:
    """The store root's own file *name*: the one sanctioned join of a FILE onto
    `store_home()`.

    Raises ValueError unless *name* is declared -- in `STORE_ROOT_FILES`, or
    under one of `STORE_ROOT_PREFIXES` -- spelled exactly, as one component. An
    undeclared root file is the collision above waiting for a project to be
    named after it, so it is refused where it is written."""
    if not (isinstance(name, str) and _ROOT_FILE_RE.fullmatch(name)
            and (name in STORE_ROOT_FILES
                 or name.startswith(STORE_ROOT_PREFIXES))):
        raise ValueError(f"{name!r} is not a store-root file declared in "
                         "paths.STORE_ROOT_FILES")
    return store_home() / name


def namespace_name(project, os_name: str | None = None) -> str:
    """*project* as a single path component inside the store, never an escape.

    `project_dir` is one expression with two halves and only one of them was
    ever guarded: `detect_project` returns `normalize(...)` on every path, so
    the DETECTED name is a slug by construction -- while the name an operator
    or a caller passes went to `Path.__truediv__` verbatim. `--project
    ../../pwned` therefore reopened, on the other half of the same line, the
    traversal `events.segment_name` closes for `session_id`.

    Deliberately NOT `normalize`, though it is right there and would refuse the
    traversal too. `normalize` collapses runs of non-alphanumerics, so it is not
    idempotent over names it did not produce, and re-slugging an existing
    namespace would silently point a caller at a DIFFERENT, empty store rather
    than at their data. The quarantine answers the security question without
    touching any name that already resolves.

    Quarantine, not raise, and not `"unknown"` -- the three reasons are
    `events.segment_name`'s, and they are the reasons a second copy of that
    shape is the right answer here rather than a smell: a hostile value that is
    dangerous only as a path component, on a path that must not throw, where
    two distinct values must not merge. The loud refusal lives at the surface a
    human typed (`cliparse.project_arg`).

    A whitelisted name is still quarantined when `is_reserved_component` says
    the filesystem would store it as something else: the reserved prefix in
    any case, and on Windows a device name or a trailing dot.

    And when it is one of the store root's own files (`is_store_root_name`), in
    any case and on every platform: a directory under that name stands where
    the store keeps the file. Deliberately NOT part of `is_reserved_component`,
    which `events.segment_name` shares -- a SESSION may be named `LIVE_LOCK`,
    because its segment lives inside `events/`, nowhere near the root."""
    raw = text_of(project)
    if raw is None:
        return QUARANTINE_PREFIX + "unstringifiable"
    if (PROJECT_RE.fullmatch(raw) and not is_reserved_component(raw, os_name)
            and not is_store_root_name(raw)):
        return raw
    return quarantine_name(raw)


def project_dir(project: str | None = None) -> Path:
    return store_home() / namespace_name(project or detect_project())


def events_dir(project: str | None = None) -> Path:
    return project_dir(project) / "events"


def state_dir(project: str | None = None) -> Path:
    """The namespace's directory for sidecars that are neither the ledger nor
    a projection of it -- the pending-embed queue first -- made on demand.

    Through `project_dir`, never a join of its own, so a hostile project lands
    inside the quarantined namespace like everything else in the store (and
    `tests/test_store_root_files.py`'s root-join guard has nothing new to
    allow). It mkdirs, unlike `events_dir`, because every caller is about to
    write; call it only then. `is_freshly_minted` probes `project_dir()` for
    existence, and a probe that minted the namespace on the way would answer
    "not fresh" to the very read it exists to warn about."""
    d = project_dir(project) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    reg = store_file("autosync.json")
    if not reg.exists():
        return set()
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {normalize(e["project"]) for e in data
            if isinstance(e, dict) and e.get("project")}


def _namespace_dirs(is_namespace) -> tuple:
    """``(canonical, unlisted)``: the store-root directories *is_namespace*
    accepts, sorted, split on whether a caller can reach them BY NAME.

    CANONICAL is a name `namespace_name` hands back unchanged, so a read or a
    prune addressed to it lands on this directory. Everything else is UNLISTED:
    a quarantine slot (`unsafe-<hash>`), a directory made by hand under a name
    the whitelist refuses (`My Project`), or, on Windows, a twin such as `abc.`
    that only an extended-length path can create and that Win32 resolves to
    `abc`. Listing those handed callers names whose reads land somewhere else,
    and `hive gc --prune --orphans` deleted THROUGH the twin: `rmtree` of
    `abc.` removed the registered `abc` and every event in it."""
    home = store_home()
    canonical, unlisted = [], []
    if not home.exists():
        return canonical, unlisted
    for p in sorted(home.iterdir()):
        if not (p.is_dir() and is_namespace(p)):
            continue
        (canonical if namespace_name(p.name) == p.name else unlisted).append(p.name)
    return canonical, unlisted


def _has_projection(p) -> bool:
    return any((p / marker).exists() for marker in ("STATE.md", "events", "LEDGER"))


def list_namespaces() -> list[str]:
    """On-disk namespace directory names under the store home, sorted. A
    namespace is a directory carrying any of the projection artifacts a real
    namespace has (STATE.md / events / LEDGER), which filters out incidental
    dirs (downloads, dist) that share the store root.

    Canonical names only: see `_namespace_dirs` for why a directory no name
    reaches is left out, and `unlisted_namespaces` for where it is reported."""
    return _namespace_dirs(_has_projection)[0]


def unlisted_namespaces() -> list[str]:
    """The namespace-shaped directories `list_namespaces` leaves out because no
    name reaches them. `hive gc` and `hive projects` report these; nothing
    lists, reads or prunes them by name."""
    return _namespace_dirs(_has_projection)[1]


def event_namespaces() -> list[str]:
    """Canonical namespaces that have an events log: the set `harvest`, the
    dashboard's project list and `hive skills trail --all` walk, from one
    helper so the three cannot disagree about which directories are projects."""
    return _namespace_dirs(lambda p: (p / "events").is_dir())[0]


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
    # Through `project_dir` itself: a raw join here probed a different path
    # from the one the read is about to land on.
    return not project_dir(name).exists()


# --- interpreter resolution ------------------------------------------------------

# The bare names an agent-authored validation script is likely to start with.
# Only these are ever swapped; `pytest`, `git`, or a path are left alone.
INTERPRETER_ALIASES = ("python", "python3")

# Where Windows keeps its App Execution Aliases. A `python.exe` found here is
# either the Store's redirector -- which exits 49 with an install prompt and
# never runs a line -- or a Store-installed Python. Either way, the interpreter
# running THIS process is the one that has mindhive on it.
_ALIAS_DIR = "windowsapps"


def resolve_interpreter(argv, which=None, executable=None) -> tuple:
    """``(argv, note)``: *argv* with a bare `python`/`python3` head swapped for
    this interpreter when the bare name would resolve to nothing on PATH or to
    a Windows App Execution Alias. *note* is ``""`` when nothing changed.

    The failure this exists for is not ENOENT. On a machine with the aliases
    on (the Windows default), `python` spawns cleanly, prints "Python was not
    found; run without arguments to install from the Microsoft Store", and
    exits 49 -- so a validation command fails for a reason that has nothing to
    do with the code under test, and `mindnet.executor` would record that as
    python-verified evidence. `tests/test_mindnet_executor.py` pins both the
    swap and the two shapes that must be left alone.

    *which* and *executable* are seams for the suite; the defaults are
    `shutil.which` and `sys.executable`."""
    argv = tuple(argv or ())
    if not argv:
        return argv, ""
    head = str(argv[0])
    if head != os.path.basename(head):
        return argv, ""                                 # a path: the author chose it
    name = head.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name not in INTERPRETER_ALIASES:
        return argv, ""
    exe = str(executable or sys.executable or "")
    if not exe:
        return argv, ""
    try:
        found = (which or shutil.which)(head)
    except Exception:                                   # noqa: BLE001
        found = None
    if found and _ALIAS_DIR not in str(found).replace("\\", "/").lower():
        return argv, ""
    why = (f"`{head}` resolves to nothing on PATH" if not found
           else f"`{head}` resolves to a Windows App Execution Alias ({found})")
    return (exe,) + argv[1:], f"{why}; using {exe}"
