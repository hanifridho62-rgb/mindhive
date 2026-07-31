# src/mindhive/playbook.py
"""MN-15 — the per-prompt playbook: which technique, which skills, which records.

MN-14 opened the skill catalog for code work and injected ONE trained skill. The
follow-up ask was for three things instead of one:

    "setiap prompt akan di tambahkan konteks dengan memberitahu ledger mana saja
     yang perlu dibaca dan menggunakan teknik apa ... ketika membangun sesuatu maka
     akan menampilkan skill yang berhubungan dengan plan kemudian di sintesakan
     skill yang dapat di andalkan sehingga lebih efisien"

So: name the technique, name the reliable skills (plural, ordered), and name the
ledger records worth reading first.

**Where the cost lands.** Measured on the live store: 373 segments, 12.54 MB, 22 835
events; `events.read_all()` takes 1.09 s, and the whole hook currently runs in
0.39-0.65 s including interpreter startup. Reading the ledger on every prompt would
several-fold the latency of every prompt the user types — so nothing here reads it.
`promptgate.train_and_store` already calls `read_all` behind a staleness gate, and
`train()` already receives `evs`, so the index is built THERE and the hook only reads
the manifest it was already reading. Per-prompt cost is a dict lookup and a join.

**What a pointer is.** `type @ ts`, a one-line gist, and the exact `hive recall`
command. Deliberately NOT the projector's `.md` filename: that name mixes a digest of
the rendered BODY (`projector.py:187`), which needs every per-type body renderer to
reproduce, and a second implementation of that scheme would be a second thing to keep
correct for no gain. Type and timestamp are stable, and `hive recall` is how you open
the entry anyway.

Pure: no disk, no clock, no ledger access. `build` takes the inventory, the stats and
the events it is given; `render` turns an already-built entry into text. Determinism
is what lets the tests pin the bytes.
"""
from __future__ import annotations

import re

# Caps. The injected block competes with the user's own prompt for attention: past a
# certain size the reader learns to skip the marker, and then the feature is worse
# than nothing. Three of each keeps the whole block near ~800 bytes.
SKILL_CAP = 3
RECORD_CAP = 3
GIST_CHARS = 110
QUERY_TERMS = 4

# How many curated memos the trained pool carries. The records are ranked against the
# actual PROMPT at hook time, not frozen per phase at train time — a first version
# picked them per phase and the result was visibly wrong: a prompt about a CSV export
# was told to read a decision about phase-deny rules, because the only thing being
# matched was the phase's generic need text. Scoring ~200 short gists is a few
# milliseconds against a 0.39-0.65 s hook, and it buys real relevance.
RECORD_POOL = 200
# Gists are deduped on this many leading characters. The live store legitimately holds
# near-identical memos (the same lesson harvested in several sessions), and pointing at
# the same text twice wastes a third of the block.
DEDUPE_CHARS = 60

# The record types worth pointing at: the curated memos a person wrote on purpose.
# Live counts — caveat 111, experiment_result 90, decision 83, code_change 76 — so
# the index stays small. `command_run` (8 501) and `assistant_response` (7 152) are
# raw history, not lessons, and would bury the signal.
POINTER_TYPES = ("caveat", "decision", "experiment_result")

# Phase cues. The phase names themselves come from `mindnet.tasks.PHASES` — this maps
# ONTO that legend and never restates it (the keys are asserted against it in the
# tests). Cues are ordered most-specific-first because `infer` returns the first hit:
# "the CI is red" is about the merge gate even though it also says "red".
#
# Indonesian sits beside English throughout, because that is what this user types and
# an English-only table would infer nothing for most real prompts.
#
# Careful with spellings here: the repo's invariant-10 guard forbids phase-name
# SYNONYMS as quoted string literals anywhere in `src/`. The compound names must be
# written hyphenated exactly as the legend spells them, and the near-miss forms of
# the single-word phases — gerunds, past tenses, abbreviations — may not appear in
# quotes at all, including inside a comment like this one. See
# tests/test_mindnet_phases.py::_LITERAL_SYNONYMS for the exact list.
_CUES = (
    ("pr-green", (
        r"\bci\b", r"\bpr\b", r"\bpull request\b", r"\bmerge\b", r"\bpipeline\b",
        r"\bgreen\b", r"\bhijau\b", r"\bworkflow gagal\b", r"\bcheck(?:s)? fail",
    )),
    ("audit-bug", (
        r"\bbug\b", r"\bcrash\b", r"\btraceback\b", r"\bregression\b",
        r"\bfails?\b", r"\bfailing\b", r"\bbroken\b", r"\berror\b",
        r"\bperbaiki\b", r"\brusak\b", r"\bgagal\b",
        # Symptom reports that never name a defect. Without these, "kucing di
        # dashboard tidak muncul" inferred no technique at all.
        r"\btidak\s+(?:mun[cs]ul|jalan|bisa|berfungsi|bekerja|ke?luar|tampil|"
        r"terbaca|tersimpan|jadi)\b",
        r"\b(?:kenapa|mengapa)\b.{0,40}\b(?:tidak|gagal|error|salah)\b",
        r"\b(?:macet|hilang|aneh|nge?blank)\b",
    )),
    ("audit-code", (
        r"\breview\b", r"\baudit\b", r"\blint\b", r"\btypecheck\b",
        r"\bmaintainab", r"\bcode quality\b", r"\btinjau\b", r"\bperiksa kode\b",
    )),
    ("audit-system", (
        r"\barchitectur", r"\bend.to.end\b", r"\bintegration\b",
        r"\bwhole system\b", r"\barsitektur\b", r"\bmenyeluruh\b",
        r"\bseluruh sistem\b",
    )),
    ("prd", (
        r"\brequirements?\b", r"\bacceptance\b", r"\bspec(?:ification)?\b",
        r"\bkebutuhan\b", r"\bkriteria\b",
    )),
    ("brainstorm", (
        r"\bidea\b", r"\bideas\b", r"\bexplore\b", r"\boptions?\b",
        r"\btrade.?offs?\b", r"\bshould we\b", r"\bide\b", r"\bgagasan\b",
        r"\bopsi\b", r"\bsebaiknya\b",
    )),
    # Building something new lands on `plan`, not the phase after it — which is
    # exactly what was asked for: "ketika membangun sesuatu maka akan menampilkan
    # skill yang berhubungan dengan plan".
    ("plan", (
        r"\bbuild\b", r"\bimplement\b", r"\badd\b", r"\bfeature\b", r"\bcreate\b",
        r"\bnew \w+ (?:module|command|endpoint|page)\b", r"\bdesign\b",
        r"\bbuat(?:kan)?\b", r"\bbikin\b", r"\btambah(?:kan)?\b", r"\bbangun\b",
        r"\bfitur\b", r"\brencana\b",
    )),
    # The catch-all for hands-on change with nothing to decide first.
    ("execution", (
        r"\brefactor\b", r"\brename\b", r"\bmove\b", r"\bwire\b", r"\bextract\b",
        r"\bmigrate\b", r"\bupdate\b", r"\bubah\b", r"\bpindah\b", r"\bterapkan\b",
        r"\blanjutkan\b", r"\bkerjakan\b", r"\bhapus\b",
    )),
)

_COMPILED = tuple((phase, tuple(re.compile(p) for p in pats))
                  for phase, pats in _CUES)

# What each phase needs from a skill, in `recommend.need_match` vocabulary. This is
# the "technique" half of the ask: the words that select plan-shaped skills for
# planning work and verification-shaped skills for the merge gate.
PHASE_NEED = {
    "brainstorm": "brainstorm explore idea design options requirements discovery",
    "plan": "plan break down structured multi-step task goal requirements roadmap",
    "prd": "requirements acceptance criteria specification scope document",
    "execution": "implementation code edit refactor apply change test driven",
    "audit-bug": "debug root cause systematic investigation reproduce failure test",
    "audit-code": "code review quality maintainability simplify abstraction",
    "audit-system": "architecture system integration verification end to end",
    "pr-green": "verification before claiming complete evidence tests continuous "
                "integration merge",
}

# How to say the technique in one line a reader acts on.
PHASE_TECHNIQUE = {
    "brainstorm": "explore the design before committing to one",
    "plan": "write the plan before the first edit",
    "prd": "pin the requirements and acceptance criteria first",
    "execution": "test-first, smallest change that works",
    "audit-bug": "find the root cause before proposing a fix",
    "audit-code": "review for structure and maintainability, not just correctness",
    "audit-system": "check the whole path end to end, not the changed file alone",
    "pr-green": "not finished until the checks actually pass",
}

_WORD = re.compile(r"[a-z][a-z0-9-]{2,}")
_QUERY_STOP = {
    "and", "the", "for", "with", "this", "that", "from", "into", "your", "you",
    "add", "please", "buat", "bikin", "yang", "pada", "untuk", "dari", "dan",
    "saya", "ini", "itu", "agar", "supaya", "tolong", "bisa",
}


def infer(text: str) -> str | None:
    """The phase whose cues the prompt matches, or None.

    First match wins, and `_CUES` is ordered most-specific-first: a prompt about a
    red pipeline is merge-gate work even though it also says "fail". None means the
    prompt named no technique — the caller then says nothing about technique rather
    than guessing one, because a confidently wrong technique is worse than silence."""
    low = " ".join(str(text or "").lower().split())
    if not low:
        return None
    for phase, pats in _COMPILED:
        if any(p.search(low) for p in pats):
            return phase
    return None


def recall_query(text: str, cap: int = QUERY_TERMS) -> str:
    """The `hive recall` query for this prompt: its most distinctive content words.

    Longest-first rather than as-written: the long words in a prompt are the ones
    that identify the subject, and a query of "add the new" would match everything.
    Stable ordering for equal lengths so the rendered text is deterministic."""
    seen, words = set(), []
    for w in _WORD.findall(str(text or "").lower()):
        if w in _QUERY_STOP or w in seen:
            continue
        seen.add(w)
        words.append(w)
    words.sort(key=lambda w: (-len(w), w))
    return " ".join(words[:cap])


_GIST_LEAD = re.compile(r"^[\s*#>_`~.\-–—:;,]+")


def _gist(payload) -> str:
    """One line of an event's own words, or "" when it has none to offer.

    Leading markdown is stripped: many memos were harvested out of formatted prose and
    open with `**` or `- `, which in a one-line pointer is noise occupying the space
    that should be telling the reader what the record says."""
    raw = None
    if isinstance(payload, dict):
        for key in ("text", "title", "summary", "explanation", "reason"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                raw = v
                break
    elif isinstance(payload, str):
        raw = payload
    if not raw or not raw.strip():
        return ""
    return _GIST_LEAD.sub("", " ".join(raw.split()))[:GIST_CHARS].strip()


def _skills_for(need, skills, stats, open_incidents=(), skill_rules=None):
    """The reliable skills for one need, best first.

    Ranking is `recommend`'s own provenance chain — measured beats unmeasured, then
    higher completion rate, then stronger need match — reused rather than
    reimplemented so this cannot call a skill proven that the rest of the system
    calls unproven. That divergence is the whole risk of a second ranker."""
    from . import recommend
    ranked = []
    for s in sorted(skills or [], key=lambda s: str(s.get("name", ""))):
        name = s.get("name")
        if not name:
            continue
        nm = recommend.need_match(s.get("description", ""), [need])
        if nm <= 0:
            continue
        st = (stats or {}).get(name) or {}
        calls = int(st.get("calls") or 0)
        rate = st.get("completion_rate")
        measured = calls >= recommend.MEASURED_MIN_CALLS and rate is not None
        # An open incident denies MUST wording even for a measured winner — R7, the
        # same gate `promptgate.train` honours.
        blocked = name in set(open_incidents or ())
        must = (measured and not blocked
                and (rate or 0.0) >= recommend.AUTO_MIN_RATE)
        key = (0 if measured else 1, -(rate if measured else -1.0), -nm,
               len(str(s.get("description") or "")), name)
        ranked.append((key, {
            "skill": name,
            "plugin": s.get("plugin", "") or "",
            "directive": "must" if must else "try",
            "ok": int(st.get("ok") or 0),
            "calls": calls,
            "provenance": "measured" if measured else "unmeasured",
        }))
    ranked.sort(key=lambda t: t[0])
    return [r[1] for r in ranked[:SKILL_CAP]]


def _dedupe_key(gist: str) -> str:
    return " ".join(str(gist or "").lower().split())[:DEDUPE_CHARS]


def record_pool(evs, cap: int = RECORD_POOL) -> list:
    """The curated memos, newest first, deduped, as the trained pool.

    Not filtered by phase and not scored here: relevance is decided against the actual
    prompt at hook time by `pick_records`. Freezing the choice at train time was the
    first version and it read badly — the only thing available to match against was a
    phase's generic need text, so a prompt about one subject was told to read records
    about another."""
    out, seen = [], set()
    for ev in sorted((evs or []), key=lambda e: str(getattr(e, "ts", "") or ""),
                     reverse=True):
        if getattr(ev, "type", None) not in POINTER_TYPES:
            continue
        gist = _gist(getattr(ev, "payload", None))
        if not gist:
            continue
        key = _dedupe_key(gist)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": ev.type, "ts": str(getattr(ev, "ts", "") or ""),
                    "gist": gist})
        if len(out) >= cap:
            break
    return out


def pick_records(records, prompt_text, phase=None, cap: int = RECORD_CAP) -> list:
    """The pooled memos most worth reading for THIS prompt, best first.

    Scored against the prompt's own words first and the phase's need text second, so a
    record that speaks to the subject outranks one that merely belongs to the same kind
    of work. Ties break newest-first: the ledger never rewrites anything, so an older
    memo is the more likely to have been superseded — recency is the honest tie-break
    rather than a claim the old one is wrong.

    Total by construction: a malformed row scores nothing and drops out rather than
    raising inside a hook that must never break a prompt."""
    from . import recommend
    need = PHASE_NEED.get(phase or "", "")
    scored = []
    for r in (records or []):
        if not isinstance(r, dict):
            continue
        gist = r.get("gist")
        if not isinstance(gist, str) or not gist.strip():
            continue
        try:
            direct = recommend.need_match(gist, [str(prompt_text or "")])
            themed = recommend.need_match(gist, [need]) if need else 0.0
        except Exception:                                   # noqa: BLE001
            continue
        if direct <= 0 and themed <= 0:
            continue
        ts = str(r.get("ts") or "")
        scored.append(((-direct, -themed, _neg_ts(ts), str(r.get("type") or "")), r))
    scored.sort(key=lambda t: t[0])
    out, seen = [], set()
    for _key, r in scored:
        k = _dedupe_key(r.get("gist"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= cap:
            break
    return out


def _neg_ts(ts: str) -> str:
    """A sort key that puts the NEWEST timestamp first among equal scores.

    ISO-8601 sorts lexicographically, so complementing each digit inverts the order
    without parsing a date — which also keeps this module clock-free and total: a
    malformed `ts` sorts somewhere stable instead of raising."""
    return "".join(chr(0x7E - ord(c)) if "0" <= c <= "9" else c for c in ts)


def build(skills, stats, evs=None, open_incidents=(), skill_rules=None) -> dict:
    """The whole playbook: per-phase skills, plus one shared pool of records.

    Called from `promptgate.train`, which already holds the inventory, the telemetry
    and the events — so this adds no read of its own.

    Two levels because the two halves are decided at different times. Which SKILLS
    suit a technique is a property of the technique, so it is settled here. Which
    RECORDS a prompt should read is a property of the prompt, so only the pool is
    settled here and the choice happens at hook time. A phase with no skills is
    omitted rather than stored empty: an empty dict in a sidecar reads like a trained
    result, and it is not one."""
    phases = {}
    for phase, need in PHASE_NEED.items():
        picked = _skills_for(need, skills, stats, open_incidents, skill_rules)
        if picked:
            phases[phase] = {"skills": picked}
    return {"phases": phases, "records": record_pool(evs)}


def render(playbook, phase, prompt_text, exclude=()) -> str:
    """The injected block for one prompt, or "" when there is nothing to say.

    *exclude* is the skill names an earlier block already directed. Naming a skill
    twice in one injection reads as either one demand or two and the reader cannot
    tell which, so a repeat is dropped rather than restated.

    Order is technique → skills → records: what to do, what to do it with, what to
    read before starting. A reader who stops after the first line still got the most
    important part."""
    if not phase or not isinstance(playbook, dict):
        return ""
    phases = playbook.get("phases")
    entry = phases.get(phase) if isinstance(phases, dict) else None
    if not isinstance(entry, dict):
        entry = {}
    skipped = set(exclude or ())
    picked = [s for s in (entry.get("skills") or [])
              if isinstance(s, dict) and s.get("skill")
              and s["skill"] not in skipped]
    records = pick_records(playbook.get("records"), prompt_text, phase)
    if not picked and not records:
        return ""

    technique = PHASE_TECHNIQUE.get(phase, "")
    lines = [f"Technique for this prompt: {phase}"
             + (f" — {technique}." if technique else ".")]

    if picked:
        lines.append("Reliable skills for it, in order:")
        for i, s in enumerate(picked, 1):
            where = f" ({_clean(s.get('plugin'))})" if s.get("plugin") else ""
            if s.get("directive") == "must":
                proof = f"measured {int(s.get('ok') or 0)}/{int(s.get('calls') or 0)}"
                lines.append(f"  {i}. MUST USE: {_clean(s['skill'])}{where} "
                             f"via the Skill tool — {proof} completion.")
            else:
                lines.append(f"  {i}. TRY & MEASURE: {_clean(s['skill'])}{where} "
                             f"via the Skill tool (not ad-hoc) so its completion "
                             f"rate gets measured.")

    if records:
        lines.append("Read these ledger records before starting:")
        for r in records:
            ts = _clean(r.get("ts"), 24)
            lines.append(f"  - {_clean(r.get('type'), 24)} @ {ts}"
                         f" — {_clean(r.get('gist'), GIST_CHARS)}")
        q = recall_query(prompt_text)
        if q:
            lines.append(f'  - open them with: hive recall "{q}"')
    return "\n".join(lines)


# The directive words this module uses to give an order. Neutralized wherever they
# appear in DATA, below.
_MARKER_RE = re.compile(r"MUST USE|TRY & MEASURE", re.IGNORECASE)


def _clean(value, width: int = 80) -> str:
    """Collapse a stored value to one harmless line that cannot pose as an order.

    Skill names, plugin names and record gists all come off disk — a local plugin
    inventory and the ledger — so they reach the injected text as DATA. Two things
    happen here:

    - whitespace collapses, so a stored newline cannot forge a line of its own (the
      same threat `promptgate._safe_text` guards, kept local so this module stays
      pure);
    - the directive words are lowercased wherever they appear in data, so a stored
      value can never read as one of this module's own instructions. That matters
      most for record gists: they are arbitrary ledger prose, and this project's own
      change-log entries genuinely contain the phrase — quoting one back into an
      injected prompt as though the system were issuing it is the kind of confusion
      worth spending three lines to make impossible."""
    text = " ".join(str(value or "").split())[:width]
    return _MARKER_RE.sub(lambda m: m.group(0).lower(), text)
