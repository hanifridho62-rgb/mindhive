# src/mindhive/state.py
"""The `state_set` contract: what the top of STATE.md may contain.

`state_set` is the payload behind the top of STATE.md — **Active task**,
**Last verified**, **Next step**, and the two bullet lists — and STATE.md is
what `sync` embeds in the auto-loaded MEMORY.md. It is therefore the
highest-trust text in the system, and until this module it was the one semantic
surface with no module of its own: no contract in
`events._PAYLOAD_CONTRACTS`, no caps, no shape.

That is not a coincidence, it is the cause. Every other registry has an owning
module (`sop`, `roadmap`, `changelog`, `autoresume`, `jobs`, `consensus`) and
every one of those registered a `validate_payload` there. `state_set` has no
subcommand at all — `grep -rn state_set src/mindhive/__main__.py` finds
nothing — so `hive append state_set --json …` is its ONLY writer, and the one
write surface it has was also the one nobody was guarding.

WHAT THIS REFUSES, and why each one is a real shape rather than a tidy-up:

  * a non-dict payload. `projector.build_state` had to grow
    `if not isinstance(s, dict): s = {}` because a list payload rendered the
    whole state block as "(none)" — a silently empty top of MEMORY.md.
  * a text field that is not text. `task` as a dict rendered as `str(dict)`.
  * a bullet list that is not a list. `open_questions` as the STRING "abc"
    iterated as three bullets, `a`, `b` and `c`, none of them anything anybody
    wrote; a dict iterated as its keys. `provenance.safe_bullets` makes that
    inert at render time, and this makes it impossible at write time.
  * an UNBOUNDED bullet list. Nothing capped the item count, so one append with
    50,000 `open_questions` was 50,000 bullets in the auto-loaded context —
    invariant 4, the one every neighbouring registry already respects
    (`roadmap.MAX_STEPS`, the notice's done-tail and pending-head).
  * a field longer than the renderer will show. Refused rather than truncated,
    on `roadmap._validate_work_link`'s reasoning: the caller still holds the
    value while the write is being rejected, and cannot once it is stored.

Every field is OPTIONAL. An empty `state_set` is a legitimate write — it means
"nothing is set" — and unknown keys are accepted ALONGSIDE a known one, because
the store is append-only and a contract that refused tomorrow's key would refuse
to read back yesterday's events. The single narrowing is a payload with NO known
key at all, which is refused for a reason the comment at that line gives: this
type is last-one-wins, so an unreadable row does not sit beside the state, it
replaces it. This is a gate on the two RAW-APPEND surfaces
(`__main__.cmd_append` and `dashboard_post`), never on `events.append_event`
itself: the store stays type-agnostic, and every reader stays total over
history that predates this file.
"""
from . import provenance

# One spelling, shared with the renderer that truncates at it, so the write
# surface and the display surface cannot come to disagree about how much of a
# task description survives.
MAX_FIELD_CHARS = provenance.FIELD_CHARS
# The bullet budget. `provenance.MAX_BULLETS` is the render-side half; this
# refuses at the write surface so the render side never has to elide.
MAX_ITEMS = provenance.MAX_BULLETS

TEXT_FIELDS = ("task", "verified", "next_step")
LIST_FIELDS = ("open_questions", "deferred")
# Exactly what the three readers consume: `projector.build_state` reads all
# five, `briefing.build_briefing` reads all five, `oversee._fold` reads `task`
# and `next_step`. A key outside this set renders nowhere.
KNOWN_FIELDS = TEXT_FIELDS + LIST_FIELDS


def validate_payload(payload) -> None:
    """Enforce the `state_set` contract at the raw-append surfaces.

    Raises ``ValueError``; `events.validate_typed_payload` appends the
    corrective hint from the registry table so both surfaces say the same
    thing."""
    if not isinstance(payload, dict):
        raise ValueError(
            f"state_set payload must be an object with the state fields — "
            f"got {type(payload).__name__}; a list or a bare string renders "
            f"the whole block as empty")

    # A payload that sets NOTHING this renderer knows about is refused, and
    # this is the one place the "accept tomorrow's key" rule below is narrowed.
    # The reason is specific rather than tidiness: `state_set` is last-one-wins,
    # so a row nobody can read does not sit inertly beside the real state, it
    # REPLACES it -- the whole top of STATE.md goes blank with no error
    # anywhere. And the shape that does it is not hypothetical:
    # `dashboard_post._post_append` wraps a plain string as `{"text": …}`, so
    # the dashboard's own Append box with the type set to `state_set` blanks
    # the operator's state. An empty `{}` stays legal: that is an explicit
    # clear, and it reads as one.
    if payload and not any(k in payload for k in KNOWN_FIELDS):
        raise ValueError(
            f"state_set payload sets none of {', '.join(KNOWN_FIELDS)} — "
            f"got {', '.join(sorted(map(str, payload)))}; the newest state_set "
            f"REPLACES the whole state block, so an unreadable one blanks it")

    for key in TEXT_FIELDS:
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"state_set {key} must be text — got "
                f"{type(value).__name__}; it is rendered as one line at the "
                f"top of STATE.md")
        if len(value) > MAX_FIELD_CHARS:
            raise ValueError(
                f"state_set {key} exceeds {MAX_FIELD_CHARS} chars — that is "
                f"the width STATE.md renders, so the rest would be dropped "
                f"silently after the write")

    for key in LIST_FIELDS:
        value = payload.get(key)
        if value is None:
            continue
        # A string is the specific hazard here, and it is a `list`-shaped
        # mistake rather than a hostile one: `{"open_questions": "one thing"}`
        # reads perfectly and renders one bullet per character. `list` and not
        # "iterable": a str, a bytes and a dict are all iterable, all three
        # are the mistake, and JSON has no other sequence to be generous to.
        if not isinstance(value, list):
            raise ValueError(
                f"state_set {key} must be a list of strings — got "
                f"{type(value).__name__}; a string here renders one bullet "
                f"per character")
        if len(value) > MAX_ITEMS:
            raise ValueError(
                f"state_set {key} exceeds {MAX_ITEMS} items — this list rides "
                f"the auto-loaded MEMORY.md, so it is bounded like every other "
                f"registry's")
        for item in value:
            if not isinstance(item, str):
                raise ValueError(
                    f"state_set {key} entries must be text — got "
                    f"{type(item).__name__}")
            if len(item) > MAX_FIELD_CHARS:
                raise ValueError(
                    f"state_set {key} entry exceeds {MAX_FIELD_CHARS} chars")


def fields(ev):
    """*ev*'s payload as a dict, or {} — the guard both renderers need.

    Total on purpose: these run on the projection path, where a raise is not an
    error message but a STATE.md that never gets written."""
    payload = getattr(ev, "payload", None) if ev is not None else None
    return payload if isinstance(payload, dict) else {}
