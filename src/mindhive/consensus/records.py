# SPDX-License-Identifier: Apache-2.0
# src/mindhive/consensus/records.py
"""The `consensus_record` append contract — a folded answer that can be argued
with.

THIS FILE IS THE ANTI-BLACK-BOX RULE, EXPRESSED AS A GATE. The PRD's failure
condition is a derived conclusion that cannot be traced to its raw inputs, and a
logging convention would not prevent it: conventions rot silently, and the number
still looks authoritative afterwards. So the record does not merely have inputs
— every input must carry the WEIGHT it was given and the REASON that weight was
chosen, and a record missing either is refused at the append surface. A record
that reached the ledger without them would be permanent (this store is
append-only), and every reader downstream would treat it as evidence.

WHY `rule_version` IS REQUIRED. The weighting rule will change. An unversioned
record cannot be re-derived, so it cannot be checked, so nothing distinguishes a
number the current rule produces from one an older rule produced. With it, two
rules coexist in one ledger and can be compared — which is what makes changing
the rule a new record rather than a migration of old ones.

WHY THE WINDOW NEEDS BOTH BOUNDS. "Last week" is not re-runnable. A record whose
window is open at either end cannot be recomputed from the ledger, because the
set of events it folded is not determined by anything it wrote down.

WHY `subjects` IS REQUIRED. It is the one number a reader needs to judge the
answer and cannot recover from `value`: a median of 62 over 400 subjects and the
same median over 2 are different claims. Deriving it from `inputs` at read time
would be possible but would let a record disagree with its own input list.

WHY EVERY INPUT ALSO CARRIES AN `occurrence`. A ref is a content address, so two
byte-identical contributions — same subject, same value, same second — share one.
Three such rows pointing at one ref name "one of these events" three times, and an
audit trail that cannot say which row is which input is ambiguous exactly where
this module promises it is not. `occurrence` is the 1-based rank of the row among
the events carrying its ref, which makes N identical inputs resolve to N events.

`ref` is checked for SHAPE (64 lowercase hex, a SHA-256 digest) but not for
RESOLVABILITY, and that split is deliberate. Resolving a ref means reading the
whole ledger, which an append-surface validator must not do — a validator that
performs I/O cannot run where this one has to. Resolvability is `consensus.trace`'s
job, and `hive consensus trace` exits non-zero when a ref resolves to nothing.
"""
import re
from fractions import Fraction

_REF_RE = re.compile(r"^[0-9a-f]{64}$")
# `Fraction`'s own str: "1/3", or plain "1" when the denominator is 1.
_EXACT_RE = re.compile(r"^[0-9]+(/[0-9]+)?$")
VALUE_KINDS = ("numeric", "categorical")


def _is_number(value) -> bool:
    """A finite number — see the same guard in `contributions`: a NaN
    reaches `canonical_json` as a bare `NaN` token, which is not JSON, in a
    ledger that cannot be rewritten."""
    import math

    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value))


def validate_payload(payload) -> None:
    """Raise ``ValueError`` unless *payload* is a well-formed consensus record."""
    if not isinstance(payload, dict):
        raise ValueError("consensus_record payload must be an object")

    for key in ("frame_id", "rule_version"):
        got = payload.get(key)
        if not isinstance(got, str) or not got.strip():
            raise ValueError(
                f"consensus_record requires a non-empty {key} — rule_version is "
                "what lets this record be re-derived and compared against a "
                "later rule")

    window = payload.get("window")
    if not isinstance(window, dict):
        raise ValueError(
            "consensus_record requires a window object with start and end")
    for bound in ("start", "end"):
        got = window.get(bound)
        if not isinstance(got, str) or not got.strip():
            raise ValueError(
                f"consensus_record window requires a {bound} timestamp — a "
                "window open at either end names no determinate set of events, "
                "so the record cannot be recomputed")

    rows = payload.get("inputs")
    if not isinstance(rows, list) or not rows:
        raise ValueError(
            "consensus_record requires a non-empty inputs list — a value with "
            "no inputs cannot be traced back to anything")
    for i, row in enumerate(rows):
        where = f"inputs[{i}]"
        if not isinstance(row, dict):
            raise ValueError(f"consensus_record {where} must be an object")
        ref = row.get("ref")
        if not isinstance(ref, str) or not _REF_RE.match(ref):
            raise ValueError(
                f"consensus_record {where} requires ref: the 64-hex content "
                "address of the event it folded (consensus.event_ref)")
        occurrence = row.get("occurrence")
        if (not isinstance(occurrence, int) or isinstance(occurrence, bool)
                or occurrence < 1):
            raise ValueError(
                f"consensus_record {where} requires occurrence >= 1: which of "
                "that ref's events this row is. Two byte-identical "
                "contributions share one content address, so the ref alone "
                "would name 'one of these' — an ambiguous trail is the black "
                "box this contract refuses")
        weight = row.get("weight")
        if not _is_number(weight) or weight <= 0:
            raise ValueError(
                f"consensus_record {where} requires a positive weight — an "
                "input whose weight is not recorded cannot be argued with, "
                "which is the black box this contract exists to refuse")
        # THE EXACT SHARE, required. The float above is a rendering for a
        # human and it is lossy: measured, a frame whose exact weights are
        # 1/3,1/3,1/3,1 records the value 50 while the same median computed
        # from the PUBLISHED weights (0.333333) gives 90. A record whose own
        # numbers do not reproduce its own answer is the black box this
        # contract exists to refuse, arriving through the audit trail
        # instead of around it. The module header already argues why the
        # weights are Fractions; this is that argument reaching the record.
        exact = row.get("weight_exact")
        if not isinstance(exact, str) or not _EXACT_RE.match(exact.strip()):
            raise ValueError(
                f"consensus_record {where} requires weight_exact: the "
                "weight as an exact fraction ('1/3', or '1'). The rounded "
                "float cannot re-derive the value it helped produce")
        try:
            share = Fraction(exact.strip())
        except (ValueError, ZeroDivisionError):
            raise ValueError(
                f"consensus_record {where} weight_exact {exact!r} is not a "
                "readable fraction")
        if share <= 0:
            raise ValueError(
                f"consensus_record {where} weight_exact must be positive")
        if abs(float(share) - weight) > 1e-6:
            raise ValueError(
                f"consensus_record {where} weight {weight} and "
                f"weight_exact {exact!r} disagree — the float is supposed "
                "to be a rendering OF the exact share, so a record that "
                "states both must state one thing")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f"consensus_record {where} requires a reason naming why that "
                "weight was assigned (e.g. 'subject-share 1/3, confidence 0.5')")

    if "value" not in payload:
        raise ValueError("consensus_record requires the folded value")
    value = payload["value"]
    if not _is_number(value) and not (isinstance(value, str) and value.strip()):
        raise ValueError(
            "consensus_record value must be a number or a non-empty string")

    if payload.get("value_kind") not in VALUE_KINDS:
        raise ValueError(
            "consensus_record requires value_kind — one of: "
            + ", ".join(VALUE_KINDS))

    subjects = payload.get("subjects")
    if not isinstance(subjects, int) or isinstance(subjects, bool) or subjects < 1:
        raise ValueError(
            "consensus_record requires subjects: how many distinct "
            "participants the fold covered — the same median over 400 subjects "
            "and over 2 are different claims")
