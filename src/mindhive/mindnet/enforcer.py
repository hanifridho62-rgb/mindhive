# src/mindhive/mindnet/enforcer.py
"""MN-4 — the teeth. Where the phase spine stops describing and starts refusing.

Everything before this unit is advisory. `inject.py` tells the agent it is in
`brainstorm`; nothing stops it writing `src/foo.py` anyway. This module is the
`PreToolUse` predicate that does.

## Two rules, and one of them is genuinely dangerous

1. **The phase block** — no writing or executing code while the active task sits
   at a planning gate (`brainstorm`, `plan`, `prd`).
2. **The language lock** — no writing a file whose extension falls outside a
   populated ``language_context``. The field has existed on `Task` since MN-2
   ("permitted languages (R13, read by MN-4)") and nothing has ever read it.

Rule 1 can make a repository unwritable. Three guards keep that from happening,
and each one is the answer to a specific way this goes wrong in production:

**`in_progress` only, never `pending`.** `inject.active` deliberately treats
`pending` as steerable, because it is the dataclass default for every declared
task and scoping injection to `in_progress` would mean the normal case gets no
steering at all. Reusing that predicate here would invert the cost: one task
declared months ago and forgotten would deny every write in the repo forever.
Advisory steering should be generous; refusal must be narrow. So the enforcer
requires a task somebody explicitly picked up.

**The current phase's own evidence directory is exempt.** `brainstorm` is
cleared by a document under `docs/superpowers/specs/`. Denying writes there
during `brainstorm` forbids the only action that clears it — the machine would
refuse to move and refuse the thing that would let it move. The exempt set is
read from `phases.PHASE_EVIDENCE` rather than restated (invariant 10), so it
cannot drift from what actually satisfies the gate. Scoped to the CURRENT phase:
a plan document does not clear `brainstorm`, and exempting all of `docs/` would
turn the rule into a suggestion.

Containment, not prefix — the same call `phases.resolve_ref` makes. A
`startswith` test accepts ``docs/superpowers/specs/../../../src/evil.py``.

**Unknown is not a violation.** The language lock works off a finite extension
table. Treating everything outside it as a breach would deny `.toml`, `.lock`,
`Makefile`, and every file type the table has not heard of.

## Fail-open, and never a ledger read

A missing, stale, corrupt, or empty sidecar ALLOWS. MindNetwork is not
load-bearing for the user's ability to work, and an enforcer that fails closed
turns a projection bug into an outage. Stale gets the same treatment `inject`
gives it: a projection that cannot prove it is current is not a basis for
refusing anything.

`PreToolUse` fires on EVERY tool call. `events.read_all` measures ~264 ms on the
real store, so this module does not import `events` at all — asserted by a test
over its own source, which is a stronger guarantee than a timing bound. Both
predicates take an already-read `Sidecar` and are pure; the single read happens
once per hook invocation in `enforce.py` and is shared by both.
"""
from __future__ import annotations

import posixpath

from . import phases as ph_mod
from . import tasks as t_mod

# The phases at which producing code is, by definition, out of order. Derived
# from the pipeline's own shape: everything before `execution` is a method
# phase. Stated as a literal anyway — a slice would silently re-scope this rule
# the day a phase is inserted, and re-scoping WHAT CAN BE DENIED must be a
# deliberate edit with a test behind it.
BLOCKED_PHASES = ("brainstorm", "plan", "prd")

# Tools that put bytes on disk, and tools that run something. Two sets rather
# than one because they are exempted differently: a write can be evidence for
# the current phase, a command never is.
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
EXECUTE_TOOLS = frozenset({"Bash", "PowerShell"})

# Extension -> language token. Finite and deliberately small: it only needs to
# cover the languages someone would plausibly WRITE a `language_context` about.
# Anything absent is unmapped, and unmapped never denies.
EXTENSION_LANGUAGE = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".sh": "shell", ".bash": "shell", ".ps1": "powershell",
    ".md": "markdown", ".markdown": "markdown",
    ".sql": "sql",
    ".html": "html", ".css": "css", ".scss": "css",
}

# The two refusal messages, verbatim from the MN-4 directive. One home each, so
# a test can assert the exact operator-facing wording.
PHASE_DENIAL = ("ACCESS DENIED: MindNetwork State Machine violation. You "
                "cannot write or execute code while in Phase: {phase}.")
LANGUAGE_DENIAL = ("ACCESS DENIED: Language Compatibility Lock violation. This "
                   "task is strictly restricted to {languages}.")


# --- selecting the task that decides --------------------------------------------------

def active_task(sc):
    """``(phase, Task)`` for the task a refusal may be based on, or
    ``(None, None)``.

    Deliberately mirrors `inject.active`'s ordering — furthest along the
    pipeline first, lowest epoch, then `task_id` — so the block the agent READS
    and the rule that DENIES it are talking about the same task. Divergence
    there would produce the worst possible message: a refusal naming a phase
    the injected context never mentioned.

    It does NOT reuse `inject.active` directly, because that function's
    eligibility set is the workable statuses (which includes `pending`) and
    narrowing it here is the whole anti-brick guard. The ORDER is shared; the
    ELIGIBILITY is not, and that difference is the point.

    A stale sidecar answers ``(None, None)``: a projection that cannot prove it
    is current cannot justify a refusal."""
    if not getattr(sc, "fresh", False):
        return None, None
    records = getattr(getattr(sc, "state", None), "records", None) or {}
    tasks = getattr(sc, "tasks", None) or {}
    best = None
    for tid, rec in records.items():
        task = tasks.get(tid)
        if task is None or getattr(task, "status", None) \
                != t_mod.STATUS_IN_PROGRESS:
            continue
        idx = t_mod.phase_index(rec.phase)
        key = (-idx if idx is not None else 1, rec.epoch, tid)
        if best is None or key < best[0]:
            best = (key, rec.phase, task)
    return (best[1], best[2]) if best else (None, None)


def locked_task(sc):
    """The task whose ``language_context`` applies, or ``None``.

    Unlike the phase block, the lock is a property of the task's declared SCOPE
    rather than of whether anyone is currently working it — a task restricted to
    Python does not become polyglot by being parked. So eligibility here is
    "has a phase record and a populated context", and among several the same
    ordering as `active_task` picks one deterministically."""
    if not getattr(sc, "fresh", False):
        return None
    records = getattr(getattr(sc, "state", None), "records", None) or {}
    tasks = getattr(sc, "tasks", None) or {}
    best = None
    for tid, rec in records.items():
        task = tasks.get(tid)
        if task is None or not getattr(task, "language_context", ()):
            continue
        idx = t_mod.phase_index(rec.phase)
        key = (-idx if idx is not None else 1, rec.epoch, tid)
        if best is None or key < best[0]:
            best = (key, task)
    return best[1] if best else None


# --- paths -------------------------------------------------------------------------

def _norm(path) -> str:
    """*path* as a normalized, forward-slashed, relative POSIX path, or ``""``.

    Normalization is what makes the evidence exemption a CONTAINMENT test rather
    than a prefix test: `posixpath.normpath` collapses ``a/../b``, so a
    traversal out of an exempt directory produces a path that no longer sits
    under it. `phases.resolve_ref` refuses prefix matching for exactly this
    reason and it would be pointless to reopen the hole one module over."""
    if not isinstance(path, str) or not path.strip():
        return ""
    text = path.replace("\\", "/").strip()
    if len(text) > 2 and text[1] == ":":       # a Windows drive letter
        text = text[2:]
    return posixpath.normpath(text).lstrip("/")


def is_phase_evidence(phase, path) -> bool:
    """Whether writing *path* would produce evidence for *phase*.

    The exempt directories come from `phases.PHASE_EVIDENCE`, never from a
    second list here (invariant 10): add a ``required_dir`` there and this
    honours it without being edited, which is what a test asserts."""
    norm = _norm(path)
    if not norm:
        return False
    desc = ph_mod.descriptor(phase) or {}
    for d in desc.get("required_dir") or ():
        base = _norm(d)
        if base and (norm == base or norm.startswith(base + "/")):
            return True
    return False


def _file_path(tool_input):
    obj = tool_input if isinstance(tool_input, dict) else {}
    for key in ("file_path", "path", "notebook_path"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


# --- the two predicates ------------------------------------------------------------

def phase_violation(tool_name, tool_input, sc) -> str | None:
    """The refusal reason for a state-machine violation, or ``None`` to allow.

    Total: any fault answers ``None``. This decides whether a real tool call
    runs, and a projection bug must not become an outage."""
    try:
        if tool_name not in WRITE_TOOLS and tool_name not in EXECUTE_TOOLS:
            return None
        phase, task = active_task(sc)
        if task is None or phase not in BLOCKED_PHASES:
            return None
        # Writes only. A shell command is not a document, whatever path string
        # it happens to mention, so `EXECUTE_TOOLS` never takes this exit.
        if tool_name in WRITE_TOOLS and \
                is_phase_evidence(phase, _file_path(tool_input)):
            return None
        return PHASE_DENIAL.format(phase=phase)
    except Exception:
        return None


def language_violation(tool_name, tool_input, sc) -> str | None:
    """The refusal reason for a language-lock violation, or ``None`` to allow.

    Writes only — inferring a language from an arbitrary shell command is a
    parsing problem whose false positives would be paid on every command, and
    the plan defers it deliberately rather than guessing."""
    try:
        if tool_name not in WRITE_TOOLS:
            return None
        task = locked_task(sc)
        if task is None:
            return None
        allowed = {str(x).strip().lower()
                   for x in getattr(task, "language_context", ()) or ()}
        if not allowed:
            return None
        path = _file_path(tool_input)
        if not path:
            return None
        ext = posixpath.splitext(_norm(path))[1].lower()
        lang = EXTENSION_LANGUAGE.get(ext)
        # Unmapped is not a violation — the table is finite, the file types a
        # repository contains are not.
        if lang is None or lang in allowed:
            return None
        return LANGUAGE_DENIAL.format(
            languages=", ".join(sorted(allowed)))
    except Exception:
        return None
