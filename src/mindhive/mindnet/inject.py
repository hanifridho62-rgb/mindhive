# src/mindhive/mindnet/inject.py
"""MN-1 U9 — phase-aware injection. The delivery mechanism for the phase spine.

Everything before this unit builds state; this is the only thing that spends it.
Without it MN-1's defining claim — unconditional, phase-aware steering — never
becomes true, and U1–U5 are an elaborate no-op.

## Unconditional is the whole difference

`enrich.extend_hook_context` (`enrich.py:456-484`) is the contract mirrored here:
same signature, same fail-open guarantee, same additive-only output. One
deliberate divergence — **it builds a context dict when handed ``None``**.

`promptgate.hook_context` returns ``{}`` for a well-specified prompt
(`promptgate.py:483-484`), and `enrich` rides that decision (`if not context:
return context`). That is exactly why injection is conditional today: a clear
prompt is steered by nothing. ADR-0005's methodology does not stop applying
because the prompt was well written, so this module never reads the prompt at
all. ``raw`` is accepted for signature parity — so `cmd_hook` composes the two
extensions identically — and deliberately unread, because reading it is the
thing that would make this conditional again.

## The read path never touches the ledger

`events.read_all` measures ~264 ms on the real store and this runs on EVERY user
prompt. Only `sidecar.read` is called, and `test_the_hook_path_never_calls_read_
all` pins that as a property rather than as a timing bound.

## Stale is not a fault, and gets its own branch (KTD9)

Three outcomes, not two:

- **fresh + a workable task** -> the phase-specific block.
- **stale** -> `AGNOSTIC_BLOCK` and nothing else. A stale-but-parseable record
  telling the agent to write code for a task that has since fallen back to the
  planning gate is worse than no injection; the fail-open contract covers faults,
  and staleness is not a fault.
- **nothing decodable, or nothing workable** -> silence. A corrupt sidecar and a
  project that has never used MindNetwork both decode to an empty state, and
  they are indistinguishable from here. Emitting the agnostic block for that
  would nag every prompt in every unrelated repo forever.

## The honesty rule has to survive the renderer (KTD2, KTD17)

U3 built two validators over one shape so a satisfaction Python did not check
cannot claim it did. A renderer that prints `audit-code` as machine-verified
undoes all of it, so the verification vocabulary here is three distinct strings
and the one used is the STRONGEST HONEST claim available: the recorded verdict's
own ``verified_by`` when the current generation carries one, otherwise the
phase descriptor's ceiling.

## Purity, bounds, and the sanitizer

`phase_block` and `active` are pure and total; only `extend_hook_context` does
I/O. Invariant 5 bounds SIZE, not item count, so the block is capped in
characters and every inventory-supplied string goes through `safe_text` — the
`promptgate._safe_text` idiom (`promptgate.py:377-382`) plus one addition: our
own marker is stripped, so a title cannot forge a second directive header.
"""
from __future__ import annotations

import re

from . import phases as ph_mod
from . import sidecar as s_mod
from . import tasks as t_mod
# WORKABLE_STATUSES lives in `controller.py`, not `tasks.py`: it is the set
# `can_advance` gates forward transitions on (KTD12). Imported from there rather
# than restated, so the injector can never steer a task the machine would refuse
# to move.
from .controller import WORKABLE_STATUSES
from .tasks import PHASES, TRIVIAL_EXIT_PHASE, phase_index

# Matches `promptgate.TRIGGER_MARK` / `enrich.ENRICH_MARK`: one grep-able token
# per injecting subsystem, so an operator reading a transcript can tell who said
# what.
MARK = "[mindnet]"

# Matches the marker in any casing and with internal padding. `str.replace` was
# not enough on either axis: it is case-SENSITIVE, so `[MINDNET]` survived it
# untouched, and it is a single left-to-right pass, so `[[mindnet]mindnet]`
# re-forms a literal `[mindnet]` after the inner occurrence is deleted. Both
# were reachable from any plugin description or SOP body once MN-3 started
# routing third-party text through here.
_MARK_RE = re.compile(r"\[\s*mindnet\s*\]", re.IGNORECASE)

# Invariant 5 bounds SIZE. Every field is already capped, so the natural block
# is ~700 chars; this is the wall that holds if a future line is added carelessly
# or a projection row arrives longer than its writer allowed.
MAX_BLOCK_CHARS = 1200
MAX_FIELD_CHARS = 120

# MN-3's advice gets its OWN budget, deliberately separate from the phase block
# above. The phase directive is the thing this module exists to deliver; sharing
# one wall would let a long SOP body push the exit criterion out of the prompt.
# `test_an_oversized_advice_payload_does_not_truncate_the_phase_block` drives
# that case directly.
# 2000, not 1800. The old value was derived from the item caps alone
# ((3+2+2) * 240 = 1680) and ignored what the RENDERER adds — three section
# headers, seven `- ` bullets and the newlines between them, 195 chars. A fully
# populated advice set renders to 1875, so the budget silently dropped the last
# skill and the guard test could not see it because it never rendered anything.
MAX_ADVICE_CHARS = 2000

# Per-item wall on the render side. `advice.MAX_ITEM_CHARS` already caps these
# at resolve time and the two are deliberately equal, but this module cannot
# import that one (it imports this one) and must not assume a file on disk was
# written by a cooperating version. The persistence suite pins them equal.
MAX_ADVICE_ITEM_CHARS = 240

# The advice section's keys, in RENDER order — SOPs and prior-failure
# constraints are hard rules and lead; skills are an offer and follow.
#
# Duplicated from `advice.to_dict` on purpose. This module must not import
# `advice` (that module imports this one, for the sanitizer), so the two ends of
# the serialization boundary know the shape independently.
# `test_the_injector_and_the_resolver_agree_on_the_section_keys` is what makes
# the duplication safe: rename a key on either side and it fails loudly instead
# of silently rendering nothing.
ADVICE_KEYS = ("sops", "constraints", "skills")

# What each section is FOR, in the imperative. Without these the block is three
# unlabelled lists and the agent has to guess which ones are binding.
ADVICE_LABELS = {
    "sops": "Standing procedures that govern this task — follow them",
    "constraints": "Prior recorded failures on work like this",
    "skills": "Skills matched to this task by the registry",
}

# The three verification claims, spelled out once. `MACHINE_VERIFIED` is the ONLY
# string containing the words "machine-verified", which is what lets a test
# assert its absence rather than a disclaimer's presence.
MACHINE_VERIFIED = ("machine-verified: a real Python check runs against this "
                    "and has to pass")
AGENT_REPORTED = ("NOT checkable by Python here — this gate is agent-reported, "
                  "so say so plainly instead of implying it was verified")
HUMAN_ATTESTED = ("NOT checkable by Python here — this gate is human-attested "
                  "at an interactive prompt")
_CLAIMS = {"python": MACHINE_VERIFIED, "agent": AGENT_REPORTED,
           "human": HUMAN_ATTESTED}

NOT_YET_SATISFIED = "no satisfaction recorded for the current generation"

# Phase-AGNOSTIC by construction: it names no phase and no task, because the
# whole reason it exists is that we cannot currently trust what the projection
# says either of them is. `test_a_stale_sidecar_yields_the_phase_agnostic_block_
# only` asserts that literally, against every name in PHASES.
AGNOSTIC_BLOCK = (
    f"{MARK} MindNetwork spine: the on-disk projection is out of date, so no "
    f"per-task direction is given this turn. Run `hive mindnet rebuild` to "
    f"refresh it and `hive mindnet show` to see where work actually stands. "
    f"Until then do not assume any gate has been cleared.")

# Selection order for equally-eligible tasks. Lower sorts first: what the agent
# already picked up beats what is merely unblocked, which beats what has not been
# started. Unknown status sorts last but is still eligible — see `active`.
_STATUS_RANK = {t_mod.STATUS_IN_PROGRESS: 0, t_mod.STATUS_READY: 1,
                t_mod.STATUS_PENDING: 2}


# --- the sanitizer ---------------------------------------------------------------

def safe_text(value, width: int = MAX_FIELD_CHARS) -> str:
    """Whitespace-collapsed, marker-stripped, length-capped rendering of a
    projection-supplied string.

    The `promptgate._safe_text` idiom (`promptgate.py:377-382`) with one
    addition. Collapsing whitespace is what stops a newline in a task title from
    becoming a new directive LINE; stripping `MARK` is what stops it forging a
    new directive HEADER, which a collapsed single line can still do. Applied to
    ``task_id`` as well as ``title``: `ID_RE` constrains an id at the append
    surface, but the sidecar is a file on disk and `sidecar._row` does not
    re-validate what it decodes.

    Total, including over `str()` itself: CPython refuses to stringify an
    integer past ``sys.get_int_max_str_digits()`` (4300 by default), and
    `sidecar._row` deliberately does not coerce decoded values, so a field this
    module expects to be text can arrive as an arbitrary-precision int off a
    corrupt file. That is a raise on the hook path hiding inside an f-string."""
    try:
        text = " ".join(str(value or "").split())
        # To a FIXED POINT, because deleting one occurrence can create another:
        # `[[mindnet]mindnet]` -> `[mindnet]` in a single pass. One `re.sub` is
        # still one pass; the loop is what actually closes it.
        while True:
            stripped = _MARK_RE.sub("", text)
            if stripped == text:
                break
            text = stripped
        return text[:width]
    except (ValueError, TypeError):
        return "?"


def safe_count(value, width: int = 24) -> str:
    """A projection-supplied integer, rendered totally.

    Not `safe_text`: ``str(0 or "")`` is ``""``, and generation 0 is the common
    case — a falsy-to-empty coercion would blank the field it exists to show."""
    try:
        return str(value)[:width]
    except (ValueError, TypeError):
        return "?"


# --- selecting the active task -----------------------------------------------------

def _rank(rec, task):
    """The total order over eligible tasks. STATED, so that ties do not fall out
    of dict order — `json.loads` preserves document order and `sidecar._decode`
    preserves it again, so dict order here is a function of WRITE order and the
    injected task would silently change on every rebuild.

    1. status rank: `in_progress` < `ready` < `pending` < unknown — steer what
       is already being worked before what merely could be;
    2. furthest along the pipeline, because the task nearest its terminal gate
       has the most actionable next step (an unrecognised phase sorts last);
    3. lowest epoch — fewest fall-backs is the healthier lane;
    4. `task_id`, alphabetically. The last tie-break must be total."""
    idx = phase_index(rec.phase)
    return (_STATUS_RANK.get(getattr(task, "status", None), len(_STATUS_RANK)),
            -idx if idx is not None else 1,
            rec.epoch,
            rec.task_id)


def active(sc):
    """``(PhaseRecord, Task | None)`` for the task this turn should steer, or
    ``(None, None)``.

    Eligibility is the WORKABLE statuses — `pending`, `ready`, `in_progress` —
    the same set `can_advance` gates forward transitions on (KTD12). `pending`
    is included deliberately and is the load-bearing part: it is the dataclass
    default for every declared task (`tasks.py:161`), so scoping this to
    `in_progress` would mean the normal case gets no injection at all. The other
    direction matters too — without the filter a task finished months ago keeps
    steering every prompt, because nothing ever removes its phase record.

    A record with no task row is eligible only when the sidecar carries NO graph
    at all (a v1 document, or a hand-repaired one): workability is then unknown,
    and unknown must not silently mean silence. When the graph IS present, a
    record missing from it is a real skew and is skipped."""
    records = getattr(getattr(sc, "state", None), "records", None) or {}
    tasks = getattr(sc, "tasks", None) or {}
    best = None
    for tid, rec in records.items():
        task = tasks.get(tid)
        if task is None:
            if tasks:
                continue
        elif task.status not in WORKABLE_STATUSES:
            continue
        key = _rank(rec, task)
        if best is None or key < best[0]:
            best = (key, rec, task)
    return (best[1], best[2]) if best else (None, None)


# --- rendering ---------------------------------------------------------------------

def _criterion(phase) -> str:
    """What clears *phase*, in one clause, read off `PHASE_EVIDENCE` rather than
    from a second copy of the legend living here (invariant 10)."""
    desc = ph_mod.descriptor(phase) or {}
    kind = desc.get("kind")
    if kind == "file":
        dirs = desc.get("required_dir") or ()
        return "a document under " + (" or ".join(dirs) or "a constrained path")
    if kind == "event":
        return "a ledger event joined to this task by task_id"
    if kind == "command":
        return "a recorded validation run (`hive mindnet validate`)"
    if kind == "human":
        return "an operator attestation at an interactive prompt"
    return "an exit criterion this build does not recognise"


def _verdict(rec):
    """``(claim, status_line)`` for the current ``(phase, epoch)``.

    The claim is the strongest HONEST one: a recorded verdict's own
    ``verified_by`` outranks the descriptor's ceiling, because `hive mindnet
    satisfy --agent-reported` can legitimately record an `agent` verdict for a
    phase whose descriptor says `python` — KTD15's refusal path does exactly
    that, and rendering the ceiling there would restate the lie U3 refused to
    accept at the append surface."""
    sat = rec.satisfaction(rec.phase, rec.epoch)
    if sat is None or not sat.valid:
        ceiling = (ph_mod.descriptor(rec.phase) or {}).get("verified_by")
        return _CLAIMS.get(ceiling, AGENT_REPORTED), NOT_YET_SATISFIED
    by = safe_text(sat.verified_by, 20)
    claim = _CLAIMS.get(by, AGENT_REPORTED)
    line = f"recorded '{safe_text(sat.result, 10)}' by {by}"
    if sat.stale:
        line += " — but a re-stat found its evidence gone, so it does not count"
    return claim, line


def _next_phase(phase, trivial):
    """The phase a forward move would land on, or None at the terminal gate.

    A bypassed task's terminal is the narrower `TRIVIAL_EXIT_PHASE`, not
    `pr-green` (ADR-0005). Deriving it from the same constant `can_complete`
    gates on is what keeps the bypass from drifting into a skeleton key."""
    idx = phase_index(phase)
    if idx is None:
        return None
    terminal = phase_index(TRIVIAL_EXIT_PHASE) if trivial else len(PHASES) - 1
    if terminal is None or idx >= terminal:
        return None
    return PHASES[idx + 1]


def phase_block(rec, task) -> str:
    """The phase-specific directive for *rec*. Pure, total, size-capped."""
    trivial = bool(getattr(task, "triviality_bypass", False))
    tid = safe_text(rec.task_id)
    title = safe_text(getattr(task, "title", "")) or "(untitled)"
    phase = safe_text(rec.phase, 40)
    idx = phase_index(rec.phase)
    where = f"step {idx + 1} of {len(PHASES)}" if idx is not None \
        else "an unrecognised position"
    claim, verdict = _verdict(rec)
    nxt = _next_phase(rec.phase, trivial)

    lines = [f"{MARK} MindNetwork spine — active task '{tid}' ({title}) is at "
             f"phase '{phase}' ({where}), generation "
             f"{safe_count(rec.epoch)}."]
    if trivial:
        lines.append(
            f"- This task carries triviality_bypass: the method phases are "
            f"waived and its terminal gate is the narrower "
            f"'{TRIVIAL_EXIT_PHASE}'.")
    lines.append(f"- Exit criterion for '{phase}': {_criterion(rec.phase)} — "
                 f"{claim}.")
    lines.append(f"- Current evidence: {verdict}.")
    if nxt:
        lines.append(f"- Next is '{nxt}', and only once the criterion above is "
                     f"cleared — `hive mindnet enter` refuses a forward move "
                     f"without it, and refuses skipping ahead entirely.")
    else:
        lines.append("- This is the terminal gate for this task; there is "
                     "nothing to advance to, only evidence to produce.")
    # The line that used to sit here — "Recall the ledger, then read the code,
    # then consult the skill catalog" — was MN-3's "partial failure": it named
    # three registries and queried none of them. The registries are now queried
    # at write time and their answers render below in `advice_block`, so a
    # pointer to them would be both redundant and, when the pointer disagreed
    # with the quoted text, actively confusing. The ordering advice it carried
    # ("ask the user last") is behaviour, not retrieval, and survives.
    lines.append("- Do not ask the user for anything the block above or below "
                 "already answers; ask last, not first.")

    # Two independent bounds, deliberately. The per-field caps above are what
    # actually bind today; this one is the wall that still holds when a future
    # line is added carelessly or a cap here is widened without thinking about
    # the total. Whole lines are dropped so a truncation never leaves a
    # half-sentence directive, and the header is kept unconditionally so an
    # oversized field degrades to a truncated line rather than to an empty
    # block. `test_the_block_is_size_capped` drives it directly rather than
    # through an oversized field, because a field-capped input would make this
    # loop unreachable and the test vacuous — a deleted-cap mutant survived
    # exactly that version of it.
    out: list = []
    total = 0
    for line in lines:
        if out and total + len(line) + 1 > MAX_BLOCK_CHARS:
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)[:MAX_BLOCK_CHARS]


def advice_block(obj) -> str:
    """MN-3's resolved advice, rendered. Pure, total, independently capped.

    *obj* is the sidecar's plain ``{section: [line]}`` dict — already resolved,
    already sanitized, already per-item capped by `advice.py`. Nothing here
    queries anything: the retrieval happened at write time, and re-deriving it
    on this path is exactly the ~264 ms the sidecar exists to avoid.

    Re-sanitizes anyway. `sidecar._decode_advice` validates the shape and
    `advice.resolve` strips the marker, but the sidecar is a file on disk and
    this string goes into a prompt — the cost of `safe_text` over at most seven
    short items is nothing next to a forged directive header.

    Empty in, empty out — and the caller relies on that: an advice header with
    no items under it would appear on every prompt in every project that has
    never resolved anything."""
    if not isinstance(obj, dict):
        return ""
    lines: list = []
    for key in ADVICE_KEYS:
        rows = obj.get(key)
        if not isinstance(rows, list):
            continue
        items = [safe_text(r, MAX_ADVICE_ITEM_CHARS) for r in rows]
        items = [i for i in items if i]
        if not items:
            continue
        lines.append(f"{MARK} {ADVICE_LABELS[key]}:")
        lines.extend(f"- {i}" for i in items)
    if not lines:
        return ""
    # Whole items are dropped rather than the text being sliced, for the same
    # reason `phase_block` drops whole lines: a half-sentence SOP reads as a
    # complete instruction that happens to be wrong. A section header left
    # dangling by its items being dropped is pruned on the way out.
    out: list = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > MAX_ADVICE_CHARS:
            break
        out.append(line)
        total += len(line) + 1
    while out and out[-1].startswith(MARK):
        out.pop()
    return "\n".join(out)


def block(sc) -> str:
    """What to inject for sidecar *sc*: the phase-specific block, the agnostic
    one, or ``""`` for silence. See the module docstring's three outcomes."""
    records = getattr(getattr(sc, "state", None), "records", None) or {}
    if not records:
        return ""
    if not getattr(sc, "fresh", False):
        return AGNOSTIC_BLOCK
    rec, task = active(sc)
    if rec is None:
        return ""
    text = phase_block(rec, task)
    # Only reached on the fresh branch, and that placement is the point: a
    # stale projection returns `AGNOSTIC_BLOCK` above and never gets here.
    # Advice resolved against a task that has since fallen back to the planning
    # gate is a confident, wrong instruction — strictly worse than silence,
    # which is the same argument this module already makes for stale phase data.
    extra = advice_block(getattr(sc, "advice", None))
    return f"{text}\n{extra}" if extra else text


# --- the hook surface --------------------------------------------------------------

def extend_hook_context(context, raw, project=None, sc=None) -> dict:
    """Append MN-1's phase directive to a UserPromptSubmit hook context.

    Mirrors `enrich.extend_hook_context` (`enrich.py:456-484`) — additive only,
    never a block decision, never raises — with the one divergence the module
    docstring explains: a ``None`` or empty *context* is BUILT rather than passed
    through, because this fires on a clear prompt too.

    *raw* is accepted for signature parity and deliberately unread. *sc* is a
    seam for callers that already hold a `Sidecar` (and for tests); the hook path
    leaves it None and pays one `sidecar.read`.

    Fail-open: any fault returns *context* exactly as given, including ``None``.
    A raise here does not degrade a feature, it breaks the user's prompt."""
    try:
        if sc is None:
            sc = s_mod.read(project)
        text = block(sc)
        if not text:
            return context
        base = context if isinstance(context, dict) else {}
        hso = dict(base.get("hookSpecificOutput") or {})
        prev = str(hso.get("additionalContext") or "")
        hso["hookEventName"] = "UserPromptSubmit"
        hso["additionalContext"] = f"{prev}\n{text}" if prev else text
        return dict(base, hookSpecificOutput=hso)
    except Exception:
        return context
