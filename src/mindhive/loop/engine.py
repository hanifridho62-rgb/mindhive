# src/mindhive/loop/engine.py
import time
from pathlib import Path

from .. import events, guard, recall as recall_mod
from ..briefing import build_briefing
from . import context as ctx_mod
from . import prompt as prompt_mod
from . import safeguards
from . import task as task_mod
from .driver import preflight


def run_loop(project, loop_dir, cfg, driver, *,
             now_fn=time.time, sleep_fn=time.sleep, progress_fn=None,
             live=False, force=False):
    """Drive one autonomous run to completion or a safeguard boundary.

    progress_fn(iteration_index)->bool is injected in tests to report whether
    files changed; in production it is a git/mtime diff (Task 11)."""
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

    from . import budget as budget_mod
    bcfg = budget_mod.BudgetConfig(cfg.window_hours, cfg.window_token_budget,
                                   cfg.window_cost_budget_usd)
    budget_entries = []

    events.append_event("loop_run_start",
                        {"goal_dir": str(loop_dir), "max_iterations": cfg.max_iterations},
                        project=project)

    start = now_fn()
    session_id = None
    iteration = 0
    reason = "max_iterations"

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

        result = driver.run(
            task_prompt, loop_context,
            allowed_tools=cfg.allowed_tools, session_id=session_id,
            model=cfg.model, permission_mode=cfg.permission_mode,
            timeout_s=cfg.iteration_timeout_seconds, live=live)

        from .contract import parse_status
        status = parse_status(result.text)
        progressed = progress_fn(iteration) if progress_fn else True
        # A driver-level error (timeout / spawn failure) feeds the breaker just
        # like a rate-limit, so repeated failures open it instead of crashing.
        error_sig = "rate_limited" if result.rate_limited else result.error
        occupancy = result.usage.context_occupancy

        events.append_event("loop_iteration", {
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
        done, exit_reason = safeguards.should_exit(status, required_remaining, json_mode=not live)
        if done:
            reason = "completed"
            break

        cb.record(progress=progressed, error_sig=error_sig, now=now_fn())
        if cb.state == "OPEN":
            reason = "breaker_open"
            break

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

    events.append_event("loop_run_end",
                        {"reason": reason, "iterations": iteration}, project=project)
    return {"reason": reason, "iterations": iteration}
