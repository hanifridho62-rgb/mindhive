# src/mindhive/consensus/__init__.py
"""Participatory consensus: many framed contributions folded into one number
that can be walked back to every input that produced it.

WHY THIS IS A FOLD AND NOT A TABLE. A contribution is an append-only event like
every other row in this ledger, so the consensus a frame reaches is derived, not
stored state — re-runnable from the same events, and covered by the same hash
chain. Nothing here writes a sidecar, a cursor, or a mutable row; `fold_window`
is a pure function over a list of events and takes no store.

WHY THE WEIGHTS ARE FRACTIONS AND NOT FLOATS. The fold decides the answer at the
point where cumulative weight reaches exactly half the total, and float
arithmetic does not reach that point exactly: three shares of `1/3` sum to
0.9999999999999999, so a half-weight boundary test written in floats picks the
wrong side of the median on the very fixture the rule exists for (one subject
sending three values against one subject sending one). `fractions.Fraction`
makes the comparison exact, which is why the weights never become floats until
the moment they are written into the record for a human to read.

WHY THE RULE IS NAMED AND VERSIONED. `RULE_VERSION` rides in every record. The
PRD forbids naive averaging, and this rule refuses it twice:

* **Per-subject normalisation.** Every SUBJECT carries total weight 1, split
  across however many contributions they sent. One participant sending fifty
  samples does not outvote fifty participants sending one each — and a
  contribution may not state its own weight (`contributions.validate_payload`
  refuses a submitted `weight`), so the audit trail records what the rule
  decided rather than what the sender asked for.
* **A weighted median, not a weighted mean.** A single extreme value moves a
  mean without limit and moves a median by at most one rank. For a biometric
  frame that is the difference between one miscalibrated sensor setting the
  board-level number and one miscalibrated sensor being outvoted.

Deliberately NOT in v1, so the name stays honest: no agreement-distance
weighting (down-weighting a contribution for sitting far from the cluster). It
is defensible and it is also a second free parameter, so it belongs in a v2 that
can be A/B'd against this one — which is exactly what `rule_version` in the
record makes possible. Changing the rule never rewrites history; it produces new
records that say which rule made them.

The public surface, and what each part is for:

* `contribute(...)` — validate one framed input and append it.
* `fold_window(evs, frame_id, ...)` — the pure fold. `None` when nothing
  arrived, which is a native branch of a participatory frame's normal life, not
  an error a caller has to wrap.
* `fold_and_record(...)` — the fold plus the append, for the CLI and the job.
* `latest_record(evs, frame_id)` / `trace(evs, record)` — the daylight half.
  `trace` REPORTS a ref it cannot resolve instead of raising, because an
  unresolvable input is a finding about the ledger, not a crash.
"""
import hashlib
from fractions import Fraction

from .. import events
from . import contributions, records

# Bumped only when the weighting or the aggregation changes. Records carry it so
# two rules can coexist in one ledger and be compared.
RULE_VERSION = "subject-share-weighted-median-v1"

CONTRIBUTION_TYPE = "contribution"
RECORD_TYPE = "consensus_record"


def event_ref(ev) -> str:
    """Content address of one event: SHA-256 over the same canonical bytes the
    hash chain covers.

    Content-addressed rather than positional on purpose. `(segment, line)` would
    be cheaper and would also keep pointing at a row after that row's bytes
    changed — a stale trace that still resolves is worse than one that visibly
    breaks, because it reads as proof. A rewritten event gets a new ref, so
    `trace` reports the old one as unresolved.
    """
    material = events.canonical_json(events.to_dict(ev))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _rows(evs, frame_id, start=None, end=None) -> list[dict]:
    """Every well-formed contribution to *frame_id* inside the window.

    Total by contract: a torn payload, a foreign event type, or a contribution
    missing the fields the weighting needs is SKIPPED, exactly as
    `events.read_all` skips a torn line. The alternative — raising — would let
    one bad row take down a whole frame's fold, and the rows that survive are
    still a truthful consensus of the rows that arrived intact.
    """
    out = []
    for ev in evs or []:
        if getattr(ev, "type", None) != CONTRIBUTION_TYPE:
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        if payload.get("frame_id") != frame_id:
            continue
        subject = payload.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            continue
        # STRIPPED, because this is the identity the whole per-subject rule
        # is keyed on. The contract validates `subject.strip()` and the fold
        # then grouped on the raw string, so 'alice' and 'alice ' were two
        # participants: measured, three spellings of one voter took three
        # full shares against another's one, which is precisely the
        # flooding this module's first anti-gaming claim refuses.
        #
        # CASE is deliberately not folded. A subject id may legitimately be
        # case-sensitive, and merging 'Alice' into 'alice' is a policy
        # decision, not a bug fix.
        subject = subject.strip()
        if "value" not in payload:
            continue
        # A stated confidence of 0 makes the row weigh nothing, and a row
        # that weighs nothing is not an input: it moves no median, and the
        # record contract rightly refuses a zero weight. Dropped HERE, not
        # after weighing, so it does not consume a share of its subject's
        # weight either. `contributions.validate_payload` now refuses it at
        # the surface; this is for the frames that already hold one, which
        # no gate can undo -- and where the alternative was a ValueError on
        # every fold of that frame, forever.
        conf = payload.get("confidence")
        if (isinstance(conf, (int, float)) and not isinstance(conf, bool)
                and conf == 0):
            continue
        ts = getattr(ev, "ts", "") or ""
        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue
        out.append({"ev": ev, "ts": ts, "subject": subject,
                    "value": payload.get("value"),
                    "confidence": payload.get("confidence")})
    return out


def _weigh(rows) -> None:
    """Assign each row its exact Fraction weight and the reason for it.

    Mutates in place because the weight belongs to the row it describes; keeping
    them in a parallel list is how a reason ends up attached to the wrong input.
    """
    per_subject = {}
    for row in rows:
        per_subject[row["subject"]] = per_subject.get(row["subject"], 0) + 1
    for row in rows:
        n = per_subject[row["subject"]]
        weight = Fraction(1, n)
        reason = f"subject-share 1/{n}"
        conf = row["confidence"]
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            # str() first: Fraction(0.9) is exact but ugly, Fraction("0.9") is
            # the decimal the contributor actually stated.
            weight *= Fraction(str(conf))
            reason += f", confidence {conf}"
        row["weight"] = weight
        row["reason"] = reason


def _as_number(value):
    """The value as a number, or None when it is not one. `bool` is excluded on
    purpose: `True` is an `int` in Python, and a boolean measurement folded as
    1 would silently join a numeric frame it does not belong to."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _weighted_median(pairs):
    """Weighted median of (number, Fraction) pairs, defined exactly.

    Walk values ascending, accumulating weight. The answer is the first value
    whose cumulative weight passes half the total. When the cumulative weight
    lands EXACTLY on half — the weighted analogue of an even-length list — the
    answer is the midpoint of that value and the next, so the fold does not
    silently prefer the lower side of a tie. This is the one place an average
    appears, and it averages two ranks rather than a whole population.
    """
    ordered = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _v, w in ordered)
    half = Fraction(total, 2) if isinstance(total, int) else total / 2
    cumulative = Fraction(0)
    for i, (value, weight) in enumerate(ordered):
        cumulative += weight
        if cumulative > half:
            return value
        if cumulative == half:
            if i + 1 < len(ordered):
                return (value + ordered[i + 1][0]) / 2
            return value
    return ordered[-1][0]


def _weighted_mode(pairs):
    """Heaviest distinct value among (str, Fraction) pairs. Ties break on the
    lexicographically smallest value so the fold is deterministic — an arbitrary
    tie-break would make the same events produce two different records."""
    totals = {}
    for value, weight in pairs:
        totals[value] = totals.get(value, Fraction(0)) + weight
    # `min` on (-weight, value), not `max` on a negated-ordinal list. That
    # list compared element-wise and THEN by length, so a value that is a
    # prefix of its rival lost to it: measured, ('a','ab') broke to 'ab' and
    # ('x','xy') to 'xy' — the opposite of the rule stated above, on exactly
    # the shapes a category name and its longer form take. A determinism
    # rule that is wrong is not determinism.
    best = min(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return best[0]


def _clean_number(value):
    """An integral float renders as `25.0` in JSON and reads as a precision
    claim nobody made; emit `25`."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Fraction):
        return int(value) if value.denominator == 1 else float(value)
    return value


def fold_window(evs, frame_id, start=None, end=None) -> "dict | None":
    """Fold *frame_id*'s contributions into a `consensus_record` payload.

    Returns ``None`` when the window holds no well-formed contribution. Window
    bounds are ISO strings compared lexicographically (which is chronological
    for the `Z`-suffixed UTC stamps this ledger writes) and are INCLUSIVE at
    both ends, so a caller can name a day without arithmetic.

    The frame decides its own aggregation from the data rather than from a
    declared type: all-numeric values take the weighted median, anything else
    takes the weighted mode over the stringified values. One interface, so the
    same pipeline serves a resting-heart-rate frame and a strategic-direction
    frame — the general-purpose requirement, taken literally.
    """
    rows = _rows(evs, frame_id, start, end)
    if not rows:
        return None
    _weigh(rows)
    # The record contract requires a POSITIVE weight on every input, and
    # this fold is its only writer — so the invariant is enforced where it
    # is produced rather than discovered at the append surface, where the
    # only outcome left is a raise. `_rows` already drops the one known way
    # to weigh nothing; this covers any later one.
    rows = [row for row in rows if row["weight"] > 0]
    if not rows:
        return None

    numbers = [_as_number(row["value"]) for row in rows]
    numeric = all(n is not None for n in numbers)
    if numeric:
        value = _weighted_median([(n, row["weight"])
                                  for n, row in zip(numbers, rows)])
        value_kind = "numeric"
    else:
        value = _weighted_mode([(str(row["value"]), row["weight"])
                                for row in rows])
        value_kind = "categorical"

    # Two byte-identical contributions share one content address, so a ref alone
    # would name "one of these" and N identical rows would all resolve to the
    # first event. `occurrence` is the 1-based rank of the row among the rows
    # carrying that ref — counted here, over the SAME ordered list `trace` walks.
    # Counting inside the window is sound because identical bytes imply an
    # identical `ts` and `frame_id`: two rows that share a ref cannot fall on
    # opposite sides of a window bound or of the frame filter.
    seen = {}
    inputs = []
    for row in rows:
        ref = event_ref(row["ev"])
        seen[ref] = seen.get(ref, 0) + 1
        # BOTH forms. The float is what a human reads; `weight_exact` is
        # what makes the record re-derivable, and without it the published
        # trail did not reproduce the published value (1/3 -> 0.333333,
        # three of which sum to 0.999999, which walks off the median).
        inputs.append({"ref": ref,
                       "occurrence": seen[ref],
                       "weight": round(float(row["weight"]), 6),
                       "weight_exact": str(row["weight"]),
                       "reason": row["reason"]})

    stamps = sorted(row["ts"] for row in rows)
    return {
        "frame_id": frame_id,
        "rule_version": RULE_VERSION,
        "window": {"start": start or stamps[0], "end": end or stamps[-1]},
        "inputs": inputs,
        "value": _clean_number(value),
        "value_kind": value_kind,
        "subjects": len({row["subject"] for row in rows}),
    }


def latest_record(evs, frame_id) -> "dict | None":
    """The newest `consensus_record` payload for *frame_id*, or None.

    Last-wins over `read_all`'s `(ts, segment, line)` order rather than a max()
    on the timestamp: two records folded inside the same second must resolve the
    same way here as everywhere else that reads this ledger.
    """
    found = None
    for ev in evs or []:
        if getattr(ev, "type", None) != RECORD_TYPE:
            continue
        payload = getattr(ev, "payload", None)
        if isinstance(payload, dict) and payload.get("frame_id") == frame_id:
            found = payload
    return found


def trace(evs, record) -> list[dict]:
    """Walk one record back to the events it folded.

    This is the "decide in daylight" requirement made mechanical rather than
    documentary: the record names its inputs, and this resolves each one to the
    event whose bytes hash to it. An input that resolves to nothing comes back
    as ``found: False`` with ``event: None`` — a finding about the ledger, which
    a caller can exit non-zero on, and never an exception, because a crash here
    hides the one thing the reader asked to see.
    """
    if not isinstance(record, dict):
        return []
    rows = record.get("inputs")
    if not isinstance(rows, list):
        return []
    # A LIST per ref, not one event: byte-identical events share an address, and
    # a dict would silently collapse them so that three inputs proved one event.
    by_ref = {}
    for ev in evs or []:
        by_ref.setdefault(event_ref(ev), []).append(ev)
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ref = row.get("ref")
        occurrence = row.get("occurrence")
        if (not isinstance(occurrence, int) or isinstance(occurrence, bool)
                or occurrence < 1):
            # `records.validate_payload` refuses a record without a usable
            # occurrence, so this only fires on a hand-written row. Read it as 1
            # rather than refusing: this function's job is to SHOW the trail,
            # and a reader who asked to see it is worst served by an exception.
            occurrence = 1
        candidates = by_ref.get(ref) or []
        found = candidates[occurrence - 1] if occurrence <= len(candidates) else None
        out.append({"ref": ref,
                    "occurrence": occurrence,
                    "weight": row.get("weight"),
                    "reason": row.get("reason"),
                    "found": found is not None,
                    "event": events.to_dict(found) if found is not None else None})
    return out


def contribute(kind, frame_id, subject, value, confidence=None,
               session_id=None, project=None):
    """Validate one framed contribution and append it. Raises ``ValueError``
    with the corrective hint on an invalid body — the same shape
    `events.validate_typed_payload` raises at the raw-append surface, so the CLI
    and the HTTP route report one thing."""
    payload = {"frame_id": frame_id, "kind": kind, "subject": subject,
               "value": value}
    if confidence is not None:
        payload["confidence"] = confidence
    contributions.validate_payload(payload)
    return events.append_event(CONTRIBUTION_TYPE, payload,
                               session_id=session_id, project=project)


def fold_and_record(frame_id, start=None, end=None, session_id=None,
                    project=None):
    """Fold the frame and append the record. ``None`` when there was nothing to
    fold — `nothing_to_do`, which this codebase counts as a success.

    The record is validated before the append rather than after: an invalid
    record that reached the ledger could not be un-appended, and the fold is the
    only writer, so a violation here is a bug in the rule and must stop at the
    boundary rather than become a permanent row.
    """
    evs = events.read_all(project)
    folded = fold_window(evs, frame_id, start=start, end=end)
    if folded is None:
        return None
    records.validate_payload(folded)
    return events.append_event(RECORD_TYPE, folded,
                               session_id=session_id, project=project)
