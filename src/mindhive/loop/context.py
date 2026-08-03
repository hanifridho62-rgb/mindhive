# src/mindhive/loop/context.py
"""The smart zone: how much context a session is carrying, and whether that has
left the band where the model still does its best work.

Two things live here and nothing else — this module is PURE. It performs no
I/O and must not gain any; every caller (the loop engine, the fleet executor,
the interactive Stop hook) hands it numbers and gets a verdict back.

## Occupancy is the true prompt size

The API's own identity is ``input + cache_creation + cache_read``. The original
formula here summed only the first and third, which reads correctly in steady
state — where ``cache_read`` dominates — and badly undercounts on a session's
opening turns, where everything lands in ``cache_creation`` and ``cache_read``
is still zero. That is exactly the stretch where a session grows fastest, so
the omission hid growth precisely when it mattered most.

## The ceiling is the minimum of two bounds

The operator thinks in absolute tokens ("keep every session under 500k"); this
module originally thought only in ratios of a configured window. Those two
agree by coincidence on a 1M-window model (1M x 0.5 = 500k) and disagree
everywhere else — most sharply on Haiku 4.5, whose real window is 200K, where
an absolute 500k bound can never fire at all and a session would run to its
hard context limit untouched.

So both bounds are computed and the smaller wins:

* the **absolute** bound is the operator's number, honoured on any model;
* the **ratio** bound is ``smart_zone_ratio`` of *that model's real window*,
  resolved from the model id rather than from a single shared scalar.

A non-positive bound means "no bound" rather than "rotate immediately" — the
opposite reading would rotate on every single turn. When neither bound is
usable the answer is "cannot say", and the gate does not rotate.

## Unknown models degrade, they do not fail open

`resolve_window` answers ``(window, source)``. A model id the table has not
learned yet resolves to the configured scalar with source ``"config"``, so a
stale table degrades to the previous behaviour and the caller can record that
it happened. It never returns "no limit".
"""
from dataclasses import dataclass

# The operator's stated smart-zone ceiling. A default, not a constant of
# nature: no vendor documentation describes a quality cliff at this number —
# it is an empirical call from this operator's own runs, and it is meant to be
# tuned in loop.toml when their evidence changes.
DEFAULT_ABSOLUTE_CEILING = 500_000

_1M = 1_000_000
_200K = 200_000

# Real context windows, keyed by the ids that actually reach this resolver.
# Deliberately only models whose window is documented — a guess here would be
# worse than the honest fallback, because a too-large guess disables the gate
# for that model silently.
MODEL_WINDOWS = {
    # 1M-window models
    "claude-fable-5": _1M,
    "claude-mythos-5": _1M,
    "claude-opus-5": _1M,
    "claude-opus-4-8": _1M,
    "claude-opus-4-7": _1M,
    "claude-opus-4-6": _1M,
    "claude-sonnet-5": _1M,
    "claude-sonnet-4-6": _1M,
    # 200K-window models
    "claude-haiku-4-5": _200K,
    "claude-haiku-4-5-20251001": _200K,
    # Bare tier aliases. These are not decoration: `claude_cli.MODEL` is the
    # literal string "sonnet", so the alias form reaches this resolver on every
    # reflection and auto-resume spawn, while the loop's tier table uses full
    # ids. Both shapes must resolve or half the fleet silently falls back.
    "opus": _1M,
    "sonnet": _1M,
    "haiku": _200K,
}

# Provider-qualified ids (Amazon Bedrock spells them "anthropic.claude-opus-5").
# Stripped rather than duplicated into the table above, so the table stays one
# row per model.
_PROVIDER_PREFIXES = ("anthropic.",)


@dataclass
class ContextConfig:
    context_window: int
    smart_zone_ratio: float
    absolute_ceiling: int = DEFAULT_ABSOLUTE_CEILING


def occupancy_from(input_tokens: int, cache_creation_input_tokens: int,
                   cache_read_input_tokens: int) -> int:
    """The tokens the model actually read this turn — the true prompt size.

    All three terms, per the API's own accounting. Dropping
    ``cache_creation_input_tokens`` (as this once did) reports near-zero
    occupancy for the first turns of every session."""
    return (int(input_tokens or 0)
            + int(cache_creation_input_tokens or 0)
            + int(cache_read_input_tokens or 0))


def fill_ratio(occupancy: int, window: int) -> float:
    """Occupancy as a fraction of *window*. A non-positive window answers 0.0
    rather than raising — this sits under a loop driving real subprocesses, and
    a projection fault must not become a crash."""
    if window <= 0:
        return 0.0
    return occupancy / window


def resolve_window(model, cfg: ContextConfig) -> tuple:
    """``(window, source)`` for *model*, where source is ``"model"`` when the
    id was recognised and ``"config"`` when it fell back to the configured
    scalar.

    The source is returned rather than logged here because this module is pure;
    the caller records it. That matters: a run that silently fell back looks
    identical to one that resolved, and the difference is whether the ceiling
    means what the operator thinks it means."""
    key = str(model or "").strip().lower()
    for prefix in _PROVIDER_PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    window = MODEL_WINDOWS.get(key)
    if window is None:
        return cfg.context_window, "config"
    return window, "model"


def smart_zone_ceiling(cfg: ContextConfig, model=None):
    """The occupancy at which this session leaves the smart zone, or ``None``
    when neither bound is usable.

    ``min`` of the bounds that are actually available. A non-positive bound is
    absent, not zero: reading ``absolute_ceiling = 0`` as a ceiling of zero
    would rotate on every turn, which is the loudest possible way to be wrong.
    """
    bounds = []

    absolute = int(cfg.absolute_ceiling or 0)
    if absolute > 0:
        bounds.append(absolute)

    window, _source = resolve_window(model, cfg)
    ratio = float(cfg.smart_zone_ratio or 0)
    if window > 0 and ratio > 0:
        bounds.append(int(window * ratio))

    return min(bounds) if bounds else None


def needs_reset(occupancy: int, cfg: ContextConfig, model=None) -> bool:
    """True once the context leaves the smart zone, meaning the caller should
    end this Claude session and reseed from the compact briefing.

    Inclusive (``>=``): a session sitting exactly on the ceiling has left the
    zone. With ``>`` it would sit there indefinitely.

    ``model`` is optional so the original two-argument call shape keeps
    working; omitting it measures against the configured scalar, which is what
    that shape always meant."""
    ceiling = smart_zone_ceiling(cfg, model)
    if ceiling is None:
        return False
    return occupancy >= ceiling
