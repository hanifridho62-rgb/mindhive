# src/mindhive/mindnet/tasks.py
"""MN-2 — the dynamic hierarchical task graph.

The flat roadmap cannot express the requirement: `roadmap.py` steps are a list
capped at ``MAX_STEPS = 50`` whose only mutation is wholesale replacement, with
no ``parent_id`` anywhere. This module is the append-friendly, arbitrarily
nested alternative — a task tree crossed with a dependency DAG, folded
deterministically from the append-only ledger.

`roadmap.py` is **not replaced.** It has five live consumers (the STATE.md
interrupted-plan marker, ``/api/oversee``, ``autoresume.fold``'s ``plan_id``
join, `hookcapture`'s TodoWrite mirror, and the CLI). This registry lands
beside it.

## The graph is assumed hostile

It is built dynamically by an agent, so every structural rule here is a
*refusal*, not a warning:

- **Bounds** (`MAX_NODES`, `MAX_DEPTH`, `MAX_DEPS_PER_TASK`) stop a runaway
  decomposition loop from exhausting memory. `fold` enforces the node cap while
  building, so the wall is hit before allocation, not after.
- **One rule per axis, whatever the spelling.** Three actions write
  ``parent_id`` — `reparent`, a re-declare, and a first declare — so all three
  route through `_placement_refusal`. A gate only two of them answer to is not
  a gate; it is a detour sign.
- **Triple-threat cycle detection.** Three INDEPENDENT checks, because a
  deadlock can exist without forming a simple cycle in either graph alone:
  `dependency_cycle` (A waits on B waits on C waits on A), `hierarchy_cycle`
  (reparenting under one's own descendant), and `cross_graph_conflict` (a task
  depending on its own ancestor or descendant — parent blocked on child while
  child cannot start until parent completes).
- **`fold` refuses the edge, it does not merely report it.** A `depend` or
  `reparent` event that would close a loop is declined. The event itself stays
  on the ledger — appends are immutable (invariant 1) — but the projection
  never enters the bad state. Detection alone would let one hallucinated cycle
  corrupt the graph permanently.

## Every traversal is iterative

No function here recurses. A hallucinated 5000-node chain would blow CPython's
~1000-frame stack, turning a reportable violation into a crash, so depth,
ancestry, descendants, and the dependency DFS all use explicit stacks.

## Purity

`fold` is deterministic and total: no clock, no RNG, no I/O. ``task_id`` is
caller-supplied for exactly that reason — minting a UUID inside the fold would
make two folds of the same events disagree. Malformed rows, unknown actions,
and lifecycle events for unknown tasks are skipped, never raised.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..sop import ID_RE

# --- the legend (MN-1's phase axis) -------------------------------------------
# Normative name set. Invariant 10: these strings are used VERBATIM in code, in
# the dashboard, and in docs — no synonyms drifting between layers. Splitting
# audit three ways is deliberate: pytest, code review, and the thermo-nuclear
# maintainability gate are three different exit criteria with three different
# verifiers, and collapsing them hides which one actually failed.
PHASES = ("brainstorm", "plan", "prd", "execution",
          "audit-bug", "audit-code", "audit-system", "pr-green")


def phase_index(phase):
    """Position of *phase* in the pipeline, or None when it is not a phase.
    Total by design — callers use it to compare progress without a try block."""
    try:
        return PHASES.index(phase)
    except ValueError:
        return None


# ADR-0005's bypass waives the METHOD — brainstorm/plan/PRD for a one-line fix —
# so a trivial task is born at `execution` and its terminal gate is the bug
# audit rather than `pr-green`. Named once because two places consume them (the
# fold derives the entry, `can_complete` gates the exit) and a drift between
# those two is exactly how a bypass turns into a skeleton key.
TRIVIAL_ENTRY_PHASE = "execution"
TRIVIAL_EXIT_PHASE = "audit-bug"


def _at_or_past(phase, mark) -> bool:
    """True when *phase* sits at or beyond *mark* in the pipeline. Total on
    both sides: an unrecognised phase answers False rather than raising, so a
    record written by some past version of the code cannot take the fold down
    (invariant 3) — and False is the fail-safe answer, since every caller uses
    this to decide whether to REFUSE something."""
    a, b = phase_index(phase), phase_index(mark)
    return a is not None and b is not None and a >= b


# --- statuses -----------------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETED = "completed"
STATUS_ABANDONED = "abandoned"
STATUSES = (STATUS_PENDING, STATUS_READY, STATUS_IN_PROGRESS,
            STATUS_BLOCKED, STATUS_COMPLETED, STATUS_ABANDONED)

# "Closed" for the subtree check: abandoned work is settled, not outstanding.
# Without this an abandoned subtask would block its parent forever.
CLOSED_STATUSES = (STATUS_COMPLETED, STATUS_ABANDONED)

# --- defensive bounds ---------------------------------------------------------
MAX_NODES = 2000
MAX_DEPTH = 12
MAX_DEPS_PER_TASK = 32

# Size caps bound every event, so one oversized field can never invalidate (and
# thereby silently drop) a whole task record — roadmap.py's rationale.
MAX_TITLE_CHARS = 120
MAX_DESC_CHARS = 2000
MAX_SCRIPT_CHARS = 500
MAX_REF_CHARS = 300
MAX_LANG_CHARS = 40
# Refusals accumulate on the record they belong to, so the same runaway-loop
# reasoning that bounds nodes has to bound them — otherwise a re-declare loop
# grows one Task without limit. The OLDEST are kept: the first refusal explains
# how the graph got into this shape, and a loop only ever repeats itself.
MAX_VIOLATIONS = 20

# ONE WRITER ON THE PHASE AXIS. "phase" was an action here until it proved to be
# a free teleport: the fold's branch tested membership in PHASES and nothing
# else (`phase_index` was defined and never called), so two raw appends —
# declare, then phase="pr-green" — parked a task at the terminal gate with no
# work done. `mindnet_phase`, gated by the controller, is now the only writer.
ACTIONS = ("declare", "status", "depend", "reparent", "evidence")


@dataclass(frozen=True)
class Task:
    """One node. Frozen on purpose: the fold rebuilds records with
    ``dataclasses.replace`` rather than mutating in place, so two folds of the
    same events can never alias each other's state.

    ``phase`` records the task's **declared entry phase** and nothing more.
    Nothing in this module writes it after the declare — the authoritative
    current phase lives in the controller's `mindnet_phase` records, which is
    the whole point of keeping one writer on that axis. The field stays because
    the entry point itself is a `mindnet_task` fact that U4 and the CLI read: a
    trivial task enters at ``TRIVIAL_ENTRY_PHASE``, everything else at the gate.
    A phase comparison written *here* would therefore compare declared defaults
    and pass vacuously — put phase-aware checks in the controller.

    ``violations`` carries the refusals the fold made against THIS record, in
    the order they happened. Appends are immutable, so a refused event stays on
    the ledger forever; without somewhere to record the refusal the projection
    would silently disagree with the ledger and nobody could see why."""

    task_id: str                        # caller-supplied; keeps the fold pure
    title: str
    parent_id: str | None = None        # None => root; infinite nesting
    description: str = ""
    phase: str = PHASES[0]              # DECLARED ENTRY phase — see the docstring
    status: str = STATUS_PENDING
    dependencies: tuple = ()            # task_ids that must be COMPLETED first
    language_context: tuple = ()        # permitted languages (R13, read by MN-4)
    validation_script: str = ""         # shell string; executed by MN-1's runner
    evidence: tuple = ()                # runlog artifact paths (MN-0)
    faculty: str | None = None          # MN-5 cat feed label
    triviality_bypass: bool = False     # ADR-0005 bypass, FIRST DECLARE ONLY
    depth: int = 0                      # DERIVED by the fold, never stored
    violations: tuple = ()              # refusals recorded here — see docstring


# --- payload contract ---------------------------------------------------------

def _id_tuple(raw, cap=MAX_LANG_CHARS) -> tuple:
    if not isinstance(raw, list):
        return ()
    return tuple(str(x)[:cap] for x in raw if str(x).strip())


def validate_payload(payload) -> None:
    """Enforce the ``mindnet_task`` event contract at every append surface
    (DATA_MODEL.md §6.2: the store stays type-agnostic, the contract lives
    here). Mirrors ``roadmap.validate_payload``."""
    if not isinstance(payload, dict):
        raise ValueError("mindnet_task payload must be an object")
    action = payload.get("action")
    if action not in ACTIONS:
        # `phase` was a legal action until one-writer landed, so it is named
        # explicitly: an operator (or an agent replaying this morning's example)
        # lands here, and the bare action list does not say where it went.
        moved = (" — a phase move is a `mindnet_phase` event, not a "
                 "`mindnet_task` one" if action == "phase" else "")
        raise ValueError("mindnet_task requires an action — one of: "
                         + ", ".join(ACTIONS) + moved)
    if not ID_RE.match(str(payload.get("task_id") or "")):
        raise ValueError(
            "mindnet_task requires a kebab-case task_id (lowercase letters, "
            "digits, hyphens; max 64 chars), e.g. 'mn2-task-graph'")
    parent = payload.get("parent_id")
    if parent is not None and not ID_RE.match(str(parent)):
        raise ValueError("parent_id must be a kebab-case task_id, or null "
                         "to promote the task to a root")
    title = payload.get("title")
    if title is not None and len(str(title).strip()) > MAX_TITLE_CHARS:
        raise ValueError(f"title exceeds {MAX_TITLE_CHARS} chars")
    desc = payload.get("description")
    if desc is not None and len(str(desc)) > MAX_DESC_CHARS:
        raise ValueError(f"description exceeds {MAX_DESC_CHARS} chars")
    script = payload.get("validation_script")
    if script is not None and len(str(script)) > MAX_SCRIPT_CHARS:
        raise ValueError(
            f"validation_script exceeds {MAX_SCRIPT_CHARS} chars — a "
            "validation command, not a program")
    deps = payload.get("dependencies")
    if deps is not None:
        if not isinstance(deps, list):
            raise ValueError("dependencies must be a list of task_ids")
        if len(deps) > MAX_DEPS_PER_TASK:
            raise ValueError(
                f"dependencies exceed {MAX_DEPS_PER_TASK} per task — a node "
                "waiting on more than that is under-decomposed")
        for d in deps:
            if not ID_RE.match(str(d)):
                raise ValueError(f"dependency '{d}' is not a valid task_id")
    if action == "declare":
        if not str(payload.get("title") or "").strip():
            raise ValueError("declare requires a title — the actionable ask")
    elif action == "status":
        if payload.get("status") not in STATUSES:
            raise ValueError("status must be one of: " + ", ".join(STATUSES))
    elif action == "depend":
        if not ID_RE.match(str(payload.get("on") or "")):
            raise ValueError("depend requires 'on': the blocking task_id")
    elif action == "evidence":
        ref = str(payload.get("ref") or "").strip()
        if not ref:
            raise ValueError(
                "evidence requires a 'ref' — the runlog artifact path proving "
                "the phase's exit criterion")
        if len(ref) > MAX_REF_CHARS:
            raise ValueError(f"evidence ref exceeds {MAX_REF_CHARS} chars")
    # reparent: task_id plus parent_id (which may legitimately be null) is
    # the whole contract.


def ledger_body(payload) -> str:
    """Self-describing markdown body for a ``mindnet_task`` event's LEDGER
    entry, mirroring `roadmap.ledger_body` / `sop.ledger_body`.

    Same two failures as `phases.ledger_body` without it: these payloads carry
    no ``text``, so `projector.build_ledger_entries`' default renders an empty
    body (invisible to keyword recall, which searches the projected markdown)
    and two same-second events hash to the SAME filename, silently overwriting
    each other.

    Total by contract — the projector hands this whatever is on the ledger,
    including rows from earlier versions of the code."""
    if not isinstance(payload, dict):
        return str(payload)
    action = payload.get("action", "")
    head = f"[{action}] mindnet task {payload.get('task_id', '')}"
    title = str(payload.get("title") or "").strip()
    if title:
        head += f" — {title}"
    parts = [head]
    # Only the fields THIS action actually writes, so the entry reads as what
    # happened rather than as a dump of the whole record.
    facts = []
    if action == "declare":
        parent = payload.get("parent_id")
        facts.append(f"parent: {parent}" if parent else "parent: (root)")
        if payload.get("triviality_bypass"):
            facts.append("triviality_bypass: true")
        if payload.get("faculty"):
            facts.append(f"faculty: {payload['faculty']}")
        langs = payload.get("language_context")
        if isinstance(langs, list) and langs:
            facts.append("language_context: " + ", ".join(str(x) for x in langs))
    elif action == "status":
        facts.append(f"status: {payload.get('status', '')}")
    elif action == "depend":
        facts.append(f"depends on: {payload.get('on', '')}")
    elif action == "reparent":
        parent = payload.get("parent_id")
        facts.append(f"parent: {parent}" if parent else "parent: (root)")
    elif action == "evidence":
        facts.append(f"ref: {payload.get('ref', '')}")
    if facts:
        parts.append(" · ".join(facts))
    script = str(payload.get("validation_script") or "").strip()
    if script:
        parts.append(f"validation_script: {script}")
    desc = str(payload.get("description") or "").strip()
    if desc:
        parts.append(desc)
    return "\n\n".join(parts)


# --- traversal (all iterative) ------------------------------------------------

def roots(tasks) -> list:
    """Tasks with no resolvable parent. A dangling ``parent_id`` counts as a
    root: the referenced task is not in the graph, so nothing can be inherited
    from it (and reads stay total)."""
    return [tid for tid, t in tasks.items()
            if t.parent_id is None or t.parent_id not in tasks]


def ancestors(tasks, task_id) -> tuple:
    """The parent chain, NEAREST FIRST. Total: stops cleanly on an unknown
    task, a dangling parent, or a pre-existing loop — never spins."""
    out: list = []
    seen = {task_id}
    cur = tasks.get(task_id)
    while cur is not None and cur.parent_id is not None:
        pid = cur.parent_id
        if pid in seen or pid not in tasks:
            break
        out.append(pid)
        seen.add(pid)
        cur = tasks[pid]
    return tuple(out)


def _children_index(tasks) -> dict:
    idx: dict = {}
    for tid, t in tasks.items():
        if t.parent_id is not None:
            idx.setdefault(t.parent_id, []).append(tid)
    return idx


def descendants(tasks, task_id) -> set:
    """Every task beneath *task_id*. Explicit stack + visited set, so a loop in
    the data terminates instead of hanging."""
    idx = _children_index(tasks)
    out: set = set()
    stack = list(idx.get(task_id, ()))
    while stack:
        tid = stack.pop()
        if tid in out or tid == task_id:
            continue
        out.add(tid)
        stack.extend(idx.get(tid, ()))
    return out


def depth_of(tasks, task_id) -> int:
    """Hops from *task_id* up to its root. ``-1`` when the chain is
    unresolvable because it loops — a sentinel the caller can report rather
    than an exception that would take down a projection."""
    if task_id not in tasks:
        return -1
    seen = {task_id}
    n = 0
    cur = tasks[task_id]
    while cur.parent_id is not None:
        pid = cur.parent_id
        if pid not in tasks:
            break               # dangling parent: treat this node as a root
        if pid in seen:
            return -1           # hierarchy cycle
        seen.add(pid)
        n += 1
        cur = tasks[pid]
    return n


def _all_depths(tasks) -> dict:
    """Every task's depth in ONE memoized pass. `check_graph` needs all of
    them, and calling `depth_of` per node is O(n^2) — measurably too slow on a
    deep graph, which is exactly the shape the bounds exist to catch."""
    depth: dict = {}
    for start in tasks:
        if start in depth:
            continue
        chain: list = []
        seen: set = set()
        cur = start
        while True:
            if cur in depth:
                base = depth[cur] + 1 if depth[cur] >= 0 else -1
                break
            if cur in seen:
                base = -1
                break
            seen.add(cur)
            chain.append(cur)
            t = tasks.get(cur)
            if t is None or t.parent_id is None or t.parent_id not in tasks:
                base = 0
                break
            cur = t.parent_id
        for tid in reversed(chain):
            depth[tid] = base
            if base >= 0:
                base += 1
    return depth


# --- triple-threat cycle detection --------------------------------------------

def dependency_cycle(tasks):
    """Threat 1. One dependency cycle as a tuple of task_ids, or None.

    Iterative three-colour DFS: GREY marks the current path, so meeting a GREY
    node is a back edge — the cycle is the path suffix from that node on.
    Dangling dependencies are ignored here; `check_graph` reports them
    separately."""
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict = {}
    for start in sorted(tasks):
        if color.get(start, WHITE) != WHITE:
            continue
        color[start] = GREY
        path = [start]
        stack = [(start, 0)]
        while stack:
            tid, i = stack[-1]
            deps = [d for d in tasks[tid].dependencies if d in tasks]
            if i < len(deps):
                stack[-1] = (tid, i + 1)
                nxt = deps[i]
                c = color.get(nxt, WHITE)
                if c == GREY:
                    return tuple(path[path.index(nxt):])
                if c == WHITE:
                    color[nxt] = GREY
                    path.append(nxt)
                    stack.append((nxt, 0))
            else:
                color[tid] = BLACK
                stack.pop()
                path.pop()
    return None


def hierarchy_cycle(tasks, task_id, new_parent_id):
    """Threat 2. Reason string when parenting *task_id* under *new_parent_id*
    would close a loop, else None. Promoting to a root (None) and parenting
    under a task not in the graph are both always legal."""
    if new_parent_id is None:
        return None
    if new_parent_id == task_id:
        return f"'{task_id}' cannot be its own parent"
    if new_parent_id not in tasks:
        return None
    if new_parent_id in descendants(tasks, task_id):
        return (f"hierarchy cycle: '{new_parent_id}' is already a descendant "
                f"of '{task_id}'")
    return None


def cross_graph_conflict(tasks):
    """Threat 3. ``(task_id, dep_id)`` for the first task depending on its own
    ancestor or its own descendant, else None.

    Neither single-graph check sees this. Parent P depending on child C means P
    cannot start until C completes, while `can_complete` refuses to close P
    while C is open. C depending on ancestor P is the mirror deadlock. Both are
    structurally sound in isolation and jointly unsatisfiable.

    "dep is my descendant" is tested as "I am dep's ancestor" — an O(depth)
    walk, instead of materialising the whole descendant set per node."""
    for tid in sorted(tasks):
        deps = tasks[tid].dependencies
        if not deps:
            continue
        anc = set(ancestors(tasks, tid))
        for dep in deps:
            if dep in anc:
                return (tid, dep)
            if tid in ancestors(tasks, dep):
                return (tid, dep)
    return None


def check_graph(tasks) -> list:
    """Every structural violation as a human-readable reason string; ``[]`` for
    a sound graph. Total — never raises, whatever the folded data looks like."""
    out: list = []
    if len(tasks) > MAX_NODES:
        out.append(f"too many nodes: {len(tasks)} exceeds MAX_NODES "
                   f"{MAX_NODES}")
    depths = _all_depths(tasks)
    for tid in sorted(tasks):
        t = tasks[tid]
        if len(t.dependencies) > MAX_DEPS_PER_TASK:
            out.append(f"'{tid}' has too many dependencies: "
                       f"{len(t.dependencies)} exceeds {MAX_DEPS_PER_TASK}")
        for dep in t.dependencies:
            if dep not in tasks:
                out.append(f"'{tid}' depends on unknown task '{dep}'")
        d = depths.get(tid, 0)
        if d == -1:
            out.append(f"hierarchy cycle in the parent chain of '{tid}'")
        elif d > MAX_DEPTH:
            out.append(f"'{tid}' sits at depth {d}, past MAX_DEPTH "
                       f"{MAX_DEPTH}")
    cyc = dependency_cycle(tasks)
    if cyc:
        out.append("dependency cycle: " + " -> ".join(cyc))
    dead = cross_graph_conflict(tasks)
    if dead:
        out.append(f"deadlock: '{dead[0]}' depends on its own relative "
                   f"'{dead[1]}'")
    return out


# --- readiness and completion (pure predicates over the fold) -----------------

def ready(tasks) -> list:
    """task_ids eligible to start, sorted for determinism.

    Three conditions: the task is PENDING, every dependency is COMPLETED, and
    no ancestor is BLOCKED. The ancestor rule is transitive on purpose — if
    only the immediate parent blocked, blocking would be escaped by nesting one
    level deeper.

    The ancestor answer is cached **by `parent_id`**, which is exact rather than
    approximate: two tasks with the same parent have, by definition, the same
    ancestor set. Keying on the parent instead of the task means a branching
    graph climbs each chain once instead of once per sibling, and — the reason it
    is written this way rather than as a hand-rolled memoized climb — the
    computation itself is still `ancestors()` verbatim, so the cache cannot drift
    from that function's own total handling of dangling parents and pre-existing
    loops. MN-7's orchestrator reads this once per wave over graphs up to
    `MAX_NODES`."""
    out: list = []
    anc_blocked: dict = {}
    for tid in sorted(tasks):
        t = tasks[tid]
        if t.status != STATUS_PENDING:
            continue
        if any(tasks.get(d) is None or tasks[d].status != STATUS_COMPLETED
               for d in t.dependencies):
            continue
        pid = t.parent_id
        if pid is None:
            blocked = False
        elif pid in anc_blocked:
            blocked = anc_blocked[pid]
        else:
            blocked = any(tasks[a].status == STATUS_BLOCKED
                          for a in ancestors(tasks, tid))
            anc_blocked[pid] = blocked
        if blocked:
            continue
        out.append(tid)
    return out


def can_complete(tasks, task_id, satisfied=None):
    """None when *task_id* may be marked COMPLETED, else the blocking reason.

    *satisfied* is an INJECTED predicate ``(task_id, phase) -> bool`` supplied
    by the controller. Injected rather than imported so the dependency stays
    one-directional: `controller.py` reads this module, never the reverse.

    Four gates, in this order:

    1. **The subtree**, first and unconditionally. ADR-0005's bypass waives the
       *method* (brainstorm/plan/PRD for a one-line fix), never graph
       integrity: a trivial parent with an open child is still not finished.
    2. **The predicate's existence.** Without it the terminal phase cannot be
       checked, and a gate that cannot check must refuse. This module ships
       ahead of the controller, so a missing argument must not silently loosen
       the very thing it was added to tighten.
    3. **The artifact**, for a non-bypassed task.
    4. **The terminal phase** — `pr-green` normally, ``TRIVIAL_EXIT_PHASE`` for
       a bypassed one. Completion previously read four things and *phase* was
       not among them, so `pr-green`'s exit evidence gated nothing at all."""
    t = tasks.get(task_id)
    if t is None:
        return f"no such task '{task_id}'"
    open_kids = sorted(d for d in descendants(tasks, task_id)
                       if tasks[d].status not in CLOSED_STATUSES)
    if open_kids:
        return (f"'{task_id}' has {len(open_kids)} open descendant(s): "
                + ", ".join(open_kids[:5]))
    if satisfied is None:
        return (f"'{task_id}' cannot complete: no satisfaction predicate was "
                f"supplied, so the terminal phase cannot be verified")
    if not t.triviality_bypass and not t.evidence:
        return (f"'{task_id}' has no evidence — a validation run must record "
                f"its artifact before this task can complete")
    gate = TRIVIAL_EXIT_PHASE if t.triviality_bypass else PHASES[-1]
    try:
        ok = bool(satisfied(task_id, gate))
    except Exception as e:          # foreign code; a fault is not a pass
        return (f"'{task_id}' cannot complete: the satisfaction predicate "
                f"raised {type(e).__name__} — this gate fails closed")
    if not ok:
        return (f"'{task_id}' has not satisfied '{gate}' — clearing that "
                f"phase's exit criterion is what completion means")
    return None


# --- refusals the fold has to record ------------------------------------------

def _note(existing, reason) -> tuple:
    """*existing* violations plus *reason*, capped at MAX_VIOLATIONS."""
    if len(existing) >= MAX_VIOLATIONS:
        return existing
    return existing + (reason,)


def _placement_refusal(tasks, task_id, cand, parent):
    """Reason string when placing *task_id* under *parent* must be declined,
    else None. THE one rule for every writer of ``parent_id`` — the `reparent`
    action, the re-declare upsert, and the first declare all route through
    here, so the three paths refuse the same placements with the same message.
    They did not: `reparent` shipped with only the cycle check and the first
    declare with none at all, so an over-deep edge landed simply by being
    spelled differently.

    *cand* is the record as it would stand WITHOUT the move — an existing task
    for the two update paths, the freshly built (still parentless) record for a
    first declare. Four gates:

    - **No-op.** Echoing the parent a task already has is not a move.
    - **Hierarchy cycle** — parenting a task under its own descendant.
    - **Status.** Settled work is not restructured (its subtree membership is
      part of the completion record `can_complete` already accepted), and open
      work is not hung under a closed parent (that would retroactively falsify
      the same record with nothing on the ledger admitting the change of mind).
    - **Depth**, measured over the WHOLE resulting subtree on a probe copy of
      the graph — the `depend` branch's idiom. Checking only the placed node is
      how a two-deep subtree slips one past the wall, and a task's subtree can
      be non-empty even on its first declare (children may have been declared
      against it before it existed).

    A parent NOT YET in the graph is never refused: ledger order is not
    declaration order, so a forward reference is legitimate and its depth is
    simply unresolvable at that moment. `hierarchy_cycle` already returns None
    for an absent parent, and `_all_depths` treats a dangling parent as a root,
    so all three gates fall through by construction rather than by a special
    case that could rot.

    There is deliberately NO phase gate. ``Task.phase`` is the declared entry
    phase with no writer in this module, so the comparison would read defaults
    and pass vacuously; the controller owns that check."""
    if parent == cand.parent_id:
        # Without this, re-declaring a COMPLETED task to sharpen its title —
        # while faithfully echoing the parent_id it already has — would
        # manufacture a violation for a move that is not happening.
        return None
    cyc = hierarchy_cycle(tasks, task_id, parent)
    if cyc is not None:
        return f"refused placement of '{task_id}': {cyc}"
    if cand.status in CLOSED_STATUSES:
        return (f"refused placement of '{task_id}': it is {cand.status} — "
                f"settled work is not restructured after the fact")
    dest = tasks.get(parent) if parent is not None else None
    if dest is not None and dest.status in CLOSED_STATUSES:
        return (f"refused placement of '{task_id}': parent '{parent}' is "
                f"{dest.status} and cannot take on open work")
    probe = dict(tasks)
    probe[task_id] = replace(cand, parent_id=parent)
    depths = _all_depths(probe)
    reached = [depths.get(m, 0) for m in {task_id} | descendants(probe, task_id)]
    if any(d < 0 for d in reached):
        return (f"refused placement of '{task_id}': it would leave an "
                f"unresolvable parent chain")
    worst = max(reached, default=0)
    if worst > MAX_DEPTH:
        return (f"refused placement of '{task_id}': the subtree would reach "
                f"depth {worst}, past MAX_DEPTH {MAX_DEPTH}")
    return None


# --- the fold -----------------------------------------------------------------

def fold(evs) -> dict:
    """Deterministic, total fold of ``mindnet_task`` events into
    ``task_id -> Task``.

    ``declare`` upserts: a re-declare sharpens the title/description but must
    NEVER reset accumulated status, dependencies, or evidence (roadmap's ``set``
    replaces wholesale — right for a flat plan, destructive for a graph node
    carrying progress).

    Structural refusals happen HERE, not just in `check_graph`: a `depend` that
    would close a cycle, ANY placement that would cycle / overflow the depth
    bound / restructure settled work (whether it arrives as a `reparent`, a
    re-declare, or a first declare — all three call `_placement_refusal`), and
    a late `triviality_bypass` are declined. The event stays on the ledger —
    appends are immutable — but the projection never enters the bad state, so
    one hallucinated edge cannot corrupt the graph permanently. Each refusal
    lands in the record's ``violations`` so the projection can say WHY it
    disagrees with the ledger.

    The phase axis is not written here at all: `mindnet_phase` owns it."""
    tasks: dict = {}
    for ev in evs or []:
        if getattr(ev, "type", None) != "mindnet_task":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        action, tid = p.get("action"), p.get("task_id")
        if action not in ACTIONS or not isinstance(tid, str) or not tid:
            continue
        cur = tasks.get(tid)

        if action == "declare":
            title = str(p.get("title") or "").strip()
            if not title:
                continue
            raw_parent = p.get("parent_id")
            parent = (str(raw_parent)
                      if isinstance(raw_parent, str) and raw_parent else None)
            if cur is None:
                if len(tasks) >= MAX_NODES:
                    continue        # runaway-decomposition wall, pre-allocation
                trivial = bool(p.get("triviality_bypass"))
                cand = Task(
                    task_id=tid,
                    title=title[:MAX_TITLE_CHARS],
                    # Born parentless; the placement is decided below by the
                    # same rule the other two writers answer to. Assigning
                    # `parent` here instead is what let a first declare land an
                    # edge that `reparent` would have refused.
                    parent_id=None,
                    description=str(p.get("description") or "")[:MAX_DESC_CHARS],
                    # The bypass waives brainstorm/plan/PRD, so a trivial task
                    # is BORN at `execution` — the spec's own written exception
                    # to the forward-skip ban. DERIVED here rather than taken
                    # from the payload: an entry phase a caller could name is
                    # the teleport this module just stopped accepting.
                    phase=TRIVIAL_ENTRY_PHASE if trivial else PHASES[0],
                    language_context=_id_tuple(p.get("language_context")),
                    validation_script=str(
                        p.get("validation_script") or "")[:MAX_SCRIPT_CHARS],
                    faculty=str(p["faculty"]) if p.get("faculty") else None,
                    triviality_bypass=trivial,
                )
                if parent is not None:
                    why = _placement_refusal(tasks, tid, cand, parent)
                    cand = (replace(cand, parent_id=parent) if why is None
                            else replace(cand, violations=_note((), why)))
                tasks[tid] = cand
                continue
            fields = {"title": title[:MAX_TITLE_CHARS]}
            viol = cur.violations
            if parent is not None:
                # A re-declare carrying parent_id IS a move, so it answers to
                # the same gate — otherwise the hardened action is trivially
                # routed around by spelling the move as a declare.
                why = _placement_refusal(tasks, tid, cur, parent)
                if why is None:
                    fields["parent_id"] = parent
                else:
                    viol = _note(viol, why)
            if p.get("description"):
                fields["description"] = str(p["description"])[:MAX_DESC_CHARS]
            if p.get("validation_script"):
                fields["validation_script"] = str(
                    p["validation_script"])[:MAX_SCRIPT_CHARS]
            if p.get("language_context"):
                fields["language_context"] = _id_tuple(p["language_context"])
            if p.get("faculty"):
                fields["faculty"] = str(p["faculty"])
            if "triviality_bypass" in p and \
                    bool(p["triviality_bypass"]) != cur.triviality_bypass:
                # FIRST DECLARE ONLY. Applying the flag on every re-declare was
                # a completion skeleton key: declare a task non-trivial, advance
                # it, then re-declare it trivial and it closed with no evidence
                # at all. The change is dropped and the ATTEMPT recorded — the
                # event is already immutable on the ledger, so a silent drop
                # would leave the projection unable to explain itself.
                want = bool(p["triviality_bypass"])
                if not want and _at_or_past(cur.phase, TRIVIAL_ENTRY_PHASE):
                    viol = _note(viol, (
                        f"refused: clearing triviality_bypass on '{tid}', "
                        f"already at or past '{TRIVIAL_ENTRY_PHASE}'"))
                else:
                    viol = _note(viol, (
                        f"ignored: triviality_bypass on '{tid}' is settable on "
                        f"the first declare only (attempted {want})"))
            if viol is not cur.violations:
                fields["violations"] = viol
            tasks[tid] = replace(cur, **fields)

        elif cur is None:
            continue                # lifecycle action on an unknown task: no-op

        elif action == "status":
            if p.get("status") in STATUSES:
                tasks[tid] = replace(cur, status=p["status"])

        elif action == "depend":
            on = p.get("on")
            if not isinstance(on, str) or not on or on == tid:
                continue
            if on in cur.dependencies:
                continue            # idempotent
            if len(cur.dependencies) >= MAX_DEPS_PER_TASK:
                continue
            cand = replace(cur, dependencies=cur.dependencies + (on,))
            probe = dict(tasks)
            probe[tid] = cand
            if dependency_cycle(probe) is not None \
                    or cross_graph_conflict(probe) is not None:
                continue            # refuse the edge, keep the graph sound
            tasks[tid] = cand

        elif action == "reparent":
            raw_parent = p.get("parent_id")
            parent = (str(raw_parent)
                      if isinstance(raw_parent, str) and raw_parent else None)
            why = _placement_refusal(tasks, tid, cur, parent)
            if why is not None:
                tasks[tid] = replace(cur, violations=_note(cur.violations, why))
                continue
            tasks[tid] = replace(cur, parent_id=parent)

        elif action == "evidence":
            ref = str(p.get("ref") or "").strip()[:MAX_REF_CHARS]
            if ref and ref not in cur.evidence:
                tasks[tid] = replace(cur, evidence=cur.evidence + (ref,))

    # depth is DERIVED, never carried on an event — one memoized pass at the end
    # so a re-parent mid-stream can never leave a stale depth behind.
    depths = _all_depths(tasks)
    return {tid: replace(t, depth=depths.get(tid, 0))
            for tid, t in tasks.items()}
