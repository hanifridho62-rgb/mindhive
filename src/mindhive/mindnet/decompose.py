# src/mindhive/mindnet/decompose.py
"""MN-11 — a task proposes its own subtasks.

The founding prompt asked for a graph where "subtask atau task dapat bertambah
sesuai kebutuhan dari apa yang diinginkan user", and gave the shape of the ask
directly: a `pembuatan graph` task decomposing into `visualisasi dari informasi`,
`generate setiap kapan`, `informasi apa saja yang ingin divisualisasikan`, and so
on — "hingga tidak ada satupun yang terlewatkan".

MN-2 built the hierarchy and `hive mindnet declare --parent` writes one edge. What
was missing is the part that decides WHAT the subtasks are, and that is a judgement
call, so it needs a model. This module asks one and then refuses to trust it.

## Model output is untrusted input

Everything after the spawn is validation, because a proposal reaching the graph
unchecked can do lasting damage: `tasks.fold` silently DROPS an edge it considers
unsound (a cycle, an over-deep parent, too many dependencies), so a bad proposal
would not error — it would half-land, and the operator would see "declared 6
subtasks" with four of them missing. Worse, `declare` is the one action that
`triviality_bypass` is honoured on, and a task id that collides with an existing
one is an UPSERT, not an error: a hallucinated id matching a real task would
rewrite that task's title.

So a proposal is checked against `tasks.ID_RE`, the char caps, collision with every
existing id, `MAX_DEPTH` at the proposed parent, `MAX_NODES` for the whole graph,
and finally `check_graph` on a probe graph holding all of them at once — before a
single event is appended.

## Dry run is the default

Same reasoning as `orchestrate --execute`: this writes to an append-only ledger,
where a mistake cannot be deleted, only superseded. The safe form has to be the one
an operator gets by accident.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from . import agent as ag_mod
from . import tasks as t_mod

# How many subtasks one call may propose. Not a model limit — a blast-radius one:
# an operator reviewing a dry run can hold this many in their head, and
# `MAX_DEPS_PER_TASK` (32) already says a node waiting on more than that is
# under-decomposed.
MAX_PROPOSED = 12

# Agents do real thinking here but produce a small answer.
DEFAULT_TIMEOUT_S = 300

_SLUG_STRIP = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class Proposal:
    """One proposed subtask, and whether it survived validation."""

    task_id: str
    title: str
    description: str = ""
    ok: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Decomposition:
    """The whole call. ``declared`` is empty on a dry run."""

    task_id: str
    proposals: list = field(default_factory=list)
    declared: list = field(default_factory=list)
    reason: str = ""

    @property
    def usable(self) -> list:
        return [p for p in self.proposals if p.ok]


def prompt_for(task) -> str:
    """The instruction asking for subtasks, and ONLY subtasks.

    The output contract is stated twice — once as a schema and once as a rule about
    what not to include — because the parser tolerates surrounding prose but cannot
    recover from a different shape. `slugify` handles ids so the model is told to
    write plain titles rather than guessing this repo's `ID_RE`."""
    # Defused through the SAME two helpers `agent.prompt_for` uses, not a second
    # implementation of the idea. This is the OTHER prompt this package sends to a
    # model, and it was rendering both fields raw at column 0 with no fence — so
    # every attack the agent path was hardened against in this branch applied here
    # verbatim, three files away, in a function whose sibling looked solved.
    #
    # Not a theoretical exposure: `declare` on an existing id is an UPSERT (see
    # this module's docstring), so a reply this prompt is talked into producing can
    # rewrite a real task's title. The proposal surface is a write surface.
    #
    # Imported inside the function the way `agent.prompt_for` imports it, for the
    # same reason: `brief` reaches back into this package and a module-level import
    # would close the ring.
    from . import brief as br_mod

    tid = br_mod.safe_line(getattr(task, "task_id", "")) or "?"
    title = br_mod.safe_line(getattr(task, "title", ""))
    desc = br_mod.safe_block(getattr(task, "description", ""))

    lines = [
        "Break one software task into the subtasks that finishing it actually "
        "requires.",
        "",
        f"Parent task: {tid}",
        f"Title:       {title or '(none recorded)'}",
    ]
    if desc:
        # Quoted per line, matching `agent.prompt_for`. `safe_block` keeps the line
        # structure an honest description needs; the marker is what stops any of
        # those lines reaching column 0 and speaking in the prompt's own voice.
        lines += ["", "Description:"]
        lines += [f"> {ln}" if ln else ">" for ln in desc.splitlines()]
    lines += [
        "",
        "Rules:",
        f"  - At most {MAX_PROPOSED} subtasks. Fewer is better than padded.",
        "  - Each must be independently completable and worth its own PR-sized "
        "unit of work.",
        "  - Cover what would otherwise be forgotten: edge cases, the failure "
        "path, the test surface, the operator-facing surface.",
        "  - Do NOT restate the parent, and do NOT invent work outside its scope.",
        "  - If the task is already atomic, return an empty list. That is a "
        "valid and useful answer.",
        "",
        "Reply with ONE json object and nothing that must be parsed around it:",
        '  {"subtasks": [{"title": "...", "description": "..."}]}',
        "",
        "No ids — they are derived from the titles. No other keys.",
    ]
    return "\n".join(lines)


def slugify(title, parent_id="", taken=()) -> str:
    """A `tasks.ID_RE`-legal id derived from *title*, unique against *taken*.

    Derived here rather than asked for, because an id is this repo's contract and
    not something a model should be guessing: `ID_RE` is
    ``^[a-z0-9][a-z0-9-]{0,63}$`` and a violation would be refused at the append
    surface after the operator had already approved the proposal.

    Prefixed with the parent so a subtask reads as belonging to it in `hive mindnet
    list`, and suffixed with a counter only on a real collision."""
    base = _SLUG_STRIP.sub("-", str(title or "").lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base) or "subtask"
    prefix = _SLUG_STRIP.sub("-", str(parent_id or "").lower()).strip("-")
    stem = f"{prefix}-{base}" if prefix else base
    stem = stem[:56].rstrip("-") or "subtask"
    if not t_mod.ID_RE.match(stem):
        stem = f"t-{stem}"[:56].rstrip("-")
    candidate, n = stem, 2
    seen = set(taken)
    while candidate in seen or not t_mod.ID_RE.match(candidate):
        candidate = f"{stem}-{n}"[:64].rstrip("-")
        n += 1
        if n > 99:                                  # pathological; give up cleanly
            return ""
    return candidate


def parse(text) -> tuple:
    """``(subtasks, reason)`` from a model reply. ``reason`` is why it is empty.

    Tolerates prose and fences around the object — `reflect.parse_reflection` set
    that precedent — but not a different shape, and an unparseable reply must say so
    rather than silently decomposing into nothing (which would read as "this task is
    already atomic", a completely different claim)."""
    if not str(text or "").strip():
        return [], "the agent returned no text"
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        return [], "no json object in the agent's reply"
    try:
        obj = json.loads(text[s:e + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        return [], f"the agent's json did not parse: {exc}"
    if not isinstance(obj, dict):
        return [], "the agent's json was not an object"
    raw = obj.get("subtasks")
    if raw is None:
        return [], "the agent's json carried no 'subtasks' key"
    if not isinstance(raw, list):
        return [], "'subtasks' was not a list"
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        out.append((title, str(item.get("description") or "").strip()))
    return out, ""


def validate(graph, parent_id, pairs) -> list:
    """Turn ``(title, description)`` pairs into checked `Proposal`s.

    Every refusal is decided HERE, before anything is appended, because
    `tasks.fold` drops an unsound edge SILENTLY — a half-landed decomposition would
    report success and leave gaps. The probe graph is checked with `check_graph`
    holding all proposals at once, since a set can be individually legal and jointly
    over-depth or over-count."""
    graph = graph if isinstance(graph, dict) else {}
    parent = graph.get(parent_id)
    out: list = []
    if parent is None:
        return [Proposal(task_id="", title=t, description=d, ok=False,
                         reason=f"no such parent task '{parent_id}'")
                for t, d in pairs]

    taken = set(graph)
    probe = dict(graph)
    room = t_mod.MAX_NODES - len(graph)

    for title, desc in pairs[:MAX_PROPOSED]:
        if len(title) > t_mod.MAX_TITLE_CHARS:
            title = title[:t_mod.MAX_TITLE_CHARS].rstrip()
        desc = desc[:t_mod.MAX_DESC_CHARS]
        tid = slugify(title, parent_id, taken)
        if not tid:
            out.append(Proposal(task_id="", title=title, description=desc,
                                ok=False, reason="cannot derive a legal task id"))
            continue
        if room <= 0:
            out.append(Proposal(task_id=tid, title=title, description=desc,
                                ok=False,
                                reason=f"the graph is at MAX_NODES "
                                       f"({t_mod.MAX_NODES})"))
            continue

        cand = t_mod.Task(task_id=tid, title=title, description=desc,
                          parent_id=parent_id)
        probe[tid] = cand
        problems = [w for w in t_mod.check_graph(probe) if tid in w]
        if problems:
            del probe[tid]
            out.append(Proposal(task_id=tid, title=title, description=desc,
                                ok=False, reason=problems[0]))
            continue

        taken.add(tid)
        room -= 1
        out.append(Proposal(task_id=tid, title=title, description=desc, ok=True))
    return out


def run(task_id, graph, runner=None, project=None, timeout_s=DEFAULT_TIMEOUT_S,
        root=None, model=None) -> Decomposition:
    """Ask an agent for *task_id*'s subtasks and validate the answer.

    Appends NOTHING — the caller decides whether to declare. *runner* defaults to
    `agent.run`, the same seam the orchestrator uses, so this inherits the durable
    transcript and the outcome mapping (an auth failure or usage limit comes back as
    a reason, not as an empty decomposition that reads like "already atomic")."""
    task = (graph or {}).get(task_id)
    if task is None:
        return Decomposition(task_id=task_id,
                             reason=f"no such task '{task_id}'")
    run_agent = runner or ag_mod.run
    try:
        res = run_agent(f"mn11-{task_id}", prompt_for(task), root=root,
                        project=project, timeout_s=timeout_s, model=model)
    except Exception as exc:                            # noqa: BLE001
        return Decomposition(task_id=task_id,
                             reason=f"the agent could not be run: "
                                    f"{type(exc).__name__}: {exc}")
    # `agent.ran`, not a local re-test: MN-7's P0 was mistaking "never started" for
    # "the task failed", and one home for that question is how it stays fixed.
    if not ag_mod.ran(res):
        return Decomposition(task_id=task_id,
                             reason=getattr(res, "reason", "")
                             or "the agent never ran")

    # `text` FIRST, and the reason is a measured one: `excerpt` is cut at
    # `runlog.EXCERPT_CHARS` for the ledger, and on 2026-09-08 mission
    # `lm-84c06ad6` answered four sound subtasks in 2507 characters. The cut
    # landed inside a string, `parse` closed the object on an inner brace, and
    # the mission degraded to root-only blaming the agent's json -- which was
    # well formed. The fall-backs stay: an injected runner sets only `excerpt`.
    pairs, why = parse(getattr(res, "text", "")
                       or getattr(res, "excerpt", "")
                       or getattr(res, "stdout", ""))
    if why:
        return Decomposition(task_id=task_id, reason=why)
    return Decomposition(task_id=task_id,
                         proposals=validate(graph, task_id, pairs))


def declare(decomposition, project=None, session_id=None) -> Decomposition:
    """Append a `declare` row per usable proposal. Returns what actually landed.

    The sidecar is NOT rebuilt here — the caller does it once after the whole batch,
    so a six-subtask decomposition pays one ~264 ms fold rather than six."""
    from .. import events
    landed = []
    for p in decomposition.usable:
        payload = {"action": "declare", "task_id": p.task_id, "title": p.title,
                   "parent_id": decomposition.task_id}
        if p.description:
            payload["description"] = p.description
        try:
            t_mod.validate_payload(payload)
            events.append_event("mindnet_task", payload, session_id=session_id,
                                tags=["mindnet", "task", "auto"],
                                project=project)
            # The phase axis too: a task declared on the graph alone orphans every
            # phase event it ever receives (`controller.fold` is create-only).
            events.append_event(
                "mindnet_phase",
                {"action": "declare", "task_id": p.task_id,
                 "phase": t_mod.PHASES[0]},
                session_id=session_id, tags=["mindnet", "phase", "auto"],
                project=project)
            landed.append(p.task_id)
        except Exception as exc:                        # noqa: BLE001
            decomposition = replace(
                decomposition,
                proposals=[replace(q, ok=False,
                                   reason=f"append failed: {exc}")
                           if q.task_id == p.task_id else q
                           for q in decomposition.proposals])
    return replace(decomposition, declared=landed)
