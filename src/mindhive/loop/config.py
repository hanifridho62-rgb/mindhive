# SPDX-License-Identifier: Apache-2.0
# src/mindhive/loop/config.py
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LoopConfig:
    # [model]
    # The FALLBACK window, used only when a model id cannot be resolved to its
    # real one (see loop/context.MODEL_WINDOWS). 1M because every model this
    # loop routes to except Haiku 4.5 has a 1M window; the previous 200000
    # meant an unresolved model rotated at 100k, ten times too early.
    context_window: int = 1000000
    smart_zone_ratio: float = 0.5
    # The operator's smart-zone ceiling in absolute tokens, applied alongside
    # the ratio bound (the smaller wins). Expressed directly rather than left
    # emergent from window * ratio, so the number the operator actually cares
    # about is written down somewhere. 0 disables this bound only.
    absolute_ceiling: int = 500000
    model: str | None = None       # explicit model pin — bypasses tier routing
    # [model.tiers] — model id per routing tier (light/mid/heavy)
    tier_light: str = "claude-haiku-4-5"
    tier_mid: str = "claude-sonnet-5"
    tier_heavy: str = "claude-opus-4-8"
    # [budget]
    window_hours: float = 5.0
    window_token_budget: int = 0          # 0 = unlimited
    window_cost_budget_usd: float = 0.0   # 0 = unlimited
    on_cap: str = "pause"                 # "pause" | "stop"
    # [loop]
    max_iterations: int = 50
    max_hours: float = 8.0
    allowed_tools: str = "Write,Read,Edit,Bash(git *),Bash(npm *)"
    iteration_timeout_minutes: int = 15
    permission_mode: str = "acceptEdits"   # headless autonomy; "" to disable
    # Graceful exit needs the explicit EXIT_SIGNAL PLUS this many distinct
    # natural-language completion signals in the reply text. 0 = explicit only.
    min_completion_heuristics: int = 2
    # [breaker]
    no_progress_threshold: int = 3
    same_error_threshold: int = 5
    cooldown_minutes: int = 30
    # [autoresume] — the absolute cap on one chain of loop-mode wake-ups (a
    # usage-capped run relaunched every cooldown). autoresume.MAX_ATTEMPTS
    # bounds a chain that gets nowhere; this bounds one that only looks like it
    # is getting somewhere, whatever its checklist says. 48 is ten days at the
    # default cooldown: an agent doing one usage window of work per wake-up for
    # ten days straight is not a run anyone meant to leave alone.
    max_wake_ups: int = 48
    # [replenish] — the ambition dial. Two keys only: whether the injector runs
    # at all, and how hard it pushes. Everything else the dial governs (score
    # threshold, active lens set, per-run cap) is derived from the position by
    # `replenish.PROFILES`, so an operator tunes one word instead of keeping
    # three numbers consistent by hand.
    replenish_enabled: bool = True
    replenish_ambition: str = "balanced"
    # How many times one run may mine. A ceiling, not a target: a run that has
    # already found and injected five rounds' worth of work is a run whose plan
    # the operator should look at before it grows again.
    replenish_max_rounds: int = 5
    # Per-setting escape hatches. `None` means "take the ambition position's
    # number"; an explicit value overrides the dial for that one setting, which is
    # how an operator keeps a position's lens set while tuning its bar. They are
    # `None` rather than 0 because 0 is itself a legal `min_score` — no floor.
    replenish_min_score: int | None = None
    replenish_max_per_round: int | None = None
    # Two consecutive rounds that find nothing end the run. Exposed because it is
    # the one structural stop whose right value depends on how much outstanding
    # work a project usually carries, and it is safe to expose only because the
    # streak is scoped to a single run_id.
    replenish_dry_rounds_to_exit: int = 2
    # The anti-lazy hold: a run the record proves healthy, which claims to be
    # finished and has never once proposed improvement work, is held open ONCE to
    # spend that capacity. Exposed because holding a run open is the single most
    # intrusive thing this feature does, and an operator who wants a loop that
    # stops the moment it says it is done must be able to say so in one line.
    replenish_anti_lazy: bool = True

    @property
    def cooldown_seconds(self) -> float:
        return self.cooldown_minutes * 60

    @property
    def iteration_timeout_seconds(self) -> float:
        return self.iteration_timeout_minutes * 60


def _int_or(value, default):
    """A hand-edited TOML value read as an int, or *default*.

    `min_score = "40"` is the natural slip in a table whose neighbour reads
    `ambition = "balanced"`, and an uncoerced string reaches a `<` comparison
    against a number. That raises TypeError inside the injector, where the loop's
    blanket guard turns it into "honour the exit" — so the symptom of a stray pair
    of quotes is a silently disabled injector and a plan that never grows.
    Coercing what can be coerced, and falling back on what cannot, keeps a typo
    from switching the feature off."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _positive_int_or(value, default):
    """`_int_or`, refusing anything below 1.

    A `max_per_round` of 0 would trim every survivor while the round's dry flag is
    computed BEFORE the trim — so the round reports neither landed work nor
    dryness, and the dry-round exit can never fire. The off switch is
    `enabled = false`."""
    coerced = _int_or(value, default)
    if isinstance(coerced, int) and coerced < 1:
        return default
    return coerced


def load_config(path: Path) -> LoopConfig:
    """Load loop.toml, falling back to defaults for any missing key/section."""
    cfg = LoopConfig()
    path = Path(path)
    if not path.exists():
        return cfg
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    model = data.get("model", {})
    tiers = model.get("tiers", {}) if isinstance(model.get("tiers"), dict) else {}
    budget = data.get("budget", {})
    loop = data.get("loop", {})
    breaker = data.get("breaker", {})
    # `[replenish]` present but not a table (a bare `replenish = "on"`) must fall
    # back to defaults rather than raise: this file is hand-edited, and a typo in
    # one section has no business stopping a run.
    replenish = data.get("replenish", {})
    if not isinstance(replenish, dict):
        replenish = {}
    autoresume = data.get("autoresume", {})
    if not isinstance(autoresume, dict):
        autoresume = {}
    return LoopConfig(
        context_window=model.get("context_window", cfg.context_window),
        smart_zone_ratio=model.get("smart_zone_ratio", cfg.smart_zone_ratio),
        absolute_ceiling=model.get("absolute_ceiling", cfg.absolute_ceiling),
        model=model.get("model", cfg.model),
        tier_light=tiers.get("light", cfg.tier_light),
        tier_mid=tiers.get("mid", cfg.tier_mid),
        tier_heavy=tiers.get("heavy", cfg.tier_heavy),
        window_hours=budget.get("window_hours", cfg.window_hours),
        window_token_budget=budget.get("window_token_budget", cfg.window_token_budget),
        window_cost_budget_usd=budget.get("window_cost_budget_usd", cfg.window_cost_budget_usd),
        on_cap=budget.get("on_cap", cfg.on_cap),
        max_iterations=loop.get("max_iterations", cfg.max_iterations),
        max_hours=loop.get("max_hours", cfg.max_hours),
        allowed_tools=loop.get("allowed_tools", cfg.allowed_tools),
        iteration_timeout_minutes=loop.get("iteration_timeout_minutes", cfg.iteration_timeout_minutes),
        permission_mode=loop.get("permission_mode", cfg.permission_mode),
        min_completion_heuristics=loop.get("min_completion_heuristics",
                                           cfg.min_completion_heuristics),
        no_progress_threshold=breaker.get("no_progress_threshold", cfg.no_progress_threshold),
        same_error_threshold=breaker.get("same_error_threshold", cfg.same_error_threshold),
        cooldown_minutes=breaker.get("cooldown_minutes", cfg.cooldown_minutes),
        replenish_enabled=replenish.get("enabled", cfg.replenish_enabled),
        replenish_ambition=replenish.get("ambition", cfg.replenish_ambition),
        replenish_max_rounds=replenish.get("max_rounds",
                                           cfg.replenish_max_rounds),
        replenish_min_score=_int_or(replenish.get("min_score"),
                                    cfg.replenish_min_score),
        replenish_max_per_round=_positive_int_or(
            replenish.get("max_per_round"), cfg.replenish_max_per_round),
        replenish_dry_rounds_to_exit=_int_or(
            replenish.get("dry_rounds_to_exit"),
            cfg.replenish_dry_rounds_to_exit),
        replenish_anti_lazy=replenish.get("anti_lazy", cfg.replenish_anti_lazy),
        # Positive only: a cap of 0 would end every chain at its first
        # wake-up, which is what `hive autoresume cancel` is for.
        max_wake_ups=_positive_int_or(autoresume.get("max_wake_ups"),
                                      cfg.max_wake_ups),
    )
