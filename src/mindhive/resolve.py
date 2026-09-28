# SPDX-License-Identifier: Apache-2.0
# src/mindhive/resolve.py
"""Active rule resolution: turn red (unresolved) lessons green.

A lesson is red when it sits at the recurrence threshold with no rule, or when
its rule LEAKED (the failure recurred despite it). Reflection only ever writes
a first rule, so before this module a leaked rule was a dead end — leaks
accumulated forever and the signature could never classify resolved again.

The resolution pass closes the loop:
  1. red_summaries() filters the red set automatically.
  2. No rule yet          -> delegate to reflect.reflect_incident (existing path).
  3. Auto rule that leaked -> ask the model for ROUTE_COUNT ranked ALTERNATIVE
     prevention routes, pick the best one that is distinct from every rule that
     already failed, deploy it, and append a rule_rerouted event. That event
     re-anchors leak/exposure accounting (incidents.signal_stats), so a route
     that holds classifies watching -> resolved and auto-promotes to SOUL.md —
     the permanent green.
  4. Human rules are authoritative (ADR 0007) — never rerouted.

Pure helpers (build_route_prompt / parse_routes / choose_route) are unit-tested;
the batch entry points take an injectable runner like reflect does. Attempts are
capped at incidents.MAX_REROUTES per signature; exhaustion surfaces as the
frontmatter flag `reroute_exhausted` for a human to take over.
"""
import json
import re

from . import events, incidents, reflect

ROUTE_COUNT = 3

_SYSTEM = (
    "You are the Resolution Engine. A prevention rule was deployed for a "
    "recurring execution failure, but the failure KEPT recurring — the rule "
    "demonstrably does not work. Propose alternative prevention routes. Output "
    "ONLY a JSON object — no prose, no code fences — with key: routes. routes "
    f"is a list of exactly {ROUTE_COUNT} objects, best first, each with keys: "
    "prevention_rule (a single imperative sentence a future agent can follow), "
    "rationale (why this route should succeed where the failed rule did not), "
    "confidence (a number 0.0-1.0). Every route must be materially different "
    "from every failed rule listed."
)


def build_route_prompt(signature, error_excerpt, failed_rules, leak_count) -> str:
    failed = "\n".join(f"- {r}" for r in (failed_rules or []) if r) or "- (unknown)"
    return (f"{_SYSTEM}\n\nSIGNATURE: {signature}\n"
            f"FAILED RULES (do not repeat these):\n{failed}\n"
            f"RECURRENCES DESPITE THE RULE: {leak_count}\n"
            f"ERROR:\n{error_excerpt}\n\nReturn the JSON now.")


def parse_routes(text: str) -> list:
    """Extract the ranked routes list from a model response, tolerating
    fences/prose. Only dict entries with a non-empty prevention_rule survive."""
    obj = reflect.parse_reflection(text)
    routes = obj.get("routes") if isinstance(obj, dict) else None
    if not isinstance(routes, list):
        return []
    return [r for r in routes
            if isinstance(r, dict) and str(r.get("prevention_rule") or "").strip()]


_NORM = re.compile(r"[^a-z0-9]+")


def _norm(text) -> str:
    """Normalized comparison key so trivial rephrasings of a failed rule
    (case, punctuation, whitespace) don't count as a distinct route."""
    return _NORM.sub(" ", str(text or "").lower()).strip()


def _confidence(route) -> float:
    try:
        return float(route.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def choose_route(routes, failed_rules) -> dict:
    """The best suggested route: highest confidence among routes distinct from
    every rule that already failed (ties keep model order — best first). Empty
    dict when nothing distinct was offered; the caller must not fabricate."""
    failed = {_norm(r) for r in (failed_rules or []) if r}
    distinct = [r for r in (routes or [])
                if _norm(r.get("prevention_rule")) not in failed]
    return max(distinct, key=_confidence) if distinct else {}


def failed_rule_texts(signature, project=None, evs=None) -> list:
    """Every rule text that has already failed for a signature: the currently
    active (leaking) rule plus the old_text of every prior reroute."""
    texts = []
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == "rule_rerouted" and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            old = str(ev.payload.get("old_text") or "").strip()
            if old and old not in texts:
                texts.append(old)
    current = incidents._active_rule_for(signature, project)
    if current and current not in texts:
        texts.append(current)
    return texts


def red_summaries(project=None, evs=None) -> list:
    """The red set: every lesson whose resolution classifies unresolved, with
    the signal_stats enrichment (leaks, reroutes) merged in — the automatic
    filter the resolution pass and `hive rules resolve --list` run on."""
    if evs is None:
        evs = events.read_all(project)
    stats = incidents.signal_stats(project, evs=evs)
    out = []
    for s in incidents.summaries(project):
        st = stats.get(s["signature"], {})
        if incidents.classify_resolution(s, st) != "unresolved":
            continue
        out.append(dict(s, leaks=int(st.get("leaks") or 0),
                        reroutes=int(st.get("reroutes") or 0),
                        last_reroute=st.get("last_reroute"),
                        resolution="unresolved"))
    out.sort(key=lambda r: (-(r.get("leaks") or 0), -(r.get("occurrences") or 0)))
    return out


def reroute_incident(signature, project=None, runner=None) -> dict:
    """Replace a leaking auto rule with the best distinct alternative route.
    Deploys via the incident file's Auto Evaluation (same mechanism as
    reflect_incident) and appends the rule_rerouted event that re-anchors leak
    accounting. Deterministic fallback verdicts are eligible only when their
    rule is distinct from everything that already failed — never fabricate."""
    runner = runner or reflect._claude_runner
    d = incidents._incident_dir(project)
    path = d / f"{incidents._safe_name(signature)}.md"
    if not path.exists():
        return {"resolved": False, "mode": "failed", "reason": "no such incident"}
    evs = events.read_all(project)
    attempts = incidents.reroute_count(project, signature, evs=evs)
    if attempts >= incidents.MAX_REROUTES:
        return {"resolved": False, "mode": "skipped",
                "reason": "reroute attempts exhausted"}
    secs = incidents._split_sections(path.read_text(encoding="utf-8"))
    excerpt = incidents._raw_excerpt(secs.get("Raw Signal", ""))
    failed = failed_rule_texts(signature, project=project, evs=evs)
    leak_count = incidents.leaks(project=project, signature=signature)
    fail_reason = "no distinct route"
    try:
        routes = parse_routes(runner(
            build_route_prompt(signature, excerpt, failed, leak_count)))
    except Exception as e:              # keep the class for the health marker
        routes = []
        fail_reason = type(e).__name__
    best = choose_route(routes, failed)
    if not best:
        fb = reflect.fallback_verdict(signature)
        if fb and _norm(fb.get("prevention_rule")) not in {_norm(r) for r in failed}:
            best = {"prevention_rule": fb["prevention_rule"],
                    "rationale": fb["lesson"], "confidence": 0.5}
            routes = routes or [best]
    if not best:
        return {"resolved": False, "mode": "failed", "reason": fail_reason}
    old_auto = secs.get("Auto Evaluation", "")
    verdict = {
        "root_cause": incidents._auto_field(old_auto, "Root cause"),
        "lesson": str(best.get("rationale") or "").strip()
                  or f"Alternative route after {leak_count} leak(s).",
        "prevention_rule": str(best["prevention_rule"]).strip(),
        "task_category": incidents._auto_field(old_auto, "Category"),
    }
    old_rule = incidents._active_rule_for(signature, project)
    # Guard the re-read -> replace -> write -> event -> project chain exactly
    # like reflect_incident; the model call above stays OUTSIDE (can take 120s).
    with incidents._write_guard(project):
        txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                         "Auto Evaluation",
                                         reflect._auto_section(verdict))
        incidents._atomic_write(path, txt)
        events.append_event(
            "rule_rerouted",
            {"signature": signature, "text": verdict["prevention_rule"],
             "old_text": old_rule, "routes": routes,
             "attempt": attempts + 1, "source": "auto"},
            tags=["lifecycle", "resolve"], project=project)
        incidents.project(project)      # re-derive status/flag with the new anchor
    return {"resolved": True, "mode": "rerouted", "attempt": attempts + 1}


def resolve_incident(signature, project=None, runner=None) -> dict:
    """Resolve ONE red lesson by the route that fits it: reflect when no rule
    exists yet, reroute when an auto rule leaked, skip when the rule is
    human-sourced (human is authoritative)."""
    rec = incidents.load(signature, project=project)
    if not rec:
        return {"resolved": False, "mode": "failed", "reason": "no such incident"}
    source = rec.get("rule_source")
    if source in (None, "", "none"):
        r = reflect.reflect_incident(signature, project=project,
                                     runner=runner or reflect._claude_runner)
        return {"resolved": bool(r.get("reflected")), "mode": "reflected",
                "reason": r.get("reason", "")}
    if source == "human":
        return {"resolved": False, "mode": "skipped",
                "reason": "human rule is authoritative"}
    return reroute_incident(signature, project=project, runner=runner)


def resolve_batch(project=None, limit=3, runner=None) -> dict:
    """Bounded resolution pass over the red set (the manual/endpoint entry
    point; the automatic one rides inside reflect.reflect_batch). Refreshes
    projections and escalates enforcement after any success. Never raises for
    a failing runner."""
    incidents.audit(project=project)
    incidents.project(project=project)
    reds = red_summaries(project)
    # Human-sourced rules are never rerouted — don't burn batch slots on them
    # (they stay in the red count/pending until a human revises or dismisses).
    actionable = [s for s in reds if s.get("rule_source") != "human"]
    counts = {"rerouted": 0, "reflected": 0, "failed": 0, "skipped": 0}
    attempted = 0
    for s in actionable[:max(0, int(limit))]:
        attempted += 1
        try:
            res = resolve_incident(s["signature"], project=project, runner=runner)
        except Exception:               # fail-open: one bad record won't stop the pass
            res = {"resolved": False, "mode": "failed"}
        if res.get("resolved"):
            counts["rerouted" if res.get("mode") == "rerouted" else "reflected"] += 1
        elif res.get("mode") == "skipped":
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
    done = counts["rerouted"] + counts["reflected"]
    if done:
        try:                            # hard-force rung for matcher families
            from . import enforce
            enforce.auto_escalate(project=project)
        except Exception:
            pass
        try:
            reflect._refresh_projections(project)
        except Exception:
            pass
    return dict(counts, attempted=attempted, red=len(reds),
                pending=max(0, len(reds) - done))
