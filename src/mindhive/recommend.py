# SPDX-License-Identifier: Apache-2.0
# src/mindhive/recommend.py
"""Deterministic skill recommendation engine (KTD6).

Pure functions: every input — the flattened skill inventory, the per-skill
telemetry from ``plugins.skill_stats``, incident summaries, and recent need
texts — is pre-fetched by the caller; nothing here reads disk or the ledger.

Score per skill::

    score = base + W_NEED * need_match - open_incident_penalty [+ explore bonus]

``base`` is the MEASURED completion rate when the skill has at least
MEASURED_MIN_CALLS recorded calls; otherwise a description-breadth prior
(adapted from plugins.evaluate's breadth scoring — same TOPIC_KEYWORDS
vocabulary, applied to one skill's description) scaled into
[0, ESTIMATE_CEILING] so an estimate can never outrank a solid measured rate.
Every entry carries provenance: "measured" or "estimated".

Overlap grouping: when >= 2 skills address the same need (need_match > 0) and
their descriptions are similar (keyword Jaccard >= SIMILARITY_THRESHOLD), they
collapse into ONE recommendation. The winner is picked by the deterministic
tie-break chain the user specified:

  1. measured completion rate — a measured skill beats an unmeasured one,
     then higher rate wins;
  2. description clarity — deterministic heuristic: a description carrying an
     explicit "use when" trigger beats one without; still tied, the SHORTER
     description wins (less ambiguity);
  3. completion record — the longer trailing streak of failure-free calls.

Final ties break alphabetically by name. Losers are listed as alternatives.

Cold-start honesty: the output reports measured coverage
``{measured: N, total: M}``, and an unmeasured skill whose need_match clears
EXPLORE_NEED_MIN gets a small deterministic exploration bonus plus a
``try_to_measure`` flag, so early measured winners cannot permanently entrench
behind the calls gate while the rest of the inventory sits unmeasured.

Promotion (blueprint P4, MEB §2.3 C, decision K2): every scored entry also
carries the MEB's Laplace machinery from ``core.recommend`` -- ``laplace_rate``
``(ok + 1) / (calls + 2)``, ``skill_score`` (that rate times
``e^(-fail_streak / 2)``) and ``promotion``, one of QUALIFIED / PROBATION /
DISCONTINUED -- and ``execution_mode`` is ``auto`` exactly when the promotion
is QUALIFIED: score >= 0.80, at least four OBSERVED calls, no open incident.
``MEASURED_MIN_CALLS`` (three) stays the bar for the ``measured`` label; the
promotion's four is a different bar on purpose. ``should_promote_skill`` is
the adapter, and it is total over the contradictory rows a folded telemetry
can carry. ``recommend`` has a second discontinuation door beside the
improvements linkage: a promotion of DISCONTINUED (a three-streak, or a score
below 0.50 after five calls) with the reason
``discontinued: skor Laplace <x> di bawah 0.50 setelah <n> panggilan``.
"""
from __future__ import annotations

import re

from .core import recommend as core_recommend
from .plugins import TOPIC_KEYWORDS

MEASURED_MIN_CALLS = 3      # calls needed before a rate counts as measured
ESTIMATE_CEILING = 0.5      # breadth prior tops out below any decent rate
BREADTH_TOPIC_CAP = 5       # distinct topics counted toward the prior
W_NEED = 0.3                # weight of the project-need match term
NEED_CAP = 5                # keyword overlaps counted toward need_match
OPEN_INCIDENT_PENALTY = 0.4  # per open skill:* incident
EXPLORE_BONUS = 0.05        # deterministic nudge for unmeasured need-matches
EXPLORE_NEED_MIN = 0.2      # need_match at/above which measuring is worth it
SIMILARITY_THRESHOLD = 0.3  # description-keyword Jaccard for overlap grouping
AUTO_MIN_RATE = 0.8         # measured completion rate needed for auto execution
DISCONTINUE_STREAK = 3      # trailing failures that, with an active rule, stop a skill
INTENDED_USE_WIDTH = 140    # chars of a skill's intended use surfaced in a brief line

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "and", "any", "are", "can", "each", "for", "from", "has", "have", "how",
    "into", "its", "not", "that", "the", "them", "then", "they", "this",
    "use", "used", "when", "will", "with", "you", "your",
}


def _keywords(text) -> set:
    """Lowercased content words (len >= 3, stopwords removed) of a text."""
    return {w for w in _WORD.findall(str(text or "").lower())
            if len(w) >= 3 and w not in _STOP}


def flatten_inventory(inventory) -> list[dict]:
    """One ``{name, plugin, description}`` per skill from a
    ``plugins.inventory()`` payload (plugin skills + standalone local ones)."""
    skills = []
    for p in (inventory or {}).get("plugins", []):
        for s in p.get("skills", []):
            skills.append({"name": s.get("name", ""), "plugin": p.get("name", ""),
                           "description": s.get("trigger", "")})
    for s in (inventory or {}).get("local_skills", []):
        skills.append({"name": s.get("name", ""), "plugin": "local",
                       "description": s.get("trigger", "")})
    return skills


def extract_needs(summaries=None, recent_events=None, limit=40) -> list[str]:
    """Need texts for the need_match term: recent failure themes (incident
    categories + signatures) and open next_step texts. Pure — both inputs are
    pre-fetched (incidents.summaries(), events.read_all())."""
    needs = []
    for s in (summaries or []):
        for key in ("category", "signature"):
            v = s.get(key)
            if v:
                needs.append(str(v))
    for e in (recent_events or []):
        if getattr(e, "type", None) == "next_step":
            p = getattr(e, "payload", None)
            text = p.get("text", "") if isinstance(p, dict) else ""
            if text:
                needs.append(text)
    return needs[-limit:]


def active_skill_rules(incidents) -> dict:
    """skill name -> active prevention-rule text from ``skill:*`` incidents.
    The improvements linkage: a skill whose failures reflected into a rule
    that is still active carries that rule as its discontinuation reason."""
    out: dict[str, str] = {}
    for s in (incidents or []):
        sig = str(s.get("signature", ""))
        if (sig.startswith("skill:") and s.get("status") == "active"
                and s.get("active_rule")):
            out[sig[len("skill:"):]] = str(s["active_rule"])
    return out


def observed_calls(st) -> int:
    """Calls in a `plugins.skill_stats` row whose OUTCOME was witnessed.

    ``calls`` alone is not evidence about completion: a slash-command
    invocation fires no tool, so no response is ever read and nothing can
    mark it failed. Those calls are counted in ``calls`` and in
    ``unobserved``, and this is the number every "measured" gate and every
    rendered ``ok/N completion`` fraction has to use. MEASURED_MIN_CALLS
    against raw ``calls`` would let a skill reach 'measured' — and, above the
    auto gate, run automatically — on commands that were merely typed."""
    st = st or {}
    return max(0, int(st.get("calls") or 0) - int(st.get("unobserved") or 0))


def outcome_category(st) -> str | None:
    """Improvements-style outcome category for a skill's recorded history:
    ``failure`` (last call failed), ``supervision-needed`` (history contains
    agent-assessed write-throughs — an agent had to intervene and grade),
    ``no-intervention-needed`` (a solid failure-free streak of clean runs),
    ``success`` (last call ok, record still thin). None when never called, and
    None when nothing was ever OBSERVED — a skill whose whole record is
    slash-command captures has calls but no witnessed outcome, and the
    ``success`` branch below would otherwise read "last call ok" off a row that
    never had a result to be ok."""
    if not st or not st.get("calls"):
        return None
    if int(st.get("calls") or 0) <= int(st.get("unobserved") or 0):
        return None
    if st.get("last_outcome") == "fail":
        return "failure"
    if (st.get("provenance") or {}).get("agent-assessed"):
        return "supervision-needed"
    if int(st.get("streak") or 0) >= MEASURED_MIN_CALLS:
        return "no-intervention-needed"
    return "success"


def open_skill_incidents(incidents) -> dict:
    """skill name -> count of OPEN (status active) ``skill:*`` incidents."""
    out: dict[str, int] = {}
    for s in (incidents or []):
        sig = str(s.get("signature", ""))
        if sig.startswith("skill:") and s.get("status") == "active":
            name = sig[len("skill:"):]
            out[name] = out.get(name, 0) + 1
    return out


def _whole(value):
    """A non-negative integer count out of a telemetry field: 0 for an absent
    field (`None`), the count for a number or a numeric string, and `None` for
    a value that is not a count at all -- a bool, NaN, an infinity (which
    overflows `int()`), a non-numeric string, a negative."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n >= 0 else None


def should_promote_skill(ok, calls, fail_streak, open_incidents=0) -> tuple:
    """``(auto, status)`` for a skill's counts under the MEB's promotion rule
    (blueprint P4, decision K2): ``status`` is ``core.recommend``'s QUALIFIED
    / PROBATION / DISCONTINUED and ``auto`` is ``status == "QUALIFIED"``.
    *calls* is the OBSERVED count (see ``observed_calls``).

    Total. ``core.recommend`` validates its counts and raises on a
    contradiction; this path must not, because the row it scores was folded
    from events by several hands. A reported streak of ``DISCONTINUE_STREAK``
    is a fact on its own and answers DISCONTINUED whatever the other counts
    say; any other contradiction -- more successes than calls, a streak longer
    than the failures -- is the residual status, PROBATION, and never a
    promotion. An absent field reads as 0; a value that is not a count at all
    (a bool, NaN, an infinity, a non-numeric string, a negative) makes the row
    one this cannot read -- PROBATION, never a promotion -- unless the streak
    alone already says DISCONTINUED. Reading garbage as 0 would promote a row
    whose streak field happens to be broken."""
    ok_n, calls_n = _whole(ok), _whole(calls)
    streak_n, incidents_n = _whole(fail_streak), _whole(open_incidents)
    if streak_n is not None and streak_n >= core_recommend.DISCONTINUE_STREAK:
        return False, core_recommend.DISCONTINUED
    if None in (ok_n, calls_n, streak_n, incidents_n):
        return False, core_recommend.PROBATION           # not a row this can read
    if ok_n > calls_n or streak_n > calls_n - ok_n:
        return False, core_recommend.PROBATION
    status = core_recommend.promotion_status(ok_n, calls_n, streak_n, incidents_n)
    return status == core_recommend.QUALIFIED, status


def breadth_prior(description) -> float:
    """Breadth fallback for unmeasured skills, reusing plugins.evaluate's topic
    vocabulary on one skill's description: distinct topics hit, capped, scaled
    into [0, ESTIMATE_CEILING]."""
    blob = str(description or "").lower()
    topics = sum(1 for kw in TOPIC_KEYWORDS if kw in blob)
    return round(min(topics, BREADTH_TOPIC_CAP) / BREADTH_TOPIC_CAP
                 * ESTIMATE_CEILING, 4)


def need_match(description, needs) -> float:
    """Keyword overlap between a skill description and the need texts, capped
    at NEED_CAP overlaps and normalized to [0, 1]."""
    need_kw: set = set()
    for n in (needs or []):
        need_kw |= _keywords(n)
    overlap = len(_keywords(description) & need_kw)
    return round(min(overlap, NEED_CAP) / NEED_CAP, 4)


def _reason(measured: bool, st: dict, calls: int, nm: float, penalty: float) -> str:
    # `calls` here is the OBSERVED count, so the fraction is a real rate. Using
    # raw calls would print "2/55 completion" for a skill whose rate is 1.0
    # because 53 of those calls were typed slash commands nobody graded.
    if measured:
        base = f"measured {st.get('ok', 0)}/{calls} completion"
    elif calls:
        base = f"only {calls} call(s) recorded — breadth-estimated"
    elif int((st or {}).get("calls") or 0):
        # Called, but never graded — a slash-only record. Saying "never
        # called" here is the mirror of the inflation this gate removes: the
        # skill really did run, the runs just prove nothing about completion.
        base = (f"{int(st['calls'])} call(s) recorded, none with an observed "
                "outcome — breadth-estimated")
    else:
        base = "never called — breadth-estimated"
    if nm > 0:
        base += ", matches current needs"
    if penalty > 0:
        base += ", open skill incident"
    return base


def _truncate(text, width=None) -> str:
    """Collapse whitespace; when ``width`` is set, cap ``text`` at ``width``
    chars with an ellipsis on overflow. ``width=None`` collapses only — the CLI
    full view uses this to render the untruncated intended use while sharing the
    same empty/whitespace handling as the truncating surfaces (so an empty
    description never leaves a dangling clause on any surface). Deterministic —
    no locale/word-boundary heuristics."""
    s = " ".join(str(text or "").split())
    if width is None or len(s) <= width:
        return s
    return s[:max(0, width - 1)].rstrip() + "…"


def intended_use_clause(entry, width: int = INTENDED_USE_WIDTH) -> str:
    """`` · intended use: …`` clause for a scored entry, or ``""`` when the
    description is empty/whitespace. Shared by :func:`brief_line` and the
    projector's directive lines so every surface renders (and omits) the
    intended use identically."""
    desc = _truncate(entry.get("description", ""), width)
    return f" · intended use: {desc}" if desc else ""


def brief_line(entry, width: int = INTENDED_USE_WIDTH) -> str:
    """One compact line for a scored :func:`recommend` entry: name, reason, and
    provenance, then the skill's *intended use* (its description) truncated to
    ``width``. Surfacing the intended use lets an agent reading MEMORY.md grasp
    a skill's scope without opening it; the clause is omitted when the
    description is empty. Provenance stays verbatim so an estimate never reads
    as measured. Shared by the projector's SUGGESTED SKILLS block and
    ``hive skills recommend --brief`` so the two surfaces cannot drift."""
    return (f"{entry.get('name', '')} — {entry.get('reason', '')} "
            f"({entry.get('provenance', '')})" + intended_use_clause(entry, width))


def score_skill(skill, stats, needs=None, open_incidents=None) -> dict:
    """One scored entry for a skill. ``stats`` is the plugins.skill_stats dict;
    ``open_incidents`` the open_skill_incidents map."""
    name = skill.get("name", "")
    desc = skill.get("description", "")
    st = (stats or {}).get(name) or {}
    calls = int(st.get("calls") or 0)
    # The gate counts calls whose outcome was OBSERVED. Against raw `calls` a
    # skill reached 'measured' — and above AUTO_MIN_RATE ran automatically —
    # on typed slash commands: 55 calls of which 2 were ever graded still
    # cleared a 3-call bar. See `observed_calls`.
    observed = observed_calls(st)
    rate = st.get("completion_rate")
    measured = observed >= MEASURED_MIN_CALLS and rate is not None
    nm = need_match(desc, needs)
    prior = breadth_prior(desc)
    penalty = OPEN_INCIDENT_PENALTY * (open_incidents or {}).get(name, 0)
    try_measure = (not measured) and nm >= EXPLORE_NEED_MIN
    score = ((rate if measured else prior) + W_NEED * nm - penalty
             + (EXPLORE_BONUS if try_measure else 0.0))
    # Learning-gated execution (blueprint P4, decision K2): a skill runs
    # automatically exactly when the MEB promotes it -- QUALIFIED: a Laplace
    # score >= 0.80 over at least four OBSERVED calls and no open incident.
    # Everything else — above all a never-used skill — is suggest-only. The
    # rate and the score on the entry come from the coherent reading of the
    # row (successes capped at the observed calls, the streak at the
    # failures) so they never raise; the promotion comes from the adapter,
    # which fails closed on a contradiction.
    ok_n = min(_whole(st.get("ok")) or 0, observed)
    streak_n = min(_whole(st.get("fail_streak")) or 0, max(0, observed - ok_n))
    auto, promotion = should_promote_skill(
        st.get("ok"), observed, st.get("fail_streak"),
        (open_incidents or {}).get(name, 0))
    laplace = core_recommend.bayesian_laplace_rate(ok_n, observed)
    skill_score = core_recommend.skill_score(ok_n, observed, streak_n)
    return {
        "name": name,
        "plugin": skill.get("plugin", ""),
        "description": desc,
        "provenance": "measured" if measured else "estimated",
        "score": round(score, 4),
        "completion_rate": rate,
        "calls": calls,
        "observed": observed,        # calls whose outcome was witnessed
        "streak": _whole(st.get("streak")) or 0,
        "need_match": nm,
        "breadth_prior": None if measured else prior,
        "incident_penalty": round(penalty, 4),
        "try_to_measure": try_measure,
        "execution_mode": "auto" if auto else "suggest",
        "outcome_category": outcome_category(st),
        "fail_streak": _whole(st.get("fail_streak")) or 0,
        "laplace_rate": laplace,     # (ok + 1) / (observed + 2), the MEB's E[theta]
        "skill_score": skill_score,  # that rate times e^(-fail_streak / 2)
        "promotion": promotion,      # QUALIFIED / PROBATION / DISCONTINUED
        "reason": _reason(measured, st, observed, nm, penalty),
    }


def _winner_key(entry: dict):
    """Sort key encoding the overlap tie-break chain (descending sort):
    measured beats unmeasured → higher rate → explicit 'use when' trigger →
    shorter description → longer failure-free streak."""
    measured = entry["provenance"] == "measured"
    rate = entry["completion_rate"] if measured and \
        entry["completion_rate"] is not None else -1.0
    desc = str(entry.get("description") or "")
    has_trigger = 1 if "use when" in desc.lower() else 0
    return (1 if measured else 0, rate, has_trigger, -len(desc), entry["streak"])


def _overlap_groups(entries: list[dict]) -> list[list[dict]]:
    """Union-find over entries: two skills join a group when BOTH match the
    current needs (need_match > 0) and their description keyword sets have
    Jaccard similarity >= SIMILARITY_THRESHOLD. Transitive by construction."""
    parent = list(range(len(entries)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    kws = [_keywords(e["description"]) for e in entries]
    for i in range(len(entries)):
        if entries[i]["need_match"] <= 0:
            continue
        for j in range(i + 1, len(entries)):
            if entries[j]["need_match"] <= 0 or not kws[i] or not kws[j]:
                continue
            jac = len(kws[i] & kws[j]) / len(kws[i] | kws[j])
            if jac >= SIMILARITY_THRESHOLD:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    groups: dict[int, list[dict]] = {}
    for i in range(len(entries)):
        groups.setdefault(find(i), []).append(entries[i])
    return [groups[k] for k in sorted(groups)]


def recommend(skills, stats, incidents=None, needs=None, top=None) -> dict:
    """Rank the most-needed skills from measured data. Returns
    ``{coverage: {measured, total}, recommendations: [...], discontinued: [...]}``
    where each recommendation is a scored entry (see score_skill) plus
    ``alternatives``: the overlap-group losers, each with its own provenance
    and flags. ``discontinued`` holds skills stopped by the improvements
    linkage — an active ``skill:*`` prevention rule plus a trailing failure
    streak of DISCONTINUE_STREAK — excluded from ranking until the incident
    resolves or a successful call breaks the streak; and, since blueprint P4
    (decision K2), skills the MEB's promotion rule alone discontinues: a
    ``promotion`` of DISCONTINUED, ``rule`` None, the Laplace reason."""
    open_inc = open_skill_incidents(incidents)
    rules = active_skill_rules(incidents)
    entries = [score_skill(s, stats, needs=needs, open_incidents=open_inc)
               for s in (skills or []) if s.get("name")]
    # Coverage counts the whole inventory — measured before the discontinued
    # split, since a discontinued skill is (by construction) a measured one.
    measured_n = sum(1 for e in entries if e["provenance"] == "measured")
    discontinued = []
    active = []
    for e in entries:
        rule = rules.get(e["name"])
        if rule and e["fail_streak"] >= DISCONTINUE_STREAK:
            discontinued.append({
                "name": e["name"], "plugin": e["plugin"],
                "fail_streak": e["fail_streak"], "rule": rule,
                "promotion": e["promotion"],
                "reason": (f"discontinued: {e['fail_streak']} consecutive "
                           f"failures with an active prevention rule"),
            })
        elif e["promotion"] == core_recommend.DISCONTINUED:
            # The MEB's door (K2): the RECORD says stop -- a three-streak, or a
            # Laplace score under 0.50 after five observed calls -- with no
            # incident and no rule needed. The score quoted is the one the
            # rule read, `skill_score`: the Laplace rate times the streak
            # penalty, which with no streak is the rate itself.
            discontinued.append({
                "name": e["name"], "plugin": e["plugin"],
                "fail_streak": e["fail_streak"], "rule": None,
                "promotion": e["promotion"],
                "reason": (f"discontinued: skor Laplace {e['skill_score']:.2f} "
                           f"di bawah {core_recommend.PROBATION_MIN_SCORE:.2f} "
                           f"setelah {e['observed']} panggilan"),
            })
        else:
            active.append(e)
    entries = active
    recs = []
    for group in _overlap_groups(entries):
        ordered = sorted(group, key=lambda e: e["name"])
        ordered.sort(key=_winner_key, reverse=True)
        primary = dict(ordered[0])
        primary["alternatives"] = [
            {"name": a["name"], "plugin": a["plugin"], "score": a["score"],
             "provenance": a["provenance"], "try_to_measure": a["try_to_measure"],
             "reason": a["reason"]}
            for a in ordered[1:]]
        recs.append(primary)
    recs.sort(key=lambda e: e["name"])
    recs.sort(key=lambda e: -e["score"])
    if top is not None:
        recs = recs[:max(0, int(top))]
    return {"coverage": {"measured": measured_n,
                         "total": len(entries) + len(discontinued)},
            "recommendations": recs,
            "discontinued": discontinued}
