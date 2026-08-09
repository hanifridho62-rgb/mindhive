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


# Run-end reasons that mean "the run stopped with work still open": the Claude
# usage/rate limit (api_cap), the local budget gate (budget_cap), and the wall
# clock (max_hours). These get the roadmap interruption marker so the next
# session inherits the big picture.
#
# `max_hours` joined the other two when the loop learned to grow its own plan. A
# run that ran out of clock has exactly the same unfinished plan as one that ran
# out of money, and dropping its marker was how injected work — evidence-backed
# work nobody else is tracking — silently disappeared at the end of a long run.
USAGE_INTERRUPT_REASONS = ("api_cap", "budget_cap", "max_hours")

# One bounded line describing what the run was doing when it stopped, carried on
# the interruption so the resumed session inherits the situation and not just
# the checklist.
MAX_RELAY_CONTEXT_CHARS = 500


def _relay_context(project, run_id, cause, iterations) -> str:
    """One line naming what stopped the run and how much of its plan it wrote.

    Folded from the injection rows rather than threaded through the loop, for
    the reason the injector has no sidecar: the events ARE the state, and a
    second source could disagree with them. Best-effort — a relay line that
    cannot be built must not cost the marker it rides on."""
    injected = 0
    try:
        from .. import replenish as rp_mod
        injected = rp_mod.history(events.read_all(project), surface="loop",
                                  run_id=run_id).run_landed
    except Exception:                                           # noqa: BLE001
        pass
    note = (f"Resuming loop run {run_id}: stopped after {iterations} "
            f"iteration(s) on {cause}.")
    if injected:
        note += (f" {injected} step(s) marked (new) were added by the "
                 "replenishment injector, not by the original plan.")
    return note[:MAX_RELAY_CONTEXT_CHARS]


def _snapshot_interrupted_plan(project, run_id, loop_dir, cause,
                               iterations=0):
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
                       "cause": cause,
                       "relay_context": _relay_context(project, run_id, cause,
                                                       iterations)}
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


def replenish_round(project, run_id, loop_dir, cfg, session_id=None):
    """Mine for outstanding work before a completion exit is honoured.

    Returns ``(landed, exhausted)``. ``landed`` above zero means
    `task.required_remaining_count` has risen and `safeguards.should_exit` will
    refuse the exit on the next pass — the forcing mechanism is the COUNT, not
    anything said in a prompt (SOP `agentic-loops` §1). ``exhausted`` means no
    further injection is possible in this run (the dry streak, the round
    ceiling, the per-run ceiling or the depth bound), so the caller should stop
    rather than iterate hoping for a different answer.

    The two are distinct because a single empty round is NOT a finished run: the
    iteration that just ran wrote events of its own, so the next round reads a
    different ledger. That is what the dry-streak bound is for — it gives the
    record a bounded number of chances to change instead of one.

    Best-effort by design: a failed injection must degrade to "nothing landed"
    and let the run exit normally, never crash a live loop or leave the ledger
    half-written. Silence would be worse than the crash, though, so the failure
    goes to `tool_failed` under the signature the failure loop already audits."""
    try:
        from .. import replenish as rp_mod
        from .. import replenish_sinks as sink_mod
        if not cfg.replenish_enabled:
            return 0, True
        # The ambition level's number is the RUN ceiling as well as the round
        # ceiling: an operator who asks for "at most 10" means ten pieces of
        # injected work, not ten per round for as many rounds as the run lasts.
        level, warning = rp_mod.resolve_ambition(cfg.replenish_ambition)
        if warning:
            events.append_event("tool_failed",
                                {"text": warning, "signature": "replenish:config",
                                 "action": "read [replenish] from loop.toml"},
                                session_id=session_id,
                                tags=["replenish", "config"], project=project)
        rcfg = rp_mod.ReplenishConfig.from_ambition(
            level,
            max_per_run=rp_mod.AMBITION[level]["max_per_round"],
            max_rounds=cfg.replenish_max_rounds,
            dry_rounds_to_exit=cfg.replenish_dry_rounds)
        res = rp_mod.run_round(
            events.read_all(project), surface="loop",
            sink=sink_mod.fix_plan_sink(loop_dir, run_id),
            # The tree the RUN is working in — the parent of `.hive-loop` — not
            # the checkout mindhive itself was installed from. Otherwise a loop
            # in someone else's project is handed work about mindhive's files,
            # with evidence paths that do not exist where the work is happening.
            lenses=rp_mod.lens_registry(root=Path(loop_dir).parent),
            cfg=rcfg, project=project, session_id=session_id, run_id=run_id)
        # A SKIPPED round is a bound biting, not an empty ledger. Reported as
        # exhausted so the run stops instead of iterating against a ceiling that
        # will refuse it every time.
        return len(res.landed), not res.ran
    except Exception as exc:                                    # noqa: BLE001
        try:
            events.append_event(
                "tool_failed",
                {"text": f"replenish round failed: "
                         f"{type(exc).__name__}: {exc}"[:500],
                 "signature": "replenish:round",
                 "action": f"replenish the loop run {run_id}"},
                session_id=session_id, tags=["replenish", "failure"],
                project=project)
        except Exception:                                       # noqa: BLE001
            pass
        return 0, True


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
            injected = 0
            if done:
                # Adaptive work injection (replenish.py). The round runs at the
                # ONE moment the run would otherwise end, so a run that never
                # reaches a completion exit never pays for it. Anything landed
                # raises `required_remaining` for the next pass, which is what
                # overturns the exit — structurally, via the gate that already
                # exists.
                injected, exhausted = replenish_round(
                    project, run_id, loop_dir, cfg,
                    session_id=result.session_id)
                # A dry round ends the run only when nothing can change its
                # answer: either the streak bound is off (so there is no second
                # chance to give) or a bound has already been hit. Otherwise the
                # run iterates once more — the last iteration's own events are
                # exactly the evidence the next round might read.
                if exhausted or (not injected and cfg.replenish_dry_rounds <= 0):
                    reason = "completed"
                    break

            # An iteration blocked ONLY by the heuristic corroboration gate is a
            # responsive agent that claims completion, not a stuck one — it must
            # not feed the breaker or escalate the model tier while it restates
            # the outcome per the contract.
            heuristic_blocked = (exit_reason == "insufficient_completion_heuristics")
            # Injecting counts as progress for the breaker. The mtime tracker
            # cannot see it — `.hive-loop` is in its ignore set — so without
            # this the no-progress counter would climb on exactly the iterations
            # the feature exists to create, and the breaker would kill the run
            # for doing what it was told to do.
            cb.record(progress=progressed or heuristic_blocked or bool(injected),
                      error_sig=error_sig, now=now_fn())
            if cb.state == "OPEN":
                reason = "breaker_open"
                break

            # measured escalation: a lightweight iteration that errored or made no
            # progress hands the next one to the tier above (saturates at heavy)
            if routing and (error_sig
                            or not (progressed or heuristic_blocked
                                    or injected)):
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
        _snapshot_interrupted_plan(project, run_id, loop_dir, reason,
                                   iterations=iteration)
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
