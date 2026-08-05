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

from dataclasses import dataclass, field

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
    round it would never dedup."""

    lens: str
    title: str
    fingerprint: str
    evidence: tuple = ()
    score: float = 1.0


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
    min_score: float = 0.5
    # Deliberately small. A round that proposes more than a person will look at
    # is how an injector becomes noise, and phase 1 has no backpressure yet.
    max_per_round: int = 3
    dry_rounds_to_exit: int = 2


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

    @property
    def ran(self) -> bool:
        return not self.skipped


# Lens registry: name -> callable(evs) -> [Candidate]. Populated at module level
# by each lens below, so a caller can run the standard set without naming them
# and a test can pass its own.
LENSES: dict = {}


def history(evs) -> History:
    """Fold `work_injected` rows into the state a round needs.

    Total, per the ledger's read contract: a row whose payload is not a dict, or
    whose `outcome` is not one this version knows, is skipped rather than
    raising. The ledger holds history written by every past version of this code
    and will hold rows written by future ones.

    A REFUSED candidate's fingerprint is spent too. That is deliberate: re-
    proposing something already judged not worth doing is exactly the loop the
    dedup exists to prevent, and without it a refused candidate would return
    every single round forever."""
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
        r = p.get("round")
        if isinstance(r, int) and not isinstance(r, bool) and r > last:
            last = r
        fp = p.get("fingerprint")
        if isinstance(fp, str) and fp.strip():
            spent.add(fp)
        if outcome == "dry":
            dry += 1
        elif outcome == "landed":
            dry = 0            # real work landed: the streak is broken
    return History(frozenset(spent), last, dry)


def refusal_reason(candidate, spent, cfg) -> str:
    """Why this candidate may not become work, or "" when it may.

    Evidence is checked first and unconditionally. Everything else here is a
    policy knob an operator can loosen; this one is the rule that separates
    replenishment from inventing work to fill a quota."""
    evidence = [e for e in (candidate.evidence or [])
                if isinstance(e, str) and e.strip()]
    if not evidence:
        return ("no evidence — a candidate must point at a real ledger row, "
                "file, or signature")
    if not (candidate.fingerprint or "").strip():
        return "no fingerprint — this candidate could never be deduplicated"
    if candidate.fingerprint in spent:
        return "already proposed — this fingerprint is spent"
    if candidate.score < cfg.min_score:
        return (f"score {candidate.score} below the round's minimum "
                f"{cfg.min_score}")
    return ""


def gate(candidates, spent, cfg):
    """``(kept, refused)`` — refused entries carry their reason.

    Fingerprints of kept candidates join the spent set as we go, so two lenses
    proposing the same condition in one round produce one piece of work and one
    recorded duplicate refusal, not two pieces of work."""
    kept, refused = [], []
    seen = set(spent)
    for c in candidates or []:
        if not isinstance(c, Candidate):
            continue           # a lens returning junk is a bug, not a decision
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

    hist = history(evs)
    if hist.dry_rounds >= cfg.dry_rounds_to_exit:
        return RoundResult(round_no=hist.last_round,
                           skipped="dry rounds exhausted")
    round_no = hist.last_round + 1

    candidates = []
    for name, lens in (LENSES if lenses is None else lenses).items():
        try:
            candidates.extend(lens(evs) or [])
        except Exception:                                       # noqa: BLE001
            # One broken lens must not cost the round every other lens found.
            continue

    kept, refused = gate(candidates, hist.fingerprints, cfg)
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
        rows.append(_row("refused", round_no, surface, lens=c.lens,
                         title=(c.title or "(untitled)")[:MAX_TITLE_CHARS],
                         fingerprint=c.fingerprint or "unfingerprinted",
                         reason=reason[:MAX_REASON_CHARS]))
    if dropped:
        # R15: a cap that cut silently would read as "nothing more was found".
        rows.append(_row("truncated", round_no, surface, dropped=dropped))
    dry = not landed
    if dry:
        rows.append(_row("dry", round_no, surface))

    if append:
        _record(rows, project=project, session_id=session_id)

    return RoundResult(round_no=round_no, landed=landed, refused=refused,
                       dropped=dropped, dry=dry)


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
    lens name would let it fire exactly once in the life of the project."""
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
                      fingerprint=f"evidence-gap:{newest_ts}",
                      evidence=tuple(evidence))]


LENSES["evidence-gap"] = evidence_gap
