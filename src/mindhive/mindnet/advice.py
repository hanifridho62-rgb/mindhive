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


class Advice(NamedTuple):
    """Resolved, rendered, sanitized advice for one task.

    Three tuples of plain strings — see the module docstring on why these are
    finished lines and not records."""
    skills: tuple = ()
    sops: tuple = ()
    constraints: tuple = ()


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


# --- the three sources ---------------------------------------------------------------

def resolve_skills(task_kw, evs, project=None) -> tuple:
    """The top matching skills, each rendered with its INTENDED USE.

    The description is the payload, not the decoration: a name alone sends the
    agent looking for the catalog again, which is the behaviour this whole
    module exists to delete."""
    from .. import plugins as plugins_mod
    reco = plugins_mod.recommend_for(project, evs=evs)
    rows = [r for r in (reco or {}).get("recommendations") or []
            if isinstance(r, dict)]
    picks = _ranked(rows, task_kw,
                    lambda r: f"{r.get('name', '')} {r.get('description', '')}",
                    MAX_SKILLS)
    out = []
    for r in picks:
        name = safe_text(r.get("name"), 60)
        desc = safe_text(r.get("description"), MAX_ITEM_CHARS)
        if not name:
            continue
        out.append(safe_text(f"{name} — {desc}" if desc else name,
                             MAX_ITEM_CHARS))
    return tuple(out)


def resolve_sops(task_kw, evs) -> tuple:
    """The top matching SOPs, carrying their REAL BODIES.

    `sop.current` is a pure fold over the events already in hand, so this costs
    nothing beyond the read the caller had already paid — and it inherits the
    registry's own lifecycle rules, so a deleted SOP is absent here without this
    module knowing what 'deleted' means."""
    from .. import sop as sop_mod
    rows = sop_mod.current(evs or [])
    picks = _ranked(
        [r for r in rows if isinstance(r, dict)], task_kw,
        lambda r: f"{r.get('sop_id', '')} {r.get('title', '')} "
                  f"{r.get('text', '')}",
        MAX_SOPS)
    out = []
    for r in picks:
        sid = safe_text(r.get("sop_id"), 64)
        title = safe_text(r.get("title"), 80)
        body = safe_text(r.get("text"), MAX_ITEM_CHARS)
        if not body:
            continue
        out.append(safe_text(f"[{sid}] {title}: {body}", MAX_ITEM_CHARS))
    return tuple(out)


def resolve_constraints(task_kw, project=None) -> tuple:
    """Prior failures matching this task, rendered as prohibitions.

    A summary with no recorded reason is SKIPPED rather than rendered with an
    empty slot: "failed due to ." costs prompt budget and teaches the agent to
    discount the constraints that do say something."""
    from .. import incidents as inc_mod
    rows = [r for r in (inc_mod.summaries(project) or [])
            if isinstance(r, dict)]
    picks = _ranked(
        rows, task_kw,
        lambda r: " ".join(str(r.get(k, "") or "") for k in
                           ("signature", "excerpt", "root_cause", "lesson",
                            "active_rule")),
        MAX_CONSTRAINTS)
    out = []
    for r in picks:
        reason = safe_text(r.get("root_cause") or r.get("lesson")
                           or r.get("active_rule"), MAX_REASON_CHARS)
        if not reason:
            continue
        out.append(safe_text(CONSTRAINT_TEMPLATE.format(reason=reason),
                             MAX_ITEM_CHARS))
    return tuple(out)


# --- the entry point -------------------------------------------------------------------

def resolve(task, evs=None, project=None) -> Advice:
    """Resolved advice for *task*, or `EMPTY`.

    *evs* is the caller's already-read event list — the whole point of the
    write-time design. Passing ``None`` reads the ledger, which is correct for a
    standalone caller and wrong for anything on a hot path.

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
    try:
        skills = resolve_skills(kw, evs, project)
    except Exception:
        skills = ()
    try:
        sops = resolve_sops(kw, evs)
    except Exception:
        sops = ()
    try:
        constraints = resolve_constraints(kw, project)
    except Exception:
        constraints = ()
    # MN-17 note, deliberately NOT an emitter. Announcing the `sop` / `improvements`
    # cats from here was the obvious placement and it was wrong: `resolve` runs during a
    # sidecar REBUILD, so writing telemetry events made the rebuild mutate the ledger and
    # broke "same events in, same bytes out" — `test_rebuild_stays_byte_idempotent_with_
    # advice` caught it immediately. The faculties are announced from the hook instead,
    # where advice is CONSULTED rather than computed. See `inject.engaged_faculties`.
    return Advice(skills=skills, sops=sops, constraints=constraints)


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
