"""Adaptive work injection: replenish a surface's plan from what the record proves.

Work in this system ends quietly. An interactive session stops when the human
stops typing; a `hive loop` run stops when its checklist empties. Neither moment
asks whether the ledger still holds proof of unfinished work, so debt accumulates
while every individual ending looks correct.

This module is the part that asks — the DECISION layer. It scores and gates the
candidates the **lenses** produce (`replenish_lenses.py`, imported on this
module's last line so `LENSES` is populated for every caller), ranks the
survivors, bounds the round, and hands what is left to a **sink**
(`replenish_sinks.py`) that lands them in whatever shape forces continuation on
that surface.

The split is by rate of change, not by size: this module is bounded by its
contract, while lenses accumulate one per thing the record can prove.

Four properties are load-bearing.

**A lens reports observations, not a verdict.** It returns what it saw —
recurrence, severity, whether a prevention rule already covers the condition —
and `gate` computes the score. A lens that could set its own number could
promote its finds past every other lens by asserting one.

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
     "fingerprint": "…", "score": 35, "plan_id": "…", "step_index": 3,
     "run_id": "…", "depth": 1}
    {"outcome": "refused",   "round": 1, "surface": "session",
     "lens": "evidence-gap", "title": "…", "fingerprint": "…",
     "reason": "no evidence", "run_id": "…"}
    {"outcome": "truncated", "round": 1, "surface": "session", "dropped": 4}
    {"outcome": "dry",       "round": 2, "surface": "session"}

`run_id` and `depth` are optional and absent on every row written before the
run-scoped bounds existed, so the fold defaults them exactly as it reads a
legacy row: no `run_id` is unattributed, no `depth` is 0.

`dry` exists as its own outcome because "this round found nothing" has to be a
recorded fact, not the absence of one: the consecutive-empty-rounds exit cannot
be derived from silence.
"""

import hashlib
from dataclasses import dataclass, field, replace

from . import events as ev_mod

# Outcome vocabulary. `landed` and `refused` are per-candidate; `truncated` and
# `dry` are per-round facts about the round itself.
OUTCOMES = ("landed", "refused", "truncated", "dry")

MAX_TITLE_CHARS = 300          # matches roadmap.MAX_STEP_CHARS — a landed title
                               # becomes a step, and a title the sink would have
                               # to truncate is one the audit row misreports.
MAX_REASON_CHARS = 500
MAX_EVIDENCE_ITEMS = 12
MAX_EVIDENCE_CHARS = 300

# The one refusal that carries NO new information: the fingerprint is spent, so
# the ledger already holds the row that spent it. Recording it again would add a
# byte-identical row per round forever — and `chain.compute_chain` hashes every
# event with no type filter, so the cost lands on `hive verify` for the life of
# the store.
DUPLICATE_REASON = "already proposed — this fingerprint is spent"

# The other refusal that is not about this candidate's merit: the project already
# holds a prevention rule for the condition, so the fix is recorded and proposing
# it again is noise. It is a REFUSAL rather than a silent skip because the
# dry-round exit reads the difference — "declined" and "found nothing" mean
# opposite things and look identical from the outside.
RULE_COVERED_REASON = ("a prevention rule already covers this signature — the "
                       "fix is on the record, so re-proposing it is noise")


# --- fingerprints ------------------------------------------------------------
# One way to identify a CONDITION INSTANCE, shared by every lens. Hand-built
# strings worked while one lens existed; six lenses each inventing a convention
# is six chances for two of them to disagree about what "the same condition"
# means, and the dedup set is the injector's only guard against re-proposing
# spent work.
#
# The lens still chooses WHAT to hash, because only the lens knows what an
# instance is: a failure signature for `repair`, a path plus a size bucket for
# `debt`, the newest covered timestamp for `evidence-gap`. Hashing the whole
# evidence list would be wrong — evidence carries incidental detail that varies
# while the condition holds, so the fingerprint would never dedup.

def fingerprint(lens, key) -> str:
    """Stable hex SHA-256 over ``(lens, key)``.

    The lens name is length-prefixed rather than merely joined, so the encoding
    is injective: without it, ``("a", "b\\0c")`` and ``("a\\0b", "c")`` would
    hash the same bytes and two unrelated conditions would dedup each other.

    An empty lens or key raises. A lens that cannot name its condition instance
    is a defect, and a digest over "" would quietly collapse every such
    candidate onto one fingerprint — which reads as "already proposed" forever."""
    lens = str(lens or "").strip()
    key = str(key or "").strip()
    if not lens:
        raise ValueError("fingerprint requires a lens name")
    if not key:
        raise ValueError(
            "fingerprint requires a non-empty key — the lens must name the "
            "condition instance it found, not just the condition")
    raw = f"{len(lens)}:{lens}{key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# --- scoring -----------------------------------------------------------------
# The value gate's numbers. They live in the core, keyed off what each lens
# OBSERVED (how often, and whether the project already treats it as blocking),
# not off a number each lens computed for itself — six implementations of
# "base plus five per repeat" is six places for the arithmetic to drift, and the
# auto-tuner has to reason about one formula.
BASE_SCORE = 10
RECURRENCE_STEP = 5
SEVERITY_BONUS = 20
# A malformed or adversarial recurrence count must not produce an unbounded
# score that outranks everything else in the round forever.
MAX_RECURRENCE = 20


def _count(value) -> int:
    """A non-negative int from a folded field, 0 for anything else. `bool` is an
    int subclass, so `True` would otherwise fold in as a count of 1."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def score_for(candidate) -> int:
    """This candidate's score, computed from what its lens observed.

    Zero when a prevention rule already covers the signature — the gate then
    refuses it with :data:`RULE_COVERED_REASON`, so `hive replenish status` can
    show a lens working and being correctly declined rather than showing
    nothing."""
    if getattr(candidate, "rule_covered", False):
        return 0
    rec = min(_count(getattr(candidate, "recurrence", 0)), MAX_RECURRENCE)
    severity = SEVERITY_BONUS if getattr(candidate, "severity", False) else 0
    return BASE_SCORE + RECURRENCE_STEP * rec + severity


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

    # Optional on every outcome, and absent on every row written before the
    # run-scoped bounds existed — so both are checked only when present, and the
    # fold defaults them the same way (no run_id = unattributed, no depth = 0).
    run_id = payload.get("run_id")
    if run_id is not None and (not isinstance(run_id, str)
                               or not run_id.strip() or len(run_id) > 128):
        raise ValueError(
            "work_injected run_id must be non-empty text under 128 chars — it "
            "scopes the per-run ceilings, and a blank one would pool every "
            "run's injections into a single budget")
    depth = payload.get("depth")
    if depth is not None and (isinstance(depth, bool)
                              or not isinstance(depth, int) or depth < 0):
        raise ValueError(
            "work_injected depth must be a non-negative integer — how far this "
            "work is from the plan a human wrote")

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


# --- the core ----------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One proposal from one lens.

    `evidence` is what makes it admissible: a pointer to a real ledger row, file,
    or signature that a reader can go and check. `fingerprint` identifies the
    CONDITION INSTANCE, not the condition — keyed on the bare lens name a
    candidate would fire once ever, and keyed on something that changes every
    round it would never dedup.

    `recurrence`, `severity` and `rule_covered` are OBSERVATIONS, not verdicts:
    how many times the lens saw this condition, whether the project already
    treats it as blocking, and whether a prevention rule already covers it.
    :func:`score_for` turns them into `score`, and :func:`gate` overwrites
    whatever a lens put there — a lens that could set its own score could
    outrank every other lens by asserting a number."""

    lens: str
    title: str
    fingerprint: str
    evidence: tuple = ()
    score: float = 1.0
    recurrence: int = 0
    severity: bool = False
    rule_covered: bool = False


@dataclass(frozen=True)
class Placement:
    """Where a landed candidate actually went. Returned by the sink, recorded on
    the `landed` row, so an audit row points at the work it produced."""

    fingerprint: str
    plan_id: str = ""
    step_index: int = -1


@dataclass
class ReplenishConfig:
    enabled: bool = True
    # The value gate's threshold, on :func:`score_for`'s scale. The default is
    # the balanced ambition level; `from_ambition` sets the other two. It shipped
    # as 0.5 against an implicit 1.0 score, which meant the score branch had
    # never rejected anything — a candidate either had evidence or it did not.
    min_score: float = 25
    # Also the balanced level. The defaults ARE an ambition level rather than a
    # fourth setting nobody chose: "no dial" and "the dial at its middle" must
    # behave identically, or an operator who never opens `loop.toml` is running
    # a configuration that appears nowhere in the documentation.
    max_per_round: int = 10
    # Which registered lenses may run. Empty means all of them — a caller that
    # never touches the dial keeps the whole registry, and a name here with no
    # registered lens is simply absent rather than an error, so `ambitious` can
    # admit the model lens before phase 3 registers it.
    lenses: tuple = ()
    # 0 DISABLES the dry-round exit, and that is the right setting for any
    # surface whose own lifecycle already bounds the rounds. The exit exists for
    # a loop, which runs many rounds back to back and must stop spinning. On a
    # surface that runs one round per session it is actively wrong: nothing can
    # reset the counter except work landing, and work can only land if a round
    # runs, so two quiet sessions would switch the injector off permanently.
    dry_rounds_to_exit: int = 0
    # Run-scoped bounds (R14). 0 disables each one independently, which is the
    # right default for the session surface: a session runs one round, so a
    # per-run ceiling there is a bound on nothing. The loop surface sets them.
    max_per_run: int = 0
    max_rounds: int = 0
    # Injected work may inject; work injected BY injected work may not go
    # further. Depth 0 is the plan the operator wrote, so the ceiling of 2
    # allows two generations of machine-proposed work and stops the third.
    max_depth: int = 2

    @classmethod
    def from_ambition(cls, level, **over):
        """The config for an ambition level. An unknown level falls back to
        balanced — see :func:`resolve_ambition` for why that is not silent."""
        name, _ = resolve_ambition(level)
        spec = AMBITION[name]
        kw = {"min_score": spec["min_score"],
              "max_per_round": spec["max_per_round"],
              "lenses": spec["lenses"]}
        kw.update(over)
        return cls(**kw)


# --- the ambition dial --------------------------------------------------------
# One setting an operator turns. It drives three numbers together because they
# are not independent: a low threshold with a small cap just means the cap picks
# the winners, and a high threshold with a large cap never fills it.

DEFAULT_AMBITION = "balanced"

# Registered by phase 3's model lens. Named here so the ambitious level admits
# it the moment it lands, rather than needing this table edited again — and the
# lens filter tolerates a name with no registered lens for exactly that reason.
INNOVATION_LENS = "innovation"

_DETERMINISTIC_LENSES = ("repair", "evidence-gap", "debt", "audit", "caveat",
                         "coverage")

AMBITION = {
    "conservative": {"min_score": 40, "max_per_round": 3,
                     "lenses": _DETERMINISTIC_LENSES[:2]},
    "balanced": {"min_score": 25, "max_per_round": 10,
                 "lenses": _DETERMINISTIC_LENSES},
    "ambitious": {"min_score": 15, "max_per_round": 25,
                  "lenses": _DETERMINISTIC_LENSES + (INNOVATION_LENS,)},
}
AMBITION_LEVELS = tuple(AMBITION)


def resolve_ambition(level):
    """``(level, warning)`` for a configured ambition string.

    An unknown value falls back to balanced AND returns a warning, rather than
    raising or silently picking a level. Both alternatives are worse in a
    specific way: refusing to run takes the injector out entirely over a typo,
    and a silent fallback would spend the operator's tokens on a setting they
    never chose. Blank is not a typo — it means "unset"."""
    name = str(level or "").strip().lower()
    if not name:
        return DEFAULT_AMBITION, ""
    if name in AMBITION:
        return name, ""
    return DEFAULT_AMBITION, (
        f"unknown replenish ambition {level!r} — falling back to "
        f"{DEFAULT_AMBITION}; valid levels: " + ", ".join(AMBITION_LEVELS))


@dataclass(frozen=True)
class History:
    """The injector's state, recovered from the ledger alone."""

    fingerprints: frozenset = frozenset()
    last_round: int = 0
    dry_rounds: int = 0
    # Scoped to one `run_id` when `history` was given one, else 0. Separate from
    # `last_round` because that counts the SURFACE's rounds: a loop that has run
    # forty rounds this month is not a loop that has run forty rounds this run,
    # and a per-run ceiling read off the surface counter would refuse the first
    # round of every new run.
    run_landed: int = 0
    run_rounds: int = 0


@dataclass
class RoundResult:
    round_no: int = 0
    landed: list = field(default_factory=list)      # [(Candidate, Placement)]
    refused: list = field(default_factory=list)     # [(Candidate, reason)]
    dropped: int = 0
    dry: bool = False
    skipped: str = ""
    lens_errors: list = field(default_factory=list)   # [(lens_name, message)]

    @property
    def ran(self) -> bool:
        return not self.skipped


# Lens registry: name -> callable(evs) -> [Candidate]. Populated at module level
# by each lens below, so a caller can run the standard set without naming them
# and a test can pass its own.
LENSES: dict = {}


def history(evs, surface=None, run_id=None) -> History:
    """Fold `work_injected` rows into the state a round needs.

    Total, per the ledger's read contract: a row whose payload is not a dict, or
    whose `outcome` is not one this version knows, is skipped rather than
    raising. The ledger holds history written by every past version of this code
    and will hold rows written by future ones.

    A REFUSED candidate's fingerprint is spent too. That is deliberate: re-
    proposing something already judged not worth doing is exactly the loop the
    dedup exists to prevent, and without it a refused candidate would return
    every single round forever.

    *surface* scopes the ROUND COUNTER and the DRY STREAK but never the spent
    fingerprints. The two answer different questions. "Has this condition
    already been proposed?" is about the work and holds no matter which surface
    proposed it — a loop and a session must not each mint a step for the same
    outstanding fact. "How many rounds has this surface run without finding
    anything?" is about one surface's own pacing, and mixing surfaces there
    would let a quiet loop switch off a session's injector.

    *run_id* scopes the per-run counters and nothing else, for the same reason:
    "how much has THIS run injected" is the question the per-run ceiling asks,
    and answering it from the surface's whole history would refuse the first
    round of every run after the first."""
    spent, last, dry = set(), 0, 0
    run_landed, run_rounds_seen = 0, set()
    for ev in evs or []:
        if getattr(ev, "type", None) != "work_injected":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        outcome = p.get("outcome")
        if outcome not in OUTCOMES:
            continue
        fp = p.get("fingerprint")
        if isinstance(fp, str) and fp.strip():
            spent.add(fp)                       # global, always
        if surface is not None and p.get("surface") != surface:
            continue
        r = p.get("round")
        if isinstance(r, int) and not isinstance(r, bool) and r > last:
            last = r
        # The dry STREAK is run-scoped whenever a run is named, and that is a
        # prerequisite of enabling the dry exit at all — not a refinement. Folded
        # over the surface's whole history, the first run that went quiet twice
        # would switch `surface="loop"` off project-wide, forever, and the
        # symptom would be an injector that silently stopped proposing.
        if run_id is None or p.get("run_id") == run_id:
            if outcome == "dry":
                dry += 1
            elif outcome == "landed":
                dry = 0        # real work landed: the streak is broken
        if run_id is not None and p.get("run_id") == run_id:
            if isinstance(r, int) and not isinstance(r, bool):
                run_rounds_seen.add(r)
            if outcome == "landed":
                run_landed += 1
    return History(frozenset(spent), last, dry, run_landed,
                   len(run_rounds_seen))


def refusal_reason(candidate, spent, cfg) -> str:
    """Why this candidate may not become work, or "" when it may.

    The spent check comes FIRST. It used to sit third, behind evidence and
    fingerprint presence, which meant an evidence-less candidate never reached
    the duplicate branch — so its identical refusal row was appended every
    round forever, the exact unbounded cost `DUPLICATE_REASON` exists to
    prevent. The reasons are not peers: "we already decided this" outranks
    every other reason not to act. Evidence remains the rule that separates
    replenishment from inventing work to fill a quota; it is simply not the
    first question."""
    if not (candidate.fingerprint or "").strip():
        return "no fingerprint — this candidate could never be deduplicated"
    if candidate.fingerprint in spent:
        return DUPLICATE_REASON
    evidence = [e for e in (candidate.evidence or [])
                if isinstance(e, str) and e.strip()]
    if not evidence:
        return ("no evidence — a candidate must point at a real ledger row, "
                "file, or signature")
    # Ahead of the score check on purpose. A rule-covered candidate scores zero,
    # so it would ALSO fail the threshold — and the generic "below the minimum"
    # text would misreport why: the condition is real and the lens is right, the
    # project simply already wrote the fix down.
    if getattr(candidate, "rule_covered", False):
        return RULE_COVERED_REASON
    if candidate.score < cfg.min_score:
        return (f"score {candidate.score} below the round's minimum "
                f"{cfg.min_score}")
    return ""


def normalise(candidate) -> Candidate:
    """Clamp a candidate to the shapes the audit contract accepts.

    This is what closes the two-phase write window all three reviewers flagged.
    The sink appends the WORK before `_record` validates the audit rows, so a
    candidate that survives the gate but yields an illegal payload leaves the
    step written, no `landed` row, an unspent fingerprint, and a duplicate step
    next round. Guaranteeing at the gate that every survivor produces a legal
    row removes the failure rather than compensating for it. The demonstrated
    case was evidence like ``('   ', 'a real pointer')`` — the gate filters
    blanks only to DECIDE, and the contract then rejected the blank item."""
    evidence = tuple(e[:MAX_EVIDENCE_CHARS]
                     for e in (candidate.evidence or [])
                     if isinstance(e, str) and e.strip())[:MAX_EVIDENCE_ITEMS]
    return Candidate(lens=str(candidate.lens)[:64],
                     title=str(candidate.title)[:MAX_TITLE_CHARS],
                     fingerprint=str(candidate.fingerprint)[:128],
                     evidence=evidence, score=candidate.score,
                     recurrence=getattr(candidate, "recurrence", 0),
                     severity=bool(getattr(candidate, "severity", False)),
                     rule_covered=bool(getattr(candidate, "rule_covered",
                                               False)))


def gate(candidates, spent, cfg):
    """``(kept, refused)`` — refused entries carry their reason.

    Fingerprints of kept candidates join the spent set as we go, so two lenses
    proposing the same condition in one round produce one piece of work and one
    recorded duplicate refusal, not two pieces of work. Every entry returned —
    kept or refused — has been normalised, so nothing downstream has to cope
    with a shape the audit contract would reject."""
    kept, refused = [], []
    seen = set(spent)
    for c in candidates or []:
        if not isinstance(c, Candidate):
            continue           # a lens returning junk is a bug, not a decision
        # Normalise clamps the shapes the audit contract accepts; the score is
        # then computed HERE rather than trusted from the lens, so no lens can
        # promote its own candidates past the gate by asserting a number.
        c = replace(normalise(c), score=score_for(c))
        reason = refusal_reason(c, seen, cfg)
        if reason:
            refused.append((c, reason))
            continue
        seen.add(c.fingerprint)
        kept.append(c)
    return kept, refused


def lens_registry(root=None):
    """The standard lenses, with the filesystem ones bound to *root*.

    A lens is called with the events alone, so the tree it reads cannot be a
    round-level parameter — it has to be bound here. Callers that know which
    tree the surface is working in (the loop engine knows: it is the parent of
    `.hive-loop`) pass it; everyone else gets the lens module's default.

    Lives beside the registry it reads rather than with the lenses: a
    `from .replenish import LENSES` binding in the lens module would be a second
    name for the same dict, and a caller that swapped the registry by
    reassigning `replenish.LENSES` would leave that binding pointing at the old
    one — two views, silently disagreeing about which lenses exist."""
    from functools import partial
    if root is None:
        return dict(LENSES)
    bound = dict(LENSES)
    # Only the lenses that read a tree take a root; the ledger lenses would
    # reject the keyword.
    for name in ("debt", "coverage", "audit"):
        if name in bound:
            bound[name] = partial(bound[name], root=root)
    return bound


def _rank(kept) -> list:
    """Order survivors so the round's cap keeps the best work from EVERY lens.

    Two rules, and the second exists because the first alone failed on the real
    store. Sorting by score is necessary — without it the cap took whichever
    candidates the registry happened to yield first, which made the scoring
    decorative: a blocking, thrice-recurring failure could be cut for a
    first-sighting from a lens that ran earlier.

    But score alone lets one lens take the whole round. Measured against this
    project's own ledger, `repair` reads occurrence counts in the hundreds, so
    even clamped its recurrence term dwarfs every other lens's and seven of ten
    injected items came from it. A round that is one lens is not a round; the
    other five answered their questions and were cut before anyone read them.

    So: rank within each lens by score, then take round-robin across lenses. The
    highest-scoring candidate overall still goes first, every lens gets its best
    find in before any lens gets its second, and the ordering is deterministic —
    ties break on the lens name, so the same ledger produces the same round."""
    by_lens: dict = {}
    for c in kept:
        by_lens.setdefault(c.lens, []).append(c)
    for group in by_lens.values():
        group.sort(key=lambda c: -c.score)
    # Lenses whose best find scores highest go first in each pass.
    order = sorted(by_lens, key=lambda name: (-by_lens[name][0].score, name))
    out = []
    for i in range(max((len(g) for g in by_lens.values()), default=0)):
        for name in order:
            if i < len(by_lens[name]):
                out.append(by_lens[name][i])
    return out


def _row(outcome, round_no, surface, **extra) -> dict:
    payload = {"outcome": outcome, "round": round_no, "surface": surface}
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
    return payload


def _record(rows, project=None, session_id=None) -> list:
    """Append each row after validating it. A row that fails its own contract is
    a bug here, not in the ledger — surface it rather than writing a payload the
    fold would later have to skip."""
    out = []
    for payload in rows:
        validate_payload(payload)
        out.append(ev_mod.append_event(
            "work_injected", payload, session_id=session_id,
            tags=["replenish", payload["outcome"]], project=project))
    return out


def _record_lens_errors(errors, surface, project=None, session_id=None) -> None:
    """Put a broken lens on the record, under the signature the failure loop
    already audits. Best-effort: a round must not fail because it could not
    report that something else failed."""
    for name, message in errors or []:
        try:
            ev_mod.append_event(
                "tool_failed",
                {"text": f"replenish lens {name} raised: {message}"[:500],
                 "signature": f"lens:{name}",
                 "action": f"run the {name} lens on the {surface} surface"},
                session_id=session_id, tags=["replenish", "lens", "failure"],
                project=project)
        except Exception:                                       # noqa: BLE001
            continue


def run_round(evs, *, surface, sink, cfg=None, lenses=None, project=None,
              session_id=None, append=True, run_id=None,
              depth=0) -> RoundResult:
    """Run one replenishment round and record what it decided.

    *sink* is a parameter, not an import: deciding what is outstanding is
    surface-independent, forcing it is not. It is called with the kept
    candidates and returns ``(placements, dropped)``.

    A candidate the sink could not place gets NO row, so its fingerprint stays
    unspent and it can be proposed again next round. Same for candidates the cap
    cut — a truncation is "not now", not "never".

    *run_id* scopes the per-run bounds, and *depth* is how far from the operator's
    own plan this round already is: 0 while replenishing the work a human asked
    for, 1 while replenishing work the injector added, and so on. The caller owns
    that number because only the caller knows which step it is executing."""
    cfg = cfg or ReplenishConfig()
    if not cfg.enabled:
        return RoundResult(skipped="disabled")

    depth = _count(depth)
    if depth > cfg.max_depth:
        # Recursion bound (R14). Checked before any lens runs: at this depth
        # nothing may land, so mining would spend a full ledger pass to produce
        # candidates that are all refused.
        return RoundResult(skipped=f"max injection depth {cfg.max_depth} "
                                   f"reached (this round is at depth {depth})")

    hist = history(evs, surface=surface, run_id=run_id)
    if cfg.dry_rounds_to_exit > 0 and hist.dry_rounds >= cfg.dry_rounds_to_exit:
        return RoundResult(round_no=hist.last_round,
                           skipped="dry rounds exhausted")
    if cfg.max_rounds > 0 and hist.run_rounds >= cfg.max_rounds:
        return RoundResult(round_no=hist.last_round,
                           skipped=f"max injection rounds ({cfg.max_rounds}) "
                                   "reached for this run")
    room = None
    if cfg.max_per_run > 0:
        room = cfg.max_per_run - hist.run_landed
        if room <= 0:
            return RoundResult(round_no=hist.last_round,
                               skipped=f"max injections per run "
                                       f"({cfg.max_per_run}) reached")
    round_no = hist.last_round + 1

    registry = LENSES if lenses is None else lenses
    if cfg.lenses:
        # An ambition level selects lenses by NAME, and a name with no
        # registered lens is simply absent. That is what lets `ambitious` list
        # the model lens before phase 3 registers it — raising here would make
        # the boldest level the only one that cannot run.
        registry = {k: v for k, v in registry.items() if k in cfg.lenses}
    candidates, lens_errors = [], []
    for name, lens in registry.items():
        try:
            candidates.extend(lens(evs) or [])
        except Exception as exc:                                # noqa: BLE001
            # One broken lens must not cost the round every other lens found —
            # but silence here is worse than the crash. A round where every lens
            # threw wrote nothing and reported "nothing outstanding that the
            # record proves", which is a false statement about the ledger. The
            # failure goes to `tool_failed` under a `lens:<name>` signature,
            # which is the signature `incidents`/`reflect` already audit, so a
            # lens that keeps breaking becomes a prevention rule on its own.
            lens_errors.append((name, f"{type(exc).__name__}: {exc}"))

    kept, refused = gate(candidates, hist.fingerprints, cfg)
    kept = _rank(kept)
    # R16 asks whether anything QUALIFIED, which is decided here. A round the cap
    # trimmed or the sink could not place found real work and must not report
    # itself dry — the CLI was printing "N candidate(s) did not fit" and
    # "nothing outstanding" in the same breath.
    dry = not kept
    dropped = 0
    # The per-round cap and the per-run remainder are the same kind of bound, so
    # they are applied as one: whichever leaves less room wins, and the cut is
    # recorded once rather than twice.
    ceiling = cfg.max_per_round if room is None else min(cfg.max_per_round, room)
    if len(kept) > ceiling:
        dropped = len(kept) - ceiling
        kept = kept[:ceiling]

    placements, sink_dropped = ([], 0)
    if kept:
        placements, sink_dropped = sink(kept)
    dropped += max(0, int(sink_dropped or 0))

    placed = {p.fingerprint: p for p in (placements or [])}
    landed = [(c, placed[c.fingerprint]) for c in kept
              if c.fingerprint in placed]

    rows = []
    for c, pl in landed:
        rows.append(_row("landed", round_no, surface, lens=c.lens,
                         title=c.title[:MAX_TITLE_CHARS],
                         fingerprint=c.fingerprint,
                         evidence=[str(e)[:MAX_EVIDENCE_CHARS]
                                   for e in c.evidence][:MAX_EVIDENCE_ITEMS],
                         score=c.score, plan_id=pl.plan_id,
                         run_id=run_id,
                         # Recorded on the row rather than recomputed later: the
                         # next round's depth is this row's depth plus one, and
                         # a derivation that had to re-walk the plan tree would
                         # disagree with it the moment a step was edited.
                         depth=depth or None,
                         step_index=pl.step_index if pl.step_index >= 0 else None))
    for c, reason in refused:
        if reason == DUPLICATE_REASON:
            continue          # the row that spent this fingerprint IS the record
        if not c.fingerprint:
            # Nothing to dedup on, so this row would repeat every round for as
            # long as the lens keeps emitting it. A fingerprint-less candidate
            # is a lens defect; the lens-error channel above is where that
            # belongs, not an unbounded run of identical refusals.
            continue
        rows.append(_row("refused", round_no, surface, lens=c.lens,
                         title=c.title or "(untitled)",
                         fingerprint=c.fingerprint, run_id=run_id,
                         reason=reason[:MAX_REASON_CHARS]))
    if dropped:
        # R15: a cap that cut silently would read as "nothing more was found".
        rows.append(_row("truncated", round_no, surface, dropped=dropped,
                         run_id=run_id))
    # The dry row exists to feed the dry-round exit. Where that exit is off, it
    # is a row nobody reads, appended once per session forever — so it is only
    # written when something actually consumes it.
    if dry and cfg.dry_rounds_to_exit > 0:
        rows.append(_row("dry", round_no, surface, run_id=run_id))

    if append:
        _record(rows, project=project, session_id=session_id)
        _record_lens_errors(lens_errors, surface, project=project,
                            session_id=session_id)

    return RoundResult(round_no=round_no, landed=landed, refused=refused,
                       dropped=dropped, dry=dry, lens_errors=lens_errors)


# --- auto-tuning the threshold ------------------------------------------------
# R17: the threshold moves with the measured outcome of previously injected
# work, in bounded steps, and every move is recorded. Measured, not configured —
# step completion is already on the record, so this asks the ledger rather than
# asking the operator.

TUNE_UP_AFTER = 3          # consecutive bad outcomes before getting quieter
TUNE_UP_STEP = 10
TUNE_DOWN_AFTER = 5        # consecutive green outcomes before getting bolder
TUNE_DOWN_STEP = 5
# The clamp is the guard that keeps a slow ratchet from switching the injector
# off in silence. The floor is the base score, below which the threshold rejects
# nothing; the ceiling is a score a severe, six-times-recurring candidate can
# still reach, so even a fully-ratcheted threshold leaves the strongest evidence
# a way through.
TUNE_MIN_THRESHOLD = BASE_SCORE
TUNE_MAX_THRESHOLD = 60

OUTCOME_KINDS = ("green", "bad", "open")


def _passing_verification_after(evs, ts) -> bool:
    for ev in evs or []:
        if getattr(ev, "type", None) != "verification":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        code = p.get("exit_code")
        if isinstance(code, bool) or not isinstance(code, int) or code != 0:
            continue
        if (getattr(ev, "ts", "") or "") >= ts:
            return True
    return False


def outcomes(evs, *, step_status, surface=None, run_id=None) -> list:
    """How previously-injected work actually turned out, oldest first.

    ``green`` — the step reached ``done`` and a passing verification followed.
    ``bad``   — the step did not finish AND the record shows why: the run that
                owned it tripped its breaker, or the session it landed in
                recorded a tool failure after it landed.
    ``open``  — anything else, including work still in progress.

    `open` is a third value rather than being folded into either side, and that
    is the load-bearing choice. Counting unfinished work as failure would ratchet
    the threshold up through any long-running plan; counting it as success would
    ratchet it down. Neither is a measurement.

    *step_status* is a parameter for the same reason the sink is: knowing
    whether a placement finished is surface-specific, and importing the roadmap
    here would tie the decision layer to one surface — which the module's own
    architecture test forbids. It is called with ``(plan_id, step_index)`` and
    returns the placement's status, or None when it cannot be resolved."""
    breaker_runs = set()
    for ev in evs or []:
        p = getattr(ev, "payload", None)
        if getattr(ev, "type", None) == "loop_run_end" and isinstance(p, dict) \
                and p.get("reason") == "breaker_open":
            rid = p.get("run_id")
            if isinstance(rid, str):
                breaker_runs.add(rid)

    out = []
    for ev in evs or []:
        if getattr(ev, "type", None) != "work_injected":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict) or p.get("outcome") != "landed":
            continue
        if surface is not None and p.get("surface") != surface:
            continue
        if run_id is not None and p.get("run_id") != run_id:
            continue
        idx = p.get("step_index")
        status = None
        if not isinstance(idx, bool) and isinstance(idx, int):
            status = step_status(p.get("plan_id"), idx)
        if status is None:
            # A placement that no longer resolves is UNKNOWN, not success.
            # Reading a missing plan as green is how a tuner talks itself into
            # being bold about work nobody can find.
            out.append("open")
            continue
        ts = getattr(ev, "ts", "") or ""
        if status == "done":
            out.append("green" if _passing_verification_after(evs, ts)
                       else "open")
            continue
        if p.get("run_id") in breaker_runs \
                or _failure_after(evs, getattr(ev, "session_id", None), ts):
            out.append("bad")
        else:
            out.append("open")
    return out


def _failure_after(evs, session_id, ts) -> bool:
    """A `tool_failed` in the SAME session, at or after *ts*.

    Scoped to the session on purpose: "any failure anywhere in the project after
    this landed" would mark every injection bad on a busy day, which is a
    measurement of the day rather than of the work."""
    if not session_id:
        return False
    for ev in evs or []:
        if getattr(ev, "type", None) != "tool_failed":
            continue
        if getattr(ev, "session_id", None) != session_id:
            continue
        if (getattr(ev, "ts", "") or "") >= ts:
            return True
    return False


def tune(threshold, outs):
    """``(threshold, reason)`` — the threshold after applying R17's two rules.

    `open` outcomes are FILTERED, not counted against either rule, so a plan
    that is simply still in progress moves nothing."""
    decided = [o for o in (outs or []) if o in ("green", "bad")]
    if len(decided) >= TUNE_UP_AFTER and all(
            o == "bad" for o in decided[-TUNE_UP_AFTER:]):
        new = min(TUNE_MAX_THRESHOLD, threshold + TUNE_UP_STEP)
        if new != threshold:
            return new, (f"{TUNE_UP_AFTER} injected step(s) in a row ended "
                         f"badly — raising the replenish threshold from "
                         f"{threshold} to {new}")
        return threshold, ""
    if len(decided) >= TUNE_DOWN_AFTER and all(
            o == "green" for o in decided[-TUNE_DOWN_AFTER:]):
        new = max(TUNE_MIN_THRESHOLD, threshold - TUNE_DOWN_STEP)
        if new != threshold:
            return new, (f"{TUNE_DOWN_AFTER} injected step(s) in a row "
                         f"completed and verified — lowering the replenish "
                         f"threshold from {threshold} to {new}")
        return threshold, ""
    return threshold, ""


def autotune(cfg, evs, *, step_status, surface=None, run_id=None, project=None,
             session_id=None, append=True):
    """Apply :func:`tune` to *cfg* and record any MOVE as a `decision` event.

    Only a move is recorded. A tuner that appended a row every round would put
    one event per session into the briefing forever to say nothing changed, and
    `decision` is a semantic type — it rides the LEDGER, the hash chain and
    recall."""
    new, reason = tune(cfg.min_score,
                       outcomes(evs, step_status=step_status, surface=surface,
                                run_id=run_id))
    if not reason:
        return cfg, ""
    if append:
        try:
            ev_mod.append_event("decision", {"text": reason},
                                session_id=session_id,
                                tags=["replenish", "autotune", "auto"],
                                project=project)
        except Exception:                                       # noqa: BLE001
            # Transparency is the point of the row, but failing to write it must
            # not cost the round the adjustment it just measured.
            pass
    return replace(cfg, min_score=new), reason


# --- injector-directed skills -------------------------------------------------
# R20: when the injector directs a skill, the direction is recorded BEFORE the
# invocation and says who directed it. The event type, the consume-once pairing
# and the trail render already exist for the prompt gate; this adds a `source`
# field and reuses all three, because a second direction event would split the
# fold that answers "was this suggestion ever followed".

SOURCE_REPLENISH = "replenish_injector"


def pick_skill(names, *, project=None, evs=None):
    """The skill to direct for a candidate, or "" when none is defensible.

    Two measured inputs, in order: a skill whose LAST recorded call failed is
    dropped (`plugins.call_status`), and among what is left the one whose past
    directions were actually followed wins (`oversee.consumed_directions`).
    Ties break on the name so the choice is reproducible from the ledger.

    Returning "" is a real outcome, not a fallback. Directing a skill with no
    basis is exactly the fabricated trail R20 forbids — better to record no
    direction than to record one the record does not support."""
    from . import oversee as ov_mod
    from . import plugins as pl_mod
    candidates = [str(n) for n in (names or []) if str(n or "").strip()]
    if not candidates:
        return ""
    status = pl_mod.call_status(project) or {}
    viable = [n for n in candidates if status.get(n) != "fail"]
    if not viable:
        return ""
    followed = ov_mod.consumed_directions(project, evs=evs) or {}
    return sorted(viable, key=lambda n: (-int(followed.get(n, 0)), n))[0]


def direct_skill(names, *, project=None, session_id=None, evs=None,
                 append=True):
    """Record one injector-sourced ``skill_directed``, or nothing.

    Called BEFORE the invocation so the consume-once pairing in
    `oversee.skill_trails` joins it to the `skill_used` that follows. Returns
    the directed skill name, or "" when none was defensible."""
    name = pick_skill(names, project=project, evs=evs)
    if not name or not append:
        return name
    try:
        ev_mod.append_event(
            "skill_directed",
            {"skills": [{"name": name}], "source": SOURCE_REPLENISH},
            session_id=session_id, tags=["skill", "replenish", "auto"],
            project=project)
    except Exception:                                           # noqa: BLE001
        # Best-effort, matching `promptgate.record_direction`: failing to record
        # the trail must not stop the work the trail is about.
        return ""
    return name




# Lens registration, on the LAST line by design. `replenish_lenses` imports the
# names above from this module, so this import must run after they are bound —
# and it must run at all, or `LENSES` is an empty dict and every round finds
# nothing while looking perfectly healthy. `lens_registry` and `repo_root` are
# re-exported so no caller had to learn about the split.
from . import replenish_lenses as _lenses          # noqa: E402
from .replenish_lenses import (audit, caveat, coverage, debt,  # noqa: E402,F401
                               evidence_gap, repair, repo_root)

assert _lenses.LENSES is LENSES, "the lenses registered into another registry"
