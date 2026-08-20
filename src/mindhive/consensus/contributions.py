# src/mindhive/consensus/contributions.py
"""The `contribution` append contract — one framed input from one subject.

Registered in `events._PAYLOAD_CONTRACTS`, which is the seam that already exists
for exactly this job: `hive append`, `hive consensus contribute` and the HTTP
route all reach `events.validate_typed_payload`, so none of them can drift from
the others. There is no separate validator layer here for the same reason there
is no separate one for `code_change` — a second gate is a second thing to keep
in sync.

ONE EVENT TYPE FOR EVERY POPULATION AND EVERY INSTRUMENT. `kind` is the
discriminant, and it is the ONLY thing that separates a youth anxiety sample from
a technology executive's strategic input. A second event type per population
would put the demographic into the schema, where it becomes a fork in every
reader — and the general-purpose requirement is precisely that the same pipeline
serves both.

WHAT THIS REFUSES, AND WHY EACH REFUSAL IS LOAD-BEARING:

* **A missing `subject`.** The fold gives every subject total weight 1, split
  across their contributions. With no subject there is nothing to normalise by,
  and one participant sending fifty samples silently outvotes fifty
  participants. A subject-less contribution is not a slightly worse input, it is
  an input that breaks the rule for everyone else in the frame.
* **A self-claimed `weight`.** The contributor does not get to say how much they
  count. Accepting it would make the record an account of what senders asked for
  rather than of what the rule decided, which is the black box the PRD forbids.
* **A `confidence` outside [0, 1].** It multiplies the weight. Unbounded, one
  contribution with `confidence: 10_000` is a self-claimed weight wearing a
  different name.

WHAT IT DELIBERATELY ALLOWS: extra keys (a `unit`, a device id, a note). A
frame's instrument knows things this contract does not, and rejecting unknown
keys would make every new sensor a change to this file.

`value` may be a number or a non-empty string, because the fold handles both
(weighted median / weighted mode). `bool` is refused: `True` is an `int` in
Python and would fold as 1 into a numeric frame it does not belong to.
"""

# Present as a module constant so a test can assert the boundary rather than a
# literal that could drift from the message.
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0

_REQUIRED_STRINGS = ("frame_id", "kind", "subject")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_payload(payload) -> None:
    """Raise ``ValueError`` unless *payload* is a well-formed contribution. The
    message carries the corrective hint, because `events.validate_typed_payload`
    shows it verbatim to whoever tried the append."""
    if not isinstance(payload, dict):
        raise ValueError("contribution payload must be an object")

    for key in _REQUIRED_STRINGS:
        got = payload.get(key)
        if not isinstance(got, str) or not got.strip():
            raise ValueError(
                f"contribution requires a non-empty {key} — "
                "frame_id names the framed question, kind names the instrument "
                "(e.g. biometric.resting_hr), subject names the participant")

    if "value" not in payload:
        raise ValueError("contribution requires a value (the measurement)")
    value = payload["value"]
    if not _is_number(value) and not (isinstance(value, str) and value.strip()):
        raise ValueError(
            "contribution value must be a number or a non-empty string — a "
            "boolean folds as 1 into a numeric frame it does not belong to; "
            "send 0/1 or a category name instead")

    if "weight" in payload:
        raise ValueError(
            "contribution must not state its own weight — the fold assigns it "
            "(one share per subject, times any stated confidence) and records "
            "the reason, so the audit trail says what the rule decided")

    conf = payload.get("confidence")
    if conf is not None:
        if not _is_number(conf) or not CONFIDENCE_MIN <= conf <= CONFIDENCE_MAX:
            raise ValueError(
                "contribution confidence must be a number in "
                f"[{CONFIDENCE_MIN}, {CONFIDENCE_MAX}] — it multiplies the "
                "weight, so an unbounded value is a self-claimed weight")
