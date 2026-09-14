# src/mindhive/chain.py
"""The ledger's tamper-evidence checkpoint.

**The chain folds in APPEND order.** A hash chain over an append-only log has
to be folded in the order rows were appended, or it is not a chain of a growing
sequence -- and this one was folded in TIMESTAMP order for a long time, which
is not the same thing. `events.append_event` takes `ts` as a CALLER-supplied
parameter, so a row can legitimately arrive stamped in the past:

* `incidents.audit` backdates `rule_expired` / `rule_activated` on purpose, to
  the failure's own ts, so the operation stays deterministic and a failure that
  has not been harvested yet cannot fall into a dead zone.
* `hookcapture` stamps `ts=now` before slow work and before it contends for the
  append lock, then writes.

Measured on the live store: 8,547 rows whose ts runs backwards against their
physical position. Every one of them inserted INSIDE the ts-ordered sealed
prefix, so `hive verify` went red until the next projection re-sealed it and
green again after -- observed in a single session as `ok: true`, then
`{"ok": false, "reason": "hash_mismatch"}` twenty minutes later with nothing
tampered in between. `sealable()` below was an earlier, partial answer to the
same defect: it fixed the same-SECOND tie and could not fix an
earlier-second arrival, because no ts-based boundary can.

The checkpoint therefore records a per-segment count of COMPLETE
(newline-terminated) lines, and verification re-folds exactly those lines in
`(segment, line)` order. Two properties follow, and they are the whole
guarantee:

* Appending to a segment cannot change that segment's earlier lines.
* A brand-new segment is simply not in the map.

So no legitimate write can disturb the sealed prefix, whatever ts it carries,
and the checkpoint can attest to the ENTIRE complete log rather than holding
back a trailing window. The alarm is quiet unless history really moved.

**v3 folds the append stamp.** `events.append_event` stamps every row with
`seq`, and the readers order rows that share a second by it (see
`events._order_key`). That makes a sealed row's `seq` history like any other
field: rewrite it and a same-second `update` replays before the `add` it edits,
in every fold downstream. v2 predates the stamp and cannot see such a rewrite,
so v3 folds it. A row without a stamp contributes identical material to both
formats, so a v2 checkpoint still verifies after the upgrade and re-anchoring a
legacy store moves no head it was ever compared against. A green v2 verdict
over stamped rows says so with `seq_unsealed` (see `verify`), and a v1
checkpoint is folded in the seq-blind order its writer used (`_legacy_order`).
"""
import hashlib
import json
from . import paths, events

# Bumped when the on-disk checkpoint shape or its fold material changes. v1 was
# `{count, head}` over a ts-ordered prefix; v2 adds `segments` and folds in
# append order; v3 keeps v2's shape and also folds each row's `seq` (`_fold`).
CHECKPOINT_VERSION = 3
# The formats the per-segment fold verifies. v1 has its own shim.
_SEGMENT_MAP_VERSIONS = (2, 3)

_ZERO = "0" * 64


def compute_chain(evs, checkpoint=None) -> dict:
    """Fold a LIST OF EVENTS into a SHA-256 hash chain -> {count, head}. When
    ``checkpoint`` is an int, also return ``checkpoint_head``: the head after
    exactly that many events, or None when the list is shorter than the
    checkpoint claims.

    This folds whatever order it is handed and knows nothing about segments; it
    is the primitive `pgmirror.parity` uses to compare the file ledger against
    the Postgres mirror (both sides folded the same way, which is the only
    thing that makes that comparison mean anything). The chain CHECKPOINT no
    longer goes through here -- see `_scan` / `_fold` -- but the v1 verification
    shim still does."""
    prev = _ZERO
    head = prev
    cp_head = prev if checkpoint == 0 else None
    for i, ev in enumerate(evs):
        material = prev + events.canonical_json(events.to_dict(ev))
        head = hashlib.sha256(material.encode("utf-8")).hexdigest()
        prev = head
        if i + 1 == checkpoint:
            cp_head = head
    out = {"count": len(evs), "head": head}
    if checkpoint is not None:
        out["checkpoint_head"] = cp_head
    return out


def sealable(evs) -> list:
    """LEGACY. Nothing in `src/` calls this any more -- `_verify_v1` does not,
    despite what an earlier draft of this docstring said; it folds
    `_legacy_order` against the stored count directly. What keeps the function
    alive is
    `tests/test_chain.py`, where it is the only honest way to BUILD a v1
    checkpoint: a fixture that hand-writes one would be asserting against a
    format guess, while this is the code that actually minted the 67 v1
    checkpoints now on disk. Delete it when the shim goes.

    The prefix a ts-ordered checkpoint could attest to: every event strictly
    before the newest timestamp on record. It exists because pre-`seq`
    `read_all` ordered by ``(ts, segment_name, line_index)`` -- the order every
    v1 checkpoint was minted in -- which is total and deterministic but not
    append-monotonic, so an event arriving with a ts equal to already-sealed
    rows could sort BEFORE them and invalidate the boundary. `read_all` now
    puts the append stamp after ts (`events._order_key`), so it no longer
    produces that order over stamped rows; a v1 fixture has to be built from
    the seq-blind order, which is what `_verify_v1` folds (`_legacy_order`).

    Ending the prefix at a second boundary fixed that for the same-second case
    and ONLY that case. An event stamped an hour ago -- which
    `incidents.audit` writes deliberately -- still sorts deep inside the sealed
    prefix. That is why the checkpoint moved to append order; nothing new should
    call this."""
    if not evs:
        return []
    newest = evs[-1].ts or ""
    end = len(evs)
    while end > 0 and (evs[end - 1].ts or "") == newest:
        end -= 1
    return evs[:end]


def _scan(project=None):
    """Every parseable row on a COMPLETE line as ``(segment, line_index,
    canonical_json, seq)`` in APPEND order, plus the number of COMPLETE lines in
    each segment.
    `canonical_json` covers the five Event fields; `seq` is the row's append
    stamp or None, parsed by the same `events._row_parts` the readers use.

    Rows come out already ordered: segments are walked in name order and each
    segment's lines in index order, which IS ``(segment, line_index)``. No sort
    runs, and none should be added -- on the live store that ordering is over
    66k rows and the fold below depends on it being free.

    Three details carry the correctness:

    * The COMPLETE-line count is what a segment is sealed at, so a segment
      ending mid-row contributes every whole line and not the fragment. Those
      trailing bytes are still mutable -- the next append concatenates onto
      them, which is exactly how the live store's `Extra data` rows were formed
      -- so sealing them would attest to something that can still change.
    * **Splitting is `split("\\n")`, NOT `splitlines()`, and the two are not
      interchangeable here.** `splitlines()` also breaks on U+2028, U+2029,
      U+000B, U+000C, U+001C-U+001E and U+0085. Three of those reach a segment
      raw (U+0085, U+2028, U+2029); the rest are below U+0020, which JSON
      forbids unescaped, so `json.dumps` escapes them whatever `ensure_ascii`
      says. One raw U+2028 pasted into a payload therefore made `splitlines()`
      yield more elements than there were newlines. The limit
      and the index were then measured in different coordinate systems, and the
      `i >= complete` break fired early: measured on a three-event segment
      carrying one U+2028, the checkpoint sealed **one** event while recording
      a line count of three. The two rows that fell out could be rewritten
      freely and `verify` stayed green. A ledger row is terminated by `\\n` by
      construction (`append_event` writes exactly one), so `\\n` is the only
      separator that may define either number.
    * ``line_index`` counts blank and malformed lines, which is exactly what
      `events._segment_lines` counts -- the generator `events.read_all`,
      `events.read_types`, `events.corrupt_rows` and `pgmirror.scan_segments`
      all share, and whose index the mirror stores as `seg_line`. This function
      keeps its own PASS, because it also needs each segment's complete-line
      COUNT, but no longer its own SPLIT: both call `events._segment_parts`, so
      where a row ends is decided in exactly one place. Keeping the pass while
      duplicating the split was the earlier arrangement and it was the weaker
      one -- a different return shape is not a different rule, and nothing
      compares the two at runtime, so a forward edit to one could drift the
      other without turning a single test red.

      That agreement was briefly untrue and the earlier wording here understated
      it: while those four still used `splitlines()`, a raw separator diverged
      not only its own row but EVERY row after it in that segment -- measured,
      the third event of a three-row segment was `line_index` 2 here and
      `seg_line` 3 there, for a row both could see. Internal consistency between
      this limit and this index is what the seal depends on; agreement with the
      readers is what keeps the mirror's join key meaning the same thing.

    Malformed rows are skipped exactly as `read_all` skips them, so they stay
    out of the chain and are reported by `events.corrupt_rows` instead."""
    d = paths.events_dir(project)
    rows: list = []
    lines: dict = {}
    if not d.exists():
        return rows, lines
    for seg in sorted(d.glob("*.jsonl"), key=lambda p: p.name):
        parts = events._segment_parts(seg)
        complete = len(parts) - 1         # the tail after the last \n is partial
        lines[seg.name] = complete
        for i, raw in enumerate(parts):
            if i >= complete:
                break                     # a trailing line with no newline
            s = raw.strip()
            if not s:
                continue
            ev, seq = events._row_parts(s)
            if ev is None:
                continue                  # torn row: reads stay total
            rows.append((seg.name, i,
                         events.canonical_json(events.to_dict(ev)), seq))
    return rows, lines


def _fold(rows, with_seq=True) -> str:
    """Hash-chain head over pre-canonicalised rows.

    Takes the canonical JSON already computed by `_scan` rather than re-deriving
    it, which is what lets `verify` fold twice -- once for the checkpoint prefix
    and once for the whole log -- off a single read. Measured on the live store:
    the scan costs 1.9s and each fold 0.1-0.2s, so the second fold is nearly
    free where a second scan would not be.

    `with_seq` selects the format's material. v3 -- the default, and what
    `write_chain` writes -- folds a stamped row as its canonical JSON, a
    newline, then the decimal stamp; v2 folds the canonical JSON alone. A row
    with no stamp is the canonical JSON under both. Splicing the stamp on
    rather than re-serialising a dict that holds it keeps the fold free of a
    second `json.dumps` per row, and the newline cannot be ambiguous:
    `canonical_json` escapes every control character inside a string, so a raw
    `\\n` never occurs in its output."""
    prev = _ZERO
    for _, _, cj, seq in rows:
        material = cj if seq is None or not with_seq else f"{cj}\n{seq}"
        prev = hashlib.sha256((prev + material).encode("utf-8")).hexdigest()
    return prev


def _prefix(rows, limits):
    """The rows a checkpoint attests to: those below their segment's stored line
    count. A segment absent from *limits* was created after the checkpoint and
    contributes nothing."""
    return [r for r in rows if r[1] < limits.get(r[0], -1)]


def _shortfall(lines, limits) -> list:
    """Segments that can no longer satisfy the checkpoint -- deleted outright,
    shorter than the count it sealed, or naming a count no honest `write_chain`
    would emit. Either way the checkpoint no longer describes the disk.

    The `want >= 0` test is not defensive noise. `_prefix` admits rows with
    ``line_index < want``, so a NEGATIVE count admits none while `have < want`
    stays false -- measured, a hand-written `{"s.jsonl": -1}` verified `ok:
    True` with no `truncated` and no `ahead`, a clean pass attesting to
    nothing. `bool` is excluded for the same reason: it is an `int` subclass,
    so `True` would otherwise read as the count 1."""
    out = []
    for name, want in limits.items():
        have = lines.get(name)
        if (have is None or isinstance(want, bool) or not isinstance(want, int)
                or want < 0 or have < want):
            out.append(name)
    return sorted(out)


def write_chain(project=None) -> dict:
    """Re-anchor the checkpoint over the whole complete log.

    Reads the segments itself rather than taking a pre-parsed event list: the
    checkpoint is defined by segment and line, and `events.read_all` discards
    both. It does NOT call `read_all`, so a caller that already made that call
    (`projector.project`) still makes exactly one."""
    rows, lines = _scan(project)
    c = {"version": CHECKPOINT_VERSION, "count": len(rows),
         "head": _fold(rows), "segments": lines}
    pdir = paths.project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    # Atomic: the dashboard overview poll and `hive verify` both parse this
    # file, and a plain write_text truncates before it writes -- measured, a
    # reader caught the empty window on 6206 of 15421 tries. verify() has a
    # `corrupt_chain_file` branch for exactly that, which reported the LEDGER as
    # corrupt when only the checkpoint was mid-write.
    paths.write_text_atomic(pdir / "chain.json", events.canonical_json(c))
    return c


def _legacy_order(project=None) -> list:
    """Every parseable event in the order EVERY v1 checkpoint was minted in:
    ``(ts, segment_name, line_index)``, blind to the append stamp.

    That was `read_all`'s order before `seq` existed, and it is still the order a
    pre-v2 `write_chain` -- code from before c41b469, such as the frozen exe --
    seals, over stamped rows as well, because that code builds its `Event` from
    the five known keys and never looks at `seq`.
    `read_all` no longer produces it: a same-second tie now follows the stamp
    (`events._order_key`). Folding `read_all` against a v1 head sealed over a
    stamped same-second pair from two sessions therefore reported untouched
    history as `hash_mismatch`, and a stale writer still re-sealing v1 kept it
    red. ``_order_key(ts, None, ...)`` IS the legacy key -- a missing stamp is a
    constant sentinel, so it orders nothing -- and going through it keeps one
    definition of the ``segment, line_index`` tail.

    It walks the segments itself rather than calling `read_all` for a second
    reason: under `MINDHIVE_EVENT_BACKEND=pg` that call answers from the Postgres
    mirror, and a check of the files has to read the files. The split and the
    parse are the readers' own (`events._segment_lines`, `events._row_parts`),
    so the ROWS are exactly the ones `read_all` sees; only their order differs."""
    d = paths.events_dir(project)
    if not d.exists():
        return []
    keyed = []
    for seg in sorted(d.glob("*.jsonl"), key=lambda p: p.name):
        for i, line in events._segment_lines(seg):
            ev, _ = events._row_parts(line)
            if ev is None:
                continue                  # torn row: reads stay total
            keyed.append((events._order_key(ev.ts, None, seg.name, i), ev))
    keyed.sort(key=lambda t: t[0])
    return [ev for _, ev in keyed]


def _verify_v1(project, stored) -> dict:
    """The pre-append-order check, kept so a store checkpointed by the old code
    is VERIFIED rather than alarmed at.

    A v1 checkpoint carries no per-segment map, so the append-order fold has
    nothing to re-fold against. Reporting that as a tamper would put every
    namespace into a red state it did not earn; reporting it as ok without
    checking anything would be a lie. Running the old check is the honest third
    option, and it retires itself -- the first `write_chain` upgrades the file.

    It folds `_legacy_order`, NOT `read_all`. The two agree on a ledger with no
    stamps, and disagree wherever stamped rows from two segments share a second
    and their stamps order them differently from their segment names; a v1
    writer only ever used the first, so only the first reproduces its head.

    **What this does NOT do, stated plainly because the first draft of this
    docstring claimed otherwise: it does not stop the flapping.** This IS the
    ts-ordered prefix check, so a backdated append still sorts inside the
    sealed prefix and still returns `hash_mismatch` until the next projection.
    A v1 checkpoint therefore keeps the old behaviour, over stamped rows too.
    Measured when this landed: all 67 `chain.json` files under
    `~/.mindhive/` were v1, so every namespace takes this path until it is
    re-anchored, and a namespace nothing writes to any more never will be. The
    fix for a namespace is one `hive project` against it; there is no way to
    re-anchor from inside a read-only verify, and `verify` writing the file it
    audits would be a worse answer than a stale format."""
    try:
        rows = _legacy_order(project)
    except json.JSONDecodeError as e:
        return {"ok": False, "reason": "corrupt_event_log", "detail": str(e)}
    except Exception as e:
        return {"ok": False, "reason": "read_error", "detail": str(e)}
    scount = stored.get("count")
    computed = compute_chain(rows, checkpoint=scount)
    cp_head = computed.pop("checkpoint_head", None)
    actual = computed
    if not isinstance(scount, int):
        return {"ok": False, "reason": "corrupt_chain_file",
                "stored": stored, "actual": actual, "format": 1}
    if cp_head is None or cp_head != stored["head"]:
        return {"ok": False, "reason": "hash_mismatch",
                "stored": stored, "actual": actual, "format": 1}
    result = {"ok": True, "stored": stored, "actual": actual, "format": 1}
    ahead = actual["count"] - scount
    if ahead:
        result["ahead"] = ahead
    return result


def _read_checkpoint(p):
    """``(stored, None)`` or ``(None, failure_dict)`` -- never raises."""
    if not p.exists():
        return None, {"ok": False, "reason": "no chain.json (run: hive project)"}
    try:
        stored = json.loads(paths.read_text_retry(p))
    except (json.JSONDecodeError, OSError) as e:
        # A corrupt/truncated chain.json must not crash verify().
        return None, {"ok": False, "reason": "corrupt_chain_file",
                      "detail": str(e)}
    if not isinstance(stored, dict) or "head" not in stored:
        return None, {"ok": False, "reason": "corrupt_chain_file",
                      "stored": stored}
    return stored, None


def verify(project=None) -> dict:
    """Is the checkpointed history still on disk, byte for byte?

    Returns a verdict, never raises -- the dashboard overview poll and every
    `hive` command depend on that. `ahead` counts rows appended since the
    checkpoint; its presence is the normal steady state, not a warning.

    `seq_unsealed` appears only on a GREEN verdict against a v2 checkpoint, and
    counts the sealed rows carrying a stamp that v2 did not fold. It is not a
    failure -- the head matched, and a v2 writer (a process still on pre-v3
    code) folded exactly what it knew -- but the head would also have matched
    after a rewrite of those stamps, and a stamp decides same-second read order.
    The field names that weaker coverage instead of hiding it. One projection on
    current code (`write_chain`) re-anchors the file to v3, which folds every
    stamp it seals, and the field goes away. Absent when the count is zero.

    **The checkpoint is read BEFORE the segments, and that order is
    load-bearing.** A projection can re-seal at any moment, and it writes
    chain.json only after it has read the segments. Scanning first would let a
    projection land in the window and leave `verify` comparing an OLD scan
    against a NEWER checkpoint -- the checkpoint would claim lines the scan
    never saw, `_shortfall` would report them, and the verdict would be a FALSE
    `hash_mismatch`. That is precisely the flapping this module was rewritten to
    end, so reintroducing it through read order would defeat the point. Reading
    the checkpoint first inverts the risk into a harmless one: the scan is then
    newer than or equal to the checkpoint, and because segments are append-only
    a newer scan still contains the sealed prefix byte for byte."""
    p = paths.project_dir(project) / "chain.json"
    stored, failure = _read_checkpoint(p)
    if stored is not None:
        # Dispatch on the DECLARED version, never on the shape of `segments`.
        # Shape-dispatch was the first draft and it was a tamper-evidence hole:
        # a v2 checkpoint whose `segments` map had been replaced with `[]`,
        # `"x"`, `5` or `null` fell through to the v1 shim, which does not look
        # at `segments` at all -- so all four returned `ok: True` off the weaker
        # ts-ordered check. An attacker who can write chain.json could downgrade
        # the check by deleting one key. Version is the only field that says
        # which contract the file was written under.
        raw = stored.get("version")
        # `True == 1` in Python, so a bare `ver == 1` below would route
        # `"version": true` to the v1 shim -- the downgrade again, spelled
        # differently. `()` matches nothing, so a bool falls through to
        # unsupported; `raw` is kept for the message so it names what it saw.
        ver = () if isinstance(raw, bool) else raw
        if ver is None and "segments" in stored:
            # No version, but a v2-only key: this is a v2 file with its version
            # deleted, not a genuine v1 one. A real v1 checkpoint is exactly
            # `{"count", "head"}` -- that is what the old `write_chain` emitted
            # and what all 67 on disk contain. Routing this to the shim would
            # reopen the downgrade from the other side.
            return {"ok": False, "reason": "corrupt_chain_file",
                    "stored": stored,
                    "detail": "checkpoint has `segments` but no `version`"}
        # Exact `int` only, for the same reason: `1.0 == 1` and `3.0 == 3`, so
        # a float would otherwise pick a contract the file never declared.
        if ver is None or (type(ver) is int and ver == 1):
            return _verify_v1(project, stored)  # legacy shim; scans nothing here
        if type(ver) is not int or ver not in _SEGMENT_MAP_VERSIONS:
            # Written by code this build does not have. Not corrupt -- there is
            # nothing here to say the LEDGER is wrong -- just unverifiable, and
            # saying so is the honest answer. Downgrading to an older check
            # would report a green we did not earn.
            return {"ok": False, "reason": "unsupported_checkpoint_version",
                    "stored": stored, "detail": f"checkpoint version {raw!r}, "
                    f"this build verifies 1 to {CHECKPOINT_VERSION}"}
        if not isinstance(stored.get("segments"), dict):
            # It claims a per-segment format and has no per-segment map: the
            # file is broken, and that contract is what it broke.
            return {"ok": False, "reason": "corrupt_chain_file",
                    "stored": stored, "format": ver,
                    "detail": f"v{ver} checkpoint without a `segments` map"}
        count = stored.get("count")
        if type(count) is not int or count < 0:
            # `count` is subtracted from the scanned row count below. Unchecked,
            # a string or null raised TypeError out of a function contracted
            # never to raise, `1.5` came back `ok: true` with `ahead: -0.5`, and
            # `True` -- an int subclass, which `type(...) is int` excludes --
            # passed as 1. Every segment-map `write_chain` emits a non-negative
            # int, so anything else, a missing key included, is a broken file.
            # Whether the int is the RIGHT count is checked after the head.
            return {"ok": False, "reason": "corrupt_chain_file",
                    "stored": stored, "format": ver,
                    "detail": f"v{ver} checkpoint `count` is not a "
                              f"non-negative int: {count!r}"}
    # The fold material follows the DECLARED format. A v2 file was written by
    # code that folds no stamps -- including a stale checkout re-sealing rows a
    # newer writer already stamped -- so folding stamps against it would report
    # untouched history as a `hash_mismatch`.
    fmt = CHECKPOINT_VERSION if stored is None else ver
    with_seq = fmt >= 3
    try:
        rows, lines = _scan(project)
    except Exception as e:
        return {"ok": False, "reason": "read_error", "detail": str(e)}
    actual = {"count": len(rows), "head": _fold(rows, with_seq)}
    if failure is not None:
        return {**failure, "actual": actual}
    limits = stored["segments"]
    short = _shortfall(lines, limits)
    if short:
        # Sealed history is missing. Reported under the same reason as a
        # rewrite because it is the same claim -- the checkpoint no longer
        # describes what is on disk -- with `truncated` naming which segments.
        return {"ok": False, "reason": "hash_mismatch", "stored": stored,
                "actual": actual, "truncated": short, "format": fmt}
    sealed = _prefix(rows, limits)
    if _fold(sealed, with_seq) != stored["head"]:
        return {"ok": False, "reason": "hash_mismatch", "stored": stored,
                "actual": actual, "format": fmt}
    if stored["count"] != len(sealed):
        # The head matched, so `sealed` is exactly the rows this file attests
        # to, and `write_chain` takes `count` and `segments` from one scan: an
        # honest file has `count == len(sealed)`. The dispatch check admits any
        # non-negative int, and without this one an int that is not the row
        # count verified green -- measured over a head sealing three rows, `0`
        # returned `ahead: 3` and `999` returned `ahead: -996`. It runs AFTER the
        # head: a rewrite that also drops a sealed row out of the fold changes
        # both, and it is the `hash_mismatch` it looks like. The dispatch check
        # still has to run first, because `True == 1` and `1.0 == 1`.
        return {"ok": False, "reason": "corrupt_chain_file", "stored": stored,
                "actual": actual, "format": fmt,
                "detail": f"v{fmt} checkpoint `count` {stored['count']} is not "
                          f"the {len(sealed)} rows its head seals"}
    result = {"ok": True, "stored": stored, "actual": actual, "format": fmt}
    ahead = actual["count"] - stored["count"]     # == len(sealed): checked above
    if ahead:
        result["ahead"] = ahead
    if fmt == 2:
        # Green, but this head folded its rows without their stamps, so a
        # rewrite of a sealed stamp would still match it. Counted over the
        # SEALED prefix only: a stamped row appended after the checkpoint is
        # not something the checkpoint claimed to cover.
        unsealed = sum(1 for r in sealed if r[3] is not None)
        if unsealed:
            result["seq_unsealed"] = unsealed
    return result
