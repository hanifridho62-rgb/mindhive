# src/mindhive/rotation/window.py
"""Model id -> context window, and window -> smart-zone rotation threshold.

The table is static on purpose. This is read by the Stop hook, which runs at the
end of EVERY agent turn in every session on the machine; a network call there
would put an unbounded stall between the model finishing and the user seeing it.

To refresh the numbers, ask the Models API rather than guessing:

    GET /v1/models/{id}   ->   .max_input_tokens

and edit WINDOWS below. Being out of date here is safe in one direction only:
an unknown or stale-small window rotates EARLY (a wasted rotation), while a
stale-large one rotates LATE (a session that fills up and degrades). That is why
the fallback is the smallest current window, not the largest.
"""

# Smallest window any current model has. Unknown ids resolve here so a model we
# have never heard of rotates early rather than never.
DEFAULT_WINDOW = 200_000

# Absolute ceiling on the rotation threshold regardless of window size. A 1M
# window does not mean a 1M-token session is a good idea: quality and latency
# degrade long before the hard limit, and the smart zone is about where the
# session still thinks well, not where it stops accepting tokens.
DEFAULT_CAP = 500_000

# (model-id prefix, context window). Longest matching prefix wins, so dated
# snapshots (`claude-haiku-4-5-20251001`) resolve without their own entry and
# ordering here is not load-bearing.
WINDOWS: tuple[tuple[str, int], ...] = (
    # Current generation — all 1M.
    ("claude-opus-5", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
    ("claude-opus-4-7", 1_000_000),
    ("claude-opus-4-6", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-fable-5", 1_000_000),
    ("claude-mythos-5", 1_000_000),
    # Current generation, small window — the one model that is not 1M.
    ("claude-haiku-4-5", 200_000),
    # Legacy, still callable.
    ("claude-opus-4-5", 200_000),
    ("claude-opus-4-1", 200_000),
    ("claude-opus-4-0", 200_000),
    ("claude-sonnet-4-5", 200_000),
    ("claude-sonnet-4-0", 200_000),
    # Rolling aliases the CLI accepts in place of an id (`--model sonnet`).
    ("opus", 1_000_000),
    ("sonnet", 1_000_000),
    ("haiku", 200_000),
)


def window_for(model) -> int:
    """Context window for *model*, or DEFAULT_WINDOW when it is unknown.

    Total by contract: a transcript may carry no model field at all, or a
    non-string, and this is called from a hook that must never raise."""
    if not isinstance(model, str) or not model:
        return DEFAULT_WINDOW
    best_len, best_window = -1, DEFAULT_WINDOW
    for prefix, size in WINDOWS:
        if model.startswith(prefix) and len(prefix) > best_len:
            best_len, best_window = len(prefix), size
    return best_window


def threshold(model, ratio: float, cap: int = DEFAULT_CAP) -> int:
    """Occupancy at which the session should rotate: ``min(cap, ratio x window)``.

    Two bounds because they answer different questions. `ratio` is proportional —
    "how much of this model's window is the smart zone" — and is what makes the
    same config behave correctly on a 200K Haiku and a 1M Opus. `cap` is absolute:
    past a few hundred thousand tokens a session is worse to work with regardless
    of how much room is left, so the ratio alone would let a 1M model coast into
    a context nobody wants. Whichever binds first wins.

    Clamped at zero so a nonsense ratio yields a useless-but-safe threshold
    (rotate always) rather than a negative one (rotate never, via comparison)."""
    try:
        raw = int(float(ratio) * window_for(model))
    except (TypeError, ValueError):
        return 0
    return max(0, min(int(cap), raw))
