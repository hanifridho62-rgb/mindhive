# src/mindhive/loop/config.py
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LoopConfig:
    # [model]
    context_window: int = 200000
    smart_zone_ratio: float = 0.5
    model: str | None = None
    effort: str | None = None
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
    # [breaker]
    no_progress_threshold: int = 3
    same_error_threshold: int = 5
    cooldown_minutes: int = 30

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
    budget = data.get("budget", {})
    loop = data.get("loop", {})
    breaker = data.get("breaker", {})
    return LoopConfig(
        context_window=model.get("context_window", cfg.context_window),
        smart_zone_ratio=model.get("smart_zone_ratio", cfg.smart_zone_ratio),
        model=model.get("model", cfg.model),
        effort=model.get("effort", cfg.effort),
        window_hours=budget.get("window_hours", cfg.window_hours),
        window_token_budget=budget.get("window_token_budget", cfg.window_token_budget),
        window_cost_budget_usd=budget.get("window_cost_budget_usd", cfg.window_cost_budget_usd),
        on_cap=budget.get("on_cap", cfg.on_cap),
        max_iterations=loop.get("max_iterations", cfg.max_iterations),
        max_hours=loop.get("max_hours", cfg.max_hours),
        allowed_tools=loop.get("allowed_tools", cfg.allowed_tools),
        iteration_timeout_minutes=loop.get("iteration_timeout_minutes", cfg.iteration_timeout_minutes),
        no_progress_threshold=breaker.get("no_progress_threshold", cfg.no_progress_threshold),
        same_error_threshold=breaker.get("same_error_threshold", cfg.same_error_threshold),
        cooldown_minutes=breaker.get("cooldown_minutes", cfg.cooldown_minutes),
    )
