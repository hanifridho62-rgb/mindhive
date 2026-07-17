# src/mindhive/loop/context.py
from dataclasses import dataclass


@dataclass
class ContextConfig:
    context_window: int
    smart_zone_ratio: float


def occupancy_from(input_tokens: int, cache_read_input_tokens: int) -> int:
    """Proxy for current context size: the tokens the model read this turn."""
    return input_tokens + cache_read_input_tokens


def fill_ratio(occupancy: int, window: int) -> float:
    if window <= 0:
        return 0.0
    return occupancy / window


def needs_reset(occupancy: int, cfg: ContextConfig) -> bool:
    """True once the context leaves the smart zone (fill >= ratio), meaning the
    loop should end this Claude session and reseed from the compact briefing."""
    return fill_ratio(occupancy, cfg.context_window) >= cfg.smart_zone_ratio
