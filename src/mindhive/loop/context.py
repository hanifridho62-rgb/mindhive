# src/mindhive/loop/context.py
from dataclasses import dataclass


@dataclass
class ContextConfig:
    context_window: int
    smart_zone_ratio: float


def occupancy_from(input_tokens: int,
                   cache_creation_input_tokens: int = 0,
                   cache_read_input_tokens: int = 0) -> int:
    """Current context size: every token the model READ this turn.

    All three fields are prompt. With prompt caching on, `input_tokens` is
    typically 1-2 — the uncached remainder — while the conversation itself sits
    in the two cache fields: `cache_read` is the span served from cache, and
    `cache_creation` is the span written to cache *on this turn*. Both were in
    the prompt; the split is a billing distinction, not a size one.

    Omitting `cache_creation` (as this did) undercounts by exactly the newly
    written span every turn, so the smart-zone boundary arrives late. `output_tokens`
    is deliberately absent: it is what the model WROTE, and it reaches the context
    only on the next turn, where it arrives inside these same input fields."""
    return input_tokens + cache_creation_input_tokens + cache_read_input_tokens


def fill_ratio(occupancy: int, window: int) -> float:
    if window <= 0:
        return 0.0
    return occupancy / window


def needs_reset(occupancy: int, cfg: ContextConfig) -> bool:
    """True once the context leaves the smart zone (fill >= ratio), meaning the
    loop should end this Claude session and reseed from the compact briefing."""
    return fill_ratio(occupancy, cfg.context_window) >= cfg.smart_zone_ratio
