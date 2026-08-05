"""Adaptive work injection: replenish a surface's plan from what the record proves.

Work in this system ends quietly. An interactive session stops when the human
stops typing; a `hive loop` run stops when its checklist empties. Neither moment
asks whether the ledger still holds proof of unfinished work, so debt accumulates
while every individual ending looks correct.

This module is the part that asks. It runs **lenses** over the ledger, each
answering one question about the record ("did code change with nothing verifying
it since?"), gates the candidates they produce, and hands the survivors to a
**sink** that lands them in whatever shape forces continuation on that surface.

Three properties are load-bearing.

**Evidence is mandatory.** A candidate must point at a real ledger row, file, or
signature. This is the single rule separating replenishment from inventing work
to fill a quota, and it is enforced here rather than trusted to each lens.

**The core knows nothing about sinks.** `round()` takes the sink as a parameter.
Deciding what is outstanding is surface-independent; forcing it is not — an
interactive session's sink writes roadmap steps, the loop's sink writes checklist
items whose count the exit gate refuses to pass. Importing a sink here would tie
the decision layer to one surface and make the second one a rewrite.

**There is no sidecar.** Spent fingerprints, the round counter, and the
consecutive dry-round count are folded back out of `work_injected` events. A
sidecar would be a second source of truth, and the failure mode when it drifted
would be invisible: work that quietly stopped being proposed.

Payload contract for `work_injected` (registered in `events._PAYLOAD_CONTRACTS`,
so `hive append` and `POST /api/append` enforce it too):

    {"outcome": "landed",    "round": 1, "surface": "session",
     "lens": "evidence-gap", "title": "…", "evidence": ["…"],
     "fingerprint": "…", "score": 1.0, "plan_id": "…", "step_index": 3}
    {"outcome": "refused",   "round": 1, "surface": "session",
     "lens": "evidence-gap", "title": "…", "fingerprint": "…",
     "reason": "no evidence"}
    {"outcome": "truncated", "round": 1, "surface": "session", "dropped": 4}
    {"outcome": "dry",       "round": 2, "surface": "session"}

`dry` exists as its own outcome because "this round found nothing" has to be a
recorded fact, not the absence of one: the two-consecutive-empty-rounds exit
cannot be derived from silence.
"""

# Outcome vocabulary. `landed` and `refused` are per-candidate; `truncated` and
# `dry` are per-round facts about the round itself.
OUTCOMES = ("landed", "refused", "truncated", "dry")

MAX_TITLE_CHARS = 300          # matches roadmap.MAX_STEP_CHARS — a landed title
                               # becomes a step, and a title the sink would have
                               # to truncate is one the audit row misreports.
MAX_REASON_CHARS = 500
MAX_EVIDENCE_ITEMS = 12
MAX_EVIDENCE_CHARS = 300


def _require_str(payload, key, cap, *, what):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"work_injected {what} requires a non-empty {key}")
    if len(value) > cap:
        raise ValueError(f"work_injected {key} exceeds {cap} chars")


def _require_positive_int(payload, key):
    value = payload.get(key)
    # bool is an int subclass; True would otherwise pass as 1 and make a
    # malformed row look like a legitimate round number.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"work_injected requires a positive integer {key}")


def validate_payload(payload) -> None:
    """Enforce the `work_injected` contract at every append surface.

    Checked rather than trusted because the round's own state is folded back out
    of these rows: a bogus `outcome` or a missing `fingerprint` would corrupt the
    dedup set and the dry-round counter, and the only symptom would be work that
    silently stopped being proposed. The event store itself stays type-agnostic —
    the contract lives here, mirroring changelog.py / roadmap.py / sop.py."""
    if not isinstance(payload, dict):
        raise ValueError("work_injected payload must be an object")
    outcome = payload.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError(
            "work_injected requires an outcome — one of: " + ", ".join(OUTCOMES))
    _require_positive_int(payload, "round")
    _require_str(payload, "surface", 64, what="event")

    if outcome == "dry":
        return

    if outcome == "truncated":
        _require_positive_int(payload, "dropped")
        return

    # landed / refused are per-candidate and carry the candidate's identity.
    _require_str(payload, "lens", 64, what=outcome)
    _require_str(payload, "title", MAX_TITLE_CHARS, what=outcome)
    _require_str(payload, "fingerprint", 128, what=outcome)

    if outcome == "refused":
        _require_str(payload, "reason", MAX_REASON_CHARS, what="refused")
        return

    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(
            "work_injected landed requires a non-empty evidence list — a "
            "candidate with nothing to point at is exactly what the value gate "
            "refuses")
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        raise ValueError(
            f"work_injected evidence exceeds {MAX_EVIDENCE_ITEMS} items")
    for item in evidence:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("work_injected evidence items must be non-empty text")
        if len(item) > MAX_EVIDENCE_CHARS:
            raise ValueError(
                f"work_injected evidence item exceeds {MAX_EVIDENCE_CHARS} chars")
    score = payload.get("score")
    if score is not None and (isinstance(score, bool)
                              or not isinstance(score, (int, float))):
        raise ValueError("work_injected score must be a number when present")
