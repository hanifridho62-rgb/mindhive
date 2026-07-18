# src/mindhive/loop/engine.py
import time
import uuid
from pathlib import Path

from .. import events, guard, recall as recall_mod
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
USAGE_INTERRUPT_REASONS = ("api_cap", "budget_cap")


def _snapshot_interrupted_plan(project, run_id, loop_dir, cause,
                               plan_id=None):
    """Out-of-usage marker (roadmap.py): persist the initial ask, the plan,
    and how far it got, as roadmap set + interrupt events. The marker rides
    STATE.md/MEMORY.md so the next session resumes from the next step instead
    of starting cold. ``plan_id`` overrides the roadmap key (a fabrication
    run keeps its one plan across every interruption; a bare loop run keys
    by run_id). Best-effort by design — a snapshot failure must never mask
    the loop_run_end event that closes the run."""
    try:
        from .. import roadmap as roadmap_mod
        plan_id = plan_id or run_id
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
        set_payload = {"action": "set", "plan_id": plan_id, "prompt": prompt,
                       "title": prompt[:roadmap_mod.MAX_TITLE_CHARS],
                       "steps": steps}
        int_payload = {"action": "interrupt", "plan_id": plan_id,
                       "cause": cause}
        roadmap_mod.validate_payload(set_payload)
        roadmap_mod.validate_payload(int_payload)
        events.append_event("roadmap", set_payload, tags=["roadmap"],
                            project=project)
        events.append_event("roadmap", int_payload, tags=["roadmap"],
                            project=project)
        # Delivery leg (autoresume.py): schedule the marker to re-enter
        # Claude once the usage window reopens — same best-effort stance as
        # the snapshot itself. Loop mode: the wake-up relaunches THIS loop on
        # its goal dir (breaker, budgets, exit gate intact), running from the
        # repo root (loop_dir is the goal dir inside it).
        from .. import autoresume as autoresume_mod
        autoresume_mod.schedule(plan_id, project=project,
                                cwd=str(Path(loop_dir).parent),
                                mode="loop", goal_dir=str(loop_dir))
    except Exception:
        pass


def run_loop(project, loop_dir, cfg, driver, *,
             now_fn=time.time, sleep_fn=time.sleep, progress_fn=None,
             live=False, force=False, plan_id=None):
    """Drive one autonomous run to completion or a safeguard boundary.

    progress_fn(iteration_index)->bool is injected in tests to report whether
    files changed; in production it is a git/mtime diff (Task 11).
    ``plan_id`` keys the interruption marker to an existing roadmap (a
    fabrication's plan survives every sleep/wake cycle) instead of the
    per-run id."""
    loop_dir = Path(loop_dir)
    guard.ensure_safe(force=force)              # LIVE_LOCK check

    if progress_fn is None:
        from .progress import MtimeTracker
        _tracker = MtimeTracker(loop_dir.parent)
        _tracker.snapshot()

        def progress_fn(_i, _t=_tracker):   # noqa: E306
            return _t.changed()

    cb = safeguards.CircuitBreaker(safeguards.BreakerConfig(
        cfg.no_progress_threshold, cfg.same_error_threshold, cfg.cooldown_seconds))
    ccfg = ctx_mod.ContextConfig(cfg.context_window, cfg.smart_zone_ratio)

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

            events.append_event("loop_iteration", {
                "run_id": run_id,
                "model": model,
                "tier": tier,
                "iteration": iteration,
                "summary": status.summary or "",
                "progress": progressed,
                "cost_usd": result.usage.cost_usd,
                "total_tokens": result.usage.total_tokens,
                "context_fill_ratio": ctx_mod.fill_ratio(occupancy, cfg.context_window),
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
            elif ctx_mod.needs_reset(occupancy, ccfg):
                events.append_event("compaction",
                                    {"text": f"loop context reset at fill "
                                             f"{ctx_mod.fill_ratio(occupancy, cfg.context_window):.2f}"},
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
        _snapshot_interrupted_plan(project, run_id, loop_dir, reason,
                                   plan_id=plan_id)
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
