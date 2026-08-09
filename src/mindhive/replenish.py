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


def score_for(candidate) -> int:
    """This candidate's score, computed from what its lens observed.

    Zero when a prevention rule already covers the signature — the gate then
    refuses it with :data:`RULE_COVERED_REASON`, so `hive replenish status` can
    show a lens working and being correctly declined rather than showing
    nothing."""
    if getattr(candidate, "rule_covered", False):
        return 0
    rec = getattr(candidate, "recurrence", 0)
    if isinstance(rec, bool) or not isinstance(rec, int) or rec < 0:
        rec = 0
    rec = min(rec, MAX_RECURRENCE)
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


# Lens registry: name -> callable(evs) -> [Candidate]. Populated at module level
# by each lens below, so a caller can run the standard set without naming them
# and a test can pass its own.
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
              session_id=None, append=True) -> RoundResult:
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

    candidates, lens_errors = [], []
    for name, lens in (LENSES if lenses is None else lenses).items():
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


def evidence_gap(evs) -> list:
    """Code changed and nothing has verified it since.

    ONE candidate, not one per file. The outstanding fact is singular — "the
    suite has not run against the current tree" — and emitting a step per changed
    file would bury the plan and blow `roadmap.MAX_STEPS` on a busy week.

    The fingerprint keys on the newest covered `code_change` timestamp, which is
    the CONDITION INSTANCE: once the suite runs the condition clears, and a later
    change mints a fresh instance so the lens may fire again. Keying on the bare
    lens name would let it fire exactly once in the life of the project.

    `severity` is set because this project treats it as blocking: CLAUDE.md's
    "any change here must land green" and SOP `change-audit` §3 both say the
    suite passing is a precondition for calling work done, not a nicety. The
    flag is that recorded stance, not this lens's opinion of itself."""
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
    return [Candidate(lens="evidence-gap", title=title[:MAX_TITLE_CHARS],
                      fingerprint=fingerprint("evidence-gap", newest_ts),
                      evidence=tuple(evidence),
                      recurrence=count, severity=True)]


LENSES["evidence-gap"] = evidence_gap
