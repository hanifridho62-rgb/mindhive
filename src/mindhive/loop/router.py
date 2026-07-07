# src/mindhive/loop/router.py
"""Deterministic model-tier routing for loop iterations.

Route each iteration to the lightest sufficient model instead of always
paying for a flagship: a static size signal picks the starting tier, ledger
history overrides it with what actually worked for comparable-size runs, and
a failed or no-progress iteration escalates the next one a tier. Pure
functions — the engine owns all I/O and the actual model ids come from
config (an explicit ``[model].model`` bypasses routing entirely)."""

TIERS = ("light", "mid", "heavy")

# static size signals: required fix_plan items and prompt length
SMALL_MAX_REQUIRED = 2
LARGE_MIN_REQUIRED = 6
SMALL_MAX_PROMPT_CHARS = 400
LARGE_MIN_PROMPT_CHARS = 2000

_STATIC_TIER = {"small": "light", "medium": "mid", "large": "heavy"}


def size_class(required_count: int, prompt_chars: int) -> str:
    """small | medium | large from the task's static shape."""
    if required_count >= LARGE_MIN_REQUIRED or prompt_chars >= LARGE_MIN_PROMPT_CHARS:
        return "large"
    if required_count <= SMALL_MAX_REQUIRED and prompt_chars <= SMALL_MAX_PROMPT_CHARS:
        return "small"
    return "medium"


def escalate(tier: str) -> str:
    """One tier up, saturating at heavy."""
    i = TIERS.index(tier) if tier in TIERS else 0
    return TIERS[min(i + 1, len(TIERS) - 1)]


def run_history(evs) -> list[dict]:
    """``{size, tier, reason}`` per finished run, oldest first, from loop
    events (run_id-stamped only — legacy runs carry no tier to learn from)."""
    runs: dict[str, dict] = {}
    order: list[str] = []
    for e in evs:
        p = e.payload if isinstance(e.payload, dict) else {}
        rid = p.get("run_id")
        if not rid:
            continue
        if e.type == "loop_run_start":
            runs[rid] = {"size": p.get("size_class"), "tier": None, "reason": None}
            order.append(rid)
        elif e.type == "loop_iteration" and rid in runs and p.get("tier"):
            runs[rid]["tier"] = p["tier"]
        elif e.type == "loop_run_end" and rid in runs:
            runs[rid]["reason"] = p.get("reason")
    return [runs[r] for r in order if runs[r]["reason"]]


def pick_initial_tier(size: str, history=None) -> str:
    """Starting tier for a run: the lowest tier that COMPLETED a
    comparable-size run wins (measured evidence beats the static guess); when
    the static tier's most recent comparable run tripped the breaker instead,
    start one tier up. No history -> the static size mapping."""
    static = _STATIC_TIER.get(size, "mid")
    comparable = [h for h in (history or [])
                  if h.get("size") == size and h.get("tier") in TIERS]
    completed = [h["tier"] for h in comparable if h.get("reason") == "completed"]
    if completed:
        return min(completed, key=TIERS.index)
    at_static = [h for h in comparable if h["tier"] == static]
    if at_static and at_static[-1].get("reason") == "breaker_open":
        return escalate(static)
    return static
