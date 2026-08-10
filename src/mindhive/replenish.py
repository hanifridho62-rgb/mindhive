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
     "fingerprint": "…", "score": 35, "plan_id": "…", "step_index": 3,
     "terms": {"base": 10, "severity": 20, "recurrence": 5, "total": 35}}
    {"outcome": "refused",   "round": 1, "surface": "session",
     "lens": "evidence-gap", "title": "…", "fingerprint": "…",
     "reason": "no evidence"}
    {"outcome": "truncated", "round": 1, "surface": "session", "dropped": 4}
    {"outcome": "dry",       "round": 2, "surface": "session"}

`dry` exists as its own outcome because "this round found nothing" has to be a
recorded fact, not the absence of one: the two-consecutive-empty-rounds exit
cannot be derived from silence.

`fingerprint` is a SHA-256 digest minted by `generate_fingerprint`, not a readable
key. The field is capped, and a readable key long enough to be clamped to that cap
would let two different conditions share one identity — which reads as "already
proposed" and silences the lens. `kind` carries the readable classification the
digest gives up, for the lenses that observe more than one kind of condition.
"""

import hashlib
import re
from dataclasses import dataclass, field, replace

from . import events as ev_mod, paths

# Outcome vocabulary. `landed` and `refused` are per-candidate; `truncated` and
# `dry` are per-round facts about the round itself.
OUTCOMES = ("landed", "refused", "truncated", "dry")

MAX_TITLE_CHARS = 300          # matches roadmap.MAX_STEP_CHARS — a landed title
                               # becomes a step, and a title the sink would have
                               # to truncate is one the audit row misreports.
MAX_REASON_CHARS = 500
MAX_EVIDENCE_ITEMS = 12
MAX_EVIDENCE_CHARS = 300

# The contract cap on `fingerprint`, and the reason `generate_fingerprint` hashes.
# A readable key long enough to be clamped to this length lets two different
# conditions collide on one identity — which reads as "already proposed" and
# silences the lens that found the second one.
MAX_FINGERPRINT_CHARS = 128

# How long a lens's own sub-condition label may be. Short on purpose: it is a
# classifier ("lines", "review"), not a description.
MAX_KIND_CHARS = 32

# How many named score terms a `landed` row may carry. Comfortably above the
# breakdown's own term count, so adding a term is a code change rather than a
# contract change — and still bounded, because the row is append-only forever.
MAX_SCORE_TERMS = 12

# The one refusal that carries NO new information: the fingerprint is spent, so
# the ledger already holds the row that spent it. Recording it again would add a
# byte-identical row per round forever — and `chain.compute_chain` hashes every
# event with no type filter, so the cost lands on `hive verify` for the life of
# the store.
DUPLICATE_REASON = "already proposed — this fingerprint is spent"


def generate_fingerprint(lens_name, parts) -> str:
    """A bounded, collision-resistant identity for one condition instance.

    Every lens mints its fingerprint through here rather than formatting one
    itself, because a hand-built key is only as bounded as its longest component
    and the contract caps the field at `MAX_FINGERPRINT_CHARS`. The audit lens is
    the proof of what that costs: keyed on `"audit:" + ",".join(uncovered)`, six
    modified source files already produce a 166-character key, `normalise` clamps
    it to 128, and two DIFFERENT change-sets that share a 128-character prefix
    become the SAME fingerprint — so the second one is refused as already
    proposed, for as long as the prefix holds. Silent, and exactly the class of
    failure this module keeps no sidecar in order to avoid.

    A digest is 64 characters whatever it is fed, so the clamp can never fire.

    *parts* are the identity components the LENS chooses, not its evidence list.
    Each lens decides what makes its condition the same condition — a signature
    plus its newest failure, a path plus a line bucket — and that judgement is the
    one thing this function must not second-guess.

    The encoding is LENGTH-PREFIXED, not separator-joined, and that is a
    correctness fix rather than a style preference. Joining on `\\x1f` was safe
    only while no component could contain that byte, which was asserted in prose
    and never enforced — and `repair` derives its components from arbitrary
    captured error text. With a separator, ``("a\\x1fb", "c")`` and
    ``("a", "b\\x1fc")`` produce the identical blob and therefore the identical
    identity, so one condition silently reads as "already proposed" and its lens
    goes quiet about the other. A length prefix cannot be forged by content, so
    the assertion is no longer needed: the encoding simply is injective.

    Parts are still SORTED, deliberately, and that IS a set contract: a key made
    of an unordered collection (`audit` keys on its uncovered file set) must keep
    one identity however its members come out of the fold. The lenses that pass
    components positionally — a signature and its newest failure, a path and a
    bucket — are unaffected, because their components are drawn from disjoint
    vocabularies and cannot swap places.

    Changing the encoding retires every fingerprint minted by the previous one.
    That is a one-time re-proposal of conditions that are genuinely still
    outstanding, which is the correct direction: the alternative is keeping a
    collision path alive to protect a dedup set."""
    items = sorted(str(p) for p in (parts or ()) if str(p).strip())
    blob = "".join(f"{len(item)}\x1f{item}"
                   for item in [str(lens_name or "")] + items)
    # `surrogatepass`, because a strict encode would RAISE here. Parts include
    # signatures derived from arbitrary captured error text, the ledger round-trips
    # a lone surrogate through JSON, and the exception would escape into the
    # per-lens guard — costing that lens every candidate, every round, for as long
    # as the row survives. Identity beats purity: an unpaired surrogate still
    # hashes to one stable digest.
    digest = hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest()
    return digest[:MAX_FINGERPRINT_CHARS]


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
    # Optional, and only the loop surface sets it. It scopes the round counter and
    # the dry streak to ONE run; a session round has no run to belong to.
    run_id = payload.get("run_id")
    if run_id is not None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("work_injected run_id must be non-empty text when "
                             "present")
        if len(run_id) > 64:
            raise ValueError("work_injected run_id exceeds 64 chars")

    if outcome == "dry":
        return

    if outcome == "truncated":
        _require_positive_int(payload, "dropped")
        return

    # landed / refused are per-candidate and carry the candidate's identity.
    _require_str(payload, "lens", 64, what=outcome)
    _require_str(payload, "title", MAX_TITLE_CHARS, what=outcome)
    _require_str(payload, "fingerprint", MAX_FINGERPRINT_CHARS, what=outcome)
    # Optional, and only a lens with more than one condition kind sets it. It
    # carries the readable classification the fingerprint gave up when it became a
    # digest: `tech-debt` proposes both oversized modules and uncleared review
    # entries, and after hashing nothing else on the row tells the two apart.
    kind = payload.get("kind")
    if kind is not None:
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(
                "work_injected kind must be non-empty text when present")
        if len(kind) > MAX_KIND_CHARS:
            raise ValueError(
                f"work_injected kind exceeds {MAX_KIND_CHARS} chars")

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
    # The arithmetic behind `score`, one named term per entry plus `total`.
    # Validated rather than trusted for the same reason as everything else on this
    # row: it is the only durable explanation of why a piece of work was chosen,
    # and a malformed one would be a lie the fold could not detect.
    terms = payload.get("terms")
    if terms is None:
        return
    if not isinstance(terms, dict):
        raise ValueError("work_injected terms must be an object when present")
    if len(terms) > MAX_SCORE_TERMS:
        raise ValueError(f"work_injected terms exceeds {MAX_SCORE_TERMS} entries")
    for name, value in terms.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("work_injected term names must be non-empty text")
        if len(name) > MAX_KIND_CHARS:
            raise ValueError(
                f"work_injected term name exceeds {MAX_KIND_CHARS} chars")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"work_injected term {name!r} must be a whole number — the score "
                f"scale is integers, and a float here would mean some term "
                f"stopped being one")


# --- the score ---------------------------------------------------------------
# A score answers one question: how hard does the record insist this work
# matters? Every term is read off the record rather than judged, so two rounds
# over the same ledger score identically — which is what makes a threshold
# meaningful at all.

BASE_SCORE = 10
RECURRENCE_STEP = 5          # per repeat beyond the first
SEVERITY_BONUS = 20          # a condition that is already blocking something

# The most repeats recurrence will pay for. Unbounded, this one term stopped
# participating in the scale and replaced it: measured on the real ledger, a
# signature seen 263 times carried a recurrence of 1310 and a total of 1118 while
# every other candidate in the round sat between 25 and 63. An audit row reading
# `total 1118` cannot tell a reader whether the record insists on the work or the
# arithmetic ran away, and `explain` exists so that question is answerable.
#
# 20 is DERIVED, not chosen, and the derivation has to live here because the
# smaller numbers look safer and are not. `repair` is the lens with the least
# score support — no severity, and it always sets `observed_ts`, so a signature
# whose newest failure is past `DECAY_STALE_DAYS` keeps only half its history
# weight. At the ceiling that lens scores
# `BASE_SCORE + RECURRENCE_STEP*c - round(RECURRENCE_STEP*c*DECAY_FLOOR)`:
#
#     ceiling  4 ->  20   below conservative's 40: the lens is DEAD there
#     ceiling  8 ->  30   below conservative: DEAD
#     ceiling 12 ->  40   clears conservative by ZERO margin, and fails the
#                         moment the tuner raises the bar
#     ceiling 20 ->  60   clears conservative even at maximum tuner drift
#
# The tuner raising the bar is not hypothetical: `TUNE_RAISE_DELTA` fires after
# three bad outcomes and `TUNE_MAX_DRIFT` allows +20 on top of conservative's 40.
# 20 is the smallest ceiling surviving full decay AND full drift together, which
# is why tightening it for a rounder-looking scale is a regression rather than a
# tidy-up. `test_the_recurrence_ceiling_matches_the_dial` asserts the derivation
# against `PROFILES` so the arithmetic cannot rot into a comment.
#
# The cost is honest: the outlier lands at 95 rather than 61, above the 52-63
# cluster instead of inside it. Worse scale hygiene, better liveness — hygiene is
# a readability preference, a dead lens is a defect this subsystem shipped five
# times before anyone noticed.
RECURRENCE_MAX_STEPS = 20

# Per step of MAGNITUDE — how far past its own limit the condition sits. It is a
# separate term from `RECURRENCE_STEP` for a reason that cost this subsystem a
# whole phase: `occurrences` means "how many times the fold SAW this condition",
# and the tech-debt lens has no occurrences at all — a file has a SIZE. Riding
# size on the recurrence field made a 1940-line module indistinguishable from one
# seen once, scored it at the bare base, and left the condition unable to clear
# any dial position on this repository. Naming the second quantity is the fix;
# tuning the first one's divisor would only have hidden it again.
MAGNITUDE_STEP = 5

# What the agentic lens is worth WHEN THE RECORD SAYS THE SYSTEM IS HEALTHY. The
# only lens-specific term, and it is deliberate rather than an oversight: every
# other term is read off the condition, while this one is read off the run's own
# outcome history. A run whose last `TUNE_LOWER_WINDOW` injections all verified
# green has earned the right to spend attention on improvement instead of repair,
# and without this term a model proposal can never outrank the tech debt that is
# always there. It is zero on an unproven or a struggling run, which is the safe
# direction: innovation is the first thing a strained system should stop doing.
INNOVATION_BONUS = 15

# Resonance: the 1.5x multiplier a fused systemic intervention earns, expressed
# as an ADDITIVE term (half the fused subtotal) so the arithmetic stays one sum of
# named parts. A multiplier applied to the total would make `ScoreBreakdown.total`
# stop equalling its terms, and that identity is the only thing that makes a
# refusal explainable.
RESONANCE_DIVISOR = 2

# Temporal decay. A condition the record proved YESTERDAY and one it proved two
# months ago are not the same claim, and treating them alike lets a long-dead
# signature outrank a live one purely by having recurred more often back then.
# Two anchors, linear between them: fresh work keeps its full history-derived
# weight, work past `DECAY_STALE_DAYS` keeps half. Never less than half — decay
# expresses fading urgency, not disproof, and a condition nothing has addressed is
# still outstanding however old it is.
DECAY_FRESH_DAYS = 1.0
DECAY_STALE_DAYS = 7.0
DECAY_FLOOR = 0.5

# A localised burst of the same failure signature: this many recorded failures
# inside this window. A spike is the one signal that outranks the score floor,
# because "this is breaking right now, repeatedly" is precisely the case the
# floor's "not yet" answer gets wrong.
SPIKE_COUNT = 3
SPIKE_WINDOW_DAYS = 1.0

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
    """Why a candidate scored what it did — every term named, and the sum of the
    named terms IS the total.

    A bare integer makes a refusal unexplainable: "below the minimum" is not
    something a reader can act on, while "base 10, magnitude 15, decay -5" says
    which term decided it. The terms also have to survive past the sum because the
    auto-tuner reasons about them and `hive replenish explain` prints them.

    That the total is a plain sum is a constraint, not an accident. Both later
    terms are conceptually multiplicative — resonance is 1.5x, decay is a
    fraction of the history-derived weight — and both are stored as the ADDITIVE
    difference they work out to, because the moment a multiplier is applied to the
    total instead, `total` stops equalling its parts and the breakdown becomes a
    story about the score rather than the score itself.

    Term ownership, since it is not guessable from the names alone:
      `base`        every admissible candidate pays the same admission price
      `recurrence`  how many times the fold SAW the condition        (the lens)
      `severity`    the condition is already blocking something      (the lens)
      `magnitude`   how far past its own limit the condition sits    (the lens)
      `innovation`  the agentic lens, on a run the record proves healthy (the round)
      `resonance`   half the subtotal of a fused systemic intervention (the round)
      `decay`       negative; the history-derived terms fading with age (the round)
    """

    base: int = BASE_SCORE
    recurrence: int = 0
    severity: int = 0
    magnitude: int = 0
    innovation: int = 0
    resonance: int = 0
    decay: int = 0
    veto_reason: str = ""

    @property
    def vetoed(self) -> bool:
        return bool(self.veto_reason)

    @property
    def history_terms(self) -> int:
        """The part of the score read off past ledger rows, which is the part age
        is allowed to discount. `base` is the flat admission price and `magnitude`
        is measured off the tree as it stands now — neither gets older."""
        return self.recurrence + self.severity

    @property
    def total(self) -> int:
        if self.vetoed:
            return 0
        # Clamped at zero: decay is the one negative term, and a score below zero
        # would compare strangely against a threshold whose floor is `BASE_SCORE`.
        return max(0, self.base + self.recurrence + self.severity
                   + self.magnitude + self.innovation + self.resonance
                   + self.decay)

    def as_terms(self) -> dict:
        """The non-zero terms, for the audit row and the explain surface.

        Zeroes are dropped because a row listing six terms that did nothing buries
        the one that decided the outcome — and the payload has a size contract to
        respect. `total` rides along so a reader never has to re-add them."""
        terms = {"base": self.base, "recurrence": self.recurrence,
                 "severity": self.severity, "magnitude": self.magnitude,
                 "innovation": self.innovation, "resonance": self.resonance,
                 "decay": self.decay}
        out = {name: value for name, value in terms.items() if value}
        out["total"] = self.total
        return out


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


def _whole(value, default=0) -> int:
    """*value* as a non-negative int, or *default*. Total on junk.

    A lens is the one thing here not validated at a boundary, so a lens returning
    ``None``, a bool, or a float must degrade to the default rather than poison
    the arithmetic — `bool` explicitly, because it is an `int` subclass and `True`
    would otherwise read as a legitimate count of one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _parse_ts(text):
    """One ledger timestamp as a datetime, or ``None``.

    Total on anything unparseable, and that matters more than it looks: the
    ledger holds timestamps written by every past version of this code, and the
    caller's response to ``None`` is "apply no decay" — the conservative
    direction, since decay can only ever lower a score."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def age_in_days(observed_ts, now_ts):
    """How old *observed_ts* is, in days, or ``None`` if either is unreadable.

    Negative ages are clamped to zero rather than rejected: a row stamped
    slightly in the future is a clock skew, not evidence of extra urgency."""
    observed, now = _parse_ts(observed_ts), _parse_ts(now_ts)
    if observed is None or now is None:
        return None
    if (observed.tzinfo is None) != (now.tzinfo is None):
        # One naive, one aware — subtracting them raises. The ledger writes UTC
        # with a `Z`, so a naive row is a legacy one; read it as UTC rather than
        # discarding it.
        from datetime import timezone
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        else:
            now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - observed).total_seconds() / 86400.0)


def count_within(timestamps, anchor, window_days) -> int:
    """How many of *timestamps* fall in the *window_days* leading up to *anchor*.

    The shared shape behind every "is this a burst or a dribble" question in this
    module. An unreadable stamp counts as outside the window, always: both callers
    use the result to RAISE a score, so an ambiguous row must never help.

    Both callers anchor on the newest stamp in the set they are measuring, which
    is what makes this clock-free — "how much of this happened close together"
    is answerable without knowing when now is. How long ago the burst itself was
    is a separate question, and the decay term is what answers it."""
    total = 0
    for stamp in timestamps or ():
        age = age_in_days(stamp, anchor)
        if age is not None and age <= window_days:
            total += 1
    return total


def decay_factor(age_days) -> float:
    """What fraction of its history-derived weight a condition this old keeps.

    ``1.0`` up to `DECAY_FRESH_DAYS`, `DECAY_FLOOR` from `DECAY_STALE_DAYS`
    onward, linear between. An unknown age keeps full weight — decay only ever
    lowers a score, so "I could not tell how old this is" must not silently
    suppress work."""
    if age_days is None or age_days <= DECAY_FRESH_DAYS:
        return 1.0
    if age_days >= DECAY_STALE_DAYS:
        return DECAY_FLOOR
    span = DECAY_STALE_DAYS - DECAY_FRESH_DAYS
    travelled = (age_days - DECAY_FRESH_DAYS) / span
    return 1.0 - travelled * (1.0 - DECAY_FLOOR)


def score_candidate(candidate, index=None, *, healthy=False,
                    now_ts="") -> ScoreBreakdown:
    """Score one candidate against the record. Pure, and total on junk input.

    *healthy* and *now_ts* come from the ROUND, not the candidate: whether the
    run's recent injections verified green, and what "now" is for the decay
    clock. Both are parameters rather than reads of the wall clock or the ledger
    so that scoring stays a pure function — two rounds over the same inputs must
    score identically, which is the whole reason a threshold means anything.

    Resonance is NOT set here. It is a property of a candidate's relationship to
    the other candidates in the same round, which one candidate cannot see;
    `resonate` computes it and rewrites the breakdown."""
    # Clamped on the STEPS, not on the product, so the ceiling reads in the unit
    # `RECURRENCE_MAX_STEPS` is named for. A candidate seen 263 times and one seen
    # 21 times pay the same recurrence: past the ceiling the record has already
    # insisted as hard as this scale can express.
    steps = min(RECURRENCE_MAX_STEPS,
                max(0, _whole(getattr(candidate, "occurrences", 1), 1) - 1))
    recurrence = RECURRENCE_STEP * steps
    severity = SEVERITY_BONUS if getattr(candidate, "severity", False) else 0
    magnitude = MAGNITUDE_STEP * _whole(getattr(candidate, "excess_steps", 0))
    innovation = (INNOVATION_BONUS
                  if healthy and getattr(candidate, "lens", "") == "innovation"
                  else 0)

    breakdown = ScoreBreakdown(
        recurrence=recurrence, severity=severity, magnitude=magnitude,
        innovation=innovation,
        veto_reason=index.veto_reason_for(candidate) if index is not None else "")

    # Decay is charged against the history-derived terms only, and only for a
    # candidate that told us WHEN its evidence was recorded. The tree-reading
    # lenses set no `observed_ts` because a file's current line count has no age.
    # Applied as a second step so the term it discounts is named by the record it
    # comes off — `history_terms` — rather than re-added by hand here.
    factor = decay_factor(age_in_days(getattr(candidate, "observed_ts", ""),
                                      now_ts))
    if factor >= 1.0:
        return breakdown
    return replace(breakdown,
                   decay=-round(breakdown.history_terms * (1.0 - factor)))


# --- the core ----------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One proposal from one lens.

    `evidence` is what makes it admissible: a pointer to a real ledger row, file,
    or signature that a reader can go and check. `fingerprint` identifies the
    CONDITION INSTANCE, not the condition — keyed on the bare lens name a
    candidate would fire once ever, and keyed on something that changes every
    round it would never dedup.

    Seven fields are set by the LENS, because only the lens knows them:

      `occurrences`   how many times its fold saw this condition — and ONLY
                      that. It is not a slot for any other quantity a lens would
                      like scored; the phase that made file size ride here is why
                      `excess_steps` exists.
      `excess_steps`  how far past its own limit the condition sits, in whatever
                      unit the lens counts (line-blocks for tech-debt). A lens
                      with no notion of "too big" leaves it at 0.
      `severity`      whether what it found is already blocking something
      `observed_ts`   when the record recorded this, for the decay clock. Set by
                      the lenses that read HISTORY; left blank by the ones that
                      read the tree, because a file's current size has no age.
      `spike`         a burst of the same condition happening right now, which is
                      the one signal allowed past the score floor
      `deferral_key`  what the record would call this, so a standing deferral can
                      be matched — and, when two lenses name the same one, what
                      makes them resonate
      `kind`          which of its own condition kinds this is, for a lens that
                      has more than one

    Three are set by the ROUND, overwriting whatever a lens put there:
    `breakdown`, `score` and `veto_reason`. Scoring lives in the round rather
    than the lens so one scale governs every lens; a lens that scored itself
    would make the threshold meaningless the moment two lenses disagreed about
    what 25 meant. `score` duplicates `breakdown.total` deliberately — the gate
    and the sink compare it on every candidate, and reaching through an optional
    record to do that would put a `None` check on the hot path."""

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
    # Lens-set, but declared last so adding them cannot shift any positional
    # construction of the fields above.
    kind: str = ""
    excess_steps: int = 0
    observed_ts: str = ""
    spike: bool = False
    breakdown: object = None
    # The lenses a FUSED candidate was built from; empty for one that came from a
    # single lens. It is what lets a reader ask which dial positions could have
    # produced this row, since `lens` on a fused candidate names no registered
    # lens at all.
    members: tuple = ()


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
    # Per-run ceilings, both 0 = unlimited. They are separate from
    # `max_per_round` because they answer a different question: that one bounds
    # one round's noise, these bound a whole run's appetite. A session round
    # leaves them off — a session runs one round and its own lifecycle ends it.
    max_injections_per_run: int = 0
    max_rounds_per_run: int = 0
    # 0 DISABLES the dry-round exit, and that is the right setting for any
    # surface whose own lifecycle already bounds the rounds. The exit exists for
    # a loop, which runs many rounds back to back and must stop spinning. On a
    # surface that runs one round per session it is actively wrong: nothing can
    # reset the counter except work landing, and work can only land if a round
    # runs, so two quiet sessions would switch the injector off permanently.
    dry_rounds_to_exit: int = 0

    @classmethod
    def for_profile(cls, profile, *, enabled=True, dry_rounds_to_exit=0,
                    max_rounds_per_run=0, bounded_run=False,
                    min_score=None, max_per_round=None):
        """A round's config, taken from an ambition position.

        `bounded_run` is what a loop passes. It turns the profile's per-run cap
        into a real budget folded from the run's landed rows, on top of the
        per-round trim — one dial, two enforcement points, so five rounds cannot
        inject five times the cap. A session leaves it off: a session runs one
        round and its own lifecycle is the bound.

        `min_score` and `max_per_round` are the operator's escape hatch: the dial
        sets both, and an explicit value in `loop.toml` overrides the one the
        position chose — which is how an operator keeps `ambitious`'s lens set
        while raising its bar. `None` means "take the profile's", and it has to be
        `None` rather than 0 because 0 is itself a legal `min_score`, meaning no
        floor at all.

        An explicit `max_per_round` trims the ROUND only. The run's total stays
        the dial's, because the two caps answer different questions (see the field
        comments) and an operator widening one round has not thereby agreed to
        five times the run's appetite."""
        return cls(enabled=enabled,
                   min_score=(profile.min_score if min_score is None
                              else min_score),
                   max_per_round=(profile.max_injections_per_run
                                  if max_per_round is None else max_per_round),
                   max_injections_per_run=(profile.max_injections_per_run
                                           if bounded_run else 0),
                   max_rounds_per_run=max_rounds_per_run,
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
    "ambitious": AmbitionProfile("ambitious", 15,
                                 _ALL_DETERMINISTIC + ("innovation",), 25),
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


# --- the auto-tuner -----------------------------------------------------------
# Homeostasis, not learning. The threshold moves in response to what injected work
# actually DID, on two windows chosen so the two directions are asymmetric: it
# takes 3 bad outcomes to become more cautious and 5 good ones to become bolder.
# Retreating faster than it advances is the correct bias for a mechanism that can
# spend an operator's quota.

TUNE_RAISE_WINDOW = 3
TUNE_RAISE_DELTA = 10
TUNE_LOWER_WINDOW = 5
TUNE_LOWER_DELTA = -5

# How far the tuner may wander from the dial's own number. Without a ceiling a
# long unlucky streak could raise the threshold past any candidate's reachable
# score and switch the injector off in a way no operator chose.
TUNE_MAX_DRIFT = 20


@dataclass(frozen=True)
class ThresholdAdjustment:
    """A shift in how selective the gate is, and why.

    `delta` of 0 means the record said nothing conclusive, which is the common
    case and must stay distinguishable from "the record said stay put" — the
    caller writes a `decision` row only when something moved."""

    delta: int = 0
    reason: str = ""
    window: int = 0

    @property
    def shifts(self) -> bool:
        return self.delta != 0


@dataclass(frozen=True)
class AutoTuningProfile:
    """What injected work actually produced, as two streaks.

    This is the record's answer to "is this system currently coping?", and it
    drives three separate decisions, which is why it is a named record rather
    than a number computed at each site: how selective the gate becomes
    (`adjustment`), whether the agentic lens earns its bonus (`healthy`), and
    whether a run claiming to be finished still has capacity it never spent (the
    loop's anti-lazy hold).

    Streaks, not totals. A project with two hundred green verifications and three
    fresh failures is a struggling system, and any measure that averaged over its
    whole history would say the opposite — the only honest question is what
    happened LAST."""

    success_streak: int = 0
    failure_streak: int = 0
    # Every resolved outcome, oldest first. Kept so `hive replenish explain` can
    # show the window the tuner read rather than only its verdict.
    outcomes: tuple = ()

    @property
    def healthy(self) -> bool:
        return self.success_streak >= TUNE_LOWER_WINDOW

    @property
    def strained(self) -> bool:
        return self.failure_streak >= TUNE_RAISE_WINDOW

    @property
    def adjustment(self) -> ThresholdAdjustment:
        """How far the gate should move. Strain is checked FIRST: the two streaks
        cannot both be satisfied, but stating the precedence makes the bias
        explicit rather than an accident of evaluation order."""
        if self.strained:
            return ThresholdAdjustment(
                TUNE_RAISE_DELTA,
                f"the last {TUNE_RAISE_WINDOW} injections were each followed by "
                f"a failure or a tripped breaker — raising the bar",
                TUNE_RAISE_WINDOW)
        if self.healthy:
            return ThresholdAdjustment(
                TUNE_LOWER_DELTA,
                f"the last {TUNE_LOWER_WINDOW} injections each verified green — "
                f"lowering the bar",
                TUNE_LOWER_WINDOW)
        return ThresholdAdjustment()


def iteration_outcome(ev) -> str:
    """What one event says happened: "failed", "verified", or "" for neither.

    Split out of the fold so that "which events count as a verdict on injected
    work" has exactly one definition. A `verification` row is judged on its exit
    code, per `verify.py`'s own rule that the stamp attaches to the code and not
    to the parsed counts."""
    etype = getattr(ev, "type", None)
    payload = getattr(ev, "payload", None)
    if etype == "tool_failed":
        return "failed"
    if etype == "loop_run_end" and isinstance(payload, dict) \
            and payload.get("reason") == "breaker_open":
        return "failed"
    if etype == "verification" and isinstance(payload, dict):
        code = payload.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool):
            return "verified" if code == 0 else "failed"
    return ""


def _streaks(outcomes) -> tuple:
    """``(success_streak, failure_streak)`` — the trailing run of each verdict."""
    success = failure = 0
    for verdict in reversed(outcomes):
        if verdict == "verified" and failure == 0:
            success += 1
        elif verdict == "failed" and success == 0:
            failure += 1
        else:
            break
    return success, failure


@dataclass(frozen=True)
class History:
    """The injector's state, recovered from the ledger alone."""

    fingerprints: frozenset = frozenset()
    last_round: int = 0
    dry_rounds: int = 0
    # How much work actually landed in the scope this was folded for. The per-run
    # injection cap is enforced against this rather than against a counter held in
    # memory, so a run that resumes after an interruption inherits its own budget
    # instead of starting over with a full one.
    landed_count: int = 0
    # What those injections then produced. Folded in the SAME pass rather than by
    # a second walk: resolving each landed row's outcome by re-scanning the ledger
    # from its timestamp is quadratic, and this store is already past 28k events.
    tuning: AutoTuningProfile = AutoTuningProfile()


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
    # What the round calls "now". Injected rather than read from the wall clock so
    # a lens that reasons about recency — is this failure signature spiking? — can
    # be driven to a fixed instant by a test instead of to whenever the suite ran.
    now_ts: str = ""
    # Paths the surface knows changed, and the model seam. Both exist for the one
    # agentic lens: the paths are the only evidence it is allowed to cite, and the
    # seam is how the suite drives it without a model call.
    changed_paths: tuple = ()
    runner: object = None


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

    *run_id* narrows that same pair to ONE run, and shipping it is a prerequisite
    of enabling the dry-round exit for the loop. Without it the streak folds over
    the surface's whole history and only a landed row resets it, so the first run
    that went quiet twice would disable `surface="loop"` project-wide and
    permanently — strictly worse than having no exit at all."""
    spent, last, dry, landed = set(), 0, 0, 0
    # The tuner's half of the fold. `awaiting` counts landed rows that have not yet
    # been answered by anything; the next verdict-bearing event answers all of them
    # at once, which is what "the FIRST outcome after this injection" means when
    # the walk is forward-only.
    awaiting, outcomes = 0, []
    for ev in evs or []:
        if getattr(ev, "type", None) != "work_injected":
            verdict = iteration_outcome(ev)
            if verdict and awaiting:
                outcomes.extend([verdict] * awaiting)
                awaiting = 0
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
        # Tuning is scoped by SURFACE and never by run: a run asks what this
        # surface's injections have been producing lately, and its own first
        # injection has by definition produced nothing yet. Narrowing to the run
        # would make the tuner permanently silent on every fresh run.
        if outcome == "landed":
            awaiting += 1
        if run_id is not None and p.get("run_id") != run_id:
            continue
        r = p.get("round")
        if isinstance(r, int) and not isinstance(r, bool) and r > last:
            last = r
        if outcome == "dry":
            dry += 1
        elif outcome == "landed":
            dry = 0            # real work landed: the streak is broken
            landed += 1

    success, failure = _streaks(outcomes)
    return History(frozenset(spent), last, dry, landed,
                   AutoTuningProfile(success, failure, tuple(outcomes)))


def tuning_profile(evs, surface=None) -> AutoTuningProfile:
    """What this surface's injections have been producing lately.

    A thin name over the fold, because three callers want the profile and only
    one of them wants the rest of the `History` record."""
    return history(evs, surface=surface).tuning


def tune_threshold(evs, surface=None) -> ThresholdAdjustment:
    """How far the gate should move, read off what injected work produced.

    Pure and total: it appends nothing and raises on nothing, so a caller can ask
    without committing. A malformed row is skipped like everywhere else — the
    ledger holds history written by every past version of this code."""
    return tuning_profile(evs, surface=surface).adjustment


def tuned_threshold(base, adjustment) -> int:
    """*base* shifted by *adjustment*, clamped twice.

    `MIN_THRESHOLD` is the hard floor — below the base score every candidate any
    lens emitted would clear the gate. `TUNE_MAX_DRIFT` bounds the other end so an
    unlucky streak cannot raise the bar out of every candidate's reach and switch
    the injector off in a way no operator chose."""
    delta = max(-TUNE_MAX_DRIFT, min(TUNE_MAX_DRIFT, adjustment.delta))
    return max(MIN_THRESHOLD, base + delta)


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
    if candidate.score < cfg.min_score and not candidate.spike:
        # The spike bypass, and the ONLY rule allowed past. A spike means the same
        # condition is recurring right now, in a burst — which is exactly the case
        # the floor's answer ("not yet, the record has not insisted hard enough")
        # gets wrong, because the record is insisting as we read it. It bypasses
        # this rule alone: a spiking candidate with no evidence, a spent identity,
        # a standing deferral or an illegal depth is still refused above, because
        # none of those become acceptable for being urgent.
        return (f"score {candidate.score} below the round's minimum "
                f"{cfg.min_score}")
    return ""


# The one refusal on that ladder that is NOT a decision. Every other reason —
# spent, no evidence, declined, too deep — is an answer that will still be true
# next round. "Below the bar" is "not yet": the score is read off the record, and
# the record grows.
#
# The distinction is load-bearing because `history()` folds every RECORDED refusal
# into the spent set. Writing a row for a below-the-bar candidate therefore SPENDS
# its identity, and the spent check runs FIRST — so the candidate can never be
# proposed again even once its score clears the threshold, and the duplicate
# refusal that blocks it writes no row, leaving nothing on the ledger to say why.
# Demonstrated: a module refused at score 15 under a threshold of 25 was still
# refused after eight further changes had taken it to 55.
#
# It is also what made refusal rows unbounded: one balanced round against the real
# ledger wrote 257 of them, and `repair` mints a fresh identity on every
# recurrence, so each future failure would add another.
_SCORE_FLOOR_RE = re.compile(r"^score \S+ below the round's minimum \S+$")


def is_transient_refusal(reason) -> bool:
    """Whether *reason* means "not yet" rather than "no".

    A transient refusal is returned to the caller but never recorded, so it cannot
    spend the fingerprint it refused."""
    return bool(_SCORE_FLOOR_RE.match(str(reason or "")))


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
                   fingerprint=str(candidate.fingerprint)[:MAX_FINGERPRINT_CHARS],
                   # Stripped, not merely clamped: `validate_payload` rejects a
                   # present-but-blank `kind`, while `_row` only drops "" — so a
                   # whitespace-only kind passed both and then failed validation
                   # AFTER the sink had written the work, which is exactly the
                   # two-phase write window this function exists to close.
                   kind=str(candidate.kind or "").strip()[:MAX_KIND_CHARS],
                   evidence=evidence)


# How many constituent titles and evidence lines ride a fused candidate. The row
# has a hard contract cap; titles come first because they say what the lenses
# actually found, and raw evidence fills whatever is left.
_RESONANCE_TITLES = 4


def resonate(scored, spent=frozenset()) -> list:
    """Fuse candidates that independently name the same target into one task.

    A module that is simultaneously oversized, untested and implicated in a
    recorded failure is not three pieces of work — it is one, and the three lenses
    agreeing is itself the strongest evidence in the round. Left unfused they
    compete: each carries its own middling score, each is capped separately, and
    the per-round trim can drop two of the three so the plan never shows that they
    were the same file.

    Fusing is decided on `deferral_key`, which already means "what the record
    would call this" and is therefore the only field in a `Candidate` that two
    different lenses can be expected to spell the same way. Agreement must come
    from DISTINCT lenses: one lens emitting two conditions about one path is that
    lens's own business, not corroboration.

    Three exclusions, each for a different reason. A VETOED candidate is left out
    because a fused row would swallow the record's own words declining it. A SPENT
    one is left out because it is already refused as a duplicate, and folding it in
    would let a dead condition inflate a live one's score. A candidate with no
    `deferral_key` names no target, so there is nothing for it to agree with.

    Order is preserved for everything that does not fuse, and each fused
    candidate takes the position of its first constituent, so the registry order
    the round relies on for stable tie-breaking survives."""
    groups, order = {}, []
    for candidate in scored:
        if not isinstance(candidate, Candidate):
            continue
        target = str(candidate.deferral_key or "").strip()
        if (not target or candidate.veto_reason
                or candidate.fingerprint in spent):
            continue
        if target not in groups:
            order.append(target)
        groups.setdefault(target, []).append(candidate)

    fused_by_target = {}
    for target in order:
        members = groups[target]
        if len({m.lens for m in members}) < 2:
            continue                # one lens agreeing with itself is not evidence
        fused_by_target[target] = _fuse(target, members)

    if not fused_by_target:
        return list(scored)

    absorbed = {id(m) for members in
                (groups[t] for t in fused_by_target) for m in members}
    out, emitted = [], set()
    for candidate in scored:
        if id(candidate) not in absorbed:
            out.append(candidate)
            continue
        target = str(candidate.deferral_key or "").strip()
        if target not in emitted:
            emitted.add(target)
            out.append(fused_by_target[target])
    return out


def _fuse(target, members) -> Candidate:
    """One systemic intervention standing in for *members*, at 1.5x their sum."""
    lenses = sorted({m.lens for m in members})
    parts = [m.breakdown if isinstance(m.breakdown, ScoreBreakdown)
             else ScoreBreakdown() for m in members]
    combined = ScoreBreakdown(
        base=sum(p.base for p in parts),
        recurrence=sum(p.recurrence for p in parts),
        severity=sum(p.severity for p in parts),
        magnitude=sum(p.magnitude for p in parts),
        innovation=sum(p.innovation for p in parts),
        decay=sum(p.decay for p in parts))
    # The multiplier, stored as the addition it works out to (see
    # `RESONANCE_DIVISOR`). Read off `combined.total` rather than re-adding the
    # terms here, so there is one definition of what the terms sum to.
    combined = replace(combined,
                       resonance=combined.total // RESONANCE_DIVISOR)

    evidence = [f"[{m.lens}] {m.title}" for m in members[:_RESONANCE_TITLES]]
    for member in members:
        evidence.extend(member.evidence)
    # Deduplicated in first-seen order: two lenses looking at one file routinely
    # cite the same path, and a row that repeats it wastes the evidence cap.
    seen, unique = set(), []
    for item in evidence:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    return Candidate(
        # Not a registered lens, and deliberately so: this candidate came from the
        # round noticing agreement, not from any one lens, and attributing it to a
        # member would misreport which fold found it.
        lens="resonance",
        kind="systemic",
        title=(f"Systemic intervention on {target} — {len(lenses)} lenses agree "
               f"({', '.join(lenses)})")[:MAX_TITLE_CHARS],
        # Keyed on the target AND every constituent identity, so the fused task is
        # re-proposable the moment any member becomes a different instance — which
        # is the same rule each member follows on its own.
        fingerprint=generate_fingerprint(
            "resonance", [target] + sorted(m.fingerprint for m in members)),
        evidence=tuple(unique[:MAX_EVIDENCE_ITEMS]),
        severity=any(m.severity for m in members),
        spike=any(getattr(m, "spike", False) for m in members),
        # The deepest member's depth: fusing must not launder a too-deep
        # proposal past the depth stop by averaging it with shallower ones.
        depth=max(m.depth for m in members),
        deferral_key=target,
        members=tuple(lenses),
        breakdown=combined,
        score=combined.total)


def _rank(kept) -> list:
    """Order survivors so the cap keeps the best work from EVERY lens.

    Score ordering came first and was necessary. Trimmed in gate order the cap
    cut by lens registration instead: measured against the real ledger under
    `balanced`, 22 candidates cleared the gate and `kept[:10]` kept ten `repair`
    rows scoring 25-65 while dropping a repair scoring 1320, an evidence-gap
    scoring 100, and every pinned caveat.

    But score ALONE lets one lens take the whole round, and on this ledger it
    does. `repair` reads occurrence counts in the hundreds, so even bounded by
    `RECURRENCE_MAX_STEPS` its subtotal dwarfs every other lens's. Measured the
    same way, a balanced round gave 7 of its 10 slots to `repair` and **zero to
    `audit` and `coverage`** — five lenses answered their questions and were cut
    before anyone read them. A round that is one lens is not a round; it is the
    loudest fold repeated ten times.

    So: rank within each lens by score, then take round-robin across lenses. The
    highest-scoring candidate overall still goes first, and the cap still cuts
    the least-insisted-on work WITHIN each lens — but every lens gets its best
    find in before any lens gets its second.

    Fused candidates ride as one group: `_fuse` gives every one of them
    `lens="resonance"`, which is correct here — the round noticing agreement is
    itself a distinct source, and it competes as one rather than crowding the
    rotation with a bucket per target.

    Deterministic: lenses are ordered by their best candidate's score, ties
    broken on the lens name, so the same ledger produces the same round."""
    by_lens: dict = {}
    for candidate in kept:
        by_lens.setdefault(candidate.lens, []).append(candidate)
    for group in by_lens.values():
        # Stable, so equal scores keep gate (registry) order within a lens.
        group.sort(key=lambda c: c.score, reverse=True)
    order = sorted(by_lens, key=lambda name: (-by_lens[name][0].score, name))
    out = []
    for i in range(max((len(g) for g in by_lens.values()), default=0)):
        for name in order:
            if i < len(by_lens[name]):
                out.append(by_lens[name][i])
    return out


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


def _row(outcome, round_no, surface, run_id="", **extra) -> dict:
    payload = {"outcome": outcome, "round": round_no, "surface": surface}
    if run_id:
        # Omitted rather than written empty: the contract treats a present
        # `run_id` as a scoping key, and "" would make every session round look
        # like one run.
        payload["run_id"] = run_id
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


def _record_tuning(adjustment, effective, surface, project=None,
                   session_id=None) -> None:
    """Put a threshold shift on the record as a `decision`.

    Only when something moved. A row per round would flood the ledger and every
    future briefing (SOP `memo-hygiene` §3), and "the tuner looked and did
    nothing" is not a decision. Best-effort: a round must not fail because it
    could not narrate itself."""
    try:
        ev_mod.append_event(
            "decision",
            {"text": f"replenish value gate {'raised' if adjustment.delta > 0 else 'lowered'} "
                     f"by {abs(adjustment.delta)} to {effective} on the {surface} "
                     f"surface: {adjustment.reason} (window: last "
                     f"{adjustment.window} injections)"[:500]},
            session_id=session_id, tags=["replenish", "auto-tune"],
            project=project)
    except Exception:                                           # noqa: BLE001
        pass


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
              session_id=None, append=True, ctx=None, run_id="",
              tune=False, now_ts="") -> RoundResult:
    """Run one replenishment round and record what it decided.

    *sink* is a parameter, not an import: deciding what is outstanding is
    surface-independent, forcing it is not. It is called with the kept
    candidates and returns ``(placements, dropped)``.

    A candidate the sink could not place gets NO row, so its fingerprint stays
    unspent and it can be proposed again next round. Same for candidates the cap
    cut — a truncation is "not now", not "never".

    *run_id* scopes the round counter, the dry streak and the injection budget to
    one loop run. Three of the four structural stops are enforced here — the dry
    streak, the per-run injection cap, and the per-run round ceiling — and all
    three are folded from `work_injected` rows rather than counted in memory, so
    a run that resumes after an interruption inherits its own spent budget
    instead of starting again with a full one. The fourth stop is depth, which
    lives in the gate because it is a property of a candidate."""
    cfg = cfg or ReplenishConfig()
    if not cfg.enabled:
        return RoundResult(skipped="disabled")

    scope = run_id or None
    hist = history(evs, surface=surface, run_id=scope)
    if cfg.dry_rounds_to_exit > 0 and hist.dry_rounds >= cfg.dry_rounds_to_exit:
        return RoundResult(round_no=hist.last_round,
                           skipped="dry rounds exhausted")
    if cfg.max_rounds_per_run > 0 and hist.last_round >= cfg.max_rounds_per_run:
        # A run that has already mined this many times is one whose plan the
        # operator should look at before it grows again.
        return RoundResult(round_no=hist.last_round,
                           skipped="injection rounds exhausted")
    injection_budget = (cfg.max_injections_per_run - hist.landed_count
                        if cfg.max_injections_per_run > 0 else None)
    if injection_budget is not None and injection_budget <= 0:
        return RoundResult(round_no=hist.last_round,
                           skipped="injection budget exhausted")
    round_no = hist.last_round + 1

    # Homeostasis. Off by default because a surface that runs one round per
    # session has no outcome history to tune on; the loop turns it on. The
    # profile comes off the fold already done above rather than a second pass —
    # `history` scopes its tuning half by surface alone, which is exactly the
    # scope the tuner wants.
    adjustment = hist.tuning.adjustment if tune else ThresholdAdjustment()
    if adjustment.shifts:
        cfg = replace(cfg, min_score=tuned_threshold(cfg.min_score, adjustment))
        if append:
            _record_tuning(adjustment, cfg.min_score, surface,
                           project=project, session_id=session_id)

    # One clock for the whole round, read once. Passed down rather than read
    # inside the lenses and the scorer so that both stay pure functions of their
    # arguments — a round that scored against the wall clock could not be replayed
    # from the ledger, and replayability is what makes the threshold auditable.
    clock = now_ts or ev_mod.now_iso()

    # One context for the whole round: the lenses that read a tree or a project
    # get those from here rather than resolving them, which is what makes them
    # testable without the real repo.
    lens_ctx = ctx if ctx is not None else _as_context(evs, project)
    if not lens_ctx.now_ts:
        # A caller that pinned its own instant keeps it; everyone else inherits
        # the round's, so "now" means one thing across every lens in the round.
        lens_ctx = replace(lens_ctx, now_ts=clock)

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
    healthy = hist.tuning.healthy
    scored = []
    for candidate in candidates:
        if not isinstance(candidate, Candidate):
            scored.append(candidate)        # gate() is where junk gets dropped
            continue
        breakdown = score_candidate(candidate, deferrals, healthy=healthy,
                                    now_ts=clock)
        scored.append(replace(candidate, score=breakdown.total,
                              veto_reason=breakdown.veto_reason,
                              breakdown=breakdown))

    # Cross-lens synthesis, AFTER scoring because a fused score is the sum of its
    # members' scores, and BEFORE the gate because fusing changes what the gate is
    # deciding about. The spent set rides along so a dead constituent cannot
    # inflate a live one (see `resonate`).
    scored = resonate(scored, hist.fingerprints)

    kept, refused = gate(scored, hist.fingerprints, cfg)
    # R16 asks whether anything QUALIFIED, which is decided here. A round the cap
    # trimmed or the sink could not place found real work and must not report
    # itself dry — the CLI was printing "N candidate(s) did not fit" and
    # "nothing outstanding" in the same breath.
    dry = not kept
    dropped = 0
    # Two ceilings, smaller wins: what one round may propose, and what is left of
    # the run's whole budget. Without the second, five rounds could each inject a
    # full round's worth and blow the dial's per-run cap five times over.
    ceiling = cfg.max_per_round
    if injection_budget is not None:
        ceiling = min(ceiling, injection_budget)
    if len(kept) > ceiling:
        kept = _rank(kept)
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
        rows.append(_row("landed", round_no, surface, run_id, lens=c.lens,
                         title=c.title[:MAX_TITLE_CHARS],
                         fingerprint=c.fingerprint,
                         evidence=[str(e)[:MAX_EVIDENCE_CHARS]
                                   for e in c.evidence][:MAX_EVIDENCE_ITEMS],
                         score=c.score, kind=c.kind, plan_id=pl.plan_id,
                         # Why it scored what it did, term by term. The bare score
                         # says a candidate cleared the bar; only the terms say
                         # whether it did so on magnitude, on a recurrence the
                         # record kept adding to, or on three lenses resonating —
                         # and that is the difference between an auditable
                         # decision and a number nobody can check.
                         terms=(c.breakdown.as_terms()
                                if isinstance(c.breakdown, ScoreBreakdown)
                                else None),
                         step_index=pl.step_index if pl.step_index >= 0 else None))
    for c, reason in refused:
        if reason == DUPLICATE_REASON:
            continue          # the row that spent this fingerprint IS the record
        if is_transient_refusal(reason):
            # "Not yet" is not a decision, and recording it would spend the
            # fingerprint (see `is_transient_refusal`). The refusal still rides
            # `RoundResult.refused`, so the CLI keeps printing it — only the
            # ledger row is skipped.
            continue
        if not c.fingerprint:
            # Nothing to dedup on, so this row would repeat every round for as
            # long as the lens keeps emitting it. A fingerprint-less candidate
            # is a lens defect; the lens-error channel above is where that
            # belongs, not an unbounded run of identical refusals.
            continue
        rows.append(_row("refused", round_no, surface, run_id, lens=c.lens,
                         title=c.title or "(untitled)",
                         fingerprint=c.fingerprint, kind=c.kind,
                         reason=reason[:MAX_REASON_CHARS]))
    if dropped:
        # R15: a cap that cut silently would read as "nothing more was found".
        rows.append(_row("truncated", round_no, surface, run_id, dropped=dropped))
    # The dry row exists to feed the dry-round exit. Where that exit is off, it
    # is a row nobody reads, appended once per session forever — so it is only
    # written when something actually consumes it.
    if dry and cfg.dry_rounds_to_exit > 0:
        rows.append(_row("dry", round_no, surface, run_id))

    if append:
        _record(rows, project=project, session_id=session_id)
        _record_lens_errors(lens_errors, surface, project=project,
                            session_id=session_id)

    return RoundResult(round_no=round_no, landed=landed, refused=refused,
                       dropped=dropped, dry=dry, lens_errors=lens_errors)


# --- explaining a round without running one -----------------------------------

def landing_positions(candidate) -> list:
    """Dial positions that would both RUN and ADMIT this candidate.

    Two conditions, and only checking the second is the mistake that makes a
    report misleading: a tech-debt candidate scoring 45 does not land at
    `conservative`, because `conservative` never runs the tech-debt lens at all.
    A fused candidate needs every lens it was built from, since a position that
    ran only some of them would have produced a different, smaller fusion."""
    needed = set(getattr(candidate, "members", ()) or (candidate.lens,))
    return sorted(
        p.name for p in PROFILES.values()
        if needed <= set(p.lens_names) and candidate.score >= p.min_score)


def explain(evs, *, ctx=None, project=None, lenses=None, now_ts="",
            tune=False) -> dict:
    """What a round WOULD decide, computed without deciding it.

    The tool that would have caught this subsystem's recurring failure in one
    command. A dead lens is invisible from a round's output — it simply produces
    nothing, exactly like a lens with nothing to report — and invisible from the
    ledger, because a below-the-bar refusal is deliberately not recorded (writing
    it would spend the fingerprint it refused). The only place the difference
    shows is a per-candidate score next to the thresholds it was measured
    against, which is what this returns.

    Side-effect free, and that is a contract rather than an implementation
    detail: no sink is called, no event is appended, and no fingerprint is spent.
    A diagnostic that changed what it was diagnosing would be worse than none —
    running it once would silence every condition it reported.

    The agentic lens is NOT in the default set. It spends a model call, and an
    operator asking what the record currently holds has not thereby asked to pay
    for a proposal. Pass it explicitly in *lenses* to include it."""
    hist = history(evs)
    clock = now_ts or ev_mod.now_iso()
    lens_ctx = ctx if ctx is not None else _as_context(evs, project)
    if not lens_ctx.now_ts:
        lens_ctx = replace(lens_ctx, now_ts=clock)
    registry = lenses if lenses is not None else {
        name: LENSES[name] for name in _ALL_DETERMINISTIC if name in LENSES}

    raw, lens_errors = [], []
    for name, lens in registry.items():
        try:
            raw.extend(lens(lens_ctx) or [])
        except Exception as exc:                                # noqa: BLE001
            lens_errors.append({"lens": name,
                                "error": f"{type(exc).__name__}: {exc}"})

    deferrals = build_deferral_index(lens_ctx.evs or evs,
                                     project=lens_ctx.project)
    healthy = hist.tuning.healthy
    scored = []
    for candidate in raw:
        if not isinstance(candidate, Candidate):
            continue
        breakdown = score_candidate(candidate, deferrals, healthy=healthy,
                                    now_ts=clock)
        scored.append(normalise(replace(candidate, score=breakdown.total,
                                        veto_reason=breakdown.veto_reason,
                                        breakdown=breakdown)))
    scored = resonate(scored, hist.fingerprints)

    rows = []
    for candidate in scored:
        breakdown = (candidate.breakdown
                     if isinstance(candidate.breakdown, ScoreBreakdown)
                     else ScoreBreakdown())
        rows.append({
            "lens": candidate.lens,
            "kind": candidate.kind,
            "title": candidate.title,
            "score": candidate.score,
            "terms": breakdown.as_terms(),
            "fingerprint": candidate.fingerprint,
            # Already proposed once. Shown rather than filtered out, because
            # "this lens is finding things and they are all spent" and "this lens
            # is finding nothing" are different diagnoses.
            "spent": candidate.fingerprint in hist.fingerprints,
            "spike": bool(candidate.spike),
            "veto_reason": candidate.veto_reason,
            "members": list(candidate.members),
            "lands_at": landing_positions(candidate),
            "evidence": list(candidate.evidence),
        })
    # Highest first: the reader's question is almost always "what would land",
    # and a dead lens shows up as a long tail of zeroes in `lands_at`.
    rows.sort(key=lambda row: row["score"], reverse=True)

    adjustment = hist.tuning.adjustment if tune else ThresholdAdjustment()
    return {
        "now": clock,
        "lenses": sorted(registry),
        "positions": {p.name: {"threshold": p.min_score,
                               "lenses": list(p.lens_names),
                               "max_per_run": p.max_injections_per_run}
                      for p in PROFILES.values()},
        "tuning": {"success_streak": hist.tuning.success_streak,
                   "failure_streak": hist.tuning.failure_streak,
                   "healthy": hist.tuning.healthy,
                   "strained": hist.tuning.strained,
                   "threshold_delta": adjustment.delta,
                   "reason": adjustment.reason},
        "spent_fingerprints": len(hist.fingerprints),
        "lens_errors": lens_errors,
        "candidates": rows,
        # A lens that ran, raised nothing, and produced nothing. Named explicitly
        # rather than left as an absence, because an absence is what a dead lens
        # and a quiet lens both look like.
        "silent_lenses": sorted(
            set(registry) - {row["lens"] for row in rows}
            - {name for row in rows for name in row["members"]}
            - {err["lens"] for err in lens_errors}),
    }


# --- injector-directed skills -------------------------------------------------
# When the injector points at a skill, the direction is recorded BEFORE the
# invocation and says who issued it. The event type, the consume-once pairing and
# the trail render already exist for the prompt gate; this adds a `source` field
# and reuses all three, because a second direction event would split the fold
# that answers "was this suggestion ever followed".

SOURCE_REPLENISH = "replenish_injector"


def pick_skill(names, *, project=None, evs=None):
    """The skill to direct for injected work, or "" when none is defensible.

    Two measured inputs, in order: a skill whose LAST recorded call failed is
    dropped (`plugins.call_status`), and among what is left the one whose past
    directions were actually followed wins (`oversee.consumed_directions`).
    Ties break on the name, so the choice is reproducible from the ledger.

    Returning "" is a real outcome, not a fallback. A direction with no basis is
    a fabricated trail, which is the one thing the honest-trail rule forbids —
    better to record no direction than to record one the record cannot support."""
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

    Called BEFORE the invocation, so the consume-once pairing in
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
        # the trail must not stop the work the trail is about. Reported as "no
        # direction" so no caller believes one was written.
        return ""
    return name


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
                      fingerprint=generate_fingerprint("evidence-gap",
                                                       (newest_ts,)),
                      evidence=tuple(evidence),
                      occurrences=count, severity=True,
                      # The condition's own age: unverified code from this
                      # morning and unverified code from last quarter are not
                      # equally urgent, and this is what lets the score say so.
                      observed_ts=newest_ts)]


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
    now = ctx.now_ts

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
            fingerprint=generate_fingerprint("repair",
                                             (sig, newest.get(sig, ""))),
            evidence=tuple([f"signature: {sig}",
                            f"{len(timestamps[sig])} recorded failure(s), "
                            f"no active prevention rule"]
                           + [f"failed @ {t}"
                              for t in seen[-_EVIDENCE_TIMESTAMPS:]]),
            occurrences=len(timestamps[sig]),
            # The newest failure, which is both what the fingerprint keys on and
            # what the decay clock measures: a signature that stopped recurring
            # months ago should not outscore a live one on recurrence count alone.
            observed_ts=newest.get(sig, ""),
            spike=is_spiking(seen, now),
            deferral_key=sig))
    return out


def is_spiking(timestamps, now_ts) -> bool:
    """Whether these failures are a burst happening NOW.

    Two conditions, and both are required. DENSITY: at least `SPIKE_COUNT` of
    them fall inside `SPIKE_WINDOW_DAYS` of the newest — a signature that failed
    four times over four months is chronic, not spiking. FRESHNESS: that newest
    one is itself inside the same window of *now* — a burst that happened last
    spring was a spike then and is history now.

    Freshness is what stops the bypass from being a back door. Density alone
    would let any long-dead flurry walk past the score floor forever, and the
    floor is the only thing standing between the injector and a plan full of work
    nobody asked for."""
    ordered = sorted(t for t in (timestamps or []) if str(t or "").strip())
    if len(ordered) < SPIKE_COUNT:
        return False
    newest = ordered[-1]
    since_newest = age_in_days(newest, now_ts)
    if since_newest is None or since_newest > SPIKE_WINDOW_DAYS:
        return False
    return count_within(ordered, newest, SPIKE_WINDOW_DAYS) >= SPIKE_COUNT


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
                touched.add(paths.normalise_path(path))
        for match in _PY_REFERENT_RE.findall(str(payload.get("text") or "")):
            touched.add(paths.normalise_path(match))
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
        referents = {paths.normalise_path(m)
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
            # The headline joins the timestamp because a timestamp alone is not
            # an identity here: ledger `ts` is second-granular, so two caveats
            # appended in the same second collapsed onto one fingerprint, and a
            # row whose `ts` is absent entirely dropped to a blank part — leaving
            # the digest of the bare lens name, shared by EVERY such caveat. That
            # only became reachable once this lens could land at all.
            fingerprint=generate_fingerprint("caveat", (ts, headline)),
            evidence=tuple(evidence),
            # Asserted, and the record is what asserts it: this lens is scoped to
            # caveats tagged `pinned`, and SOP memo-hygiene §4 defines that tag as
            # the one reserved for SAFETY-CRITICAL caveats. An unresolved
            # safety-critical hazard is a blocking condition by the project's own
            # definition, so the severity term is read off the tag rather than
            # judged here.
            #
            # Without it this lens was DEAD: base 10 alone sits below both
            # thresholds it runs under (balanced 25, ambitious 15), so no pinned
            # caveat could ever have landed anywhere, in silence.
            severity=True,
            # When the caveat was pinned. A hazard recorded this week and one
            # recorded a year ago are not equally urgent — and this lens is the
            # one most exposed to that, because a pinned caveat is never folded
            # away by consolidation and so can sit in the record indefinitely.
            observed_ts=ts,
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

# How many lines past the threshold make one step of magnitude. It does double
# duty on purpose, and that is the point: it buckets the SCORE and it buckets the
# FINGERPRINT, so a module cannot quietly mint a fresh identity — and be proposed
# again — without also having become measurably worse. The two used to be
# different numbers (identity on hundreds, score on thousands), which meant a file
# growing from 1400 to 1500 lines was a brand new condition worth re-proposing at
# exactly the same score.
#
# 250 is chosen against the real tree rather than picked round. At this size the
# largest modules here (1848 and 1940 lines) reach `balanced`, the mid-sized ones
# (1353-1462) reach `ambitious` only, and a file barely over the line scores the
# bare base and lands nowhere — which is the behaviour the lens's own docstring
# always claimed and, before the magnitude term existed, never had.
MAGNITUDE_LINES_STEP = 250


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
        # How many `MAGNITUDE_LINES_STEP` blocks past the threshold this module
        # sits. It is the SCORE bucket and the IDENTITY bucket both, which is what
        # makes the two impossible to drift apart.
        excess_steps = (lines - LINE_THRESHOLD) // MAGNITUDE_LINES_STEP
        out.append(Candidate(
            lens="tech-debt",
            title=(f"Refactor and reduce line count in {relative} "
                   f"({lines} lines, threshold {LINE_THRESHOLD})")[:MAX_TITLE_CHARS],
            # Bucketed, not the exact count: keyed on the raw number a single
            # added line would mint a fresh instance and re-propose the same
            # refactor every round.
            fingerprint=generate_fingerprint(
                "tech-debt", ("lines", relative, excess_steps)),
            kind="lines",
            evidence=(f"{relative} is {lines} lines",
                      f"threshold is {LINE_THRESHOLD} lines",
                      f"module {stem}"),
            # Magnitude, in its own named term. It rode `occurrences` for one
            # phase and the cost was total: a file has no occurrences, integer
            # division by the threshold gave every module in this repository the
            # same value of 1, and the condition could not clear a single dial
            # position — measurably, on the real tree, in silence, with a green
            # suite. `occurrences` is left at its default here because this lens
            # genuinely does not have one.
            excess_steps=excess_steps,
            # No `observed_ts`: a file's current line count is a fact about the
            # tree as it stands, not a ledger row that gets older. Decay would be
            # lying about it.
            deferral_key=relative))

    for path in _pending_review_files(ctx.project):
        relative = _repo_relative(path, trees.repo_root)
        out.append(Candidate(
            lens="tech-debt",
            title=f"Clear the thermo-nuclear review queue for {relative}"[:MAX_TITLE_CHARS],
            fingerprint=generate_fingerprint("tech-debt", ("review", relative)),
            kind="review",
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
                       if paths.normalise_path(p) not in logged)
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
        fingerprint=generate_fingerprint("audit", uncovered),
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


def _change_timestamps(evs) -> dict:
    """`{path spelling: [ts, …]}` — when each path was named by a `code_change`.

    Timestamps rather than a bare count, because the two callers want different
    slices of the same fold: `audit` only asks whether a path appears at all,
    while `coverage` asks how often it appeared RECENTLY. Returning the raw
    instants lets each answer its own question off one pass.

    Recorded per EVENT, not per listed file: one entry naming the same path twice
    is one change to it."""
    stamps = {}
    for ev in evs or []:
        if getattr(ev, "type", None) != "code_change":
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        seen = set()
        for path in (payload.get("files") or []):
            if not isinstance(path, str):
                continue
            seen |= paths.path_spellings(path)
        ts = getattr(ev, "ts", "") or ""
        for spelling in seen:
            stamps.setdefault(spelling, []).append(ts)
    return stamps


def _logged_files(evs) -> set:
    """Paths any `code_change` event claims."""
    return set(_change_timestamps(evs))


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
    changes = _change_timestamps(ctx.evs)
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
        stamps = sorted(t for t in changes.get(relative, ()) if t)
        newest = stamps[-1] if stamps else ""
        # Changes clustered around the most recent one, NOT the lifetime total.
        # The lifetime count only ever grows, so in an ageing store every module
        # eventually crosses every threshold on nothing but the passage of time —
        # a score that rises without the condition worsening is not a measurement.
        # Anchored on the newest change rather than on the wall clock, so the lens
        # stays a pure fold; how long ago that burst was is the decay term's
        # question, and `observed_ts` below is what lets it ask.
        churn = count_within(stamps, newest, COVERAGE_CHURN_WINDOW_DAYS)
        out.append(Candidate(
            lens="coverage",
            title=f"Bootstrap a unit test suite for {relative}"[:MAX_TITLE_CHARS],
            fingerprint=generate_fingerprint("coverage", (relative,)),
            evidence=(f"{relative} has no test module",
                      f"expected tests/test_{stem}.py, and no test file names "
                      f"`{stem}`",
                      f"{churn} recorded change(s) to it within "
                      f"{int(COVERAGE_CHURN_WINDOW_DAYS)} days of the newest, "
                      f"{len(stamps)} in total"),
            # How many times the record changed this module, recently, while it
            # still had no test. NO severity: an untested module is not blocking
            # anything, and asserting otherwise would put a fabricated +20 on
            # every module in the tree. Recurrence is the honest term — a module
            # nobody touches should not interrupt the operator, and one being
            # changed repeatedly without cover is one the record insists on each
            # time.
            #
            # Without this the lens was DEAD: base 10 alone sits below both
            # thresholds it runs under (balanced 25, ambitious 15), so no missing
            # test suite could ever have landed anywhere, in silence.
            occurrences=max(1, churn),
            observed_ts=newest,
            deferral_key=relative))
    return out


# How close together changes have to be to count as churn for the coverage lens.
# A month, because that is roughly the span over which "this module is under
# active development and still has no tests" is a true statement rather than a
# summary of its whole history.
COVERAGE_CHURN_WINDOW_DAYS = 30.0


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


# --- the agentic lens ---------------------------------------------------------
# The only lens that spends a model call, so the only one gated to `ambitious`.
# Its output is untrusted in the strong sense: the model may propose anything, and
# nothing it SAYS is treated as a fact about the record.

# Five, not `decompose.MAX_PROPOSED`'s twelve. A decomposition is asked for once
# per task; this fires at the end of every loop round, so its blast radius is the
# run rather than one node.
INNOVATION_MAX_PROPOSALS = 5

# The light tier. This call proposes work, it does not do the work — the loop's own
# router decides what tier executes what lands.
INNOVATION_MODEL = "claude-haiku-4-5"
INNOVATION_TIMEOUT_S = 120

# How much of the record rides the prompt. Bounded because this runs inside a loop
# that is already spending context (SOP `context-reliability` §3).
_INNOVATION_CONTEXT_ITEMS = 8

# The fewest recorded facts a proposal may rest on before the model is worth
# calling. Derived, not chosen: a candidate's grounding count IS its recurrence
# term, so at one fact the score is the bare `BASE_SCORE`, which sits below
# `ambitious` — the only position that runs this lens. Below this line the call
# spends real money to produce candidates the gate is guaranteed to refuse, and
# refuse transiently, so nothing is even recorded to explain where it went.
#
# `test_the_innovation_grounding_floor_matches_the_dial` asserts this constant
# against `PROFILES` rather than trusting the arithmetic above to stay true, per
# the standing caveat: when a threshold or a lens moves, assert the lens still
# clears its own dial position — a green suite does not catch this.
INNOVATION_MIN_GROUNDING = 2


def innovation_prompt(changed_paths, failure_signatures, completed) -> str:
    """Ask for proposals, and only proposals.

    The output contract is stated as a schema AND as a rule about what not to
    include, following `decompose.prompt_for` — the parser tolerates prose around
    the object but cannot recover from a different shape."""
    lines = ["You are proposing follow-up engineering work for a repository.",
             "",
             "Recently changed files:"]
    shown = [f"- {p}" for p in changed_paths[:_INNOVATION_CONTEXT_ITEMS]]
    lines += shown if shown else ["- (none)"]
    if failure_signatures:
        lines += ["", "Recently recorded tool-failure signatures:"]
        lines += [f"- {s}" for s in failure_signatures[:_INNOVATION_CONTEXT_ITEMS]]
    if completed:
        lines += ["", "Work just completed:"]
        lines += [f"- {c}" for c in completed[:_INNOVATION_CONTEXT_ITEMS]]
    lines += [
        "",
        f"Propose at most {INNOVATION_MAX_PROPOSALS} concrete improvements that "
        "follow from the above — edge cases left unhandled, missing "
        "parameterisation, structural simplifications, hardening.",
        "",
        "Reply with ONLY this JSON object and nothing else:",
        '{"subtasks": [{"title": "...", "description": "..."}]}',
        "",
        "Do not include commentary, a plan, or any key other than subtasks.",
    ]
    return "\n".join(lines)


def _default_innovation_runner(prompt):
    """The real seam: one headless `claude` call through the shared helper."""
    from . import claude_cli
    return claude_cli.run_headless(prompt, model=INNOVATION_MODEL,
                                   timeout_s=INNOVATION_TIMEOUT_S)


def _recent_failure_signatures(evs) -> list:
    """Distinct `tool_failed` signatures, newest last. Total on malformed rows."""
    seen = []
    for ev in evs or []:
        if getattr(ev, "type", None) != "tool_failed":
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        sig = _failure_signature(payload)
        if sig and sig not in seen:
            seen.append(sig)
    return seen[-_INNOVATION_CONTEXT_ITEMS:]


def innovation(arg) -> list:
    """Model-proposed work the folds cannot see.

    Three properties make this safe enough to run inside an autonomous loop.

    **Its evidence is ours, never the model's.** A candidate cites the changed
    paths and failure signatures that were put IN the prompt — facts this process
    already knew. The model's own claims about the record are exactly what cannot
    be checked, and a candidate whose evidence came from the model would launder an
    invention into the audit row. With nothing to cite the lens returns nothing
    rather than proposing ungrounded work; the value gate would refuse it anyway.

    **Failure raises; emptiness returns.** An unparseable reply, a non-zero exit
    and a timeout all raise, so `run_round` records one `lens:innovation` failure.
    A model that legitimately proposes nothing returns `[]`. Collapsing those two
    into an empty list would make a broken lens read as "the system is robust",
    which is the specific lie this project's honesty rule exists to prevent.

    **The graph checks from `decompose.validate` are deliberately NOT applied.**
    That function validates proposals against a MindNetwork task graph — id
    collisions, dependency cycles, depth. A replenishment round has no graph: these
    are flat candidates for a checklist. Running a cycle check over a list with no
    edges would be theatre. What does transfer is reused verbatim: `decompose.parse`
    for the tolerant parse, and `decompose.slugify` for the id, so a proposal that
    cannot be given a well-formed identity is dropped with a reason."""
    ctx = _as_context(arg)
    changed = [str(p) for p in (ctx.changed_paths or ()) if str(p).strip()]
    signatures = _recent_failure_signatures(ctx.evs)
    # The facts a candidate would be allowed to cite, counted BEFORE the model is
    # called, because the count decides whether any answer could survive the gate.
    # It used to check only that this list was non-empty, which left a real and
    # ordinary window — one changed file, no recorded failures — where the lens
    # spent a model call on a proposal that scored `BASE_SCORE` and was then
    # refused for sitting below `ambitious`. The liveness guard found it.
    grounding = min(len(changed), _EVIDENCE_FILES) + min(len(signatures), 2)
    if grounding < INNOVATION_MIN_GROUNDING:
        return []

    runner = ctx.runner or _default_innovation_runner
    result = runner(innovation_prompt(changed, signatures, []))
    if not isinstance(result, dict):
        raise RuntimeError(
            f"innovation runner returned {type(result).__name__}, not a result "
            f"mapping")
    exit_code = result.get("exit")
    if exit_code != 0:
        raise RuntimeError(
            f"innovation model call exited {exit_code}: "
            f"{str(result.get('stdout') or '')[:200]}")

    from .mindnet import decompose as decompose_mod
    pairs, reason = decompose_mod.parse(result.get("stdout") or "")
    if reason:
        raise RuntimeError(f"innovation reply unusable: {reason}")

    evidence = tuple([f"changed: {p}" for p in changed[:_EVIDENCE_FILES]]
                     + [f"recorded failure: {s}" for s in signatures[:2]])
    taken, out = [], []
    for title, description in pairs[:INNOVATION_MAX_PROPOSALS]:
        slug = decompose_mod.slugify(title, taken=tuple(taken))
        if not slug:
            continue                # no well-formed identity: not proposable
        taken.append(slug)
        headline = title if not description else f"{title} — {description}"
        out.append(Candidate(
            lens="innovation",
            title=headline[:MAX_TITLE_CHARS],
            # Keyed on the slug, so the same proposal twice is one piece of work.
            fingerprint=generate_fingerprint("innovation", (slug,)),
            evidence=evidence,
            # Each cited fact is one INDEPENDENT observation of the condition
            # "recent activity has follow-ups nobody handled", so counting them is
            # what `occurrences` means rather than a reuse of the field. The
            # distinction matters: the tech-debt lens spent a phase riding a file's
            # SIZE here, which is not a count of anything, and the condition was
            # dead for as long as it did. Grounding genuinely is a count.
            #
            # NO severity. The record cannot say a suggestion about the future is
            # blocking anything, and asserting it would put a fabricated +20 on
            # every model proposal.
            occurrences=len(evidence),
            # No `observed_ts` either: a proposal is about work still to do, so
            # there is no moment for it to be old relative to.
            deferral_key=slug))
    return out


LENSES["innovation"] = innovation
