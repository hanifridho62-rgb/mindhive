# SPDX-License-Identifier: Apache-2.0
# src/mindhive/mindnet/optin.py
"""MN-6 — the blast-radius gate. MindNetwork enforcement is opt-in, per checkout.

`enforce.py`'s own docstring states the contract this module restores:
"Enforcement is always opt-in and never fires for a signature MindHive has not
escalated." MN-4 then registered `gate:mindnet-phase` and `gate:mindnet-language`
into `POLICY_CHECKS`, whose members default ON, with the note "the adoption
decision IS the opt-in, so they default ON."

That reasoning is right for `gate:no-verify` and wrong here, for one structural
reason. Every other member of `POLICY_CHECKS` decides from the tool call alone:
`--no-verify` is in the command string or it is not, and the answer is the same
in every repository on the machine. The two mindnet checks decide from mutable
*projection* state — and `paths.detect_project` deliberately collapses every git
worktree of a repo onto ONE memory namespace (SOP 10), so a single
`mindnet/state.json` answers for every checkout at once. One task left
`in_progress` at a planning gate therefore denied writes in every worktree
simultaneously, in projects whose operator never asked for enforcement at all.

## Two mechanisms, deliberately not one

**`MINDNETWORK_ENFORCE`** — process-scoped, and the only switch that needs no
writable disk. It decides in BOTH directions, so it out-ranks the record on
disk. That is the whole point: MN-4's third P0 was a kill switch locked inside
the room it existed to open (every documented remedy ran through `Bash`, and the
phase block denied `Bash` unconditionally). A switch that lives in the
environment cannot be locked in by any file-level rule.

**The record at ``<project>/mindnet/enforce.json``** — durable, per checkout.
Written by `hive mindnet enforce --enable/--disable`, and shared across
worktrees because the namespace is shared, which is exactly why it is keyed by
ROOT rather than being a single boolean.

## Why roots nest, and why the deepest one wins

This repository's linked worktrees live UNDER the main checkout
(``<repo>/.claude/worktrees/<name>``). Plain containment would therefore mean
arming the main checkout silently arms every worktree inside it — the same
repo-wide reach the gate exists to remove, one level down. So the DEEPEST
recorded root that covers the current directory decides, which makes
``--disable`` from inside one worktree a real, local escape from an armed
ancestor, and makes ``--enable`` there a real, local opt-in under a disarmed
one. A disable is consequently RECORDED rather than deleted: a deleted entry
cannot out-rank anything.

Matching is segment-aligned, not a string prefix — `is_phase_evidence`'s lesson,
which cost a P0: ``<x>/mindhive-old`` shares a character prefix with
``<x>/mindhive`` and is a different tree.

## The failure direction, and the hot path

Every fault answers DORMANT. This module gates a rule that can stop an operator
working; a bug in it must fail towards "everything is allowed", never towards
"nothing is". That is the same direction `enforce.py` fails in, for the same
reason.

`PreToolUse` fires on every tool call, so resolving the CURRENT CHECKOUT is pure
path arithmetic — `Path.cwd()` and tuple comparison, no subprocess. The one git
call for a checkout root (`paths.worktree_root`, 2-second timeout) happens once,
in the CLI, when a root is RECORDED.

That claim is scoped deliberately, because a broader version of it was wrong.
Resolving the *namespace* is a different matter: `record_path(None)` reaches
`paths.project_dir(None)` -> `paths.detect_project()`, which DOES shell out to
git. `enforce.hook_decision` therefore resolves the namespace once per hook
invocation and threads the concrete name down, so this module never triggers that
call on the hot path — but a caller that passes ``project=None`` will.
`test_status_never_shells_out_when_the_project_is_known` covers only the
already-resolved shape, and its name says so; the ``project=None`` shape has its
own test asserting where the cost actually lands, because the first version of
this docstring claimed "no subprocess" flatly and the only test covering it passed
the one argument that avoids the call — the same defect the MN-4 P0 was.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from .. import paths

# One legend (invariant 10). The name breaks the store's `MINDHIVE_` prefix on
# purpose: this gates the MindNetwork subsystem specifically, and a second
# spelling would be a place for the two to drift — in the direction of
# "enforcement was live and nobody could see which variable said so".
ENV_VAR = "MINDNETWORK_ENFORCE"

# The exact text surfaced by `hive mindnet enforce`. A stable contract, asserted
# verbatim by the suite, so an operator can grep for it.
DORMANT_NOTE = "[mindnet] enforcement dormant: opt-in required"

# Only these spellings decide. Anything else — including the empty string a
# shell leaves behind when a variable is cleared — falls through to the record,
# rather than being guessed at in either direction.
_TRUE = frozenset({"1", "true", "yes", "on", "enable", "enabled"})
_FALSE = frozenset({"0", "false", "no", "off", "disable", "disabled"})

_RECORD_VERSION = 1


# --- the record ---------------------------------------------------------------

def record_path(project=None) -> Path:
    """``<project>/mindnet/enforce.json``.

    Beside `sidecar.sidecar_path`, in the one mindnet directory per namespace —
    this is mindnet state, not a second store."""
    return paths.project_dir(project) / "mindnet" / "enforce.json"


def read_record(project=None) -> dict:
    """The opt-in record as ``{root_key: bool}``, or ``{}``.

    Total: missing, unreadable, corrupt, wrong-shaped, and half-written entries
    all answer ``{}`` or omit the offending row. Absent is never true — an entry
    with no ``enabled`` key is a half-written row, and arming from one would
    make a crashed write into an enforcement decision."""
    try:
        raw = record_path(project).read_text(encoding="utf-8")
        obj = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    roots = obj.get("roots")
    if not isinstance(roots, dict):
        return {}
    out = {}
    for key, entry in roots.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(entry, dict) or "enabled" not in entry:
            continue
        # RELATIVE keys are dropped. `_resolve` would resolve them against the
        # READER's cwd, so a hand-edited or copied `{"roots": {".": ...}}` becomes
        # a root at maximum depth that follows the operator into every directory —
        # out-ranking every genuinely recorded ancestor and, because the tie-break
        # is strict, un-out-rankable by `--disable` (which records the same depth).
        # `set_enabled` only ever writes absolute keys, so this drops nothing
        # production can produce.
        if not _is_absolute_key(key):
            continue
        out[key] = bool(entry.get("enabled"))
    return out


def _is_absolute_key(key) -> bool:
    """Whether a record key names an absolute path, under EITHER platform's rules.

    Both flavours are consulted because the record is shared across every checkout
    of one repo: a Windows-authored ``C:/x`` key must still read as absolute to a
    POSIX reader rather than being silently treated as relative to its cwd (and
    then resolving to something inside it). ``/x`` is absolute to
    `PurePosixPath` and not to `PureWindowsPath`; ``C:/x`` is the reverse."""
    text = str(key).strip()
    if not text:
        return False
    return (PurePosixPath(text).is_absolute()
            or PureWindowsPath(text).is_absolute())


# --- paths: absolute on both sides, always ------------------------------------

def _resolve(path) -> Path | None:
    """*path* as an absolute, traversal-collapsed ``Path``, or ``None``.

    MN-4's P0 in one function. `Write`/`Edit` require an absolute `file_path`
    and this project's rule 12 mandates absolute paths everywhere, but an
    operator types ``.`` and a test writes a relative fixture. Normalizing at
    the boundary means every comparison downstream is absolute-to-absolute — the
    guard that failed was one comparing an absolute path against a
    repo-relative prefix, which answered False on every call production ever
    made."""
    if path is None:
        return None
    try:
        text = str(path).strip()
        if not text:
            return None
        return Path(text).expanduser().resolve()
    except Exception:
        return None


def _key(root: Path) -> str:
    """The stored form: absolute, forward-slashed. Readable in the JSON, and
    stable across the backslash/forward-slash spellings Windows hands around."""
    return str(root).replace("\\", "/")


def _parts(root: Path) -> tuple:
    """Case-folded path segments, for a segment-ALIGNED containment test.

    `os.path.normcase` is the platform's own answer to case sensitivity: a
    no-op on POSIX, lowercasing on Windows, where ``C:/Repo`` and ``c:/repo``
    are one directory."""
    return tuple(os.path.normcase(p) for p in root.parts)


def _covers(root: Path, cwd: Path) -> bool:
    r, c = _parts(root), _parts(cwd)
    return len(r) <= len(c) and c[:len(r)] == r


# --- the decision -------------------------------------------------------------

def _env_decision(env):
    """``True``/``False`` when the environment states a decision, else ``None``."""
    raw = (env if env is not None else os.environ).get(ENV_VAR)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def status(project=None, cwd=None, env=None) -> dict:
    """The full enforcement decision for this process, in this directory.

    ``{"armed": bool, "source": "env"|"file"|"default", "note": str,
    "root": str|None}``. ``note`` is `DORMANT_NOTE` exactly when dormant, and
    an explanation of what armed it otherwise — a status that cannot say WHY is
    not actionable.

    Total: any fault answers dormant."""
    try:
        decision = _env_decision(env)
        if decision is True:
            return {"armed": True, "source": "env", "root": None,
                    "note": f"armed for this process by {ENV_VAR}"}
        if decision is False:
            return {"armed": False, "source": "env", "root": None,
                    "note": DORMANT_NOTE}

        here = _resolve(cwd if cwd is not None else Path.cwd())
        if here is not None:
            # Deepest recorded root wins, so a nested checkout can dissent from
            # an armed ancestor (and vice versa).
            best = None
            for key, enabled in read_record(project).items():
                root = _resolve(key)
                if root is None or not _covers(root, here):
                    continue
                depth = len(_parts(root))
                if best is None or depth > best[0]:
                    best = (depth, root, enabled)
                elif depth == best[0] and enabled != best[2]:
                    # Two records at the SAME depth disagreeing can only come from
                    # a hand-edited file (`set_enabled` writes one canonical key
                    # per root). Resolving it by iteration order would make the
                    # answer depend on JSON key sorting; an opt-in gate resolves an
                    # ambiguity the same way it resolves a fault — dormant.
                    best = (depth, root, False)
            if best is not None:
                _d, root, enabled = best
                return {"armed": bool(enabled), "source": "file",
                        "root": _key(root),
                        "note": (f"armed for {_key(root)}" if enabled
                                 else DORMANT_NOTE)}
    except Exception:
        pass
    return {"armed": False, "source": "default", "root": None,
            "note": DORMANT_NOTE}


def armed(project=None, cwd=None, env=None) -> bool:
    """Whether the MN-4 policy checks may deny anything here. The predicate
    `enforce.py` consults; `status` is the same answer with its reasons."""
    return bool(status(project, cwd=cwd, env=env)["armed"])


# --- the toggle ---------------------------------------------------------------

def set_enabled(enabled: bool, project=None, root=None, cwd=None,
                session=None) -> dict:
    """Record *enabled* for one checkout root. Returns
    ``{"root", "enabled", "path"}``.

    *root* defaults to the git worktree containing *cwd* — resolved HERE, in the
    CLI, because `paths.worktree_root` costs a subprocess and must never land on
    the hook path. Off a repository that call answers ``None`` by contract, and
    the honest fallback is the directory itself.

    Appends NO ledger event, for two independent reasons. `sidecar._staleness`
    counts every event, so a row written here without also rewriting the sidecar
    would read STALE and silently degrade MN-1's phase-aware injection. And an
    operator disarming a frozen repository must not need the ledger to be
    writable to do it."""
    target = _resolve(root) if root is not None else None
    if target is None:
        base = _resolve(cwd) or _resolve(Path.cwd())
        target = _resolve(paths.worktree_root(base)) or base
    if target is None:                                  # pragma: no cover
        raise ValueError("cannot resolve a checkout root to record")

    path = record_path(project)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        roots = obj.get("roots") if isinstance(obj, dict) else None
        roots = dict(roots) if isinstance(roots, dict) else {}
    except Exception:
        # A corrupt record is replaced, not merged. Merging into a document we
        # could not parse is how a half-understood file becomes an enforcement
        # decision; the toggle's job is to leave a state the reader trusts.
        roots = {}

    from .. import events                 # lazy: the hook path never pays this
    roots[_key(target)] = {"enabled": bool(enabled), "at": events.now_iso(),
                           "session": session}
    paths.write_text_atomic(
        path,
        json.dumps({"version": _RECORD_VERSION, "roots": roots},
                   indent=2, sort_keys=True),
    )
    return {"root": _key(target), "enabled": bool(enabled), "path": str(path)}
