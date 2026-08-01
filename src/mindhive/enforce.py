# src/mindhive/enforce.py
"""Deterministic PreToolUse enforcement — the "enforce" rung of MindHive's
nudge -> enforce ladder.

A MindHive prevention rule is a *nudge*: advisory text injected into the
briefing that a model may or may not heed. When a signature keeps recurring
*despite* an active rule, nudging has demonstrably failed — the only thing that
actually FORCES the effective method is code that inspects a tool call before it
runs and refuses the ineffective form. That is this module.

The ladder, made concrete:
  1. A failure recurs        -> `incidents` records it, `rules reflect` writes a
                                nudge (advisory prevention rule).
  2. It recurs despite the   -> `suggest()` adds a DISABLED stub to the
     nudge (>= ENFORCE_          enforcement manifest (rules/enforce.json). A
     THRESHOLD occurrences)      human enables it. Enforcement is always opt-in
                                 and never fires for a signature MindHive has not
                                 escalated.
  3. Enabled + a matcher     -> the PreToolUse hook calls `evaluate()`, which
     exists                      denies the ineffective tool call and hands the
                                 model the effective method instead.

MATCHERS are pure predicates over (tool_name, tool_input); everything here is
fail-open by construction — any error, missing manifest, or unknown signature
means *allow*, so a bug in this module can never block a real tool call.
"""
import json

from . import paths

# How many recurrences of a *ruled* signature before enforcement is suggested.
# Below this, the nudge is still given a fair chance to work.
ENFORCE_THRESHOLD = 5


# --- matchers: (tool_name, tool_input) -> deny reason (the effective method) | None

def _grep_unscoped(tool_name, tool_input):
    """The Grep tool with no path/glob/type runs ripgrep over the whole tree from
    the root — the slow path that has timed out repeatedly. Force a bounded search."""
    if tool_name != "Grep":
        return None
    ti = tool_input or {}
    if any(ti.get(k) for k in ("path", "glob", "type")):
        return None
    return ("This Grep is unscoped (no path, glob, or type) and searches the whole "
            "tree from the root — the exact shape that has timed out here before. "
            "Re-run it bounded: add a `path` (a subdirectory), a `glob` "
            "(e.g. \"**/*.py\"), or a `type` (e.g. \"py\").")


def _worktree_no_force(tool_name, tool_input):
    """`git worktree remove` without --force fails with 'Permission denied' on
    Windows whenever any process still holds a handle in the tree."""
    if tool_name not in ("Bash", "PowerShell"):
        return None
    cmd = (tool_input or {}).get("command", "") or ""
    low = cmd.lower()
    if "git worktree remove" not in low:
        return None
    if "--force" in low or " -f " in low or low.rstrip().endswith(" -f"):
        return None
    return ("`git worktree remove` without --force fails with 'Permission denied' "
            "on Windows when a process, editor, or AV still holds a handle inside "
            "the tree. Use `git worktree remove --force <path>`, do NOT cd into the "
            "worktree you are removing, and follow with `git worktree prune`.")


MATCHERS = {
    "tool:ripgrep-timeout": _grep_unscoped,
    "git:worktree-locked": _worktree_no_force,
}


# --- static policy tier (GateGuard port, 2026-07 ECC adoption) -----------------
# A second, deliberate layer distinct from the learned ladder above: these
# encode standing operating rules the user explicitly adopted (see
# docs/plans/2026-07-21-001-feat-ecc-integration-wave-plan.md), not signatures
# escalated from incidents — the adoption decision IS the opt-in, so they
# default ON. Each is individually overridable in rules/enforce.json under
# "policy:<id>" ({"enabled": false} disables; a "reason" entry overrides the
# classifier's text). The classifiers live in gateguard.py (pure, tested);
# ordering puts deny decisions before ask so a command tripping both gates
# gets the stronger outcome.

def _policy_no_verify(tool_name, tool_input):
    if tool_name not in ("Bash", "PowerShell"):
        return None
    from . import gateguard
    reason = gateguard.check_no_verify((tool_input or {}).get("command", ""))
    if not reason:
        return None
    return f"{reason}. Investigate and fix whatever the hook rejects instead."


def _policy_config_protection(tool_name, tool_input):
    from . import gateguard
    return gateguard.protected_config_file(tool_name, tool_input)


def _policy_destructive(tool_name, tool_input):
    if tool_name not in ("Bash", "PowerShell"):
        return None
    from . import gateguard
    reason = gateguard.is_destructive_command(
        (tool_input or {}).get("command", ""))
    if not reason:
        return None
    return (f"Destructive-command gate: {reason}. Confirm this is genuinely "
            f"intended, or use a safer alternative.")


# --- MN-4: the MindNetwork state machine gets teeth ---------------------------------

def _mindnet_sidecar(cache):
    """The MindNetwork projection, read AT MOST ONCE per hook invocation.

    Both mindnet checks need it, and `PreToolUse` fires on every tool call — two
    independent reads would double the file I/O on the hottest path in the
    system. The cache is a plain dict owned by `evaluate_policies` for the
    duration of one call, so nothing is shared between invocations and there is
    no staleness window to reason about.

    ``None`` on any fault, which every caller treats as allow."""
    if cache is None:
        cache = {}
    if "sc" not in cache:
        from .mindnet import sidecar as s_mod
        try:
            cache["sc"] = s_mod.read(cache.get("project"))
        except Exception:
            cache["sc"] = None
    return cache["sc"]


def _mindnet_armed(cache):
    """Whether MindNetwork enforcement was opted into here — read AT MOST ONCE
    per hook invocation, like the sidecar and for the same reason.

    MN-6's blast-radius gate. Both mindnet checks default DORMANT: unlike every
    other member of `POLICY_CHECKS`, they decide from mutable projection state,
    and `paths.detect_project` collapses every worktree of a repo onto ONE
    namespace — so before this gate a single task parked at a planning gate
    denied writes in every checkout at once, with nobody having opted in. See
    `mindnet/optin.py` for the two mechanisms and the nesting rule.

    ``False`` on any fault: a bug in an opt-in gate must fail towards "allowed"."""
    if cache is None:
        cache = {}
    if "mn_armed" not in cache:
        from .mindnet import optin
        try:
            cache["mn_armed"] = bool(optin.armed(cache.get("project")))
        except Exception:
            cache["mn_armed"] = False
    return cache["mn_armed"]


def _policy_mindnet_phase(tool_name, tool_input, cache=None):
    """MN-4's phase block: no producing code while the active task sits at a
    planning gate.

    The import is lazy for the same reason `_policy_destructive` reaches
    `gateguard` lazily — `enforce.py` is imported by the hook shim in every
    project, including the ones with no MindNetwork state at all.

    Three tests, cheapest first, and the order is the contract. The tool-name
    test comes first: neither rule can ever deny a `Read`, and paying anything to
    discover that on the most common tool call in the system is pure waste. The
    opt-in test comes before the sidecar read, so a project that never opted in
    pays no mindnet file I/O on any tool call at all."""
    from .mindnet import enforcer as mn
    if tool_name not in mn.WRITE_TOOLS and tool_name not in mn.EXECUTE_TOOLS:
        return None
    if not _mindnet_armed(cache):
        return None
    return mn.phase_violation(tool_name, tool_input, _mindnet_sidecar(cache))


def _policy_mindnet_language(tool_name, tool_input, cache=None):
    """MN-4's language lock: no producing a file whose extension falls outside a
    populated ``language_context``.

    Execute tools are in the prefilter because the lock now reaches the two
    shell shapes that create files (a redirect target, a scaffolding command).
    The predicate still declines everything else, so widening this set costs a
    dict lookup on `Bash`, not a parse."""
    from .mindnet import enforcer as mn
    if tool_name not in mn.WRITE_TOOLS and tool_name not in mn.EXECUTE_TOOLS:
        return None
    if not _mindnet_armed(cache):
        return None
    return mn.language_violation(tool_name, tool_input,
                                 _mindnet_sidecar(cache))


# Opt in to the shared per-call cache. An explicit flag rather than arity
# inspection: the check contract stays `(tool_name, tool_input)` for everything
# that does not need the sidecar, and a future check declares its need instead
# of having it guessed from a signature.
_policy_mindnet_phase.wants_cache = True
_policy_mindnet_language.wants_cache = True


# (id, permissionDecision, check). "deny" blocks outright; "ask" escalates to
# the human — right for destructive commands, which are sometimes legitimate.
#
# The two mindnet entries are appended, never inserted: `gate:no-verify` and the
# config-protection gate were standing rules before MindNetwork existed and must
# keep their precedence. Both are "deny" rather than "ask" — a destructive
# command is sometimes legitimate, but a state-machine violation is not: the
# phase is the system's statement that this work is out of order.
POLICY_CHECKS = (
    ("gate:no-verify", "deny", _policy_no_verify),
    ("gate:config-protection", "deny", _policy_config_protection),
    ("gate:destructive-command", "ask", _policy_destructive),
    ("gate:mindnet-phase", "deny", _policy_mindnet_phase),
    ("gate:mindnet-language", "deny", _policy_mindnet_language),
)


def evaluate_policies(tool_name, tool_input, manifest=None,
                      project=None) -> tuple | None:
    """(signature, decision, reason) when a policy check fires, else None.
    Policies are ON unless the manifest explicitly disables them; a per-entry
    ``reason`` override wins. Fail-open like everything in this module.

    The signature rides along because `hook_decision` records the refusal and a
    denial that cannot name the rule that issued it is not an audit trail — the
    operator's remedy is a manifest entry keyed by exactly this string."""
    try:
        man = manifest if manifest is not None else load_manifest(project)
        # Scoped to this one call. Checks that declare `wants_cache` share it,
        # so a projection several of them need is read once rather than once
        # each — and nothing survives into the next invocation, so there is no
        # staleness window.
        cache = {"project": project}
        for pid, decision, fn in POLICY_CHECKS:
            entry = man.get(f"policy:{pid}")
            entry = entry if isinstance(entry, dict) else {}
            if entry.get("enabled") is False:
                continue
            reason = (fn(tool_name, tool_input, cache)
                      if getattr(fn, "wants_cache", False)
                      else fn(tool_name, tool_input))
            if reason:
                return (pid, decision,
                        (entry.get("reason") or "").strip() or reason)
    except Exception:
        return None
    return None


# --- enforcement manifest (rules/enforce.json): which signatures are enforced ---

def _manifest_path(project=None):
    return paths.project_dir(project) / "rules" / "enforce.json"


def load_manifest(project=None) -> dict:
    """The per-project enforcement manifest: {signature: {enabled: bool,
    reason: str, ...}}. Empty dict when missing or corrupt (fail-open)."""
    try:
        obj = json.loads(_manifest_path(project).read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_manifest(project, data) -> None:
    paths.write_text_atomic(
        _manifest_path(project),
        json.dumps(data, indent=2, sort_keys=True),
    )


def evaluate(tool_name, tool_input, project=None, manifest=None) -> str | None:
    """Deny-reason for this tool call, or None to allow. Only *enabled* manifest
    entries with a known matcher are consulted; first match wins. A per-entry
    ``reason`` override wins over the matcher's default. Fail-open: any exception
    returns None (allow) rather than blocking a real tool call."""
    try:
        man = manifest if manifest is not None else load_manifest(project)
        for sig, entry in man.items():
            if not isinstance(entry, dict) or not entry.get("enabled"):
                continue
            fn = MATCHERS.get(sig)
            if fn is None:
                continue
            reason = fn(tool_name, tool_input)
            if reason:
                return (entry.get("reason") or "").strip() or reason
    except Exception:
        return None
    return None


# Stable marker on every deny reason this hook emits. The denial surfaces in
# the transcript as an is_error tool_result; without the marker the failure
# capture re-ingests our own enforcement text as a brand-new raw:* incident —
# the learning loop eating its own output (this happened: raw:bc40af13ca was
# the grep-unscoped deny text). failures.py treats the marker as non-failure.
ENFORCED_MARK = "[mindhive-rule]"


# A denial message is a paragraph, not an essay — but a manifest `reason`
# override is operator-supplied text of any length, and the ledger must not
# carry a novel per refusal. The operator still sees all of it: truncation is
# applied to the STORED copy only, never to the hook payload.
DENIAL_REASON_LIMIT = 600


def _record_denial(signature, tool_name, decision, reason, project) -> None:
    """Append the ledger evidence that this refusal happened.

    Called on DENY ONLY (KTD5). `PreToolUse` fires on every tool call, so an
    append on the allow path would put a file write on the hottest path in the
    system; a refusal is rare by construction and the tool call is being blocked
    anyway, so the write is free in practice.

    Fail-open and result-discarding (KTD6). A ledger that is full, locked, or
    missing must not turn a deny into an allow — the audit is a record OF the
    decision, never a participant in it. Same contract `telemetry.emit` already
    documents for itself.

    The import is lazy for the reason every import in this module is: the hook
    shim loads `enforce` in every project on every tool call, and `events` pulls
    in the whole store.

    Stored without `ENFORCED_MARK` — that marker is a wire artifact whose job is
    keeping `failures.py` from re-ingesting our own refusal text as a fresh
    incident. The ledger wants the rule's own words."""
    try:
        from . import events
        events.append_event(
            "policy_denied",
            {"signature": signature or "", "tool_name": str(tool_name or ""),
             "decision": decision, "reason": reason[:DENIAL_REASON_LIMIT]},
            tags=["enforce", "denial"], project=project)
    except Exception:
        pass


def hook_decision(raw: str, project=None) -> dict:
    """Parse a PreToolUse payload (JSON on the hook's stdin) and return the hook's
    output: a deny/ask decision when a policy or matcher fires, or {} (allow)
    otherwise. The static policy tier runs first (its checks are standing
    rules); the learned manifest matchers follow. Never raises — a malformed
    payload is an allow."""
    try:
        payload = json.loads(raw or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    # Resolve the namespace ONCE. `paths.detect_project` shells out to git (up to
    # two `rev-parse` calls, each with a 2s timeout), and every consumer that
    # takes `project=None` pays it independently: `load_manifest` did, and MN-6's
    # opt-in read added a second. On a hook that fires on EVERY tool call that is
    # a duplicated subprocess per call, so the concrete name is resolved here and
    # threaded down.
    if project is None:
        try:
            project = paths.detect_project()
        except Exception:
            project = None
    manifest = load_manifest(project)
    decision, reason, signature = "deny", None, ""
    pol = evaluate_policies(tool_name, tool_input, manifest=manifest,
                            project=project)
    if pol:
        signature, decision, reason = pol
    else:
        reason = evaluate(tool_name, tool_input, project=project,
                          manifest=manifest)
    if not reason:
        return {}
    _record_denial(signature, tool_name, decision, reason, project)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": f"{ENFORCED_MARK} {reason}",
    }}


# --- the escalation step: suggest / enable / disable enforcement ---------------

def suggest(project=None) -> list:
    """Scan incidents; add a DISABLED manifest stub for any signature that (a) has
    an enforcement matcher, (b) carries an active prevention rule, and (c) has
    recurred >= ENFORCE_THRESHOLD times. Enforcement stays opt-in — a human runs
    `hive rules enforce --enable <sig>`. Returns the signatures newly suggested."""
    from . import incidents
    man = load_manifest(project)
    added = []
    for row in incidents.summaries(project=project):
        sig = row.get("signature")
        if sig not in MATCHERS:
            continue
        if row.get("status") != "active":
            continue
        if row.get("rule_source") in (None, "", "none"):
            continue
        if int(row.get("occurrences", 0) or 0) < ENFORCE_THRESHOLD:
            continue
        if sig in man:
            continue
        man[sig] = {"enabled": False,
                    "suggested_at_occurrences": int(row.get("occurrences", 0) or 0),
                    "reason": ""}
        added.append(sig)
    if added:
        _write_manifest(project, man)
    return added


def set_enabled(project, signature, enabled: bool) -> dict:
    """Enable or disable enforcement for one signature or ``policy:<id>``
    entry. Refuses a signature with no matcher/policy (there would be nothing
    to enforce). A human toggle PINS the entry: auto_escalate never overrides
    an explicit human decision."""
    policy_ids = {f"policy:{pid}" for pid, _d, _f in POLICY_CHECKS}
    if signature not in MATCHERS and signature not in policy_ids:
        return {"error": f"no enforcement matcher for '{signature}'",
                "known": sorted(MATCHERS) + sorted(policy_ids)}
    man = load_manifest(project)
    entry = man.get(signature) if isinstance(man.get(signature), dict) else {}
    entry["enabled"] = bool(enabled)
    entry["pinned"] = True
    entry.setdefault("reason", "")
    man[signature] = entry
    _write_manifest(project, man)
    return {"signature": signature, "enabled": bool(enabled)}


def auto_escalate(project=None) -> list:
    """The self-acting rung the ladder was missing: when a signature has an
    enforcement matcher, an active prevention rule, AND the rule has leaked
    (recurred from another session after activation) at least
    ``incidents.LEAK_LIMIT`` times, the nudge has demonstrably failed — so
    enforcement flips ON without waiting for a human. Matchers are fail-open
    pure predicates, so the worst a wrong auto-enable can do is deny one tool
    call with an explanation of the effective method.

    Entries a human explicitly toggled (``pinned``) are never overridden.
    Returns the signatures newly auto-enabled."""
    from . import events, incidents
    man = load_manifest(project)
    stats = incidents.signal_stats(project)
    enabled = []
    for row in incidents.summaries(project=project):
        sig = row.get("signature")
        # "demoted" is included deliberately: an auto rule that leaked twice is
        # auto-demoted from injection at the same LEAK_LIMIT — for a signature
        # WITH a matcher that is precisely the moment to switch from nudging
        # to forcing, not to give up.
        if sig not in MATCHERS or row.get("status") not in ("active", "demoted"):
            continue
        # An EXPLICIT human demote (rule_demoted event -> dismissed) means "this
        # rule is wrong" — never force it. Auto-demotion writes no event and
        # stays eligible.
        if (stats.get(sig) or {}).get("dismissed"):
            continue
        if row.get("rule_source") in (None, "", "none"):
            continue
        # Cumulative since activation (total_leaks): a reroute re-anchors the
        # classification leak count, but must not postpone the hard-force rung.
        st = stats.get(sig) or {}
        if int(st.get("total_leaks") or st.get("leaks") or 0) < incidents.LEAK_LIMIT:
            continue
        entry = man.get(sig) if isinstance(man.get(sig), dict) else {}
        if entry.get("enabled") or entry.get("pinned"):
            continue
        entry.update({"enabled": True, "auto_enabled": True})
        entry.setdefault("reason", "")
        man[sig] = entry
        enabled.append(sig)
    if enabled:
        _write_manifest(project, man)
        for sig in enabled:
            events.append_event(
                "rule_enforced",
                {"signature": sig, "text": "auto-escalated: nudge leaked "
                 f">= {incidents.LEAK_LIMIT}x with an active rule"},
                tags=["lifecycle", "enforce"], project=project)
    return enabled
