# src/mindhive/mindnet/phases.py
"""MN-1 — the per-phase evidence descriptors and the ``mindnet_phase`` contract.

Two things live here and nothing else: WHAT counts as evidence that a phase's
exit criterion was cleared, and WHAT SHAPE a ``mindnet_phase`` event may take.
The state machine that consumes both is `controller.py`.

## The legend is imported, never redefined

``PHASES`` comes from `tasks.py`. Invariant 10 — one legend, one home. A second
tuple here would be exactly the drift the invariant exists to stop, and it would
drift *silently*, because each copy looks correct in isolation.

## ``verified_by`` is DERIVED by the appending code path, never accepted

Hence two validators over one shape (KTD2):

- `validate_payload` — the **append surface** (``hive append mindnet_phase``
  and the dashboard's ``POST /api/append``). It REFUSES any payload carrying
  ``verified_by``. Without that refusal the untrusted party fills in the field
  that says whether to trust them, and the honesty rule is decoration.
- `validate_stored` — the **writer** shape, called by the CLI immediately
  before ``append_event`` (the roadmap/sop convention). It REQUIRES
  ``verified_by``, because a satisfaction that does not say who checked it
  cannot be weighed against a contradicting one (KTD3's authority order:
  python > agent > human, and a python ``fail`` is never overridden by an agent
  ``ok``).

## Purity

Everything here is total and deterministic — no clock, no RNG, and no path
lookup of its own. `resolve_ref` takes the worktree root as an ARGUMENT rather
than calling `paths.worktree_root` itself, so the fold that will consume these
descriptors stays free of I/O.
"""
from __future__ import annotations

from pathlib import Path

from ..sop import ID_RE
from .tasks import PHASES

# --- what counts as evidence ---------------------------------------------------

# file    — a document at a constrained path (the three method phases)
# event   — another ledger event joins to this task (execution -> code_change)
# command — a validation run's runlog artifact (MN-0)
# human   — an operator attested it at an interactive prompt
EVIDENCE_KINDS = ("file", "event", "command", "human")

# Who did the checking. Ordered STRONGEST FIRST: `controller.py` resolves two
# contradicting records for the same (task, phase, epoch) by this order rather
# than by replay order, because replay order is manipulable (second-resolution
# `ts` ties break alphabetically by session id, and `ts` itself is
# caller-supplied). The sequence is load-bearing, not decorative.
VERIFIERS = ("python", "agent", "human")

ACTIONS = ("declare", "enter", "satisfy", "fall_back")
RESULTS = ("ok", "fail")

# Size caps bound every event, so one oversized field can never invalidate (and
# thereby silently drop) a whole phase record — tasks.py / roadmap.py rationale.
MAX_DETAIL_CHARS = 2000
MAX_REF_CHARS = 300

# Per-phase exit criterion. ``required_dir`` is ALWAYS a tuple of
# worktree-root-relative directories — one shape to reason about, empty for a
# phase that takes no file evidence, single-entry where only one location is
# legal. "Sometimes a string, sometimes a tuple" is how a caller ends up
# iterating the characters of a path. ``verified_by`` is the STRONGEST authority
# this phase can honestly claim.
#
# `plan` carries TWO directories because this repo genuinely has two: the
# superpowers workflow wrote `docs/superpowers/plans/`, `ce-plan` writes
# `docs/plans/`, and neither is going away in this slice. MN-1's own plan lives
# in the latter, so a single-directory rule would have made this machine unable
# to validate the document that specified it — and a spec that cannot validate
# its own plan file gets worked around rather than followed. Widening is
# bounded, not open: two named directories, still containment-checked, so this
# never degrades into "any file under docs/".
#
# `brainstorm` gaining a directory at all closes a real hole: it previously had
# none, so *any* non-empty file satisfied it — including `mindnet/state.json`,
# the machine's own sidecar.
#
# `audit-code` and `audit-system` are agent-reported (KTD17) and that is not a
# concession, it is the honesty rule applied to ourselves: no review event type
# is registered in `events.py`, and `hive review clear` (`review.py:121-126`)
# unlinks a sidecar and appends NOTHING. There is no artifact Python could read,
# so a `python` claim for these two would be a lie — the precise thing KTD2
# exists to prevent. A `review_cleared` producer is its own slice.
#
# `pr-green` is NOT in that group, and MN-8b moved it out. It shipped as
# `agent` on a rationale borrowed from the two audits above — Python cannot see
# GitHub CI, because `executor.DENIED_PROGRAMS` refuses `gh`. That much is still
# true and the boundary is unchanged. What does not follow is the conclusion:
# the audits have NO artifact Python can read, and this phase does. The
# repository's own suite exits with a code, `executor.validate` runs it as a
# real subprocess, and `python` stamped off a real exit code is the honest label
# — the same one `audit-bug` already carries for the same reason.
#
# So the claim is bounded rather than dropped: this gate asserts the LOCAL suite
# passed, which is what `CLAUDE.md` already accepts as standing in for CI. It
# does not assert anything about GitHub's matrix, and nothing here may.
#
# The consequence is deliberate: `controller._apply_satisfy` refuses a claim
# weaker than the descriptor, so an agent saying "the PR is green" now lands as
# a violation instead of closing the task. `executor.agent_satisfy` reports that
# through `clears=False`. The task is not wedged — a mechanical re-run clears
# it, and `fall_back` remains — but it can no longer be finished by assertion.
PHASE_EVIDENCE = {
    "brainstorm": {"kind": "file",
                   "required_dir": ("docs/superpowers/specs/",),
                   "verified_by": "python"},
    "plan": {"kind": "file",
             "required_dir": ("docs/plans/", "docs/superpowers/plans/"),
             "verified_by": "python"},
    "prd": {"kind": "file",
            "required_dir": ("docs/prds/",),
            "verified_by": "python"},
    "execution": {"kind": "event",
                  "required_dir": (),
                  "verified_by": "python"},
    "audit-bug": {"kind": "command",
                  "required_dir": (),
                  "verified_by": "python"},
    "audit-code": {"kind": "event",
                   "required_dir": (),
                   "verified_by": "agent"},
    "audit-system": {"kind": "command",
                     "required_dir": (),
                     "verified_by": "agent"},
    "pr-green": {"kind": "command",
                 "required_dir": (),
                 "verified_by": "python"},
}


def descriptor(phase):
    """*phase*'s evidence descriptor, or ``None`` when it is not a phase. Total
    by design — every caller uses it to decide whether to REFUSE something, and
    ``None`` is the fail-safe answer."""
    return PHASE_EVIDENCE.get(phase)


# --- evidence refs: resolve, never prefix --------------------------------------

def resolve_ref(phase, ref, root):
    """``(resolved_path, None)`` when *ref* is acceptable file evidence for
    *phase* under worktree root *root*, else ``(None, reason)``.

    CONTAINMENT, not prefix. ``str.startswith`` on the joined path accepts
    ``docs/prds/../../../etc/passwd`` — it starts with the required directory
    and lands nowhere near it. Resolving both sides first and asking
    `Path.is_relative_to` is the only test that survives ``..``. It is also what
    keeps a sibling out: ``docs/plans-archive/x.md`` shares a string prefix with
    ``docs/plans`` and is a different directory.

    A phase may list SEVERAL acceptable directories (see `plan`); the ref must
    be contained in at least one. Widening the list never weakens the check —
    each entry is still resolved and contained independently.

    *root* is the WORKTREE root (`paths.worktree_root`), passed in rather than
    looked up so this stays pure. ``None`` means "not inside a worktree", which
    is a refusal with a reason: resolving against the cwd instead would silently
    accept a file from some unrelated directory as this task's evidence."""
    desc = descriptor(phase)
    if desc is None:
        return None, f"'{phase}' is not a phase — one of: {', '.join(PHASES)}"
    if desc["kind"] != "file":
        return None, (f"phase '{phase}' takes {desc['kind']} evidence, not a "
                      f"file ref")
    text = str(ref or "").strip()
    if not text:
        return None, f"phase '{phase}' requires a file ref"
    if len(text) > MAX_REF_CHARS:
        return None, f"evidence ref exceeds {MAX_REF_CHARS} chars"
    if root is None:
        return None, ("no git worktree here — file evidence is resolved "
                      "relative to the worktree root, so there is nothing to "
                      "resolve against")
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        # An absolute ref is not merely wrong, it is unresolvable LATER: the
        # re-stat happens from whatever checkout is current, and a ref that
        # names one machine's drive cannot be re-stat'd from another.
        return None, (f"evidence ref must be worktree-root-relative, not "
                      f"absolute: '{text}'")
    required = desc["required_dir"]
    try:
        target = Path(root, candidate).resolve()
        bases = [Path(root, d).resolve() for d in required]
    except (OSError, ValueError) as e:
        # A gate answers, it does not raise: an unresolvable path (a bad
        # surrogate, a too-long name) is a refusal like any other.
        return None, f"unresolvable evidence ref '{text}': {type(e).__name__}"
    # `target == base` is refused with the escapes: the directory itself is not
    # a file, and `is_relative_to` says True for it.
    if any(target != base and target.is_relative_to(base) for base in bases):
        return target, None
    return None, (f"'{text}' resolves outside "
                  + " and ".join(f"'{d}'" for d in required)
                  + f" — {phase} evidence must be a file inside "
                  + ("one of those directories" if len(required) > 1
                     else "that directory"))


# --- payload contract ----------------------------------------------------------

def _check_epoch(epoch) -> None:
    """``bool`` is an ``int`` in Python, so ``True`` sails through a bare
    ``isinstance(x, int)`` and folds as epoch 1 — binding a satisfaction to a
    fall-back generation that never happened. tasks.py and roadmap.py both guard
    this; this is the same guard, spelled the same way on purpose."""
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("epoch must be a non-negative integer — the fold's "
                         "fall_back count for this task at the moment the "
                         "evidence was produced")


def _check_evidence(ev) -> None:
    if not isinstance(ev, dict):
        raise ValueError(
            "evidence must be an object {kind, ref, result, detail}")
    kind = ev.get("kind")
    if kind not in EVIDENCE_KINDS:
        raise ValueError("evidence kind must be one of: "
                         + ", ".join(EVIDENCE_KINDS))
    result = ev.get("result")
    if result is not None and result not in RESULTS:
        raise ValueError("evidence result must be one of: " + ", ".join(RESULTS))
    ref = ev.get("ref")
    if ref is not None and len(str(ref)) > MAX_REF_CHARS:
        raise ValueError(f"evidence ref exceeds {MAX_REF_CHARS} chars")
    if kind == "file" and not str(ref or "").strip():
        # Only file evidence is re-stat'able, and only if it says WHAT to
        # re-stat. Command evidence's ref is a runlog artifact path and event
        # evidence joins by task_id, so neither is required here.
        raise ValueError("file evidence requires a 'ref' — the "
                         "worktree-root-relative path to the document")
    detail = ev.get("detail")
    if detail is not None and len(str(detail)) > MAX_DETAIL_CHARS:
        raise ValueError(f"evidence detail exceeds {MAX_DETAIL_CHARS} chars")


def _check_shape(payload) -> None:
    """The shape BOTH surfaces share. ``verified_by`` is deliberately not
    mentioned here — the two callers disagree about it, and that disagreement is
    the whole point of having two of them."""
    if not isinstance(payload, dict):
        raise ValueError("mindnet_phase payload must be an object")
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError("mindnet_phase requires an action — one of: "
                         + ", ".join(ACTIONS))
    if not ID_RE.match(str(payload.get("task_id") or "")):
        raise ValueError(
            "mindnet_phase requires a kebab-case task_id (lowercase letters, "
            "digits, hyphens; max 64 chars), e.g. 'mn1-phase-spine'")
    phase = payload.get("phase")
    if phase is not None and phase not in PHASES:
        raise ValueError(f"unknown phase '{phase}' — one of: "
                         + ", ".join(PHASES))
    epoch = payload.get("epoch")
    if epoch is not None:
        _check_epoch(epoch)
    detail = payload.get("detail")
    if detail is not None and len(str(detail)) > MAX_DETAIL_CHARS:
        raise ValueError(f"detail exceeds {MAX_DETAIL_CHARS} chars")
    evidence = payload.get("evidence")
    if evidence is not None:
        _check_evidence(evidence)

    # `declare` is the only action that names no phase: a task's ENTRY phase is
    # derived by tasks.fold (gate, or `execution` for a bypassed task) and a
    # caller-named entry phase is the teleport MN-2 just stopped accepting.
    if action != "declare" and phase is None:
        raise ValueError(f"{action} requires a phase — one of: "
                         + ", ".join(PHASES))
    if action == "satisfy":
        if epoch is None:
            raise ValueError(
                "satisfy requires an epoch — a satisfaction not bound to the "
                "task's current fall_back generation is re-usable forever")
        if evidence is None:
            raise ValueError(
                "satisfy requires an evidence object {kind, ref, result, "
                "detail} — a satisfaction with nothing behind it is a claim")
    if action == "fall_back" and not str(payload.get("detail") or "").strip():
        raise ValueError(
            "fall_back requires a non-empty detail naming what failed — it is "
            "the one transition that exists to record a failure, and it "
            "currently records nothing about it")


def validate_payload(payload) -> None:
    """Enforce the ``mindnet_phase`` contract at the arbitrary-payload append
    surfaces (DATA_MODEL.md §6.2). Registered in ``events._PAYLOAD_CONTRACTS``.

    This is the half of KTD2 that says NO: a payload carrying ``verified_by`` is
    refused outright, so ``hive append mindnet_phase`` cannot self-attest a
    python verification that nothing ran."""
    # Checked FIRST so the honesty violation is the reported reason even when
    # the payload is also malformed some other way.
    if isinstance(payload, dict) and "verified_by" in payload:
        raise ValueError(
            "verified_by is derived by the appending code path, never accepted "
            "from a payload — drop the field and use `hive mindnet satisfy` "
            "(with --agent-reported when no Python check actually ran)")
    _check_shape(payload)


def ledger_body(payload) -> str:
    """Self-describing markdown body for a ``mindnet_phase`` event's LEDGER
    entry, mirroring `roadmap.ledger_body` / `sop.ledger_body`.

    Two things break without it, because `projector.build_ledger_entries`
    defaults to ``payload.get("text", "")`` and these payloads carry no
    ``text``:

    1. Every phase event renders as an EMPTY body — invisible to keyword
       recall, which searches the projected markdown, not the raw ledger.
    2. Same-second events COLLIDE. The LEDGER filename is
       ``<ts>-<type>-<sha256(ts + type + body)[:8]>.md``; with an empty body two
       events sharing a second produce byte-identical names and the second
       silently overwrites the first. `now_iso` is second-resolution and a
       satisfy immediately followed by an enter is the normal case, so this is
       not a corner.

    Total by contract: the projector hands this whatever is on the ledger,
    including rows written by earlier versions of the code, so every field is
    read defensively and nothing here raises."""
    if not isinstance(payload, dict):
        return str(payload)
    action = payload.get("action", "")
    phase = payload.get("phase", "")
    head = f"[{action}] mindnet phase {payload.get('task_id', '')}"
    if phase:
        head += f" — {phase}"
    parts = [head]
    epoch = payload.get("epoch")
    verified_by = payload.get("verified_by")
    if epoch is not None or verified_by:
        # The two fields that decide how a satisfaction is WEIGHED (KTD2/KTD3)
        # must survive into the searchable projection — otherwise "was this
        # machine-checked?" is answerable only by reading the raw jsonl.
        marks = []
        if epoch is not None:
            marks.append(f"epoch {epoch}")
        if verified_by:
            marks.append(f"verified_by: {verified_by}")
        parts.append(" · ".join(marks))
    ev = payload.get("evidence")
    if isinstance(ev, dict):
        bits = [f"kind: {ev.get('kind', '')}"]
        for key in ("result", "ref"):
            if ev.get(key):
                bits.append(f"{key}: {ev[key]}")
        parts.append("evidence — " + ", ".join(bits))
        if ev.get("detail"):
            parts.append(str(ev["detail"]))
    detail = str(payload.get("detail") or "").strip()
    if detail:
        parts.append(detail)
    return "\n\n".join(parts)


def validate_stored(payload) -> None:
    """Enforce the WRITER's shape: everything `validate_payload` checks, plus a
    ``verified_by`` the appending code path derived. The CLI calls this
    immediately before ``append_event``, mirroring roadmap/sop.

    An event on the ledger without this field cannot be weighed against a
    contradicting one, so the fold has no honest choice but to treat it as a
    recorded violation — which is a strictly worse outcome than refusing the
    write here, where the caller can still fix it."""
    _check_shape(payload)
    verified_by = payload.get("verified_by")
    if verified_by not in VERIFIERS:
        raise ValueError(
            "a stored mindnet_phase event must record who verified it — "
            "verified_by is one of: " + ", ".join(VERIFIERS)
            + " (derived by the writer: a Python check yields 'python', "
              "--agent-reported yields 'agent', an interactive confirmation "
              "yields 'human')")
