# src/mindhive/enrich.py
"""Deterministic prompt enrichment for the Oversee section.

Takes a draft prompt and composes an enriched, specification-shaped version
per the Anthropic prompt-engineering SOP (project SOP 5: scope, constraints,
and the verification the output must pass), gathering context three ways:

1. **Asking the user** — clarifying questions derived from the promptgate
   quality-gap findings; typed answers fold back into the composed prompt.
2. **Checking the source code** — candidate target files from the ledger's
   own record of the project's code (``file_touched`` history) plus a
   bounded filename scan of the autosync-registered repo folder.
3. **Injecting skills** — directives from the trained triggers manifest for
   the firing dimensions, plus inventory suggestions ranked by measured
   completion telemetry (promptgate's provenance rules: an unproven skill is
   never presented as a proven one).

The feature is **enabled by default** and toggleable per project via a small
``enrich.json`` sidecar (triggers.json precedent: config/derived data stays
out of the append-only chain); every toggle appends an ``enrich_toggled``
audit event, and recorded enrichment acts append ``prompt_enriched``. Shape
mirrors ``promptgate.py``: pure functions, fail-open everywhere — enrichment
can never break a prompt, a hook, or the dashboard.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import paths, promptgate

ENRICH_MARK = "[mindhive-enrich]"
DEFAULT_ENABLED = True          # the feature is ON unless explicitly disabled

QUESTION_CAP = 4                # clarifying questions per prompt
SOURCE_CAP = 6                  # source-file candidates per prompt
SKILL_CAP = 3                   # general skill suggestions per prompt
RECORD_TEXT_CAP = 200           # chars of the raw prompt kept in the audit event
SCAN_FILE_CAP = 4000            # repo-scan bound: files visited, not matched
SCAN_PRUNE = {".git", ".hg", ".svn", "node_modules", ".venv", "venv",
              "__pycache__", "dist", "build", ".claude", ".mindhive",
              ".hive-loop"}

_PATH_TOKEN = re.compile(r"[a-z0-9]+")

# One clarifying question per promptgate dimension — what the USER must
# resolve, phrased as a question (the gate's remedy lines steer the agent;
# these steer the human).
_QUESTIONS = {
    "too-short": "What exactly should change, and where? Name the file, "
                 "module, or feature.",
    "vague-verb": "What does improved look like here — which concrete target "
                  "changes, and what is the visible outcome?",
    "dangling-reference": "What does the leading reference (it/this/that) "
                          "point at?",
    "ambiguity-markers": "Which of the open options do you actually want? "
                         "Replace the hedge words with an explicit choice.",
    "method-skip": "The proven method (plan, tests, review) still applies — "
                   "which constraint made you want to skip it?",
}

# Verification vocabulary: a prompt naming any of these is treated as having
# stated what "done" looks like (SOP 5.4: specification-shaped prompts carry
# the verification the output must pass).
_VERIFY_WORDS = {
    "test", "tests", "tested", "verify", "verified", "verification", "green",
    "acceptance", "criteria", "assert", "asserts", "asserting", "expect",
    "expected", "passes", "passing", "regression",
}


def _tokens(text: str) -> set:
    return set(_PATH_TOKEN.findall(str(text or "").lower()))


def verification_stated(text: str) -> bool:
    return bool(_tokens(text) & _VERIFY_WORDS)


# --- settings: default-on toggle (sidecar + audit event) -----------------------

def _settings_path(project=None) -> Path:
    return paths.project_dir(project) / "enrich.json"


def settings(project=None) -> dict:
    """The persisted toggle, or the default (enabled) when the sidecar is
    missing or corrupt — the feature must fail toward ON, never toward a
    silent, unexplained OFF."""
    try:
        obj = json.loads(_settings_path(project).read_text(encoding="utf-8"))
        if isinstance(obj, dict) and isinstance(obj.get("enabled"), bool):
            return {"enabled": obj["enabled"],
                    "updated": obj.get("updated"), "source": "sidecar"}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {"enabled": DEFAULT_ENABLED, "updated": None, "source": "default"}


def set_enabled(enabled: bool, project=None, session_id=None) -> dict:
    """Persist the toggle (atomic sidecar write — the source of truth) and
    append an ``enrich_toggled`` audit event, best-effort: a failed append
    must not lose the toggle itself (train_and_store precedent)."""
    from . import events as ev_mod
    enabled = bool(enabled)
    data = {"enabled": enabled, "updated": ev_mod.now_iso()}
    paths.write_text_atomic(_settings_path(project),
                            json.dumps(data, indent=2, sort_keys=True))
    try:
        ev_mod.append_event(
            "enrich_toggled",
            {"enabled": enabled,
             "text": f"prompt enrichment turned {'on' if enabled else 'off'}"},
            session_id=session_id, tags=["enrich", "config"], project=project)
    except Exception:
        pass
    return dict(data, source="sidecar")


# --- 1. asking the user: clarifying questions ----------------------------------

def clarifying_questions(verdict, text) -> list[dict]:
    """Questions the user should answer before the prompt is run: one per
    firing quality-gap dimension, plus the verification question whenever the
    prompt never says what done looks like. Capped, deterministic order."""
    out = []
    for f in (verdict or {}).get("findings", []):
        q = _QUESTIONS.get(f.get("dimension"))
        if q:
            out.append({"id": f["dimension"], "text": q})
    if not verification_stated(text):
        out.append({"id": "verification",
                    "text": "What does done look like — which test, output, "
                            "or behavior proves it?"})
    return out[:QUESTION_CAP]


# --- 2. checking the source code: candidate target files -----------------------

def _match_count(keywords, path_tokens) -> int:
    n = 0
    for kw in keywords:
        variants = {kw, kw + "s"}
        if kw.endswith("s"):
            variants.add(kw[:-1])       # plural keyword vs singular path token
        if variants & path_tokens:
            n += 1
    return n


def registered_root(project=None):
    """The autosync-registered repo folder for this project, or None. The
    registry is the one place the store already knows where the project's
    working copy lives — no new configuration surface needed."""
    try:
        from . import autosync
        want = paths.normalize(project or paths.detect_project())
        for e in autosync.load_registry():
            if isinstance(e, dict) and e.get("folder") and \
                    paths.normalize(str(e.get("project") or "")) == want:
                p = Path(e["folder"])
                if p.is_dir():
                    return p
    except Exception:
        pass
    return None


def source_candidates(text, evs=None, project=None, root=None,
                      cap=SOURCE_CAP) -> list[dict]:
    """Likely target files for the prompt, ranked deterministically: keyword
    overlap first, then how often the project's sessions touched the file,
    then recency. Ledger history first (the project's real code surface),
    then a bounded scan of the registered repo folder. Fail-open: reads stay
    total, a broken scan yields fewer candidates, never an error."""
    keywords = {w for w in promptgate._content_words(text)}
    if not keywords:
        return []
    if evs is None:
        from . import events as ev_mod
        try:
            evs = ev_mod.read_all(project)
        except Exception:
            evs = []
    touched: dict[str, dict] = {}
    for e in evs or []:
        if getattr(e, "type", None) != "file_touched":
            continue
        p = e.payload if isinstance(e.payload, dict) else {}
        path = str(p.get("path") or p.get("text") or "").strip()
        if not path:
            continue
        rec = touched.setdefault(path, {"count": 0, "last": ""})
        rec["count"] += 1
        rec["last"] = max(rec["last"], getattr(e, "ts", "") or "")
    scored = []
    for path, rec in touched.items():
        overlap = _match_count(keywords, _tokens(path))
        if overlap:
            scored.append({"path": path, "overlap": overlap,
                           "touched": rec["count"], "last": rec["last"],
                           "source": "history",
                           "why": f"matches {overlap} prompt keyword(s), "
                                  f"touched {rec['count']}x"})
    # Three stable passes: path asc as final tiebreak, then recency desc
    # (ISO strings can't be negated in a single key), then the primary rank.
    scored.sort(key=lambda s: s["path"])
    scored.sort(key=lambda s: s["last"], reverse=True)
    scored.sort(key=lambda s: (-s["overlap"], -s["touched"]))
    out = scored[:cap]
    if len(out) < cap:
        out.extend(_scan_candidates(keywords, project=project, root=root,
                                    have=[s["path"] for s in out],
                                    cap=cap - len(out)))
    for s in out:
        s.pop("overlap", None)
    return out


def _scan_candidates(keywords, project=None, root=None, have=(), cap=3):
    """Bounded filename scan of the registered repo folder: deterministic
    (sorted walk), pruned, capped by files VISITED so a huge repo can never
    stall the dashboard. Never raises."""
    out = []
    try:
        root = Path(root) if root else registered_root(project)
        if not root or cap <= 0:
            return []
        have_norm = [str(h).lower().replace("\\", "/") for h in have]
        visited = 0
        matches = []
        for cur, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SCAN_PRUNE)
            for name in sorted(files):
                visited += 1
                if visited > SCAN_FILE_CAP:
                    dirs[:] = []
                    break
                rel = os.path.relpath(os.path.join(cur, name), root)
                rel = rel.replace("\\", "/")
                overlap = _match_count(keywords, _tokens(rel))
                if not overlap:
                    continue
                if any(h.endswith(rel.lower()) for h in have_norm):
                    continue        # already listed from history
                matches.append({"path": rel, "overlap": overlap,
                                "touched": 0, "last": "", "source": "scan",
                                "why": f"matches {overlap} prompt keyword(s) "
                                       f"in the repo tree"})
            if visited > SCAN_FILE_CAP:
                break
        matches.sort(key=lambda s: (-s["overlap"], len(s["path"]), s["path"]))
        out = matches[:cap]
    except Exception:
        return []
    return out


# --- 3. injecting skills: manifest directives + inventory suggestions ----------

def manifest_directives(findings, project=None) -> list[dict]:
    """The trained triggers manifest's directives for the firing dimensions —
    exactly what promptgate would inject, as structured data (deduped by
    skill, provenance preserved)."""
    rules = promptgate.load_manifest(project).get("rules") or {}
    out, seen = [], set()
    for f in findings or []:
        r = rules.get(f.get("dimension"))
        if not isinstance(r, dict) or not r.get("skill") or r["skill"] in seen:
            continue
        seen.add(r["skill"])
        out.append({"skill": r["skill"], "plugin": r.get("plugin", ""),
                    "directive": r.get("directive", "try"),
                    "provenance": r.get("provenance", "estimated"),
                    "ok": r.get("ok", 0), "calls": r.get("calls", 0),
                    "remedy": r.get("remedy", "")})
    return out


def skill_suggestions(text, skills=None, stats=None, exclude=(),
                      cap=SKILL_CAP) -> list[dict]:
    """Inventory skills relevant to the prompt itself (not just its quality
    gaps), ranked by the recommend engine's provenance chain: measured beats
    unmeasured → higher completion rate → stronger match. ``must`` wording
    only for measured skills above the auto gate — R7 stands here too."""
    from . import recommend
    if skills is None:
        try:
            from . import plugins
            inv = plugins.live_inventory()["inventory"]
            skills = recommend.flatten_inventory(inv)
            if stats is None:
                stats = plugins.skill_stats()
        except Exception:
            return []
    ranked = []
    for s in sorted(skills or [], key=lambda s: str(s.get("name", ""))):
        name = s.get("name")
        if not name or name in exclude:
            continue
        nm = recommend.need_match(s.get("description", ""), [text])
        if nm <= 0:
            continue
        st = (stats or {}).get(name) or {}
        calls = int(st.get("calls") or 0)
        rate = st.get("completion_rate")
        measured = calls >= recommend.MEASURED_MIN_CALLS and rate is not None
        # recommend-engine order, expressed ascending: measured first, higher
        # rate, stronger match, shorter description, then name.
        key = (0 if measured else 1, -(rate if measured else -1.0), -nm,
               len(str(s.get("description") or "")), name)
        ranked.append((key,
                       {"skill": name, "plugin": s.get("plugin", ""),
                        "provenance": "measured" if measured else "estimated",
                        "ok": int(st.get("ok") or 0), "calls": calls,
                        "directive": ("must" if measured and
                                      (rate or 0.0) >= recommend.AUTO_MIN_RATE
                                      else "try")}))
    ranked.sort(key=lambda t: t[0])
    return [r[1] for r in ranked[:cap]]


# --- the Anthropic-SOP compliance checklist ------------------------------------

def sop_checklist(text, verdict) -> list[dict]:
    """Deterministic compliance check against the prompt-engineering SOP: a
    specification-shaped prompt names its target, carries enough detail,
    leaves no open hedges or dangling references, keeps the method, and
    states its verification. Each criterion: {id, label, met, hint}."""
    dims = {f["dimension"] for f in (verdict or {}).get("findings", [])}
    has_target = promptgate._has_target(text)
    enough = len(promptgate._content_words(text)) >= promptgate.MIN_CONTENT_WORDS
    return [
        {"id": "concrete-target", "label": "concrete target named",
         "met": has_target,
         "hint": "name the file, identifier, or URL the work targets"},
        {"id": "sufficient-detail", "label": "enough detail to act",
         "met": enough or has_target,
         "hint": "say what should change and where, not just the verb"},
        {"id": "unambiguous-intent", "label": "intent free of hedges",
         "met": "ambiguity-markers" not in dims and "vague-verb" not in dims,
         "hint": "replace maybe/something/improve with an explicit choice"},
        {"id": "clear-antecedents", "label": "references resolved",
         "met": "dangling-reference" not in dims,
         "hint": "spell out what it/this/that refers to"},
        {"id": "method-intact", "label": "method kept (plan/tests/review)",
         "met": "method-skip" not in dims,
         "hint": "drop the skip — the proven method still applies"},
        {"id": "verification-stated", "label": "verification stated",
         "met": verification_stated(text),
         "hint": "state which test, output, or behavior proves it done"},
    ]


# --- composition: the enriched, specification-shaped prompt --------------------

def compose(text, questions, answers, sources, skills, checklist) -> str:
    """Deterministic markdown composition per the Anthropic prompt SOP:
    Task → Context (source files) → Clarifications (answers folded in) →
    Skills to apply → Definition of done → remaining SOP gaps."""
    answers = answers if isinstance(answers, dict) else {}
    lines = ["## Task", str(text or "").strip()]
    if sources:
        lines += ["", "## Context — likely source files"]
        for s in sources:
            tail = (f"touched {s['touched']}x, last {s['last']}"
                    if s.get("source") == "history" else "repo scan")
            lines.append(f"- `{s['path']}` ({tail})")
    pending = False
    clar = []
    for q in questions or []:
        a = str(answers.get(q["id"]) or "").strip()
        if a:
            clar.append(f"- {q['text']}\n  Answer: {a}")
        else:
            clar.append(f"- {q['text']}\n  Answer: (unanswered — resolve "
                        f"before running)")
            pending = True
    if clar:
        lines += ["", "## Clarifications"] + clar
    if skills:
        lines += ["", "## Skills to apply"]
        for s in skills:
            where = f" ({s['plugin']})" if s.get("plugin") else ""
            if s.get("provenance") == "measured":
                proof = f"measured {s.get('ok', 0)}/{s.get('calls', 0)} completion"
            else:
                proof = "unproven — try & measure"
            lines.append(f"- Use `{s['skill']}`{where} via the Skill tool — "
                         f"{proof}.")
    lines += ["", "## Definition of done"]
    v = str(answers.get("verification") or "").strip()
    if v:
        lines.append(f"- {v}")
    elif verification_stated(text):
        lines.append("- Verify against the acceptance criteria stated in the "
                     "task, using the real flow's own output.")
    else:
        lines.append("- State explicit acceptance criteria, then verify with "
                     "the real flow's output (tests green, feature "
                     "exercised) before calling it done.")
    gaps = [c for c in checklist or [] if not c["met"]]
    if gaps:
        lines += ["", "## SOP gaps to close"]
        lines += [f"- {c['label']}: {c['hint']}" for c in gaps]
    if pending:
        lines += ["", "> Unanswered clarifications remain — answer them (or "
                      "the agent must ask) before large-scale work."]
    return "\n".join(lines)


# --- the aggregate + audit ------------------------------------------------------

def enrich(text, project=None, answers=None, evs=None, skills=None,
           stats=None, root=None) -> dict:
    """The full enrichment package for one draft prompt. Deterministic given
    its inputs; every context source fails open to an empty list."""
    st = settings(project)
    verdict = promptgate.assess(text)
    questions = clarifying_questions(verdict, text)
    sources = source_candidates(text, evs=evs, project=project, root=root)
    directives = manifest_directives(verdict["findings"], project)
    suggestions = skill_suggestions(text, skills=skills, stats=stats,
                                    exclude={d["skill"] for d in directives})
    checklist = sop_checklist(text, verdict)
    composed = compose(text, questions, answers, sources,
                       directives + suggestions, checklist)
    return {"enabled": st["enabled"], "assessment": verdict,
            "questions": questions, "sources": sources,
            "skills": {"directives": directives, "suggestions": suggestions},
            "sop": checklist, "enriched_prompt": composed}


def record(text, result, project=None, session_id=None):
    """Ledger evidence for one enrichment act the user actually used (copied
    or explicitly recorded). Payload is capped — the composed prompt itself is
    a derived artifact, not history."""
    from . import events as ev_mod
    sop = result.get("sop") or []
    return ev_mod.append_event(
        "prompt_enriched",
        {"text": str(text or "")[:RECORD_TEXT_CAP],
         "questions": len(result.get("questions") or []),
         "sources": len(result.get("sources") or []),
         "skills": (len((result.get("skills") or {}).get("directives") or [])
                    + len((result.get("skills") or {}).get("suggestions") or [])),
         "sop_met": sum(1 for c in sop if c.get("met")),
         "sop_total": len(sop)},
        session_id=session_id, tags=["enrich"], project=project)


# --- hook extension: ride the promptgate injection when enabled ----------------

def extend_hook_context(context, raw, project=None) -> dict:
    """Additive extension of a fired promptgate context: when enrichment is
    enabled, append the clarifying questions so the agent asks the user
    instead of guessing. Reads only the settings sidecar (never the ledger —
    this runs on every gated prompt). Fail-open: any error returns the
    context unchanged."""
    try:
        if not context or not settings(project).get("enabled", DEFAULT_ENABLED):
            return context
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return context
        text = payload.get("prompt", "")
        verdict = promptgate.assess(text)
        if verdict["skip"] or verdict["clear"]:
            return context
        questions = clarifying_questions(verdict, text)
        if not questions:
            return context
        hso = context.get("hookSpecificOutput") or {}
        extra = (f"\n{ENRICH_MARK} Prompt enrichment (default-on; `hive "
                 f"enrich off` disables): clarify with the user before "
                 f"starting:\n"
                 + "\n".join(f"- {q['text']}" for q in questions))
        new_hso = dict(hso, additionalContext=str(
            hso.get("additionalContext") or "") + extra)
        return dict(context, hookSpecificOutput=new_hso)
    except Exception:
        return context


# --- T2: the injection probe ----------------------------------------------------

def probe(project=None) -> dict:
    """Whether enrichment would extend the gate's context at all.

    `silent` here is a SETTING, not a fault, and the reason says so — the whole
    point of a three-state verdict is that an operator can tell a switched-off
    pass from a broken one."""
    s = settings(project)
    on = bool(s.get("enabled"))
    src = s.get("source") or "default"
    return {"verdict": "verbatim" if on else "silent", "chars": 0,
            "reason": (f"enrichment enabled (source: {src}); extends the gate's "
                       "context when the gate fires" if on
                       else f"enrichment switched off (source: {src})")}
