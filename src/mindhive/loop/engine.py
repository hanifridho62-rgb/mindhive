# src/mindhive/loop/engine.py
import time
import uuid
from pathlib import Path

from .. import events, guard, recall as recall_mod, runlog as runlog_mod
from ..briefing import build_briefing
from . import budget as budget_mod
from . import context as ctx_mod
from . import prompt as prompt_mod
from . import router as router_mod
from . import safeguards
from . import task as task_mod
from .contract import parse_status
from .driver import preflight


# Run-end reasons that mean "usage ran out mid-plan": the Claude usage/rate
# limit (api_cap) and the local budget gate (budget_cap). These get the
# roadmap interruption marker so the next session inherits the big picture.
# `max_hours` joins the two usage caps: a run that ran out of wall clock has
# exactly the same unfinished plan as one that ran out of quota, and the next
# session needs the baton either way.
USAGE_INTERRUPT_REASONS = ("api_cap", "budget_cap", "max_hours")


def _relay_state(project, run_id, cause, steps) -> dict:
    """The baton: what the next session needs that the steps alone do not carry.

    Three fields, and one of them is a CACHE rather than a source of truth. The
    spent fingerprints are already recoverable by folding `work_injected` — that
    is the whole reason `replenish` has no sidecar — so this copy exists only to
    save a re-mine after a five-hour cooldown. `autoresume` unions it with the
    fresh fold and lets the fold win, which means a stale or truncated cache can
    only ever cost a candidate that the ledger would also have skipped. A cache
    that could REMOVE something from the spent set would be the invisible drift
    the no-sidecar rule exists to prevent.

    Best-effort: this runs inside the snapshot's own try, and a relay that cannot
    be computed must not cost the marker itself."""
    try:
        from .. import replenish as rp_mod
        from .. import roadmap as roadmap_mod

        hist = rp_mod.history(events.read_all(project), surface="loop",
                              run_id=run_id)
        injected = sum(1 for s in steps if s.get("new"))
        remaining = sum(1 for s in steps
                        if s.get("status") in ("active", "pending"))
        context = (f"loop run {run_id} stopped on {cause} with {remaining} "
                   f"step(s) open, {injected} of them added by the injector. "
                   f"Steps marked (new) were mined from the record, not planned "
                   f"at the start.")
        return {
            "relay_context": context[:roadmap_mod.MAX_RELAY_CONTEXT_CHARS],
            "spent_fingerprints":
                sorted(hist.fingerprints)[:roadmap_mod.MAX_RELAY_FINGERPRINTS],
            "dry_rounds": hist.dry_rounds,
        }
    except Exception:                                           # noqa: BLE001
        return {}


def _snapshot_interrupted_plan(project, run_id, loop_dir, cause):
    """Out-of-usage marker (roadmap.py): persist the initial ask, the plan,
    and how far it got, as roadmap set + interrupt events. The marker rides
    STATE.md/MEMORY.md so the next session resumes from the next step instead
    of starting cold. Best-effort by design — a snapshot failure must never
    mask the loop_run_end event that closes the run."""
    try:
        from .. import roadmap as roadmap_mod
        tasks = task_mod.load_tasks(loop_dir)
        lines = tasks.prompt.strip().splitlines()
        prompt = lines[0].lstrip("# ").strip() if lines else ""
        if not prompt:
            return
        prompt = prompt[:roadmap_mod.MAX_PROMPT_CHARS]
        steps = roadmap_mod.steps_from_plan_items(
            task_mod.plan_items(tasks.fix_plan))
        if not steps:
            # No fix_plan checklist: the ask itself is the one open step, so
            # the marker still records what was being built and why it stopped.
            steps = [{"text": prompt[:roadmap_mod.MAX_STEP_CHARS],
                      "status": "active"}]
        set_payload = {"action": "set", "plan_id": run_id, "prompt": prompt,
                       "title": prompt[:roadmap_mod.MAX_TITLE_CHARS],
                       "steps": steps}
        int_payload = {"action": "interrupt", "plan_id": run_id,
                       "cause": cause}
        int_payload.update(_relay_state(project, run_id, cause, steps))
        roadmap_mod.validate_payload(set_payload)
        roadmap_mod.validate_payload(int_payload)
        events.append_event("roadmap", set_payload, tags=["roadmap"],
                            project=project)
        events.append_event("roadmap", int_payload, tags=["roadmap"],
                            project=project)
        # Delivery leg (autoresume.py): schedule the marker to re-enter
        # Claude once the usage window reopens — same best-effort stance as
        # the snapshot itself. The resumed work runs from the loop's repo
        # root (loop_dir is the .hive-loop dir inside it).
        from .. import autoresume as autoresume_mod
        autoresume_mod.schedule(run_id, project=project,
                                cwd=str(Path(loop_dir).parent))
    except Exception:
        pass


def _replenish_before_exit(project, loop_dir, cfg, run_id, tracker=None) -> int:
    """Mine the record for outstanding work and land it as required checklist
    items. Returns how many pieces landed.

    Called at the one moment a run would otherwise finish cleanly, which is why it
    is best-effort in the same sense as the interruption snapshot: an injector
    fault must not turn a completed run into a crashed one, and it must never be
    able to pin a run open. Every failure path returns 0, which means "honour the
    exit" — the safe direction.

    Silence is not one of the failure modes, though. `replenish` records a broken
    lens through the `tool_failed` channel the failure loop already audits, and an
    outer fault is recorded here for the same reason: an injector that quietly
    stopped working would look exactly like a project with no outstanding work."""
    if not getattr(cfg, "replenish_enabled", True):
        return 0
    try:
        from .. import replenish as rp_mod, replenish_sinks as sink_mod

        choice = rp_mod.resolve_ambition(cfg.replenish_ambition)
        round_cfg = rp_mod.ReplenishConfig.for_profile(
            choice.profile,
            # Two consecutive rounds that find nothing end the run. Safe here and
            # only here: the streak is scoped to this run_id, so a quiet run
            # cannot switch the loop surface off for the next one.
            dry_rounds_to_exit=2,
            max_rounds_per_run=getattr(cfg, "replenish_max_rounds", 5),
            bounded_run=True)
        evs = events.read_all(project)
        # The agentic lens may cite ONLY facts this process already knows, and the
        # changed paths are its share of those. A tracker-less run (tests inject
        # their own progress_fn) simply gives it less to stand on, which the score
        # then reflects rather than hides.
        changed = tuple(tracker.changed_paths()) if tracker is not None else ()
        result = rp_mod.run_round(
            evs, surface="loop",
            sink=sink_mod.fix_plan_sink(loop_dir, run_id=run_id),
            lenses=rp_mod.lenses_for(choice.profile),
            cfg=round_cfg, project=project, run_id=run_id,
            # The loop is the surface with an outcome history to tune on: it runs
            # rounds back to back and can see what its own injections produced.
            tune=True,
            ctx=rp_mod.LensContext(evs=tuple(evs), project=project,
                                   run_id=run_id, changed_paths=changed))
        if choice.fell_back and result.round_no == 1:
            # Once per run, not once per round: an operator misspelling the dial
            # deserves to be told, and deserves not to have it repeated into every
            # future briefing (SOP memo-hygiene §3).
            events.append_event(
                "decision",
                {"text": f"replenish ambition {choice.requested!r} is not a "
                         f"known position — running {choice.profile.name} "
                         f"instead. Valid: "
                         f"{', '.join(sorted(rp_mod.PROFILES))}."},
                tags=["replenish", "config"], project=project)
        return len(result.landed)
    except Exception as exc:                                    # noqa: BLE001
        try:
            events.append_event(
                "tool_failed",
                {"text": f"replenish round failed: {type(exc).__name__}: "
                         f"{exc}"[:500],
                 "signature": "replenish:round",
                 "action": "run a replenishment round before the loop exits"},
                tags=["replenish", "failure"], project=project)
        except Exception:                                       # noqa: BLE001
            pass
        return 0


def run_loop(project, loop_dir, cfg, driver, *,
             now_fn=time.time, sleep_fn=time.sleep, progress_fn=None,
             live=False, force=False):
    """Drive one autonomous run to completion or a safeguard boundary.

    progress_fn(iteration_index)->bool is injected in tests to report whether
    files changed; in production it is a git/mtime diff (Task 11)."""
    loop_dir = Path(loop_dir)
    guard.ensure_safe(force=force)              # LIVE_LOCK check

    # Held so the replenishment round can ask WHICH files moved, not just whether
    # any did. `changed_paths()` is non-destructive, so reading it does not consume
    # the snapshot the progress detector below depends on.
    tracker = None
    if progress_fn is None:
        from .progress import MtimeTracker
        tracker = MtimeTracker(loop_dir.parent)
        tracker.snapshot()

        def progress_fn(_i, _t=tracker):   # noqa: E306
            return _t.changed()

    cb = safeguards.CircuitBreaker(safeguards.BreakerConfig(
        cfg.no_progress_threshold, cfg.same_error_threshold, cfg.cooldown_seconds))
    ccfg = ctx_mod.ContextConfig(cfg.context_window, cfg.smart_zone_ratio,
                                 cfg.absolute_ceiling)

    bcfg = budget_mod.BudgetConfig(cfg.window_hours, cfg.window_token_budget,
                                   cfg.window_cost_budget_usd)
    budget_entries = []

    run_id = uuid.uuid4().hex[:12]
    tasks0 = task_mod.load_tasks(loop_dir)
    title = tasks0.prompt.strip().splitlines()
    title = title[0].lstrip("# ").strip() if title else ""
    # Model-tier routing: only when no explicit model is pinned. The starting
    # tier comes from the task's static size, overridden by what actually
    # worked for comparable runs in this project's ledger.
    size = router_mod.size_class(
        task_mod.required_remaining_count(tasks0.fix_plan), len(tasks0.prompt))
    routing = cfg.model is None
    tier = router_mod.pick_initial_tier(
        size, router_mod.run_history(events.read_all(project))) if routing else None
    tier_models = {"light": cfg.tier_light, "mid": cfg.tier_mid,
                   "heavy": cfg.tier_heavy}
    events.append_event("loop_run_start",
                        {"run_id": run_id, "title": title, "size_class": size,
                         "goal_dir": str(loop_dir), "max_iterations": cfg.max_iterations,
                         # Oversee derives the per-run online window from this,
                         # so a long configured iteration never reads as offline.
                         "heartbeat_seconds": cfg.iteration_timeout_seconds + 60},
                        project=project)

    start = now_fn()
    session_id = None
    iteration = 0
    reason = "max_iterations"

    try:
        while iteration < cfg.max_iterations:
            if now_fn() - start >= cfg.max_hours * 3600:
                reason = "max_hours"
                break
            if not cb.allow(now=now_fn()):
                reason = "breaker_open"
                break

            # --- budget projection gate ---
            avg_t, avg_c = budget_mod.rolling_avg(budget_entries, n=3)
            if budget_entries and budget_mod.would_exceed(
                    budget_entries, now_fn(), bcfg, avg_t, avg_c):
                if cfg.on_cap == "stop":
                    reason = "budget_cap"
                    break
                sleep_fn(budget_mod.wait_seconds(budget_entries, now_fn(), bcfg, avg_t, avg_c))

            iteration += 1
            tasks = task_mod.load_tasks(loop_dir)
            briefing_text = build_briefing(events.read_all(project))
            hits = recall_mod.recall(tasks.prompt[:120], project=project, limit=3) if tasks.prompt else []
            loop_context = prompt_mod.build_loop_context(briefing_text)
            task_prompt = prompt_mod.build_task_prompt(tasks, hits)

            model = tier_models[tier] if routing else cfg.model
            result = driver.run(
                task_prompt, loop_context,
                allowed_tools=cfg.allowed_tools, session_id=session_id,
                model=model, permission_mode=cfg.permission_mode,
                timeout_s=cfg.iteration_timeout_seconds, live=live)

            status = parse_status(result.text)
            progressed = progress_fn(iteration) if progress_fn else True
            # A driver-level error (timeout / spawn failure) feeds the breaker just
            # like a rate-limit, so repeated failures open it instead of crashing.
            error_sig = "rate_limited" if result.rate_limited else result.error
            occupancy = result.usage.context_occupancy
            # Resolved against the model that actually ran, not a single shared
            # scalar — the routing tiers span a 200K model and 1M models, so one
            # number cannot be right for all of them. `window_source` records
            # whether the id was recognised: a silent fallback and a real
            # resolution look identical downstream otherwise.
            window, window_source = ctx_mod.resolve_window(model, ccfg)

            # The payload's `summary` is one line captured by `summary:\s*(.+)`
            # and is "" whenever the model omits that field — which used to make
            # the whole reply unrecoverable. Persist the reply itself first, in
            # full, and carry the pointer; `summary` stays exactly as it was.
            artifact = runlog_mod.write_once(
                runlog_mod.loop_key(run_id, iteration), result.text,
                project=project)

            events.append_event("loop_iteration", {
                "run_id": run_id,
                "model": model,
                "tier": tier,
                "iteration": iteration,
                "summary": status.summary or "",
                "artifact": artifact,
                "progress": progressed,
                "cost_usd": result.usage.cost_usd,
                "total_tokens": result.usage.total_tokens,
                "context_fill_ratio": ctx_mod.fill_ratio(occupancy, window),
                "context_window": window,
                "context_window_source": window_source,
                "breaker_state": cb.state,
                "session_id": result.session_id,
            }, project=project)

            budget_entries.append(budget_mod.BudgetEntry(
                ts=now_fn(), tokens=result.usage.total_tokens,
                cost_usd=result.usage.cost_usd))

            if result.rate_limited:
                reason = "api_cap"
                break

            required_remaining = task_mod.required_remaining_count(tasks.fix_plan)
            done, exit_reason = safeguards.should_exit(
                status, required_remaining, json_mode=not live,
                min_heuristics=cfg.min_completion_heuristics)
            if done:
                # The run believes it is finished and the checklist agrees. Ask
                # the record before honouring that: a run that stops here while
                # the ledger still proves work outstanding is the quiet ending
                # this whole feature exists to prevent.
                if _replenish_before_exit(project, loop_dir, cfg, run_id,
                                         tracker):
                    # Required items now remain, so `should_exit` will refuse on
                    # its own next iteration — the count does the forcing, not
                    # this branch. The breaker resets because finding
                    # evidence-backed work IS progress; carrying a no-progress
                    # streak across it would trip the breaker for succeeding.
                    cb.reset()
                    continue
                reason = "completed"
                break

            # An iteration blocked ONLY by the heuristic corroboration gate is a
            # responsive agent that claims completion, not a stuck one — it must
            # not feed the breaker or escalate the model tier while it restates
            # the outcome per the contract.
            heuristic_blocked = (exit_reason == "insufficient_completion_heuristics")
            cb.record(progress=progressed or heuristic_blocked,
                      error_sig=error_sig, now=now_fn())
            if cb.state == "OPEN":
                reason = "breaker_open"
                break

            # measured escalation: a lightweight iteration that errored or made no
            # progress hands the next one to the tier above (saturates at heavy)
            if routing and (error_sig or not (progressed or heuristic_blocked)):
                tier = router_mod.escalate(tier)

            # A timed-out / failed iteration leaves no usable session to resume —
            # drop it so the next iteration starts fresh.
            if result.error:
                session_id = None
            # context smart-zone: keep session warm, or reset before the dumb zone
            elif ctx_mod.needs_reset(occupancy, ccfg, model):
                events.append_event("compaction",
                                    {"text": f"loop context reset at "
                                             f"{occupancy} tokens (fill "
                                             f"{ctx_mod.fill_ratio(occupancy, window):.2f} "
                                             f"of {window}, window from "
                                             f"{window_source})"},
                                    project=project)
                session_id = None
            else:
                session_id = result.session_id

    except BaseException:
        # Ctrl-C or an unexpected crash must not strand the run as "running"
        # forever in Oversee: close it in the ledger, then re-raise.
        events.append_event("loop_run_end",
                            {"run_id": run_id, "reason": "interrupted",
                             "iterations": iteration}, project=project)
        raise

    # Out-of-usage interruption: preserve the plan + progress BEFORE the run
    # closes, so even a ledger reader that stops at loop_run_end sees the mark.
    if reason in USAGE_INTERRUPT_REASONS:
        _snapshot_interrupted_plan(project, run_id, loop_dir, reason)
    events.append_event("loop_run_end",
                        {"run_id": run_id, "reason": reason, "iterations": iteration},
                        project=project)
    # Lifecycle deletion: a gracefully-exited or breaker-tripped run leaves the
    # Oversee view via an append-only tombstone — never by touching the chain.
    if reason in ("completed", "breaker_open"):
        cause = "completed" if reason == "completed" else "breaker"
        events.append_event("loop_run_dismissed",
                            {"run_id": run_id, "cause": cause}, project=project)
    return {"reason": reason, "iterations": iteration, "run_id": run_id}
