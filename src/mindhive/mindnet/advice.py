# src/mindhive/mindnet/advice.py
"""MN-3 U2 — the advice resolver. The unit that makes injection MECHANICAL.

Before this module, `inject.py` ended its directive with one line of prose:
"Recall the ledger, then read the code, then consult the skill catalog." No
query ran. The agent was told a catalog existed and left to find it, which in
practice means it did not. This module runs the queries and hands back what
they returned.

## Why resolution happens HERE and not on the hook path

The obvious implementation calls the registries from inside
`inject.extend_hook_context`. It cannot: every one of the three sources is
ledger- or filesystem-backed, and that path fires on EVERY user prompt.

- `plugins.recommend_for` -> `events.read_all` (~264 ms) plus a `live_inventory`
  filesystem scan across the whole plugin cache.
- `sop.current` -> `events.read_all` again.
- `incidents.summaries` -> a directory of per-incident `.md` files.

`inject.py` carries a property test — `test_the_hook_path_never_calls_read_all`
— pinning the no-ledger rule as structure rather than as a timing bound, and it
is right to. So resolution runs at WRITE time, where a ledger read is already
being paid (`sidecar.rebuild`, a controller transition), and the resolved text
rides the sidecar to the hook. Python still does the retrieval and the real
text still lands in the prompt; only the clock moves.

## Everything leaves here already rendered

`Advice` holds three tuples of finished, sanitized, capped LINES rather than
structured records. Two reasons, both about the boundary this crosses:

1. It serializes into the sidecar as plain JSON lists with no custom encoder
   and no schema for a future reader to misinterpret (`telemetry.Activity` made
   the same call for the same reason).
2. It keeps the hook-side consumer dumb. `inject.advice_block` joins strings;
   it cannot accidentally reintroduce a registry lookup, because there is no
   structure left to look anything up with.

## Per-source isolation, not one big try

Each of the three lookups is wrapped separately. One `try` around all three
reads identically in every normal case and then, on the day the plugin cache is
mid-write, silently drops the SOPs and the prior-failure constraints as well —
the block goes quiet exactly when the machinery matters most. The tests pin
this directly, because the all-in-one version passes everything else.

## Retrieved text is untrusted input

Plugin descriptions, SOP bodies and incident excerpts are third-party strings on
their way into a prompt. Every one goes through `inject.safe_text`, which
collapses whitespace (so a newline cannot become a new directive LINE) and
strips `MARK` (so retrieved content cannot forge a directive HEADER). Imported
from `inject` rather than restated: two spellings of the marker is exactly the
drift that makes a sanitizer stop working.

## Import direction

This module imports `inject`; `inject` does not import this one — it only reads
the plain strings off the sidecar. `sidecar` reaches this module through a lazy
import inside `rebuild`, the same idiom `plugins.recommend_for` uses to keep off
`incidents`/`recommend` at import time. So there is no cycle, and the heavy
write-time dependency never loads on the read path.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from .. import events as ev_mod
from .inject import safe_text

# Bounds. The per-item cap is what makes the injector's total budget
# predictable, and `test_the_resolved_total_fits_the_injector_budget` pins the
# arithmetic so widening one constant without the other fails loudly here rather
# than silently truncating a directive in production.
MAX_SKILLS = 3          # the directive's "top 3 highly relevant"
MAX_SOPS = 2
MAX_CONSTRAINTS = 2
MAX_ITEM_CHARS = 240
# Mirrors `inject.MAX_ADVICE_CHARS`; see the note there on why the renderer's
# own 195 chars of headers and bullets have to be counted too.
MAX_ADVICE_CHARS = 2000

# Words too common to carry any signal about what a task is ABOUT. Without this
# a task titled "update the code" matches every SOP mentioning "code".
_STOP = frozenset((
    "the", "and", "for", "with", "that", "this", "into", "onto", "over",
    "under", "from", "when", "then", "than", "have", "has", "was", "were",
    "are", "not", "but", "all", "any", "its", "it", "a", "an", "of", "to",
    "in", "on", "at", "by", "or", "is", "be", "as", "use", "using", "used",
    "add", "new", "get", "set", "run", "make", "made", "do", "does", "done",
    "code", "file", "files", "test", "tests", "work", "task", "change",
    "changes", "update", "updates", "fix", "fixes", "should", "must", "will",
    # Inflections of the above. They matter because `_STOP` is checked against
    # the RAW word while matching runs on the STEM: "test" was filtered but
    # "testing" survived as the stem "test", so a task titled "Testing the
    # updated dashboard" and an unrelated SOP about "testing the updated
    # release notes" shared {test, updat} and cleared the threshold on two
    # words carrying no topic at all. Listing them lets `_STOP_STEMS` fold
    # every form into one filtered stem.
    "testing", "tested", "updating", "updated", "changing", "changed",
    "fixing", "fixed", "working", "worked", "adding", "added", "making",
    "running", "runs", "coding", "tasks", "uses", "needed", "needs",
))

_WORD_RE = re.compile(r"[a-z0-9]+")

# The forbidding sentence, verbatim from the directive. One home: the tests
# assert on "forbidden" and a future edit that softens this into a suggestion
# should have to come through here.
CONSTRAINT_TEMPLATE = (
    "SYSTEM WARNING: In a previous execution, this approach failed due to "
    "{reason}. You are strictly forbidden from repeating this pattern. Use an "
    "alternative method.")

# DERIVED, not guessed. A hand-picked 90 plus the template's own 153 chars came
# to 243 against a 240-char item cap, so a long root cause pushed the closing
# sentence off the end and the constraint rendered as "...Use an alternative
# meth" — losing exactly the prohibition that makes it a constraint rather than
# trivia. Deriving it means the two cannot drift.
MAX_REASON_CHARS = MAX_ITEM_CHARS - len(CONSTRAINT_TEMPLATE.format(reason=""))

# How many keyword overlaps a candidate needs before it is considered relevant.
# One shared word is noise; two is a signal.
MIN_OVERLAP = 2

# Session-id prefixes that mean "a spawned orchestration agent wrote this row".
#
# The threat this closes: `resolve_sops` renders SOP bodies VERBATIM, and every
# spawned child holds `hive` on PATH — `agent.prompt_for` closes by instructing
# it to run `hive log` — with its own `MINDHIVE_SESSION`, which
# `events.append_event` stamps onto everything it writes. So a wave-1 agent that
# has been prompt-injected can append one `sop` row, and that text is composed
# into every subsequent agent's brief, inside the fence, indistinguishable from
# human-authored project history (TODOS 26).
#
# The fence's own docstring records the well-formed-imperative-sentence risk as
# ACCEPTED. That argument was made about HUMANS and past sessions writing the
# ledger — a writer outside the blast radius. A previous agent in the same swarm
# is inside it. Different threat model; the accepted risk does not cover it.
#
# A FAMILY rather than one literal (`orchestrator._run_wave` sets
# `f"{worktree.PREFIX}-{tid}"`, `PREFIX = "mn9"`). Only that one is observed
# setting a child's `MINDHIVE_SESSION` today; `mn7-` (`orchestrator.run_key`),
# `mn11-` (`decompose`) and `mn-` (`executor.run_key`) are the same family,
# listed defensively rather than because a spawn path was seen writing SOPs
# through them. One hardcoded string would go stale the first time a new spawner
# appeared, so `test_the_orchestrators_own_prefix_is_covered` asserts against the
# imported constant rather than a copy of it.
#
# `lm-` is `orchestrator.lintester.MISSION_PREFIX`, and it was added the day
# that spawner was found passing no `env=` at all: its children inherited the
# operator's `MINDHIVE_SESSION`, so their rows were attributed to a human, and
# even once the overlay was added the run_key prefix was outside this family.
# Both halves are needed — an identity nothing recognises is no identity.
# `test_the_lintester_mission_prefix_is_covered` pins it the same way.
ORCHESTRATION_PREFIXES = ("mn9-", "mn7-", "mn11-", "mn-", "lm-")


def is_orchestration_session(session_id) -> bool:
    """Whether *session_id* belongs to a spawned orchestration agent.

    Total, and FAILS OPEN. A missing, blank or unstringifiable id is unknown
    authorship, which answers False — the row is composed. Dropping every row
    whose author was never recorded would delete a working feature to close a
    second-order hole, and every SOP written before session stamping, and every
    incident record written before this change."""
    try:
        sid = str(session_id or "").strip()
    except (ValueError, TypeError):
        return False
    return bool(sid) and sid.startswith(ORCHESTRATION_PREFIXES)


class Advice(NamedTuple):
    """Resolved, rendered, sanitized advice for one task.

    Three tuples of plain strings — see the module docstring on why these are
    finished lines and not records."""
    skills: tuple = ()
    sops: tuple = ()
    constraints: tuple = ()
    # How many RELEVANT rows were withheld because a spawned orchestration agent
    # wrote them (TODOS 26). Appended last, and deliberately NOT carried by
    # `to_dict`/`from_dict`: the sidecar feeds the hook path, where this is a
    # stale number about a resolve that already happened, and adding a key there
    # would move the persisted shape and its schema version for no reader. It
    # travels `resolve` -> `brief.build` -> `Stats.authored` and no further, so a
    # rehydrated `Advice` correctly reports 0.
    withheld: int = 0
    # The same number, SPLIT BY SOURCE. One total says an attempt happened; it
    # does not say where, and the two sources are not the same size of hole --
    # `resolve_sops` withholds rows whose BODY would have been rendered
    # verbatim, `resolve_constraints` withholds rows whose attacker-controlled
    # field (`excerpt`) is never rendered at all. An operator triaging a
    # non-zero count needs to know which one moved. Appended last, same
    # `to_dict` exclusion and same reason as `withheld` above.
    withheld_sops: int = 0
    withheld_constraints: int = 0


EMPTY = Advice()


# --- matching ----------------------------------------------------------------------

def stem(word: str) -> str:
    """A crude, deterministic suffix stripper.

    It exists for one specific failure: a task titled "database migration"
    matching a skill described as "use when migrating schemas". Without it the
    two share only "database" and the best-matching skill in the registry is
    never offered — the resolver would be mechanical and useless at the same
    time.

    Deliberately not a real stemmer. No dependency is permitted here, and an
    approximate one is cheap to reason about: it over-stems ("scanning" ->
    "scann", which will not meet "scan") and under-stems irregulars. Both cost
    RECALL only. Nothing downstream treats a stem as a word, so a wrong stem can
    never produce a wrong claim — only a missed match."""
    w = word
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    for suf in ("ing", "ion", "ed"):
        if w.endswith(suf) and len(w) > len(suf) + 2:
            return w[:-len(suf)]
    return w


_STOP_STEMS = frozenset(stem(w) for w in _STOP)


def keywords(text) -> set:
    """Content words in *text*, lowercased and stemmed. Total over non-strings:
    this runs over registry values that a corrupt file can deliver as anything.

    Filtered on BOTH the raw word and its stem. Checking only the raw word let
    every inflected stopword straight back in through the stem — see `_STOP`."""
    try:
        out = set()
        for w in _WORD_RE.findall(str(text or "").lower()):
            if len(w) <= 2 or w in _STOP:
                continue
            s = stem(w)
            if s and s not in _STOP_STEMS:
                out.add(s)
        return out
    except (ValueError, TypeError):
        return set()


def task_text(task) -> str:
    """The task's title and description — the whole basis for every match here.
    The directive is explicit that retrieval is driven by what the task IS."""
    title = getattr(task, "title", "") or ""
    desc = getattr(task, "description", "") or ""
    return f"{title} {desc}"


def _overlap(candidate_text, task_kw) -> int:
    return len(keywords(candidate_text) & task_kw)


def required_overlap(task_kw) -> int:
    """How many shared keywords a candidate needs to count as relevant.

    ADAPTIVE, because a fixed 2 made whole classes of ordinary task titles
    structurally unmatchable rather than merely hard to match. "Refactor"
    yields one keyword; "Fix the failing tests" yields one after stopwords.
    Against a fixed threshold of 2 the maximum achievable overlap is 1, so NO
    source could ever match no matter what the registries held — the feature
    was silently a no-op and looked identical to "nothing relevant exists".

    One shared word out of a one-word task is a weaker signal than two out of
    four, and that is accepted deliberately: a slightly noisy suggestion beats
    a guarantee of silence. A task with no content words at all still resolves
    to nothing, which is honest — there is no basis for a match."""
    return min(MIN_OVERLAP, len(task_kw)) if task_kw else MIN_OVERLAP


def _ranked(rows, task_kw, text_of, limit):
    """The *limit* best-matching rows, most relevant first.

    Sorted on ``(-overlap, index)``: the index tie-break is what keeps the
    result stable when two candidates match equally well, so the injected block
    does not reshuffle itself between two rebuilds of identical data. The
    incoming order is itself deterministic — `sop.current` sorts by
    ``(created, sop_id)``, `incidents.summaries` by ``sorted(glob)`` and
    `recommend.recommend` by ``(-score, name)``."""
    need = required_overlap(task_kw)
    scored = []
    for i, row in enumerate(rows):
        n = _overlap(text_of(row), task_kw)
        if n >= need:
            scored.append((-n, i, row))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [row for _n, _i, row in scored[:limit]]


def _ranked_rendered(rows, task_kw, text_of, limit, render):
    """The *limit* best-matching rows that ACTUALLY RENDER, most relevant first.

    **The cap counts rendered items, not candidates**, and that distinction is
    the whole reason this function exists. All three resolvers used to rank to
    `limit` first and then skip the rows whose render came back empty -- a SOP
    with no body, a skill with no name, an incident with no recorded reason --
    so an unrenderable row spent a slot and produced nothing. A red-team run
    measured `resolve_sops` rendering 0 of 4 legitimate matching SOPs that way:
    the same denial of service that partitioning-before-ranking closes for
    agent-authored rows, reopened by a row that is merely blank.

    Ranking is done over ALL of *rows* and the walk stops at *limit* renders, so
    the ORDER is identical to what the old code produced -- only the rows that
    contributed nothing are no longer counted. The extra cost is ranking rows
    the old code would have discarded; `_ranked` is one pass plus a sort over
    what a registry holds, which is small and already bounded by the caller.

    *render* answers `""` for a row that cannot be shown. It is never called on
    a row that did not match, so a renderer with a side effect (none has one)
    would not see more rows than before -- only the ones it now reaches later in
    the ranking."""
    out = []
    for row in _ranked(rows, task_kw, text_of, len(rows)):
        item = render(row)
        if not item:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return tuple(out)


# --- the three sources ---------------------------------------------------------------

def resolve_skills(task_kw, evs, project=None, reco=None) -> tuple:
    """The top matching skills, each rendered with its INTENDED USE.

    The description is the payload, not the decoration: a name alone sends the
    agent looking for the catalog again, which is the behaviour this whole
    module exists to delete.

    *reco* is `plugins.recommend_for`'s answer, already computed. It takes no
    task and is consulted here BEFORE `task_kw` is touched below, so one wave's
    tasks all get the identical query result and re-running it per task buys
    nothing — 279 ms of the 445 ms this function costs, measured. Passing it in
    is therefore behaviour-preserving by construction, not by promise; ``None``
    keeps the standalone caller's behaviour exactly as it was."""
    from .. import plugins as plugins_mod
    if reco is None:
        reco = plugins_mod.recommend_for(project, evs=evs)
    rows = [r for r in (reco or {}).get("recommendations") or []
            if isinstance(r, dict)]
    def _render(r):
        name = safe_text(r.get("name"), 60)
        desc = safe_text(r.get("description"), MAX_ITEM_CHARS)
        if not name:
            return ""
        return safe_text(f"{name} — {desc}" if desc else name, MAX_ITEM_CHARS)

    return _ranked_rendered(
        rows, task_kw,
        lambda r: f"{r.get('name', '')} {r.get('description', '')}",
        MAX_SKILLS, _render)


def _sop_text(r) -> str:
    return (f"{r.get('sop_id', '')} {r.get('title', '')} {r.get('text', '')}")


def resolve_sops(task_kw, evs):
    """``(rows, withheld)`` — the top matching SOPs carrying their REAL BODIES,
    and how many RELEVANT rows were withheld because an agent wrote them.

    `sop.current` is a pure fold over the events already in hand, so this costs
    nothing beyond the read the caller had already paid — and it inherits the
    registry's own lifecycle rules, so a deleted SOP is absent here without this
    module knowing what 'deleted' means. It also already carries `session_id`
    (`sop.fold` writes the latest event's), which is the whole reason TODOS 26 is
    fixable without a store migration: the marker was on disk and nothing read it.

    **Partition BEFORE ranking, not after.** Ranking first and dropping
    afterwards would let a withheld row consume one of the `MAX_SOPS` slots, so a
    single injected SOP could halve what a legitimate one gets to say — the
    denial-of-service half of the same attack.

    `withheld` counts the agent-authored rows that WOULD have been relevant, not
    every agent-authored row in the store. A store holding fifty of them should
    not report fifty on a brief none of them matched; the operator's question is
    whether anything was kept out of THIS brief.

    Exclusion rather than a heading naming the author, which TODOS 26 offers as
    the alternative: the fence already says everything inside it is background,
    so a second label adds a word rather than a control. The row still exists —
    in the ledger, in `hive sop list`, in STATE.md. It is only kept off the edge
    the amplification travels along. And it is never silent: the count rides out
    to `Stats.authored`, the `mindnet_brief` payload and `hive mindnet brief`."""
    from .. import sop as sop_mod
    rows = [r for r in sop_mod.current(evs or []) if isinstance(r, dict)]
    kept, agent = [], []
    for r in rows:
        (agent if is_orchestration_session(r.get("session_id")) else kept
         ).append(r)
    # Uncapped limit on the withheld side: this is a count, not a render, and
    # capping it at MAX_SOPS would under-report a store with several relevant
    # injected rows — which is precisely the case worth seeing.
    withheld = len(_ranked(agent, task_kw, _sop_text, len(agent))) if agent else 0

    def _render(r):
        body = safe_text(r.get("text"), MAX_ITEM_CHARS)
        if not body:
            return ""
        sid = safe_text(r.get("sop_id"), 64)
        title = safe_text(r.get("title"), 80)
        return safe_text(f"[{sid}] {title}: {body}", MAX_ITEM_CHARS)

    return _ranked_rendered(kept, task_kw, _sop_text, MAX_SOPS, _render), withheld


def _constraint_text(r) -> str:
    return " ".join(str(r.get(k, "") or "") for k in
                    ("signature", "excerpt", "root_cause", "lesson",
                     "active_rule"))


def agent_authored_incident(row) -> bool:
    """Whether an incident summary was produced ONLY by orchestration sessions.

    Two ways to answer False, both the fail-open case (R8):

    - **No ids recorded.** Every record written before `session_ids` existed.
      Absent authorship is unknown authorship, not agent authorship.
    - **Any contributor is not an orchestration session.** A failure a human ALSO
      hit is not agent-manufactured, and the lesson distilled from it is worth
      exactly as much as it was before an agent happened to hit it too.

    **Truncation now fails CLOSED, and that is a deliberate reversal.**
    `incidents.MAX_SESSION_IDS` bounds what the frontmatter carries while
    `sessions` keeps the true count, so a longer `sessions` means contributors
    this row cannot show. The old reading — "a partial view might hide a human,
    so compose it" — turned "too many authors to list" into "trusted", and an
    attacker who can write rows can also make the list overflow. A row whose
    every VISIBLE contributor is an orchestration session is withheld whatever
    `sessions` says; one visible human contributor still composes it, so the
    case the old bullet protected (a failure a human also hit) is protected by
    the human's own id rather than by the absence of information.

    Worth stating plainly what this does and does not close, because the shape
    invites overclaiming: the incident path is materially HARDER to exploit than
    the SOP path. `resolve_constraints` renders `root_cause`, `lesson` and
    `active_rule` — all written by the reflection pass, not by the failing agent,
    which controls only `excerpt`, and `excerpt` is never rendered. So this
    closes a second-order hole and captures the provenance that makes the
    first-order one checkable. It is not the same size of hole as `resolve_sops`'."""
    if not isinstance(row, dict):
        return False
    ids = [str(s) for s in (row.get("session_ids") or [])]
    if not ids:
        return False
    # `sessions` is deliberately NOT consulted. It used to gate this return and
    # a larger value answered False; see the docstring on why that inverted the
    # control it was meant to soften.
    return all(is_orchestration_session(s) for s in ids)


def resolve_constraints(task_kw, project=None):
    """``(rows, withheld)`` — prior failures matching this task rendered as
    prohibitions, and how many relevant ones an agent manufactured.

    A summary with no recorded reason is SKIPPED rather than rendered with an
    empty slot: "failed due to ." costs prompt budget and teaches the agent to
    discount the constraints that do say something.

    Partitioned before ranking for the same reason `resolve_sops` is; see there."""
    from .. import incidents as inc_mod
    rows = [r for r in (inc_mod.summaries(project) or [])
            if isinstance(r, dict)]
    kept = [r for r in rows if not agent_authored_incident(r)]
    agent = [r for r in rows if agent_authored_incident(r)]
    withheld = (len(_ranked(agent, task_kw, _constraint_text, len(agent)))
                if agent else 0)

    def _render(r):
        reason = safe_text(r.get("root_cause") or r.get("lesson")
                           or r.get("active_rule"), MAX_REASON_CHARS)
        if not reason:
            return ""
        return safe_text(CONSTRAINT_TEMPLATE.format(reason=reason),
                         MAX_ITEM_CHARS)

    return (_ranked_rendered(kept, task_kw, _constraint_text, MAX_CONSTRAINTS,
                             _render), withheld)


# --- the entry point -------------------------------------------------------------------

def resolve(task, evs=None, project=None, reco=None) -> Advice:
    """Resolved advice for *task*, or `EMPTY`.

    *evs* is the caller's already-read event list — the whole point of the
    write-time design. Passing ``None`` reads the ledger, which is correct for a
    standalone caller and wrong for anything on a hot path.

    *reco* is the same bargain one level down: `plugins.recommend_for`'s answer,
    which is task-blind and therefore shared by every task in a wave. Hoisting
    *evs* alone captures 504 ms of a 949 ms call and leaves 279 ms of identical
    recomputation behind — see `resolve_skills`.

    Each source is isolated: a fault in one leaves the other two intact. Total
    by construction — a resolver that raises would take down whatever write
    invoked it, and advice is the least important thing happening there."""
    kw = keywords(task_text(task)) if task is not None else set()
    if not kw:
        return EMPTY
    if evs is None:
        try:
            evs = ev_mod.read_all(project)
        except Exception:
            evs = []
    # Each source's own count is initialised BEFORE its try, so a source that
    # raises reports 0 withheld rather than leaking the previous source's
    # number or raising a NameError inside an except handler.
    withheld = sop_withheld = con_withheld = 0
    try:
        skills = resolve_skills(kw, evs, project, reco=reco)
    except Exception:
        skills = ()
    try:
        sops, sop_withheld = resolve_sops(kw, evs)
        withheld += sop_withheld
    except Exception:
        sops, sop_withheld = (), 0
    try:
        constraints, con_withheld = resolve_constraints(kw, project)
        withheld += con_withheld
    except Exception:
        constraints, con_withheld = (), 0
    # MN-17 note, deliberately NOT an emitter. Announcing the `sop` / `improvements`
    # cats from here was the obvious placement and it was wrong: `resolve` runs during a
    # sidecar REBUILD, so writing telemetry events made the rebuild mutate the ledger and
    # broke "same events in, same bytes out" — `test_rebuild_stays_byte_idempotent_with_
    # advice` caught it immediately. The faculties are announced from the hook instead,
    # where advice is CONSULTED rather than computed. See `inject.engaged_faculties`.
    return Advice(skills=skills, sops=sops, constraints=constraints,
                  withheld=withheld, withheld_sops=sop_withheld,
                  withheld_constraints=con_withheld)


# --- serialization -----------------------------------------------------------------------

def to_dict(advice) -> dict:
    """*advice* as plain JSON-able lists, for the sidecar."""
    a = advice if isinstance(advice, Advice) else EMPTY
    return {"skills": list(a.skills), "sops": list(a.sops),
            "constraints": list(a.constraints)}


def _strings(value, limit) -> tuple:
    if not isinstance(value, list):
        return ()
    out = []
    for item in value[:limit]:
        try:
            text = str(item or "")[:MAX_ITEM_CHARS]
        except (ValueError, TypeError):
            continue
        if text:
            out.append(text)
    return tuple(out)


def from_dict(obj) -> Advice:
    """An `Advice` from whatever the sidecar decoded.

    Total, and deliberately so: this is the read path, the document on disk was
    written by some past version of this code, and `sidecar._row` does not
    re-validate what it decodes. Junk becomes `EMPTY`, never an exception on the
    hook path."""
    if not isinstance(obj, dict):
        return EMPTY
    return Advice(skills=_strings(obj.get("skills"), MAX_SKILLS),
                  sops=_strings(obj.get("sops"), MAX_SOPS),
                  constraints=_strings(obj.get("constraints"),
                                       MAX_CONSTRAINTS))
