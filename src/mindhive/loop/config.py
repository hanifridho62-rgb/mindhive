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
    # [replenish] — the ambition dial (replenish.AMBITION). Only two settings
    # are exposed: the threshold, the per-round cap and the active lens set all
    # move together with the level, because tuning them apart produces
    # combinations that cancel out (a low threshold with a small cap just means
    # the cap picks the winners).
    replenish_enabled: bool = True
    replenish_ambition: str = "balanced"

    @property
    def cooldown_seconds(self) -> float:
        return self.cooldown_minutes * 60

    @property
    def iteration_timeout_seconds(self) -> float:
        return self.iteration_timeout_minutes * 60


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
    # A missing section yields the dataclass defaults, so every loop.toml
    # written before the dial existed loads unchanged.
    replenish = data.get("replenish", {})
    if not isinstance(replenish, dict):
        replenish = {}
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
    )
