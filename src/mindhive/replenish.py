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

import re
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


# --- the score ---------------------------------------------------------------
# A score answers one question: how hard does the record insist this work
# matters? Every term is read off the record rather than judged, so two rounds
# over the same ledger score identically — which is what makes a threshold
# meaningful at all.

BASE_SCORE = 10
RECURRENCE_STEP = 5          # per repeat beyond the first
SEVERITY_BONUS = 20          # a condition that is already blocking something

# The floor a tuned threshold may never cross. A threshold below the base score
# would admit every candidate any lens emitted, which is precisely the
# inject-to-fill-a-quota failure the gate exists to prevent.
MIN_THRESHOLD = BASE_SCORE

# A standing decision NOT to act, in the record's own words. Matched against
# active prevention rules and pinned caveats, because those are the two places
# this project records "known, and not now". Re-proposing work a human already
# declined is the loop that would make the injector adversarial rather than
# useful, so it is a veto and not merely a penalty.
_DEFERRAL_RE = re.compile(
    r"\b(defer|deferred|deferring|deferral|wont ?fix|won't ?fix|"
    r"not worth|out of scope|by design|accepted risk|as designed)\b",
    re.IGNORECASE)


@dataclass(frozen=True)
class ScoreBreakdown:
    """Why a candidate scored what it did.

    A bare integer makes a refusal unexplainable — "below the minimum" is not
    something a reader can act on, while "base 10, recurrence 0, severity 0" says
    which term was missing. The terms also have to survive past the sum because
    the auto-tuner reasons about them."""

    base: int = BASE_SCORE
    recurrence: int = 0
    severity: int = 0
    veto_reason: str = ""

    @property
    def vetoed(self) -> bool:
        return bool(self.veto_reason)

    @property
    def total(self) -> int:
        return 0 if self.vetoed else self.base + self.recurrence + self.severity


@dataclass(frozen=True)
class DeferralIndex:
    """What the record has already declined to act on, folded ONCE per round.

    Only the veto lives here, and that asymmetry is deliberate. The recurrence
    count does NOT: the lens that grouped the rows already counted them, so
    re-deriving it would need a second fold plus a mapping from candidate back to
    event type that only the lens can supply. `Candidate.occurrences` carries it
    instead.

    `rule_reasons` is keyed exactly (a prevention rule names its signature);
    `caveat_texts` has to be scanned, because a caveat is prose that mentions a
    signature rather than a record keyed by one. Both are small — active rules are
    capped at `incidents.INJECT_CAP` for injection and pinned caveats are few by
    construction."""

    rule_reasons: dict = field(default_factory=dict)
    caveat_texts: tuple = ()

    def veto_reason_for(self, candidate) -> str:
        """The record's own words declining this candidate, or "".

        Matched on `deferral_key`, which a lens sets to whatever the record would
        name — a failure signature, a file path. A candidate that names no key
        cannot be matched against a decision about a key, and gets no veto rather
        than a guessed one."""
        key = str(getattr(candidate, "deferral_key", "") or "").strip()
        if not key:
            return ""
        exact = self.rule_reasons.get(key)
        if exact:
            return exact
        for text in self.caveat_texts:
            if key in text:
                return f"a pinned caveat defers this: {text[:200]}"
        return ""


def build_deferral_index(evs=None, project=None) -> DeferralIndex:
    """Fold the record's standing deferrals. Total — never raises.

    Reads incident records through `incidents.summaries()` (the public accessor,
    not the private rule walker) and pinned caveats out of *evs*. Either source
    being unavailable yields an emptier index, never an exception: a round that
    could not read the deferrals must still run, and the cost of missing one is a
    candidate proposed again, not a corrupted ledger."""
    rule_reasons, caveat_texts = {}, []

    try:
        from . import incidents as incidents_mod
        for record in incidents_mod.summaries(project) or []:
            if not isinstance(record, dict):
                continue
            signature = str(record.get("signature") or "").strip()
            rule = str(record.get("active_rule") or "")
            if signature and rule and _DEFERRAL_RE.search(rule):
                rule_reasons[signature] = (
                    f"an active prevention rule defers this: {rule[:200]}")
    except Exception:                                           # noqa: BLE001
        pass

    for ev in evs or []:
        if getattr(ev, "type", None) != "caveat":
            continue
        if "pinned" not in (getattr(ev, "tags", None) or []):
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("text") or "")
        if text and _DEFERRAL_RE.search(text):
            caveat_texts.append(text)

    return DeferralIndex(rule_reasons, tuple(caveat_texts))


def score_candidate(candidate, index=None) -> ScoreBreakdown:
    """Score one candidate against the record. Pure, and total on junk input.

    `occurrences` is coerced rather than trusted because a lens is the one thing
    here that is not validated at a boundary — a lens returning ``None`` or a
    bool must degrade to "seen once", not poison the arithmetic."""
    occurrences = getattr(candidate, "occurrences", 1)
    if isinstance(occurrences, bool) or not isinstance(occurrences, int):
        occurrences = 1
    recurrence = RECURRENCE_STEP * max(0, occurrences - 1)
    severity = SEVERITY_BONUS if getattr(candidate, "severity", False) else 0
    veto = index.veto_reason_for(candidate) if index is not None else ""
    return ScoreBreakdown(recurrence=recurrence, severity=severity,
                          veto_reason=veto)


# --- the core ----------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One proposal from one lens.

    `evidence` is what makes it admissible: a pointer to a real ledger row, file,
    or signature that a reader can go and check. `fingerprint` identifies the
    CONDITION INSTANCE, not the condition — keyed on the bare lens name a
    candidate would fire once ever, and keyed on something that changes every
    round it would never dedup.

    Three fields are set by the LENS, because only the lens knows them:
    `occurrences` (how many times its fold saw this condition), `severity`
    (whether what it found is already blocking something), and `deferral_key`
    (what the record would call this, so a standing deferral can be matched).

    Two are set by the ROUND, overwriting whatever a lens put there: `score` and
    `veto_reason`. Scoring lives in the round rather than the lens so one scale
    governs every lens; a lens that scored itself would make the threshold
    meaningless the moment two lenses disagreed about what 25 meant."""

    lens: str
    title: str
    fingerprint: str
    evidence: tuple = ()
    occurrences: int = 1
    severity: bool = False
    deferral_key: str = ""
    depth: int = 1
    score: float = BASE_SCORE
    veto_reason: str = ""


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
    # 0 means NO score floor, and that is the right bare default: the threshold
    # is the ambition dial's to set (`ReplenishConfig.for_profile`), and a
    # hard-coded one here would silently be a fourth ambition position that no
    # operator chose. Phase 1's 0.5 was a placeholder on a scale that no longer
    # exists — scores are integers now, base `BASE_SCORE`.
    min_score: int = 0
    # An injected step may itself propose work; that work may not. Depth 2 is
    # where the tree stops being a plan and starts being a fan-out.
    max_depth: int = 2
    # Deliberately small. A round that proposes more than a person will look at
    # is how an injector becomes noise, and phase 1 has no backpressure yet.
    max_per_round: int = 3
    # 0 DISABLES the dry-round exit, and that is the right setting for any
    # surface whose own lifecycle already bounds the rounds. The exit exists for
    # a loop, which runs many rounds back to back and must stop spinning. On a
    # surface that runs one round per session it is actively wrong: nothing can
    # reset the counter except work landing, and work can only land if a round
    # runs, so two quiet sessions would switch the injector off permanently.
    dry_rounds_to_exit: int = 0

    @classmethod
    def for_profile(cls, profile, *, enabled=True, dry_rounds_to_exit=0):
        """A round's config, taken from an ambition position.

        `max_per_round` takes the profile's per-run cap because they are one dial
        enforced at two points: this trim stops a single round flooding the plan,
        and the caller folds the run's own landed rows to stop five rounds
        injecting five times the cap."""
        return cls(enabled=enabled, min_score=profile.min_score,
                   max_per_round=profile.max_injections_per_run,
                   dry_rounds_to_exit=dry_rounds_to_exit)


@dataclass(frozen=True)
class AmbitionProfile:
    """One position on the operator's dial.

    Three settings vary together — how selective the gate is, which lenses run,
    and how much one run may inject — so they live in one record. Scattered across
    three conditionals the relationship between them would be invisible, and the
    reason `conservative` runs two lenses AND wants a score of 40 is precisely
    that relationship: fewer sources, higher bar, smaller budget.

    `lens_names` is resolved against `LENSES` rather than holding callables, so a
    profile naming a lens that does not exist is a loud error instead of a lens
    that silently never runs."""

    name: str
    min_score: int
    lens_names: tuple
    max_injections_per_run: int


# The six deterministic lenses, cheapest-signal-first. `innovation` is not here:
# it is the only lens that spends a model call, so it joins `ambitious` alone.
_REPAIR_AND_GAP = ("repair", "evidence-gap")
_ALL_DETERMINISTIC = _REPAIR_AND_GAP + ("tech-debt", "audit", "caveat", "coverage")

PROFILES = {
    "conservative": AmbitionProfile("conservative", 40, _REPAIR_AND_GAP, 3),
    "balanced": AmbitionProfile("balanced", 25, _ALL_DETERMINISTIC, 10),
    "ambitious": AmbitionProfile("ambitious", 15, _ALL_DETERMINISTIC, 25),
}

DEFAULT_AMBITION = "balanced"


@dataclass(frozen=True)
class AmbitionChoice:
    """The resolved profile, plus whether the operator's word was understood.

    The fallback is reported rather than swallowed: a typo'd dial that quietly
    runs at a different aggression than the operator set is worse than a loud one,
    because the operator's next move is to wonder why the injector is behaving
    strangely rather than to fix a spelling."""

    profile: AmbitionProfile
    requested: str = ""

    @property
    def fell_back(self) -> bool:
        return bool(self.requested) and self.requested != self.profile.name


def resolve_ambition(name) -> AmbitionChoice:
    """Look up an ambition position by name, falling back to `balanced`."""
    requested = str(name or "").strip().lower()
    return AmbitionChoice(PROFILES.get(requested, PROFILES[DEFAULT_AMBITION]),
                          requested)


def lenses_for(profile, registry=None) -> dict:
    """The profile's lenses, resolved against the registry.

    Raises on a name the registry does not hold. That is deliberate: the failure
    mode of returning what it could find is a lens the operator believes is
    running and is not, which is invisible from every surface."""
    available = LENSES if registry is None else registry
    missing = [n for n in profile.lens_names if n not in available]
    if missing:
        raise KeyError(
            f"ambition profile {profile.name!r} names lenses that are not "
            f"registered: {', '.join(missing)}")
    return {n: available[n] for n in profile.lens_names}


@dataclass(frozen=True)
class History:
    """The injector's state, recovered from the ledger alone."""

    fingerprints: frozenset = frozenset()
    last_round: int = 0
    dry_rounds: int = 0


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


@dataclass(frozen=True)
class LensContext:
    """Everything a lens is allowed to read, named.

    A lens started life as `callable(evs)`, which was right while one lens read
    only the ledger. It stopped being right the moment lenses needed a project
    (to resolve incident records), a source tree (to count lines), and a working
    tree (to diff) — passing those positionally is how a signature becomes
    unreadable, and passing them as a bare dict loses which is which.

    The roots are injectable rather than resolved internally because a lens that
    resolved its own tree could only ever be tested against the real repo."""

    evs: tuple = ()
    project: str | None = None
    src_root: str = ""              # defaults to the checkout's src/mindhive
    tests_root: str = ""            # defaults to the checkout's tests
    repo_root: str = ""             # defaults to the checkout root
    run_id: str = ""                # loop surface only; "" on a session round


def _as_context(arg, project=None) -> LensContext:
    """Normalise a lens argument to a `LensContext`.

    Existing callers — and every test stub — pass a bare event list. Normalising
    once here rather than branching inside each lens is the whole reason the
    lenses below have no special cases: they read `ctx.evs`, always."""
    if isinstance(arg, LensContext):
        return arg
    return LensContext(evs=tuple(arg or ()), project=project)


# Lens registry: name -> callable(LensContext) -> [Candidate]. Populated at module
# level by each lens below, so a caller can run the standard set without naming
# them and a test can pass its own.
LENSES: dict = {}


def history(evs, surface=None) -> History:
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
    would let a quiet loop switch off a session's injector."""
    spent, last, dry = set(), 0, 0
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
        if outcome == "dry":
            dry += 1
        elif outcome == "landed":
            dry = 0            # real work landed: the streak is broken
    return History(frozenset(spent), last, dry)


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
    if candidate.veto_reason:
        # Its own branch rather than a score comparison: a veto zeroes the score,
        # but the bare config has no score floor, so relying on the arithmetic
        # would let a declined candidate through whenever no threshold was set.
        # It also states WHICH record declined it, which "below the minimum"
        # cannot.
        return f"already declined — {candidate.veto_reason}"
    if candidate.depth > cfg.max_depth:
        return (f"depth {candidate.depth} exceeds the maximum injection depth "
                f"{cfg.max_depth} — an injected step may propose work, its "
                f"proposals may not")
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
    # `replace` rather than a fresh construction: rebuilding field-by-field
    # silently DROPS every field added later, and the fields most likely to be
    # added are exactly the ones the gate reads (a veto, a depth). Clamp what has
    # a contract cap; carry the rest through untouched.
    return replace(candidate,
                   lens=str(candidate.lens)[:64],
                   title=str(candidate.title)[:MAX_TITLE_CHARS],
                   fingerprint=str(candidate.fingerprint)[:128],
                   evidence=evidence)


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
        c = normalise(c)
        reason = refusal_reason(c, seen, cfg)
        if reason:
            refused.append((c, reason))
            continue
        seen.add(c.fingerprint)
        kept.append(c)
    return kept, refused


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
              session_id=None, append=True, ctx=None) -> RoundResult:
    """Run one replenishment round and record what it decided.

    *sink* is a parameter, not an import: deciding what is outstanding is
    surface-independent, forcing it is not. It is called with the kept
    candidates and returns ``(placements, dropped)``.

    A candidate the sink could not place gets NO row, so its fingerprint stays
    unspent and it can be proposed again next round. Same for candidates the cap
    cut — a truncation is "not now", not "never"."""
    cfg = cfg or ReplenishConfig()
    if not cfg.enabled:
        return RoundResult(skipped="disabled")

    hist = history(evs, surface=surface)
    if cfg.dry_rounds_to_exit > 0 and hist.dry_rounds >= cfg.dry_rounds_to_exit:
        return RoundResult(round_no=hist.last_round,
                           skipped="dry rounds exhausted")
    round_no = hist.last_round + 1

    # One context for the whole round: the lenses that read a tree or a project
    # get those from here rather than resolving them, which is what makes them
    # testable without the real repo.
    lens_ctx = ctx if ctx is not None else _as_context(evs, project)

    candidates, lens_errors = [], []
    for name, lens in (LENSES if lenses is None else lenses).items():
        try:
            candidates.extend(lens(lens_ctx) or [])
        except Exception as exc:                                # noqa: BLE001
            # One broken lens must not cost the round every other lens found —
            # but silence here is worse than the crash. A round where every lens
            # threw wrote nothing and reported "nothing outstanding that the
            # record proves", which is a false statement about the ledger. The
            # failure goes to `tool_failed` under a `lens:<name>` signature,
            # which is the signature `incidents`/`reflect` already audit, so a
            # lens that keeps breaking becomes a prevention rule on its own.
            lens_errors.append((name, f"{type(exc).__name__}: {exc}"))

    # Scoring happens HERE rather than in each lens, so one scale governs all of
    # them — the moment two lenses decide for themselves what 25 means, the
    # threshold stops being a threshold. The deferral index is folded once for the
    # whole round; per candidate it would be a ledger pass each.
    deferrals = build_deferral_index(lens_ctx.evs or evs,
                                     project=lens_ctx.project)
    scored = []
    for candidate in candidates:
        if not isinstance(candidate, Candidate):
            scored.append(candidate)        # gate() is where junk gets dropped
            continue
        breakdown = score_candidate(candidate, deferrals)
        scored.append(replace(candidate, score=breakdown.total,
                              veto_reason=breakdown.veto_reason))

    kept, refused = gate(scored, hist.fingerprints, cfg)
    # R16 asks whether anything QUALIFIED, which is decided here. A round the cap
    # trimmed or the sink could not place found real work and must not report
    # itself dry — the CLI was printing "N candidate(s) did not fit" and
    # "nothing outstanding" in the same breath.
    dry = not kept
    dropped = 0
    if len(kept) > cfg.max_per_round:
        dropped = len(kept) - cfg.max_per_round
        kept = kept[:cfg.max_per_round]

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
                         fingerprint=c.fingerprint,
                         reason=reason[:MAX_REASON_CHARS]))
    if dropped:
        # R15: a cap that cut silently would read as "nothing more was found".
        rows.append(_row("truncated", round_no, surface, dropped=dropped))
    # The dry row exists to feed the dry-round exit. Where that exit is off, it
    # is a row nobody reads, appended once per session forever — so it is only
    # written when something actually consumes it.
    if dry and cfg.dry_rounds_to_exit > 0:
        rows.append(_row("dry", round_no, surface))

    if append:
        _record(rows, project=project, session_id=session_id)
        _record_lens_errors(lens_errors, surface, project=project,
                            session_id=session_id)

    return RoundResult(round_no=round_no, landed=landed, refused=refused,
                       dropped=dropped, dry=dry, lens_errors=lens_errors)


# --- lenses ------------------------------------------------------------------
# Each lens answers ONE question about the record and returns candidates. They
# are plain folds: no model, no subprocess, no network. A lens that needs a model
# is a later phase and runs through the agent seam, not here.

# How many changed files ride the evidence list. Enough to recognise the
# change-set, bounded so one enormous commit cannot blow the row.
_EVIDENCE_FILES = 6


def _passing_verification_ts(evs) -> str:
    """Timestamp of the newest verification whose SUBPROCESS SUCCEEDED, or "".

    The exit code is the observed fact — `verify.py` is explicit that the
    `verified_by` stamp attaches to it and not to the counts, because an
    unparseable summary records counts absent rather than zero. So a run with a
    zero exit code and no parsed counts is a pass here, and a run with counts but
    a non-zero exit code is not."""
    newest = ""
    for ev in evs or []:
        if getattr(ev, "type", None) != "verification":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        code = p.get("exit_code")
        if isinstance(code, bool) or not isinstance(code, int) or code != 0:
            continue
        ts = ev.ts or ""
        if ts > newest:
            newest = ts
    return newest


def evidence_gap(arg) -> list:
    """Code changed and nothing has verified it since.

    ONE candidate, not one per file. The outstanding fact is singular — "the
    suite has not run against the current tree" — and emitting a step per changed
    file would bury the plan and blow `roadmap.MAX_STEPS` on a busy week.

    The fingerprint keys on the newest covered `code_change` timestamp, which is
    the CONDITION INSTANCE: once the suite runs the condition clears, and a later
    change mints a fresh instance so the lens may fire again. Keying on the bare
    lens name would let it fire exactly once in the life of the project."""
    evs = _as_context(arg).evs
    newest_ts, newest_payload, count = "", None, 0
    verified_ts = _passing_verification_ts(evs)
    for ev in evs or []:
        if getattr(ev, "type", None) != "code_change":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        ts = ev.ts or ""
        if ts > verified_ts:
            count += 1
        if ts > newest_ts:
            newest_ts, newest_payload = ts, p
    if not newest_ts or newest_ts <= verified_ts:
        return []

    files = [str(f) for f in (newest_payload.get("files") or [])
             if isinstance(f, str)]
    plural = "" if count == 1 else "s"
    title = (f"Run the test suite: {count} code change{plural} since the last "
             f"passing verification")
    evidence = [
        f"newest code_change @ {newest_ts}",
        (f"last passing verification @ {verified_ts}" if verified_ts
         else "no passing verification has ever been recorded"),
    ]
    evidence += [f"changed: {f}" for f in files[:_EVIDENCE_FILES]]
    # `occurrences` is the change count the fold above already produced, and
    # `severity` is asserted because unverified code IS the blocking condition
    # (SOP change-audit §3) rather than a preference. No `deferral_key`: "run the
    # suite" is not a thing the record can stand down from, so there is nothing
    # for a deferral to match.
    return [Candidate(lens="evidence-gap", title=title[:MAX_TITLE_CHARS],
                      fingerprint=f"evidence-gap:{newest_ts}",
                      evidence=tuple(evidence),
                      occurrences=count, severity=True)]


LENSES["evidence-gap"] = evidence_gap


# How many failure timestamps ride a repair candidate's evidence list. Enough to
# show the pattern is real, bounded so a signature that fired 200 times cannot
# blow the row.
_EVIDENCE_TIMESTAMPS = 5


def _signatures_with_an_active_rule(project) -> set:
    """Signatures the failure loop has already turned into an enforced rule.

    Read through `incidents.summaries()` — the public accessor — rather than the
    private rule walker beside it, so this does not break the next time the
    incident file format moves. Total: an unreadable incident store yields an
    empty set, which proposes work rather than hiding it."""
    covered = set()
    try:
        from . import incidents as incidents_mod
        for record in incidents_mod.summaries(project) or []:
            if not isinstance(record, dict):
                continue
            signature = str(record.get("signature") or "").strip()
            if signature and str(record.get("active_rule") or "").strip():
                covered.add(signature)
    except Exception:                                           # noqa: BLE001
        pass
    return covered


def _failure_signature(payload) -> str:
    """The canonical signature for one `tool_failed` row.

    Prefers the recorded `signature` and derives one from the text otherwise.
    That order matters: SOP `tool-mcp-integration` §1 records that 260 existing
    rows carry no parseable fields, so deriving is the common path for history and
    reading is the common path for anything written since."""
    recorded = str(payload.get("signature") or "").strip()
    if recorded:
        return recorded
    text = str(payload.get("text") or payload.get("error") or "").strip()
    if not text:
        return ""
    try:
        from . import signature as signature_mod
        return signature_mod.signature(text)
    except Exception:                                           # noqa: BLE001
        return ""


def repair(arg) -> list:
    """A recorded failure that never became a prevention rule.

    The failure loop already turns a recurring signature into an enforced rule
    (`incidents` → `reflect` → `rules enforce`). What it does not do is fix the
    CAUSE — a rule stops the agent repeating the mistake, it does not remove the
    thing that made the mistake possible. This lens proposes that work, and only
    where no rule exists yet: a signature with an active rule is already handled
    as well as this mechanism can handle it, and proposing on top would double-
    count the same fact the rule already covers.

    One candidate per signature, not per failure. `occurrences` is the live count,
    which is what earns the recurrence term — a signature seen four times has to
    outscore one seen once, and that is the whole reason the term exists."""
    ctx = _as_context(arg)
    covered = _signatures_with_an_active_rule(ctx.project)

    timestamps, newest = {}, {}
    for ev in ctx.evs or []:
        if getattr(ev, "type", None) != "tool_failed":
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        sig = _failure_signature(payload)
        if not sig or sig in covered:
            continue
        ts = getattr(ev, "ts", "") or ""
        timestamps.setdefault(sig, []).append(ts)
        if ts > newest.get(sig, ""):
            newest[sig] = ts

    out = []
    for sig in sorted(timestamps):
        seen = sorted(t for t in timestamps[sig] if t)
        out.append(Candidate(
            lens="repair",
            title=f"Investigate and patch the root cause of {sig}"[:MAX_TITLE_CHARS],
            # Keyed on the newest failure: once the cause is fixed the signature
            # stops recurring, and a later recurrence is a NEW instance that
            # deserves proposing again rather than being silently deduped away.
            fingerprint=f"repair:{sig}:{newest.get(sig, '')}",
            evidence=tuple([f"signature: {sig}",
                            f"{len(timestamps[sig])} recorded failure(s), "
                            f"no active prevention rule"]
                           + [f"failed @ {t}"
                              for t in seen[-_EVIDENCE_TIMESTAMPS:]]),
            occurrences=len(timestamps[sig]),
            deferral_key=sig))
    return out


LENSES["repair"] = repair


# What a caveat can be said to be ABOUT, for the purpose of deciding whether
# later work addressed it. Deliberately narrow — a Python path is unambiguous,
# whereas matching prose against prose would make "addressed" a guess. A caveat
# naming no path stays outstanding, which is the safe direction: the fingerprint
# spends after one proposal, so it is proposed once and never again rather than
# every round forever.
_PY_REFERENT_RE = re.compile(r"[\w./\\-]+\.py")


def _addressed_referents(evs) -> set:
    """Paths that a later `code_change` or `experiment_result` names."""
    touched = set()
    for ev in evs or []:
        etype = getattr(ev, "type", None)
        if etype not in ("code_change", "experiment_result"):
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        for path in (payload.get("files") or []):
            if isinstance(path, str):
                touched.add(path.replace("\\", "/").lstrip("./"))
        for match in _PY_REFERENT_RE.findall(str(payload.get("text") or "")):
            touched.add(match.replace("\\", "/").lstrip("./"))
    return touched


def caveat(arg) -> list:
    """A pinned caveat nothing has since addressed.

    Pinned is the discriminating tag, not decoration: SOP `memo-hygiene` §4 makes
    it the marker for a safety-critical fact that survives the MEMORY.md cap
    rotation and that consolidation never folds away. An unpinned caveat is an
    observation; treating those as outstanding work would turn every passing
    remark into a task.

    "Addressed" is decided on paths rather than prose. A caveat naming
    `src/mindhive/foo.py` is answered by a later `code_change` touching that file;
    a caveat naming nothing checkable stays outstanding and is proposed exactly
    once, because its fingerprint spends on the first round."""
    ctx = _as_context(arg)
    addressed = _addressed_referents(ctx.evs)

    out = []
    for ev in ctx.evs or []:
        if getattr(ev, "type", None) != "caveat":
            continue
        if "pinned" not in (getattr(ev, "tags", None) or []):
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("text") or "").strip()
        if not text:
            continue                     # nothing to title the work with
        referents = {m.replace("\\", "/").lstrip("./")
                     for m in _PY_REFERENT_RE.findall(text)}
        if referents & addressed:
            continue
        headline = text.splitlines()[0].strip()
        ts = getattr(ev, "ts", "") or ""
        evidence = [f"pinned caveat @ {ts}"]
        evidence += [f"names: {r}" for r in sorted(referents)][:_EVIDENCE_FILES]
        out.append(Candidate(
            lens="caveat",
            title=f"Resolve pinned architectural caveat: {headline}"[:MAX_TITLE_CHARS],
            fingerprint=f"caveat:{ts}",
            evidence=tuple(evidence),
            # The path is what a standing deferral would name, so it is also what
            # a veto matches on. Prose cannot be a deferral key.
            deferral_key=sorted(referents)[0] if referents else ""))
    return out


LENSES["caveat"] = caveat


# --- the surface lenses -------------------------------------------------------
# These three read the checkout rather than the ledger. Their roots arrive on the
# LensContext because a lens that resolved its own tree could only ever be tested
# against the real repo — and the real repo is 4000 tests wide.

# The thermo-nuclear threshold. Not a new opinion: `docs/session-legend.md`
# already records `__main__.py` sitting past this line as a standing caveat, and
# the review skill checks against the same number.
LINE_THRESHOLD = 1000


@dataclass(frozen=True)
class SourceTrees:
    """The three directories the surface lenses read, resolved once."""

    src_root: str = ""
    tests_root: str = ""
    repo_root: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.src_root and self.tests_root)


def resolve_trees(ctx) -> SourceTrees:
    """Where this checkout's source and tests live.

    `paths.worktree_root()`, deliberately, and not `project_dir()`: these lenses
    read THIS checkout's files, while the store collapses every worktree of a repo
    onto one namespace on purpose (SOP `session-lifecycle` §3). Conflating the two
    is a real bug, so the choice is stated here rather than left incidental."""
    src_root, tests_root = ctx.src_root, ctx.tests_root
    repo_root = ctx.repo_root
    if not (src_root and tests_root):
        try:
            from . import paths
            root = paths.worktree_root()
            if root is not None:
                repo_root = str(root)
                src_root = src_root or str(root / "src" / "mindhive")
                tests_root = tests_root or str(root / "tests")
        except Exception:                                       # noqa: BLE001
            pass
    if not repo_root and src_root:
        # src/mindhive -> the checkout two levels up, so the audit lens can diff
        # without a second resolution path.
        try:
            from pathlib import Path
            repo_root = str(Path(src_root).parent.parent)
        except Exception:                                       # noqa: BLE001
            pass
    return SourceTrees(src_root, tests_root, repo_root)


def _python_modules(root):
    """`(stem, absolute path)` for each first-party module directly under *root*.

    Non-recursive on purpose: the packages below (`loop/`, `mindnet/`,
    `rotation/`) have their own test-naming conventions, and a lens that walked
    into them would emit confident nonsense about files it had no rule for."""
    out = []
    try:
        from pathlib import Path
        directory = Path(root)
        if not directory.is_dir():
            return out
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("__"):
                continue                 # __init__ / __main__ are not modules
                                         # with a test-file convention
            out.append((path.stem, str(path)))
    except OSError:
        pass
    return out


def technical_debt(arg) -> list:
    """A module past the line threshold, or a file stuck in the review queue.

    Two conditions, one lens, because both answer "what in this tree is already
    known to be too big to reason about". The queue entry carries `severity`: a
    file sitting in `pending_review.json` is blocking a gate right now (SOP
    `change-audit` §4 makes the notice a gate, not a suggestion), whereas an
    oversized module is debt that is merely accruing."""
    ctx = _as_context(arg)
    trees = resolve_trees(ctx)
    out = []

    for stem, path in _python_modules(trees.src_root):
        lines = _line_count(path)
        if lines <= LINE_THRESHOLD:
            continue
        relative = _repo_relative(path, trees.repo_root)
        out.append(Candidate(
            lens="tech-debt",
            title=(f"Refactor and reduce line count in {relative} "
                   f"({lines} lines, threshold {LINE_THRESHOLD})")[:MAX_TITLE_CHARS],
            # Bucketed by hundreds, not the exact count: keyed on the raw number a
            # single added line would mint a fresh instance and re-propose the same
            # refactor every round.
            fingerprint=f"tech-debt:lines:{relative}:{lines // 100}",
            evidence=(f"{relative} is {lines} lines",
                      f"threshold is {LINE_THRESHOLD} lines",
                      f"module {stem}"),
            deferral_key=relative))

    for path in _pending_review_files(ctx.project):
        relative = _repo_relative(path, trees.repo_root)
        out.append(Candidate(
            lens="tech-debt",
            title=f"Clear the thermo-nuclear review queue for {relative}"[:MAX_TITLE_CHARS],
            fingerprint=f"tech-debt:review:{relative}",
            evidence=(f"{relative} is queued in pending_review.json",
                      "SOP change-audit §4: the notice is a gate, not a "
                      "suggestion"),
            severity=True,
            deferral_key=relative))
    return out


def _line_count(path) -> int:
    """Delegated to `review._line_count` so "how long is this file" has exactly
    one answer in this codebase; falls back to 0 (treated as absent) if review
    cannot be imported."""
    try:
        from . import review as review_mod
        return review_mod._line_count(str(path))
    except Exception:                                           # noqa: BLE001
        return 0


def _pending_review_files(project) -> list:
    """Files sitting uncleared in the thermo-nuclear queue. Total on a missing or
    corrupt marker — `review.load_pending` already returns None for both."""
    try:
        from . import review as review_mod
        marker = review_mod.load_pending(project)
    except Exception:                                           # noqa: BLE001
        return []
    if not isinstance(marker, dict):
        return []
    return [f for f in (marker.get("files") or []) if isinstance(f, str) and f.strip()]


def _repo_relative(path, repo_root) -> str:
    """A repo-relative, forward-slashed path. Absolute paths in a candidate title
    would leak one machine's layout into the ledger and the dashboard."""
    text = str(path).replace("\\", "/")
    root = str(repo_root or "").replace("\\", "/").rstrip("/")
    if root and text.startswith(root + "/"):
        return text[len(root) + 1:]
    return text


LENSES["tech-debt"] = technical_debt


def git_modified_paths(repo_root) -> list:
    """Working-tree paths git reports as changed. The single git seam.

    One function so a test can replace it, and so a git failure has exactly one
    place to be handled. Raises on failure rather than returning empty: the audit
    lens must be able to tell "nothing is modified" from "I could not look", and
    the second one has to reach the lens-error channel."""
    import subprocess
    completed = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo_root),
        capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        raise RuntimeError(
            f"git status failed ({completed.returncode}): "
            f"{(completed.stderr or '').strip()[:200]}")
    paths_out = []
    for line in (completed.stdout or "").splitlines():
        # Porcelain v1: two status chars, a space, then the path. A rename
        # carries "old -> new"; the new name is the one that exists to review.
        entry = line[3:].strip().strip('"')
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1].strip().strip('"')
        if entry:
            paths_out.append(entry.replace("\\", "/"))
    return paths_out


def audit(arg) -> list:
    """Source modified in the working tree with no change-log entry covering it.

    This is SOP `change-audit` §1 read back off the record: the entry is mandatory
    before the work is called done, and the two halves of the fact live in
    different places — the modification is in the working tree, the entry is in
    the ledger. Nothing else compares them.

    ONE candidate for the whole uncovered set. The outstanding fact is singular
    ("this change-set has no entry"), and a candidate per file would blow
    `roadmap.MAX_STEPS` on any ordinary day's work."""
    ctx = _as_context(arg)
    trees = resolve_trees(ctx)
    if not trees.repo_root:
        return []

    modified = [p for p in git_modified_paths(trees.repo_root)
                if _in_review_scope(p)]
    if not modified:
        return []

    logged = _logged_files(ctx.evs)
    uncovered = sorted(p for p in modified
                       if p.replace("\\", "/").lstrip("./") not in logged)
    if not uncovered:
        return []

    shown = uncovered[:_EVIDENCE_FILES]
    more = len(uncovered) - len(shown)
    suffix = f" (+{more} more)" if more > 0 else ""
    return [Candidate(
        lens="audit",
        title=(f"Record a change-log entry for {len(uncovered)} modified "
               f"source file(s): {', '.join(shown)}{suffix}")[:MAX_TITLE_CHARS],
        # Keyed on the uncovered SET: log one of them and the set changes, which
        # is a different outstanding fact and deserves a fresh proposal.
        fingerprint="audit:" + ",".join(uncovered),
        evidence=tuple([f"modified, no code_change: {p}" for p in shown]
                       + ["SOP change-audit §1: `hive log` is mandatory before "
                          "the work is called done"]),
        severity=True,
        deferral_key=uncovered[0])]


def _in_review_scope(path) -> bool:
    """`review.in_scope` expects an absolute-ish path with `/src/mindhive/` or
    `/tests/` in it; git reports repo-relative ones. Prefixing a slash makes the
    two agree without duplicating the rule."""
    try:
        from . import review as review_mod
        return review_mod.in_scope("/" + str(path).replace("\\", "/").lstrip("/"))
    except Exception:                                           # noqa: BLE001
        return False


def _logged_files(evs) -> set:
    """Paths any `code_change` event claims. Basenames are matched too, because
    the ledger holds worktree-absolute paths from every past checkout while git
    reports repo-relative ones — comparing them literally would call everything
    unlogged."""
    logged = set()
    for ev in evs or []:
        if getattr(ev, "type", None) != "code_change":
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        for path in (payload.get("files") or []):
            if not isinstance(path, str):
                continue
            normalised = path.replace("\\", "/")
            logged.add(normalised.lstrip("./"))
            marker = "/src/mindhive/"
            if marker in normalised:
                logged.add("src/mindhive/" + normalised.split(marker, 1)[1])
            marker = "/tests/"
            if marker in normalised:
                logged.add("tests/" + normalised.split(marker, 1)[1])
    return logged


LENSES["audit"] = audit


def coverage(arg) -> list:
    """A module no test file names.

    The naive rule — `tests/test_<stem>.py` must exist — is wrong here and would
    emit confident nonsense: `dashboard_page.py` is exercised from
    `test_dashboard.py`, and several modules are covered by suites named for the
    behaviour rather than the file. So a module counts as covered when a test file
    of that name exists OR any test file mentions the module by name. The mention
    scan is one pass over `tests/`, shared across every module."""
    ctx = _as_context(arg)
    trees = resolve_trees(ctx)
    if not trees.usable:
        return []

    mentioned = _modules_mentioned_by_tests(trees.tests_root)
    out = []
    for stem, path in _python_modules(trees.src_root):
        try:
            from pathlib import Path
            named = (Path(trees.tests_root) / f"test_{stem}.py").exists()
        except OSError:
            named = False
        if named or stem in mentioned:
            continue
        relative = _repo_relative(path, trees.repo_root)
        out.append(Candidate(
            lens="coverage",
            title=f"Bootstrap a unit test suite for {relative}"[:MAX_TITLE_CHARS],
            fingerprint=f"coverage:{relative}",
            evidence=(f"{relative} has no test module",
                      f"expected tests/test_{stem}.py, and no test file names "
                      f"`{stem}`"),
            deferral_key=relative))
    return out


# How a test file says which module it exercises: `from mindhive import x, y`,
# or any `mindhive.x` attribute path.
_MODULE_MENTION_RE = re.compile(
    r"from\s+mindhive\s+import\s+([\w,\s]+)|mindhive\.(\w+)")


def _modules_mentioned_by_tests(tests_root) -> set:
    """Module names any test file references. One pass, shared by every module.

    Reads the text rather than importing: importing each test module to inspect
    what it touches would execute the suite as a side effect of asking a
    question about it."""
    mentioned = set()
    try:
        from pathlib import Path
        directory = Path(tests_root)
        if not directory.is_dir():
            return mentioned
        for path in sorted(directory.rglob("test_*.py")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for grouped, attribute in _MODULE_MENTION_RE.findall(text):
                if attribute:
                    mentioned.add(attribute)
                for name in grouped.split(","):
                    name = name.strip()
                    if name:
                        mentioned.add(name)
    except OSError:
        pass
    return mentioned


LENSES["coverage"] = coverage
