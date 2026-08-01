# src/mindhive/promptgate.py
"""Deterministic UserPromptSubmit prompt-quality gate.

When the user's typed prompt is underspecified — vague, target-less,
ambiguous, or explicitly method-skipping — the hook injects *trigger
directives*: "use skill X via the Skill tool before starting" lines chosen
from the live skills/plugins inventory. The rules are permanent (a sidecar
manifest applied to every matching prompt until retrained) and trained from
the ledger: per quality-gap dimension the winning skill is ranked by measured
completion telemetry (``plugins.skill_stats``) with the recommend engine's
learning-gated provenance rules, so an unproven skill is never presented as a
proven one.

Shape mirrors ``enforce.py``: pure predicates over the prompt text, a
``rules/triggers.json`` manifest with atomic writes, and a fail-open
``hook_context`` entry point — any error, missing manifest, or malformed
payload yields no injection; the gate can never break a prompt.
"""
from __future__ import annotations

import json
import os
import re

from . import paths

TRIGGER_MARK = "[mindhive-promptgate]"

# Soft findings need SOFT_MIN together to fire; one hard finding fires alone.
SOFT_MIN = 2
MIN_CONTENT_WORDS = 4     # fewer content words than this reads as "too short"
SHORT_PROMPT_WORDS = 15   # dangling references only matter in short prompts
LEAD_WINDOW = 6           # a pronoun this early usually has no antecedent
# assess() runs on EVERY prompt; past this size the text is treated as clear
# without scanning. Vagueness is a property of short prompts by definition,
# and the cap bounds the target regexes' worst-case backtracking (a pasted
# 100KB unbroken run of dots measured ~100s unscanned-capped vs ~ms capped).
PROMPT_SCAN_CAP = 2000

# Deliberately a separate tokenizer from recommend._keywords: that one scores
# skill DESCRIPTIONS (where "use"/"when" are noise), this one scores typed
# PROMPTS (where politeness words like "please" are the noise). Keep the two
# vocabularies independent so tuning one never silently shifts the other.
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "and", "any", "are", "can", "each", "for", "from", "has", "have", "how",
    "into", "its", "not", "that", "the", "them", "then", "they", "this",
    "use", "used", "when", "will", "with", "you", "your", "please",
}

# Concrete-target signals: a prompt naming any of these is specific enough
# that the vagueness dimensions stand down.
_TARGET_RES = (
    # file path / extension; the lookahead keeps prose abbreviations
    # (e.g., i.e., etc., vs.) from reading as a concrete file target
    re.compile(r"\b(?!(?:e\.g|i\.e|etc|vs)\b)[\w./\\-]{2,}\.[A-Za-z]{1,4}\b"),
    re.compile(r"`[^`]+`"),                          # backtick span
    # double quotes only: two apostrophes ("don't … it's") must not read as a
    # quoted span and silently suppress the gate
    re.compile(r"\"[^\"]{3,}\""),                    # quoted span
    re.compile(r"#\d+\b"),                           # issue / PR number
    re.compile(r"https?://"),                        # URL
    re.compile(r"\b\w+_\w+\b"),                      # snake_case identifier
    re.compile(r"\b[a-z]+[A-Z]\w*\b"),               # camelCase identifier
)

_VAGUE_VERBS = {
    "improve", "enhance", "optimize", "optimise", "fix", "clean", "cleanup",
    "polish", "refactor", "upgrade", "modernize", "beautify", "streamline",
}
_AMBIGUITY_MARKERS = {
    "somehow", "maybe", "something", "stuff", "whatever", "somewhere",
    "anything", "everything", "etc",
}
_LEAD_PRONOUNS = {"it", "this", "that", "them"}

# Imperative method-skip phrases only, word-bounded. Descriptive state
# ("the module has no tests", "there is no plan yet") is what a prompt may
# legitimately REPORT — only an instruction to skip the method is a finding —
# and word boundaries keep innocent containing strings ("casino plan") from
# firing the hard gate.
_METHOD_SKIP_RES = tuple(re.compile(r"\b" + re.escape(p) + r"\b") for p in (
    "skip the plan", "without a plan", "skip planning",
    "just do it", "just wing it", "quick and dirty",
    "skip tests", "without tests", "skip the tests",
    "don't test", "dont test", "no need to test",
    "skip review", "skip the review",
))

# Quality-gap dimensions. ``need`` is the remediation-need text a skill
# description must match to fill the dimension (curated, like
# plugins.CURATED_NOTES); the SKILL that fills it is picked by training from
# measured ledger data, never hardcoded here.
DIMENSIONS = {
    "too-short": {
        "hard": False,
        "label": "very few content words and no concrete target",
        "need": "plan break down structured multi-step task goal requirements",
        "remedy": "break the request into a concrete plan before acting",
    },
    "vague-verb": {
        "hard": False,
        "label": "an improvement verb with no concrete target",
        "need": "plan break down requirements scope clarify goal",
        "remedy": "pin down the target and scope in a plan first",
    },
    "dangling-reference": {
        "hard": False,
        "label": "a leading pronoun with no stated antecedent",
        "need": "plan clarify requirements scope questions goal",
        "remedy": "confirm what the reference points at before acting",
    },
    "ambiguity-markers": {
        "hard": False,
        "label": "hedge words that leave the intent open",
        "need": "plan requirements clarify scope structured breakdown",
        "remedy": "resolve the open intent into explicit requirements",
    },
    "method-skip": {
        "hard": True,
        "label": "asks to skip the proven method (plan/tests/review)",
        "need": "verification before claiming complete evidence tests "
                "implementation code",
        "remedy": "keep the method: plan, tests, and review still apply",
    },
}

# --- MN-14: the always-on catalog pass ----------------------------------------
#
# The gate above only fires on a prompt that looks BAD. MindNetwork's founding
# requirement is stronger and different: the system should "secara paksa
# mengoptimalisasi dan improvisasi untuk prompt ... pada pembangunan kode sekecil
# apapun ... dengan membuka katalog skill and plugin". Forcibly — for code work
# however small — open the catalog and add the context the model needs.
#
# A well-specified prompt got nothing, which is exactly the case the requirement
# names: "sekecil apapun" is not "only when you phrased it badly". So a second,
# independent pass runs on any prompt that reads as code work and surfaces the
# trained catalog entry for it.
#
# It is the same channel as the gate — `additionalContext`, additive, never a
# decision. Forcing CONTEXT is what was asked for; denying the user's own prompt
# would be a different and hostile thing, and is not built here.
ALWAYS_KEY = "code-work"
ALWAYS_SPEC = {
    "label": "code work — the catalog was consulted",
    "need": "implementation code test verification plan review structured "
            "engineering method",
    "remedy": "open it before the first edit, not after the work is done",
}

# Code-intent signals. Deliberately narrow: a false positive costs a few injected
# lines on a prompt that did not need them, but every miss is the requirement not
# being met, so the list covers the ordinary vocabulary of asking for code work
# rather than trying to be clever.
_CODE_INTENT_RES = (
    re.compile(r"\b(?:code|coding|implement|implementation|refactor|refactoring)\b"),
    re.compile(r"\b(?:bug|debug|fix|patch|repair|broken|crash|error|failing)\b"),
    re.compile(r"\b(?:test|tests|testing|pytest|coverage|regression)\b"),
    re.compile(r"\b(?:feature|function|method|class|module|api|endpoint)\b"),
    re.compile(r"\b(?:build|ship|deploy|release|migrate|migration)\b"),
    re.compile(r"\b(?:review|audit|lint|typecheck|ci|pipeline)\b"),
    re.compile(r"\b(?:commit|branch|merge|rebase|pr|pull request)\b"),
    # Indonesian, because the user writes their prompts in it and an
    # English-only detector would answer "not code work" for most of them.
    re.compile(r"\b(?:kode|koding|perbaiki|perbaikan|buatkan|bikin|tambahkan|"
               r"ubah|hapus|periksa|uji|fitur|berkas|terapkan|jalankan)\b"),
    # Symptom reports that never say "bug". "kucing di dashboard tidak muncul" is a
    # bug report with no keyword from any list above, and it read as conversation —
    # the catalog stayed shut on exactly the prompt that needed the debugging skill.
    # A false positive here costs a few injected lines; a miss costs the whole
    # feature on the phrasing this user actually uses.
    re.compile(r"\btidak\s+(?:mun[cs]ul|jalan|bisa|berfungsi|bekerja|ke?luar|"
               r"tampil|terbaca|tersimpan|jadi)\b"),
    re.compile(r"\b(?:kenapa|mengapa)\b.{0,40}\b(?:tidak|gagal|error|salah)\b"),
    re.compile(r"\b(?:macet|hilang|salah|aneh|lambat|nge?blank)\b"),
    # A named file or a fenced/backticked span is code work whatever the verb.
    re.compile(r"\b[\w./\\-]{2,}\.(?:py|js|ts|tsx|jsx|md|json|ya?ml|toml|"
               r"cfg|ini|sh|ps1|rs|go|java|rb|c|h|cpp)\b"),
)


def _content_words(text: str) -> list[str]:
    return [w for w in _WORD.findall(str(text or "").lower())
            if len(w) >= 3 and w not in _STOP]


def _has_target(text: str) -> bool:
    t = str(text or "")
    return any(rx.search(t) for rx in _TARGET_RES)


def is_machine_prompt(text: str) -> bool:
    """True for prompt bodies the user did not actually type: injected tags
    (task notifications, system reminders), slash-command envelopes, and
    injected skill bodies."""
    s = str(text or "").lstrip()
    if not s:
        return True
    if s.startswith("<") or s.startswith("/"):
        return True
    return s[:40].lower().startswith("base directory for this skill:")


def is_code_work(text: str) -> bool:
    """Whether *text* reads as a request to build, change or check code.

    Pure and cheap — it runs on every prompt beside `assess`. Machine-injected text
    answers False for the same reason the gate skips it: a task notification or an
    injected skill body is not the user asking for anything. Past
    `PROMPT_SCAN_CAP` it also answers False, so a pasted log cannot turn into a
    100KB regex scan on the hook's critical path."""
    if is_machine_prompt(text):
        return False
    t = str(text or "")
    if len(t) > PROMPT_SCAN_CAP:
        return False
    low = " ".join(t.lower().split())
    return any(rx.search(low) for rx in _CODE_INTENT_RES)


def assess(text: str) -> dict:
    """Deterministic prompt-quality verdict:
    ``{skip: bool, clear: bool, findings: [{dimension, hard, label}]}``.

    ``skip`` means the text is not a human-typed prompt (nothing to gate).
    ``clear`` means it passed: no hard finding and fewer than SOFT_MIN soft
    ones. Pure — no disk, ledger, or inventory access."""
    if is_machine_prompt(text):
        return {"skip": True, "clear": True, "findings": []}
    t = str(text or "")
    if len(t) > PROMPT_SCAN_CAP:
        return {"skip": False, "clear": True, "findings": []}
    low = " ".join(t.lower().split())
    # Regex tokens (not whitespace split) everywhere, so punctuation never
    # hides a word from a check ("it, please fix" must still lead with "it").
    tokens = _WORD.findall(low)
    content = _content_words(t)
    target = _has_target(t)
    findings = []

    def hit(dim):
        d = DIMENSIONS[dim]
        findings.append({"dimension": dim, "hard": d["hard"], "label": d["label"]})

    if len(content) < MIN_CONTENT_WORDS and not target:
        hit("too-short")
    if not target and any(w in _VAGUE_VERBS for w in tokens):
        hit("vague-verb")
    if (not target and len(tokens) < SHORT_PROMPT_WORDS
            and any(w in _LEAD_PRONOUNS for w in tokens[:LEAD_WINDOW])):
        hit("dangling-reference")
    if any(w in _AMBIGUITY_MARKERS for w in tokens):
        hit("ambiguity-markers")
    if any(rx.search(low) for rx in _METHOD_SKIP_RES):
        hit("method-skip")

    hard = any(f["hard"] for f in findings)
    soft = sum(1 for f in findings if not f["hard"])
    return {"skip": False, "clear": not (hard or soft >= SOFT_MIN),
            "findings": findings}


# --- training: pick each dimension's skill from measured ledger data ----------

def _pick_skill(need: str, skills, stats, open_incidents=None,
                skill_rules=None) -> dict | None:
    """Best inventory skill for one dimension's need text. Deterministic
    tie-break chain (recommend-engine order): measured beats unmeasured →
    higher completion rate → stronger need match → shorter description →
    name. None when nothing in the inventory matches the need at all.

    Mirrors the recommend engine's learning gates: a skill discontinued by
    the improvements linkage (active ``skill:*`` rule + DISCONTINUE_STREAK
    trailing failures) is excluded entirely, and ``must`` wording — like
    execution_mode auto — additionally requires no open skill incident."""
    from . import recommend
    open_inc = open_incidents or {}
    rules = skill_rules or {}
    best = None
    best_key = None
    for s in sorted((skills or []), key=lambda s: str(s.get("name", ""))):
        name = s.get("name")
        if not name:
            continue
        nm = recommend.need_match(s.get("description", ""), [need])
        if nm <= 0:
            continue
        st = (stats or {}).get(name) or {}
        calls = int(st.get("calls") or 0)
        rate = st.get("completion_rate")
        fail_streak = int(st.get("fail_streak") or 0)
        if rules.get(name) and fail_streak >= recommend.DISCONTINUE_STREAK:
            continue                     # discontinued — never recommend it
        measured = calls >= recommend.MEASURED_MIN_CALLS and rate is not None
        key = (1 if measured else 0, rate if measured else -1.0, nm,
               -len(str(s.get("description") or "")))
        if best_key is None or key > best_key:
            best_key = key
            best = {
                "skill": name,
                "plugin": s.get("plugin", ""),
                "provenance": "measured" if measured else "estimated",
                "calls": calls,
                "ok": int(st.get("ok") or 0),
                "completion_rate": rate,
                "need_match": nm,
                "directive": ("must" if measured
                              and (rate or 0.0) >= recommend.AUTO_MIN_RATE
                              and not open_inc.get(name)
                              else "try"),
            }
    return best


def observed_counts(evs) -> dict:
    """How often each dimension actually fired across the ledger's past
    human-typed ``user_prompt`` events — the trained rules' grounding in the
    project's real history, not hypothetical prompts."""
    counts = {dim: 0 for dim in DIMENSIONS}
    scanned = 0
    for e in (evs or []):
        if getattr(e, "type", None) != "user_prompt":
            continue
        p = getattr(e, "payload", None)
        text = p.get("text", "") if isinstance(p, dict) else ""
        verdict = assess(text)
        if verdict["skip"]:
            continue
        scanned += 1
        for f in verdict["findings"]:
            counts[f["dimension"]] += 1
    return {"prompts_scanned": scanned, "by_dimension": counts}


def train(skills, stats, evs=None, now=None, incident_summaries=None) -> dict:
    """Pure trainer: derive the full trigger manifest from a flattened skill
    inventory, per-skill telemetry, (optionally) the event history used for
    observed counts, and (optionally) incident summaries powering the
    recommend-engine learning gates. Dimensions no inventory skill matches
    are omitted — the gate stays silent for them rather than inventing a
    directive."""
    from . import events as ev_mod
    from . import recommend
    observed = observed_counts(evs)
    open_inc = recommend.open_skill_incidents(incident_summaries)
    skill_rules = recommend.active_skill_rules(incident_summaries)
    rules = {}
    for dim, spec in DIMENSIONS.items():
        pick = _pick_skill(spec["need"], skills, stats,
                           open_incidents=open_inc, skill_rules=skill_rules)
        if pick is None:
            continue
        rules[dim] = dict(pick, label=spec["label"], remedy=spec["remedy"],
                          need=spec["need"],
                          observed=observed["by_dimension"].get(dim, 0))
    # MN-14: the always-on catalog entry, trained through the SAME picker as every
    # dimension. A second selection path here would be a second thing to keep
    # honest about measured-vs-unmeasured provenance, and the first time they
    # disagreed the always-on line would be the one claiming a skill is proven when
    # the gate's own ranking says otherwise.
    always_pick = _pick_skill(ALWAYS_SPEC["need"], skills, stats,
                              open_incidents=open_inc, skill_rules=skill_rules)
    always = (dict(always_pick, label=ALWAYS_SPEC["label"],
                   remedy=ALWAYS_SPEC["remedy"], need=ALWAYS_SPEC["need"])
              if always_pick else {})
    # MN-15: the per-phase playbook — reliable skills plus the ledger records worth
    # reading. Built HERE because this function already holds the inventory, the
    # telemetry and `evs`; the live store is 22.8k events and `read_all` measures
    # 1.09 s, so a hook that rebuilt this per prompt would several-fold the latency
    # of every prompt the user types.
    from . import playbook as pb_mod
    play = pb_mod.build(skills, stats, evs=evs, open_incidents=open_inc,
                        skill_rules=skill_rules)
    measured_n = sum(1 for r in rules.values() if r["provenance"] == "measured")
    return {
        "trained_at": now or ev_mod.now_iso(),
        "coverage": {
            "skills_total": len(skills or []),
            "rules": len(rules),
            "measured_rules": measured_n,
            "prompts_scanned": observed["prompts_scanned"],
            "playbook_phases": len(play.get("phases") or {}),
            "playbook_records": len(play.get("records") or []),
            # What the record pool dropped, and by which mechanism. Reported because
            # the shrinkage was previously undisclosed AND misattributed: the pool cap
            # is never reached on a real store, and dedupe is what removes the rows.
            "playbook_pool": dict(play.get("pool") or {}),
        },
        "rules": rules,
        "always": always,
        "playbook": play,
    }


# --- manifest (rules/triggers.json) -------------------------------------------

def _manifest_path(project=None):
    return paths.project_dir(project) / "rules" / "triggers.json"


def load_manifest(project=None) -> dict:
    """The trained trigger manifest, or {} when missing/corrupt (fail-open)."""
    try:
        obj = json.loads(_manifest_path(project).read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_manifest(project, data) -> None:
    p = _manifest_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Per-writer tmp name: two concurrent session-start trainers must never
    # interleave into one tmp file before the atomic rename.
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _age_hours(ts: str):
    import calendar
    import time as _time
    try:
        t = _time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return (_time.time() - calendar.timegm(t)) / 3600.0
    except Exception:
        return None


def train_and_store(project=None, max_age_hours: float = 0.0) -> dict:
    """Train from the live inventory + ledger and overwrite the manifest
    sidecar (plugin_catalog precedent: derived, retrainable data stays out of
    the append-only chain). One small ``trigger_trained`` event per real train
    keeps retraining auditable. ``max_age_hours`` > 0 makes the call
    stale-gated: a fresh manifest is returned untouched, so session hooks can
    fire this on every start for free."""
    from . import events as ev_mod
    from . import incidents as incidents_mod
    from . import plugins, recommend
    if max_age_hours > 0:
        existing = load_manifest(project)
        age = _age_hours(str(existing.get("trained_at", "")))
        # A negative age (future trained_at from a clock rollback or a
        # hand-edited manifest) must count as stale, not freeze the gate.
        if age is not None and 0 <= age < max_age_hours:
            return {"trained": False, "age_hours": age, "manifest": existing}
    evs = ev_mod.read_all(project)
    skills = recommend.flatten_inventory(plugins.live_inventory()["inventory"])
    stats = plugins.skill_stats(project=project, evs=evs)
    manifest = train(skills, stats, evs=evs,
                     incident_summaries=incidents_mod.summaries(project))
    _write_manifest(project, manifest)
    # Best-effort audit: the manifest is the source of truth; a failed append
    # must not crash the detached background trainer after a good write.
    # session_id "manual": the detached session-start trainer inherits the
    # live session's MINDHIVE_SESSION, and letting it append to that session's
    # segment file would race the session's own hook captures.
    try:
        ev_mod.append_event(
            "trigger_trained",
            {"text": (f"trained {manifest['coverage']['rules']} prompt-quality "
                      f"trigger rule(s) from "
                      f"{manifest['coverage']['skills_total']} skills"),
             "rules": {d: r["skill"] for d, r in manifest["rules"].items()}},
            session_id="manual", tags=["trigger", "train"], project=project)
    except Exception:
        pass
    return {"trained": True, "age_hours": 0.0, "manifest": manifest}


# --- the hook: assess the prompt, render directives ----------------------------

def _safe_text(value, width: int = 80) -> str:
    """Whitespace-collapsed, length-capped rendering of an inventory-supplied
    string. Skill/plugin names come from local SKILL.md frontmatter and flow
    verbatim into agent-steering context — a newline or essay-sized name must
    not be able to smuggle extra directive lines into the injection."""
    return " ".join(str(value or "").split())[:width]


def render_context(findings, rules) -> str:
    """The additionalContext block for a failed gate: what looked
    underspecified, then one directive line per firing dimension that has a
    trained rule (deduped by skill, MUST USE only for proven ones — R7)."""
    labels = "; ".join(f["label"] for f in findings)
    lines = [f"{TRIGGER_MARK} Prompt-quality gate: this prompt looks "
             f"underspecified — {labels}. Before starting:"]
    seen = set()
    for f in findings:
        r = (rules or {}).get(f["dimension"])
        if not isinstance(r, dict) or not r.get("skill"):
            continue
        if r["skill"] in seen:
            continue
        seen.add(r["skill"])
        skill = _safe_text(r["skill"])
        plugin = _safe_text(r.get("plugin"))
        where = f" ({plugin})" if plugin else ""
        if r.get("directive") == "must":
            lines.append(
                f"- MUST USE: {skill}{where} via the Skill tool — "
                f"measured {r.get('ok', 0)}/{r.get('calls', 0)} completion; "
                f"{r.get('remedy', '')}.")
        else:
            lines.append(
                f"- TRY & MEASURE: {skill}{where} via the Skill tool (not "
                f"ad-hoc) so its completion rate gets measured; "
                f"{r.get('remedy', '')}.")
    if len(lines) == 1:
        return ""          # findings fired but no trained rule covers any
    lines.append("- If the target files or acceptance criteria are genuinely "
                 "missing, ask the user to specify them before large-scale "
                 "work.")
    return "\n".join(lines)


def render_always(always, exclude=()) -> str:
    """MN-14's block: the catalog entry for ordinary code work.

    *exclude* is the set of skill names the quality gate already directed in the
    same injection. Repeating a skill under two headings reads like two different
    demands, and the model has to guess whether it is being told the same thing
    twice or something new."""
    if not isinstance(always, dict) or not always.get("skill"):
        return ""
    if always["skill"] in set(exclude):
        return ""
    skill = _safe_text(always["skill"])
    plugin = _safe_text(always.get("plugin"))
    where = f" ({plugin})" if plugin else ""
    lead = (f"{TRIGGER_MARK} Code work — skill & plugin catalog consulted. "
            f"Before the first edit:")
    if always.get("directive") == "must":
        line = (f"- MUST USE: {skill}{where} via the Skill tool — measured "
                f"{always.get('ok', 0)}/{always.get('calls', 0)} completion; "
                f"{always.get('remedy', '')}.")
    else:
        line = (f"- TRY & MEASURE: {skill}{where} via the Skill tool (not ad-hoc) "
                f"so its completion rate gets measured; "
                f"{always.get('remedy', '')}.")
    return "\n".join([lead, line])


def directed_skills(findings, rules) -> list:
    """The skills the gate would direct for these findings, one per firing
    dimension that has a trained rule, deduped by skill name. Each entry:
    ``{name, plugin?, measured?}`` — ``measured`` is ``{ok, calls}`` only when
    the rule is a proven MUST-USE (directive ``must``). Pure."""
    out, seen = [], set()
    for f in findings or []:
        r = (rules or {}).get(f.get("dimension"))
        if not isinstance(r, dict):
            continue
        name = r.get("skill")
        if not name or name in seen:
            continue
        seen.add(name)
        entry = {"name": name}
        plugin = r.get("plugin")
        if plugin:
            entry["plugin"] = plugin
        if r.get("directive") == "must":
            entry["measured"] = {"ok": r.get("ok", 0), "calls": r.get("calls", 0)}
        out.append(entry)
    return out


def catalog_block(prompt, manifest, exclude=()):
    """`(text, skills)` — the MN-14/MN-15 catalog block and the skills it names.

    ONE decision with two consumers: `hook_context` prints the text, `record_direction`
    records the skills. Deciding "playbook or the single-skill fallback" separately in
    each would let the causal trail claim a direction the block never rendered, and
    nothing in the system would surface the disagreement.

    Deliberately NOT memoized, even though the two callers compute it twice per prompt
    (~0.08 s of a 0.36 s hook). The two load the manifest separately, so any cache key
    cheap enough to be worth it — object identity, or a content fingerprint — is either
    a guaranteed miss or a staleness hazard where a caller with a different manifest
    gets the previous answer. Correctness in a hook that runs on every prompt is worth
    more than 80 ms, and the honest number is reported rather than optimized away.

    Returns `("", [])` when there is nothing to say. Pure — the manifest is passed in."""
    if not is_code_work(prompt):
        return "", []
    from . import playbook as pb_mod
    play = (manifest or {}).get("playbook")
    phase = pb_mod.infer(prompt)
    body = pb_mod.render(play, phase, prompt, exclude=exclude)
    if body:
        return (f"{TRIGGER_MARK} Code work — skill & plugin catalog consulted.\n"
                f"{body}"), pb_mod.directed_skills(play, phase, exclude=exclude)
    # Fallback to MN-14's single trained skill. Measured as live, not dead: 33 of the
    # 191 distinct real code prompts still infer no technique at all.
    always = (manifest or {}).get("always")
    text = render_always(always, exclude=exclude)
    if not text:
        return "", []
    entry = {"name": _safe_text(always.get("skill"))}
    plugin = _safe_text(always.get("plugin"))
    if plugin:
        entry["plugin"] = plugin
    if always.get("directive") == "must":
        entry["measured"] = {"ok": int(always.get("ok") or 0),
                             "calls": int(always.get("calls") or 0)}
    return text, [entry]


def announce_faculties(faculties, project=None, session_id=None) -> int:
    """Report each faculty in *faculties* as having woken and finished. Returns the
    number of pairs written.

    An `active` row and an `idle` row together, not a `busy` block, because by the time
    this is called the work is done — the prompt's block has already been rendered and
    printed. `ui_cats` hides an idle faculty once its 15-second flicker grace passes, so
    the pair is exactly what makes the cat appear and then vanish, which is the behaviour
    that was asked for ("ketika tidak sedang berjalan maka kucing akan menghilang").

    Never raises. A cat animation that cannot be logged must not disturb the prompt —
    the same contract `telemetry.emit` and `record_direction` already keep."""
    from .mindnet import telemetry as tel_mod
    written = 0
    for faculty in sorted(faculties or ()):
        try:
            if tel_mod.emit(faculty, tel_mod.STATUS_ACTIVE, project=project,
                            session_id=session_id) is None:
                continue
            tel_mod.emit(faculty, tel_mod.STATUS_IDLE, project=project,
                         session_id=session_id)
            written += 1
        except Exception:                                   # noqa: BLE001
            continue
    return written


def record_direction(raw: str, project=None):
    """Best-effort: append one ``skill_directed`` event naming every skill this prompt's
    injection actually directed, so Oversee can later show the honest causal "why" and
    training can tell a suggestion that gets taken from one that never does.

    Covers BOTH paths. It used to return early on a clear verdict, which meant a
    well-phrased code prompt directed skills through the MN-14/MN-15 catalog and
    recorded nothing at all — exactly the case MN-14 opened up, and the reason there was
    no feedback signal to learn from.

    Returns the event or None. Never raises — a write failure must never disturb the
    UserPromptSubmit hook (fail-open, like the gate)."""
    try:
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return None
        prompt = payload.get("prompt", "")
        verdict = assess(prompt)
        if verdict["skip"]:
            return None
        manifest = load_manifest(project)
        skills = ([] if verdict["clear"]
                  else directed_skills(verdict["findings"], manifest.get("rules")))
        # One event, not two: the gate's directions and the catalog's are the same kind
        # of claim about the same prompt, and `oversee` consumes one direction per
        # skill. The catalog is asked to exclude what the gate already named, matching
        # what `hook_context` printed.
        exclude = {s["name"] for s in skills}
        _text, catalog = catalog_block(prompt, manifest, exclude=exclude)
        named = {s["name"] for s in skills}
        skills += [s for s in catalog if s["name"] not in named]
        # MN-17: light the cats for what actually ran. This sits here, in the
        # side-effect function, rather than in `hook_context` — that one has to stay
        # pure and deterministic because tests pin its bytes, and a ledger write inside
        # it would break both. Announced BEFORE the early return below, because a
        # faculty can engage on a prompt that directs no skill at all (records surfaced,
        # no skill matched), and the operator still needs to see that it ran.
        if is_code_work(prompt):
            from . import playbook as pb_ann
            announce_faculties(
                pb_ann.engaged(manifest.get("playbook"), pb_ann.infer(prompt),
                               prompt, exclude=exclude),
                project=project, session_id=payload.get("session_id") or None)
        if not skills:
            return None
        from . import events as ev_mod
        # session_id from the hook payload so the event lands in the SAME
        # segment as the skill_used it will be joined with (matches how
        # hookcapture stamps skill_used); default fallback if absent.
        sid = payload.get("session_id") or None
        return ev_mod.append_event("skill_directed", {"skills": skills},
                                   session_id=sid, tags=["skill", "gate", "auto"],
                                   project=project)
    except Exception:
        return None


def hook_context(raw: str, project=None) -> dict:
    """Parse a UserPromptSubmit payload (JSON on the hook's stdin) and return the
    hook output: additive context, or {} when there is nothing to say.

    Two independent passes, joined into one injection:

    - the **quality gate** — fires only on a prompt that looks underspecified;
    - the **catalog pass** (MN-14) — fires on any prompt that reads as code work,
      however small and however well phrased, because that is what the founding
      requirement asked for. A clear prompt used to get nothing at all.

    Additive only — never a block decision. Never raises; any error is an empty
    (silent) result, because a gate that can break a prompt is worse than a gate
    that occasionally says nothing."""
    try:
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return {}
        prompt = payload.get("prompt", "")
        verdict = assess(prompt)
        if verdict["skip"]:
            return {}
        # One manifest read serves both passes. Reading it twice would double the
        # hook's disk cost on every prompt for no gain.
        manifest = load_manifest(project)
        blocks, directed = [], set()
        if not verdict["clear"]:
            gate = render_context(verdict["findings"], manifest.get("rules"))
            if gate:
                blocks.append(gate)
                directed = {s["name"] for s in
                            directed_skills(verdict["findings"],
                                            manifest.get("rules"))}
        # The catalog block: the playbook when a technique can be inferred, MN-14's
        # single trained skill otherwise. `catalog_block` owns that choice so the
        # recorded causal trail cannot disagree with what was printed.
        catalog, _skills = catalog_block(prompt, manifest, exclude=directed)
        if catalog:
            blocks.append(catalog)
        if not blocks:
            return {}
        return {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(blocks),
        }}
    except Exception:
        return {}
