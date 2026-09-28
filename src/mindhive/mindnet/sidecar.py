# SPDX-License-Identifier: Apache-2.0
# src/mindhive/mindnet/sidecar.py
"""MN-1 U5 — the phase projection's sidecar, and the `rebuild` that mints it.

The ledger is the source of truth and this file is a rebuildable projection of
it (invariant: every sidecar is derived). It exists for one reason: **cost**.
`events.read_all` measures ~264 ms on the real store (20k+ events), and U9's
injector fires on EVERY user prompt. A hook path that touched the ledger would
put a quarter of a second on every message the user sends, so the read path here
never calls it — `test_the_read_path_never_touches_the_ledger` pins that as a
property rather than as a timing bound.

Its own module rather than a section of `controller.py` (which the plan named):
`controller.py` is already 1016 lines, past this repo's 1000-line maintainability
threshold, and the two halves have nothing in common — one is a pure state
machine, the other is filesystem I/O. Nothing in `controller.py`, `tasks.py`, or
`phases.py` changed to make this land; the dependency runs one way,
``sidecar -> controller -> tasks/phases``.

## The freshness stamp is the whole design (KTD9)

``{events_seen, last_ts, segments: {name: [size, mtime_ns]}}``.

The per-segment map is **load-bearing and must not be "simplified"**. A reader
has to answer one question — *am I stale?* — and it must answer it without
reading the ledger, because reading the ledger is the exact cost this file
exists to avoid. ``events_seen`` and ``last_ts`` cannot answer it: knowing the
sidecar was written at 20,409 events says nothing about how many there are now
without counting them. The segment map can, from `os.stat` alone: a handful of
directory-entry lookups, already cached by `os.scandir`, against numbers the
filesystem maintains for free.

A content hash would be a strictly worse spelling of the same idea — it reads
and digests every byte of every segment, which is the 264 ms back again.

Both halves of the pair are needed. `size` alone misses an in-place edit or a
restore-from-backup that preserves the byte count; `mtime_ns` alone misses a
filesystem with coarse timestamp resolution when two writes land inside one
tick. Together they are cheap and they are enough.

## A stale read reports its state; it does not blank it

`read` returns what it decoded even when the stamp diverges, with ``fresh=False``
and a reason. Callers on the hook path MUST branch on ``fresh`` — KTD9 requires
a stale sidecar to yield the phase-AGNOSTIC block only, because telling an agent
to write code for a task that has since fallen back to `plan` is worse than
saying nothing. But blanking the state inside `read` would break the thing that
needs it most: after `hygiene.prune` the stamp is necessarily stale, and that is
precisely the moment `rebuild` has to see the standing records in order to
refuse to erase them.

## Purity, and the one ordering that matters

Encoding and decoding are deterministic: no clock, no RNG, keys sorted, so the
same fold produces byte-identical output. ``mtime_ns`` is read from the
filesystem, never generated.

`rebuild` stats the segments **before** it reads the events, and the order is
not cosmetic. Stat-then-read means an append landing in between produces a stamp
that looks OLDER than the folded state — the next reader sees divergence and
rebuilds, which is merely wasteful. Read-then-stat means that same append
produces a stamp that looks CURRENT for a state that does not contain it, and
the sidecar lies about its freshness until something else happens to touch the
ledger. One direction costs a rebuild, the other costs correctness.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import NamedTuple

from .. import events as ev_mod
from .. import paths
from . import controller as c_mod
from . import tasks as t_mod
from .controller import Orphan, PhaseRecord, PhaseState, Satisfaction
from .tasks import Task

# Bumped only when the on-disk shape changes in a way a reader must notice.
# Nothing branches on it today: an unrecognised document already degrades to an
# empty read through the same path a corrupt one takes, which is the fail-open
# behaviour a version check would have to implement anyway. It is written so a
# future reader can tell WHICH shape it is looking at.
#
# 2 added the ``tasks`` block. A v1 document decodes to an empty task map rather
# than an error, which is the honest answer: it genuinely carried no graph.
#
# 3 added the ``advice`` block (MN-3). Same story in both directions: a v2
# document decodes to no advice, which is true — it was written before anything
# resolved any — and a v2 reader ignores a key it does not know about.
SCHEMA_VERSION = 3


class Sidecar(NamedTuple):
    """`read`'s answer.

    ``fresh`` is the field callers must branch on. ``state`` is populated even
    when ``fresh`` is False — see the module docstring.

    ``tasks`` is the GRAPH the phase state was folded against, carried here for
    one reason: `inject.py` runs on every prompt and needs three facts the phase
    record does not hold — ``status`` (an injector that steers a completed task
    forever is worse than none), ``triviality_bypass`` (whose whole point is a
    different, narrower exit gate), and ``title``. The only other source is
    `tasks.fold(events.read_all())`, which is the ~264 ms this file exists to
    avoid. It is a projection of the same events the records came from, taken in
    the same pass, so it cannot disagree with them.

    ``advice`` is MN-3's resolved skill/SOP/prior-failure text, carried for
    exactly the same reason as ``tasks``: resolving it needs the ledger AND a
    filesystem scan of the plugin cache, and `inject.py` runs on every prompt.
    It is kept as a plain ``{str: [str]}`` dict rather than an `advice.Advice`
    so that decoding it here would not require importing `advice` — which
    imports `inject`, which imports this module. The shape contract between the
    two ends is pinned by a test rather than by a shared import."""

    state: PhaseState
    fresh: bool
    reason: str                 # why it is not fresh; "" when it is
    stamp: dict                 # as read; {} when there was nothing to read
    tasks: dict = {}            # task_id -> Task; {} for a v1 document
    advice: dict = {}           # MN-3 resolved advice; {} for a v1/v2 document


class Rebuild(NamedTuple):
    """`rebuild`'s answer. A refusal is ``written=False`` plus a reason, never
    an exception: this runs from the CLI and from U9's fallback path, and a
    projection refresh that can crash its caller is not a safe thing to wire
    into a hook."""

    written: bool
    reason: str
    events_seen: int = 0
    records: int = 0


def empty_state() -> PhaseState:
    """The state a read degrades to. Named because three call sites return it
    and an equality test against it reads better than against a bare tuple."""
    return PhaseState({}, {})


def sidecar_path(project=None) -> Path:
    """``<project>/mindnet/state.json``.

    `phases.py` gave `brainstorm` a required directory specifically so this file
    could not be offered as evidence for it (it previously accepted any non-empty
    file), so this location is referenced in prose elsewhere — moving it means
    updating that comment too."""
    return paths.project_dir(project) / "mindnet" / "state.json"


# --- the freshness stamp ----------------------------------------------------------

def segment_stats(project=None) -> dict:
    """``{segment name: [size, mtime_ns]}`` for the ledger's segment files.

    THE cheap half of the staleness test — `os.scandir` caches the stat data the
    directory enumeration already returned, so this is a few microseconds even
    with dozens of sessions, and it never opens, reads, or parses a byte.

    Total: a missing events directory is an empty ledger (``{}``), and any
    filesystem error answers ``{}`` too. ``{}`` compares unequal to a stamp that
    named segments, so an unreadable directory degrades to *stale*, which is the
    fail-safe direction."""
    out: dict = {}
    try:
        with os.scandir(paths.events_dir(project)) as entries:
            for e in entries:
                if not e.name.endswith(".jsonl"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                # A list, not a tuple: JSON has one sequence type, and writing
                # the same shape both ways keeps the comparison in `_staleness`
                # from having to normalise.
                out[e.name] = [st.st_size, st.st_mtime_ns]
    except OSError:
        return {}
    return out


def _stamp(evs, segments) -> dict:
    """The stamp for *evs*, given segment stats taken BEFORE they were read.

    ``last_ts`` is a max rather than ``evs[-1].ts``: `read_all` sorts, but a
    caller may inject an unsorted list, and a stamp that depended on the
    caller's ordering would not be deterministic."""
    return {
        "events_seen": len(evs),
        "last_ts": max((str(getattr(ev, "ts", "") or "") for ev in evs),
                       default=""),
        "segments": segments,
    }


def _staleness(stamp, project=None):
    """The reason the ledger has moved since *stamp* was taken, or None.

    Compared per segment so the reason names the file that changed — "the
    sidecar is stale" sends an operator to read code, "s2.jsonl is new" sends
    them to the session that wrote it."""
    stamped = stamp.get("segments")
    if not isinstance(stamped, dict):
        # Distinct from an empty map, which legitimately means "the ledger has
        # no segments" and is fresh against an empty events dir. A stamp with no
        # map at all cannot prove anything and must not be read as proof of
        # nothing having changed.
        return ("the sidecar carries no segment stamp, so it cannot prove it is "
                "current — run `hive mindnet rebuild`")
    live = segment_stats(project)
    for name in sorted(set(stamped) | set(live)):
        was, now = stamped.get(name), live.get(name)
        if was is None:
            return f"segment '{name}' is new since the sidecar was written"
        if now is None:
            return f"segment '{name}' is gone since the sidecar was written"
        if not isinstance(was, (list, tuple)) or list(was) != list(now):
            return (f"segment '{name}' changed since the sidecar was written "
                    f"(stamped {was}, now {now})")
    return None


# --- serialization ------------------------------------------------------------------

def _encode(state, stamp, tasks=None, advice=None) -> str:
    """The sidecar document. ``sort_keys`` makes the bytes a function of the
    CONTENT alone — two folds that built their dicts in a different insertion
    order still produce an identical file, which is what makes `rebuild`
    idempotent byte-for-byte rather than merely equivalent.

    `dataclasses.asdict` walks the records field by field, so a field added to
    `Satisfaction` or `PhaseRecord` later round-trips without touching this
    module. A hand-written field list here would keep passing its tests and
    silently drop the new field."""
    return json.dumps({
        "version": SCHEMA_VERSION,
        "stamp": stamp,
        "records": {tid: asdict(rec) for tid, rec in state.records.items()},
        "orphans": {tid: [asdict(o) for o in rows]
                    for tid, rows in state.orphans.items()},
        "tasks": {tid: asdict(t) for tid, t in (tasks or {}).items()},
        # Empty buckets are dropped rather than written, the same call `_decode`
        # makes for empty orphan lists: it keeps the round trip an exact
        # equality, and a key whose value is `[]` claims a resolution ran and
        # found nothing, which is indistinguishable here from none having run.
        "advice": {k: list(v) for k, v in (advice or {}).items() if v},
    }, sort_keys=True, indent=2) + "\n"


def _row(cls, obj, nested=None):
    """Rebuild one frozen controller record from its JSON object, or None when
    it cannot be rebuilt.

    Field-driven, like `_encode` and like `events._row_to_event`: unknown keys
    are ignored (a field some future version wrote is not ours to guess at) and
    absent ones fall through to the dataclass default, so a required field that
    is missing raises `TypeError` here and costs exactly one row.

    Lists become tuples. Every field on a controller record is a str/int/bool/
    tuple (`controller.py:77`) and JSON has one sequence type, so a list that
    came back as a list would compare unequal to the fold AND would sit mutable
    inside a frozen record. Values are otherwise NOT coerced: a corrupt sidecar
    is not authoritative, and inventing an int from a string would hide the
    corruption the freshness stamp is there to route around."""
    if not isinstance(obj, dict):
        return None
    fields = cls.__dataclass_fields__
    kw = {}
    for name, value in obj.items():
        if name not in fields:
            continue
        if nested and name in nested:
            rows = ([_row(nested[name], r) for r in value]
                    if isinstance(value, list) else [])
            value = tuple(r for r in rows if r is not None)
        elif isinstance(value, list):
            value = tuple(value)
        kw[name] = value
    try:
        return cls(**kw)
    except TypeError:
        return None


def _decode(doc) -> PhaseState:
    """The document's two halves as a `PhaseState`. Total at the row level: one
    unbuildable record costs that record, not the projection."""
    records: dict = {}
    raw = doc.get("records")
    if isinstance(raw, dict):
        for tid, obj in raw.items():
            rec = _row(PhaseRecord, obj,
                       nested={"satisfactions": Satisfaction})
            if isinstance(tid, str) and rec is not None:
                records[tid] = rec
    orphans: dict = {}
    raw = doc.get("orphans")
    if isinstance(raw, dict):
        for tid, rows in raw.items():
            if not isinstance(tid, str) or not isinstance(rows, list):
                continue
            built = tuple(o for o in (_row(Orphan, r) for r in rows)
                          if o is not None)
            if built:
                # `fold` never creates an empty orphan bucket, so dropping one
                # here keeps the round trip an exact equality.
                orphans[tid] = built
    return PhaseState(records, orphans)


def _decode_tasks(doc) -> dict:
    """The document's ``tasks`` block as ``task_id -> Task``. Absent (a v1
    document) or malformed reads as ``{}``, and one unbuildable row costs that
    row — the same row-level totality `_decode` gives the records."""
    raw = doc.get("tasks")
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for tid, obj in raw.items():
        task = _row(Task, obj)
        if isinstance(tid, str) and task is not None:
            out[tid] = task
    return out


def _decode_advice(doc) -> dict:
    """The document's ``advice`` block as ``{str: [str]}``. Absent (a v1/v2
    document) or malformed reads as ``{}``.

    Validated down to the leaves on the way OUT, not merely on the way in — the
    same call `telemetry.fold` makes. This decodes on the hook path and the
    result goes straight into a prompt, so a value that is not a string is
    dropped here rather than rendered as one somewhere downstream."""
    raw = doc.get("advice")
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, rows in raw.items():
        if not isinstance(key, str) or not isinstance(rows, list):
            continue
        vals = [r for r in rows if isinstance(r, str) and r.strip()]
        if vals:
            out[key] = vals
    return out


# --- the read path (hook-safe: degrades, never raises) --------------------------------

def read(project=None) -> Sidecar:
    """The projection, and whether it can still be trusted.

    **Never raises.** Missing, empty, truncated, corrupt-JSON, wrong-shaped and
    unreadable-encoding sidecars all answer an empty state with ``fresh=False``
    and a reason. This runs on the UserPromptSubmit hook path, where an
    exception does not degrade a feature — it breaks the user's prompt."""
    p = sidecar_path(project)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError — a sidecar written by something
        # that did not agree with us about the encoding.
        return Sidecar(empty_state(), False,
                       f"no readable sidecar at {p} — run `hive mindnet "
                       f"rebuild`", {})
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return Sidecar(empty_state(), False,
                       f"the sidecar at {p} is not readable JSON (truncated or "
                       f"corrupt) — run `hive mindnet rebuild`", {})
    if not isinstance(doc, dict):
        return Sidecar(empty_state(), False,
                       f"the sidecar at {p} is not an object — run `hive "
                       f"mindnet rebuild`", {})
    stamp = doc.get("stamp")
    if not isinstance(stamp, dict):
        stamp = {}
    why = _staleness(stamp, project)
    return Sidecar(_decode(doc), why is None, why or "", stamp,
                   _decode_tasks(doc), _decode_advice(doc))


# --- the write path -------------------------------------------------------------------

def write(state, stamp, project=None, tasks=None, advice=None) -> Path:
    """Serialize *state* + *stamp* (+ the *tasks* graph, + MN-3 *advice*) to the
    sidecar, atomically.

    Raises `OSError` on a filesystem failure — this is the low-level primitive
    and its callers decide what a failure means. `rebuild` turns it into a
    reported reason; nothing on the hook path calls it at all."""
    # Through the shared atomic writer, which now owns the pid+counter temp
    # name this module used to hand-roll (and whose stale `_tmp_path` comment
    # claimed `write_text_atomic` could not do). Its per-call counter is unique
    # across the dashboard's threads exactly as the old pid+tid name was, and it
    # retries the Windows publish a concurrent sidecar reader can refuse.
    p = sidecar_path(project)
    paths.write_text_atomic(p, _encode(state, stamp, tasks, advice))
    return p


def resolve_advice(state, tasks, evs, project=None) -> dict:
    """MN-3 advice for the task the injector would actually steer, as the plain
    dict `write` persists. ``{}`` for anything that cannot be resolved.

    **Keyed off `inject.active`, deliberately.** That function owns the
    selection order — workable statuses only, furthest along the pipeline first,
    total tie-break — and advice resolved for a DIFFERENT task than the one the
    block describes would be worse than no advice at all. One home for the rule
    rather than a second implementation drifting beside it.

    Both imports are local. `inject` imports this module, so a module-level
    import here would close a cycle; `advice` reaches `plugins`, `sop` and
    `incidents`, which is a lot of machinery to load on a path that may only
    ever read. Same idiom as `plugins.recommend_for`'s lazy imports.

    Total: advice is the least important thing a rebuild does, and an exception
    escaping here would take the phase projection down with it."""
    try:
        from . import advice as adv_mod
        from . import inject as inj_mod
        # `fresh=True`: this is the state being written right now, so by
        # construction it is current — the flag exists for READERS deciding
        # whether a file on disk can still be trusted.
        _rec, task = inj_mod.active(Sidecar(state, True, "", {}, tasks or {}))
        if task is None:
            return {}
        return adv_mod.to_dict(adv_mod.resolve(task, evs=evs, project=project))
    except Exception:
        return {}


def rebuild(project=None, evs=None, tasks=None) -> Rebuild:
    """Fold the ledger and write the sidecar. What makes the sidecar honest
    rather than authoritative.

    Idempotent: the same ledger produces byte-identical output, because the fold
    is deterministic and `_encode` sorts.

    *evs* / *tasks* are injectable so a caller that already folded (U7's CLI,
    U9's fallback) does not pay `read_all` twice.

    **ONE REFUSAL.** It will not overwrite a sidecar holding real state from an
    EMPTY ledger. `hygiene.prune` deletes a whole namespace including
    ``events/``, and `projector.project` then re-creates the directory and
    writes every projection from zero events with no guard at all
    (`projector.py:191-220`). A rebuild that followed that precedent would blank
    the phase spine and report success. The guard is deliberately about STATE,
    not about the file: an empty or corrupt sidecar has nothing to lose, so a
    fresh namespace can still mint its first one and a damaged one can still be
    repaired."""
    # BEFORE the read — see the module docstring. A stamp taken after the fold
    # can claim freshness for a state that predates an interleaved append.
    segments = segment_stats(project)
    if evs is None:
        evs = ev_mod.read_all(project)
    if not evs:
        standing = read(project).state
        if standing.records or standing.orphans:
            p = sidecar_path(project)
            return Rebuild(
                False,
                f"refusing to rebuild from an empty ledger: the sidecar holds "
                f"{len(standing.records)} task(s) and "
                f"{len(standing.orphans)} orphan(s), and "
                f"{paths.events_dir(project)} has no events. If the namespace "
                f"was pruned this would erase real state; if that is what you "
                f"want, delete {p} and rebuild again.",
                0, len(standing.records))
    # Folded HERE rather than left to `controller.fold`'s own default, because
    # the graph is not just an input to the phase fold any more — it is written
    # beside it, and a caller that injected nothing must still get a sidecar
    # whose two halves came from the same events.
    if tasks is None:
        tasks = t_mod.fold(evs)
    state = c_mod.fold(evs, tasks)
    # *evs* is passed through rather than left to the resolver's own default:
    # that default reads the ledger, and the entire reason MN-3 resolves at
    # write time is that this read has already been paid for.
    advice = resolve_advice(state, tasks, evs, project)
    try:
        write(state, _stamp(evs, segments), project, tasks, advice)
    except OSError as e:
        return Rebuild(False,
                       f"could not write {sidecar_path(project)}: "
                       f"{type(e).__name__}: {e}",
                       len(evs), len(state.records))
    return Rebuild(True, "", len(evs), len(state.records))
