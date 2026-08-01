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

The language lock originally inspected write tools only, which left the most
obvious way an agent puts bytes on disk — a redirect or a heredoc — outside the
rule entirely. It now covers execute tools too, but ONLY through two shapes that
create files: a redirect/`tee` target, and a small closed table of scaffolding
generators. Running a program still produces no source and is still allowed.
The deferral's reason was never "shell commands don't matter", it was that
classifying an arbitrary command's language is a parsing problem with a
false-positive cost on every call — and that reason bounds the rule instead of
being overturned by it.

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

import re

from . import phases as ph_mod
from . import tasks as t_mod
from .controller import WORKABLE_STATUSES

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

# Commands whose PURPOSE is generating a source tree, keyed by a token prefix.
#
# The lock asks "does this put a file on disk in a forbidden language". A
# scaffolder answers yes without any path ever appearing in the tool call, so
# the extension table cannot see it. Matching is on a token PREFIX rather than
# the program alone, and that is the whole reason this is a table of tuples:
# `go mod init` creates a module, `go mod tidy` edits one that exists, and a
# program-keyed rule would deny both.
#
# Deliberately tiny. Anything absent is unmapped, and unmapped never denies —
# the same failure direction `EXTENSION_LANGUAGE` errs in.
SCAFFOLD_LANGUAGE = {
    ("npx", "create-react-app"): "javascript",
    ("npm", "init"): "javascript",
    ("yarn", "create"): "javascript",
    ("pnpm", "create"): "javascript",
    ("cargo", "new"): "rust",
    ("cargo", "init"): "rust",
    ("go", "mod", "init"): "go",
    ("rails", "new"): "ruby",
}

# An output redirect and its target. `\d?>>?` covers `>`, `>>` and `2>`; the
# `(?!&)` is load-bearing rather than defensive — `2>&1` and `>&2` are the two
# most common redirects in this repo's own commands, and a pattern that read
# `&1` as a filename would deny a stderr redirect. Quotes are captured and
# stripped by the caller rather than excluded, so `> "src/app ts.ts"` is seen
# instead of silently missed.
_REDIRECT_RE = re.compile(r"\d?>>?\s*(?!&)([^\s;&|<>()]+)")

# `tee` is a redirect wearing a different hat: it is how a pipeline writes a
# file. `-a` is the append form.
_TEE_RE = re.compile(r"(?:^|[\s;&|])tee\s+(?:-a\s+)?([^\s;&|<>()]+)")

# Programs the phase block must NEVER deny, because they are how the block is
# escaped. `hive` is the state machine's own control surface — the injected
# context tells the agent to clear a gate with `hive mindnet satisfy`, and
# denying that is the system refusing the remedy it just prescribed. `git` and
# `gh` are inspection, not code execution.
#
# Without this the block is unrecoverable from inside a session: every
# documented way out (`hive mindnet satisfy`, `hive mindnet enter`,
# `hive rules enforce --disable`) runs through Bash, as does the mandatory
# `hive log` entry.
EXEMPT_PROGRAMS = frozenset({"hive", "git", "gh"})

# The two refusal messages. The directive's wording is the first sentence; the
# rest is the way out.
#
# A refusal an operator cannot act on is an outage. The original text named only
# the phase — with several tasks in the graph that does not even say WHICH task
# froze the repo, let alone how to unfreeze it.
PHASE_DENIAL = (
    "ACCESS DENIED: MindNetwork State Machine violation. You cannot write or "
    "execute code while in Phase: {phase}. (task '{task_id}') To proceed: "
    "produce this phase's evidence under {where}, then `hive mindnet satisfy "
    "{task_id}` and `hive mindnet enter {task_id} <next>`. `hive`, `git` and "
    "`gh` commands are not blocked. To turn enforcement off for this checkout: "
    "`hive mindnet enforce --disable`  ·  for this process only: "
    "`MINDNETWORK_ENFORCE=0`  ·  or just this one rule: "
    "`hive rules enforce --disable policy:gate:mindnet-phase`.")
LANGUAGE_DENIAL = (
    "ACCESS DENIED: Language Compatibility Lock violation. This task is "
    "strictly restricted to {languages}. (task '{task_id}') Either write a "
    "permitted file type, widen the task's language_context, or turn "
    "enforcement off: `hive mindnet enforce --disable`  ·  "
    "`MINDNETWORK_ENFORCE=0`  ·  or just this rule: "
    "`hive rules enforce --disable policy:gate:mindnet-language`.")

# A drive letter is exactly one alpha character followed by a colon. The
# original test — `text[1] == ":"` — also matched `a:docs/plans/x.md`, a
# drive-RELATIVE path on the A: volume, and amputating the prefix turned it into
# something that looked like this phase's evidence.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Windows silently strips trailing dots and spaces from a path, and everything
# after `::` is an NTFS alternate-data-stream name. `src/app.ts.` and
# `src/app.ts::$DATA` both create `app.ts`, but both parsed to an extension the
# language table does not know — and "unmapped never denies" then waved them
# through.
_ADS_RE = re.compile(r"::.*$")


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


def blocking_task(sc):
    """``(phase, Task)`` for an `in_progress` task sitting at a BLOCKED phase,
    or ``(None, None)``.

    Distinct from `active_task`, and the distinction is a real defect this
    replaces. `active_task` returns the single task FURTHEST along the pipeline;
    routing the phase block through it meant that starting any second task and
    advancing it to `execution` silently switched the whole gate off — the rule
    defeated by unrelated state, invisibly, and switched back on just as
    invisibly when that task finished.

    The block is a property of the REPOSITORY: if any task you have explicitly
    picked up is still at a planning gate, producing code is out of order.

    Ordering among several is stated and total (earliest phase, then epoch, then
    id) so the task NAMED in the refusal is reproducible — an operator who
    cannot reproduce a denial cannot clear it."""
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
        if rec.phase not in BLOCKED_PHASES:
            continue
        idx = t_mod.phase_index(rec.phase)
        key = (idx if idx is not None else len(t_mod.PHASES), rec.epoch, tid)
        if best is None or key < best[0]:
            best = (key, rec.phase, task)
    return (best[1], best[2]) if best else (None, None)


def locked_task(sc):
    """``(phase, Task)`` whose ``language_context`` applies, or
    ``(None, None)``.

    Eligibility is `WORKABLE_STATUSES` — the same set `can_advance` gates on.
    Restricting it is a fix, not tightening for its own sake: the first version
    consulted status not at all, so a task COMPLETED last month still enforced
    its `language_context` on every write in the repository, forever. Because
    the shared ordering prefers the task furthest along, and a finished task is
    by definition furthest along, the settled task actively out-competed the
    live one. That is precisely the brick the `in_progress` rule prevents for
    the phase block, applied to only one of the two rules.

    Still broader than `blocking_task`: `pending` and `ready` count, because the
    lock describes the task's declared SCOPE rather than whether anyone is
    typing right now. But a settled task has no scope left to defend."""
    if not getattr(sc, "fresh", False):
        return None, None
    records = getattr(getattr(sc, "state", None), "records", None) or {}
    tasks = getattr(sc, "tasks", None) or {}
    best = None
    for tid, rec in records.items():
        task = tasks.get(tid)
        if task is None or not getattr(task, "language_context", ()):
            continue
        if getattr(task, "status", None) not in WORKABLE_STATUSES:
            continue
        idx = t_mod.phase_index(rec.phase)
        key = (-idx if idx is not None else 1, rec.epoch, tid)
        if best is None or key < best[0]:
            best = (key, rec.phase, task)
    return (best[1], best[2]) if best else (None, None)


# --- paths -------------------------------------------------------------------------

def _norm(path) -> str:
    """*path* as a normalized, forward-slashed POSIX path, or ``""``.

    Normalization is what makes the evidence exemption survive ``..``:
    `posixpath.normpath` collapses ``a/../b``, so a traversal out of an exempt
    directory produces a path that no longer sits under it."""
    if not isinstance(path, str) or not path.strip():
        return ""
    text = _DRIVE_RE.sub("", path.replace("\\", "/").strip())
    return posixpath.normpath(text).lstrip("/")


def _segments(path) -> tuple:
    return tuple(p for p in _norm(path).split("/") if p and p != ".")


def is_phase_evidence(phase, path) -> bool:
    """Whether writing *path* would produce evidence for *phase*.

    **Matched on a segment-aligned SUFFIX, not anchored at the start.** That is
    the whole correction here, and it is not a style choice: the `Write`/`Edit`
    tool schemas REQUIRE an absolute `file_path`, and this project's own rule 12
    mandates absolute paths everywhere. So the only shape this ever really sees
    is ``C:/Users/hanif/mindhive/docs/superpowers/specs/thing.md``. An anchored
    containment test against ``docs/superpowers/specs/`` fails on every one of
    them, the exemption never fires, and `brainstorm` denies the very document
    that clears it — the repository deadlocks with no way out. The first
    implementation did exactly that, and every test that was supposed to catch
    it passed a RELATIVE path.

    Comparing whole segments rather than characters keeps it honest:
    ``docs/plans-archive/x.md`` shares a string prefix with ``docs/plans`` and
    is a different directory. At least one segment must follow the matched run,
    so the directory itself is not mistaken for a file in it.

    The tradeoff is deliberate. A suffix match also exempts
    ``src/docs/plans/x.py``, which is not really this phase's evidence — it
    over-exempts, letting one write through that the rule would rather stop.
    Under-exempting bricks the repository. When a guard exists to prevent a
    deadlock its failure direction must be "too permissive", so this errs open.

    Root-relative resolution would be exact, but `paths.worktree_root` shells
    out to git with a 2-second timeout and this runs on EVERY tool call.

    The exempt directories come from `phases.PHASE_EVIDENCE`, never from a
    second list here (invariant 10)."""
    segs = _segments(path)
    if not segs:
        return False
    desc = ph_mod.descriptor(phase) or {}
    for d in desc.get("required_dir") or ():
        need = _segments(d)
        if not need:
            continue
        # `len(segs) - len(need)` (not +1) requires a segment AFTER the run.
        for i in range(len(segs) - len(need)):
            if segs[i:i + len(need)] == need:
                return True
    return False


def _program(command) -> str:
    """The bare program name a shell command invokes, lowercased, or ``""``.

    Deliberately crude — first token only. It gates an EXEMPTION, so its failure
    direction is over-exempting something that merely starts with
    `hive`/`git`/`gh`, never denying a legitimate escape."""
    if not isinstance(command, str):
        return ""
    head = command.strip().split()
    if not head:
        return ""
    return posixpath.basename(head[0].replace("\\", "/")).lower()


def _tokens(command) -> list:
    """*command* as lowercased tokens with the program basenamed, or ``[]``.

    Whitespace splitting, not `shlex` — a heredoc body or an unbalanced quote
    makes `shlex.split` raise, and this runs on a path that must be total."""
    if not isinstance(command, str) or not command.strip():
        return []
    toks = [t.lower() for t in command.split()]
    toks[0] = posixpath.basename(toks[0].replace("\\", "/"))
    return toks


def _scaffold_language(command) -> str | None:
    """The language a scaffolding command would generate, or ``None``."""
    toks = _tokens(command)
    if not toks:
        return None
    for key, lang in SCAFFOLD_LANGUAGE.items():
        if tuple(toks[:len(key)]) == key:
            return lang
    return None


def _redirect_targets(command) -> tuple:
    """Every path *command* would write through a redirect or `tee`.

    Crude on purpose, and in the safe direction: it feeds a DENY, so a shape it
    fails to recognise is a bypass this iteration misses, while a token it
    wrongly reads as a path is a refusal the operator did not earn. Missing is
    recoverable; a false refusal on an execute tool is the false-positive cost
    MN-4 declined to pay. Every candidate still has to survive the extension
    table afterwards, where unmapped allows."""
    if not isinstance(command, str) or not command.strip():
        return ()
    out = []
    for rx in (_REDIRECT_RE, _TEE_RE):
        for match in rx.finditer(command):
            token = match.group(1).strip("'\"")
            if token:
                out.append(token)
    return tuple(out)


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
        phase, task = blocking_task(sc)
        if task is None:
            return None
        # Writes only. A shell command is not a document, whatever path string
        # it happens to mention, so `EXECUTE_TOOLS` never takes this exit.
        if tool_name in WRITE_TOOLS and \
                is_phase_evidence(phase, _file_path(tool_input)):
            return None
        # The escape hatch. Every documented remedy for this block — satisfy,
        # enter, disable the rule, and the mandatory `hive log` entry — is a
        # `hive` command, and denying those made the block unrecoverable
        # without leaving the session entirely.
        if tool_name in EXECUTE_TOOLS and \
                _program((tool_input or {}).get("command")
                         if isinstance(tool_input, dict) else "") \
                in EXEMPT_PROGRAMS:
            return None
        desc = ph_mod.descriptor(phase) or {}
        where = " or ".join(desc.get("required_dir") or ()) \
            or "this phase's evidence path"
        return PHASE_DENIAL.format(
            phase=phase, task_id=getattr(task, "task_id", "?"), where=where)
    except Exception:
        return None


def _path_language_violation(phase, path, allowed) -> bool:
    """Whether writing *path* would breach *allowed*. One resolution of "what
    language is this file", shared by both tool classes (invariant 10)."""
    if not path:
        return False
    # The evidence exemption has to hold across BOTH rules. It did not: every
    # planning phase is cleared by a markdown document, `.md` maps to
    # `markdown`, and so any language_context that did not literally list
    # `markdown` made all three planning gates unclearable — the phase rule
    # correctly waved the document through and this rule denied it one check
    # later. Two guards, each individually sound, composing into the deadlock
    # both were written to prevent. Writing that same document with `>` instead
    # of `Write` must not reopen it, which is why this sits below the split
    # rather than in the write branch.
    if is_phase_evidence(phase, path):
        return False
    # Trailing dots, trailing spaces and `::$DATA` are stripped by Windows when
    # the file is actually created, so `app.ts.` writes `app.ts`. Parse the name
    # Windows will use, not the one we were handed.
    base = _ADS_RE.sub("", posixpath.basename(_norm(path)))
    ext = posixpath.splitext(base.rstrip(". "))[1].lower()
    lang = EXTENSION_LANGUAGE.get(ext)
    # Unmapped is not a violation — the table is finite, the file types a
    # repository contains are not.
    return lang is not None and lang not in allowed


def language_violation(tool_name, tool_input, sc) -> str | None:
    """The refusal reason for a language-lock violation, or ``None`` to allow.

    MN-4 shipped this over write tools only, and deferred shell commands with a
    reason that still stands: inferring a language from an arbitrary command is
    a parsing problem whose false positives would be paid on every call. That
    reason bounds this rather than being overturned by it. The question asked
    here is not "what language does this command imply" — it is the narrower,
    decidable one the lock was always asking: does this command PUT A FILE ON
    DISK in a forbidden language. Two shapes answer it, a redirect target and a
    scaffolding generator, and everything else allows.

    So `node app.js` stays allowed under a Python-only context — running a
    program produces no source — while `cat > src/app.ts <<EOF` does not, which
    is the same bytes on disk that a `Write` would have been refused for.

    Total: any fault answers ``None``."""
    try:
        if tool_name not in WRITE_TOOLS and tool_name not in EXECUTE_TOOLS:
            return None
        phase, task = locked_task(sc)
        if task is None:
            return None
        allowed = {str(x).strip().lower()
                   for x in getattr(task, "language_context", ()) or ()}
        if not allowed:
            return None
        obj = tool_input if isinstance(tool_input, dict) else {}
        if tool_name in WRITE_TOOLS:
            paths, scaffold = (_file_path(obj),), None
        else:
            command = obj.get("command")
            # The escape hatch has to survive BOTH rules for the same reason it
            # exists at all: every documented way out of an enforcement state —
            # `hive mindnet satisfy`, `hive rules enforce --disable`, the
            # mandatory `hive log` — runs through Bash. `git` additionally
            # writes files constantly, and a rule that refused `git checkout`
            # over a `.ts` path in the command text would be unrecoverable from
            # inside a session.
            if _program(command) in EXEMPT_PROGRAMS:
                return None
            paths, scaffold = _redirect_targets(command), \
                _scaffold_language(command)
        if (scaffold is not None and scaffold not in allowed) or \
                any(_path_language_violation(phase, p, allowed) for p in paths):
            return LANGUAGE_DENIAL.format(
                languages=", ".join(sorted(allowed)),
                task_id=getattr(task, "task_id", "?"))
        return None
    except Exception:
        return None
