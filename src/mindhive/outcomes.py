# SPDX-License-Identifier: Apache-2.0
"""One vocabulary for "did that actually do anything?", and a registry of the
surfaces that answer it.

Five surfaces in this system report progress to a human: the dashboard's long
operations, roadmap merge detection, the rotation hook's advice, the
SOP-injection probe, and the context health check. Each one had invented its own
way of saying "fine" — a bare ``ok: True``, an empty list, a zero count, a
cheerful log line — and every one of those reads identically whether the surface
did real work or quietly did nothing. That is the failure this module names:
**"it ran" and "it changed something" are different facts, and only the second
one is what a person clicking a button wants to know.**

So there are five outcomes, not two, and the distinction that earns the module
is `DID_WORK` vs `NOTHING_TO_DO`. A harvest that found no new transcripts is a
SUCCESS, and reporting it as one is honest; reporting it the same way as a
harvest that ingested forty sessions is not.

`DEGRADED` exists for the third case that kept being flattened into one of the
first two: the operation ran, the part it could do it did, and some part it
could not (no network, chromadb absent, `gh` rate-limited). Reporting that as
success hides a broken half; reporting it as failure hides a working half.

The registry is **opt-in**, and that is the point rather than an omission. A
big-bang test that reached into all five surfaces would only pass once the last
of five PRs landed, so it would be red for weeks and tell nobody anything;
`tests/test_outcomes.py` instead parametrizes over whatever is registered TODAY
and grows a case each time a surface joins. The final assertion that all five
are present belongs to the PR that lands the fifth one — a test that counts to
five before five exist is a test that fails for being early.

Probes are cheap and read-only by contract. `probe` answers "is this surface's
feedback loop working?", never "do the work" — nothing here spawns pytest, calls
a model, or writes to the ledger, because the parametrized test runs every
registered probe on every suite run.
"""

from __future__ import annotations

import collections


# The one outcome vocabulary. Order is meaningful for a human reading a list of
# them: descending by how much attention the outcome deserves.
FAILED = "failed"
DEGRADED = "degraded"
DID_WORK = "did_work"
NOTHING_TO_DO = "nothing_to_do"
# Written by nobody at the time the work happens. A run can only be called
# abandoned by something that outlives it and finds it unfinished — the
# dashboard's boot seal (see `jobs.seal_orphans`). A surface returning this from
# its own probe would be claiming to have witnessed its own death.
ABANDONED = "abandoned"

OUTCOMES = (FAILED, DEGRADED, DID_WORK, NOTHING_TO_DO, ABANDONED)

# What a caller may report about its own run. `ABANDONED` is excluded for the
# reason given above.
SELF_REPORTABLE = (FAILED, DEGRADED, DID_WORK, NOTHING_TO_DO)

MAX_SUMMARY_CHARS = 240


class Report(collections.namedtuple("Report", "outcome summary result")):
    """What every surface returns: one outcome, one human line, and the
    surface's own body.

    `summary` is the sentence a person reads — required, because an outcome
    without a reason is the same dead end as the bare ``ok: True`` this module
    replaces. "nothing_to_do" alone prompts "…because?"; "no transcripts newer
    than the last harvest" answers it.

    `result` is passed through untouched and is never inspected here. It stays
    opaque on purpose: the shape of a selftest result and the shape of an embed
    result have nothing in common, and a module that knew both would have to
    change every time either did. This is the one place a caller-defined
    structure crosses the boundary, and it crosses it as data, not as a
    namespace to hang fields on.
    """

    __slots__ = ()

    def __new__(cls, outcome, summary, result=None):
        if outcome not in OUTCOMES:
            raise ValueError(
                f"unknown outcome {outcome!r} — one of: {', '.join(OUTCOMES)}")
        text = str(summary or "").strip()
        if not text:
            raise ValueError(f"{outcome} report requires a summary saying why")
        return super().__new__(cls, outcome, text[:MAX_SUMMARY_CHARS], result)

    @property
    def worked(self) -> bool:
        """True when the surface completed what it set out to do — including the
        case where the honest answer was that there was nothing to do."""
        return self.outcome in (DID_WORK, NOTHING_TO_DO)


class Surface(collections.namedtuple("Surface", "name what probe")):
    """A registered feedback loop: its id, the one line saying which loop it
    closes, and the read-only probe that reports on it."""

    __slots__ = ()


_SURFACES: "collections.OrderedDict[str, Surface]" = collections.OrderedDict()


def register(name, what, probe) -> Surface:
    """Add a surface to the registry. Idempotent per name only when re-registered
    with the same probe — a silent overwrite by a different probe is how two
    modules would each think they owned the same loop, so it is refused."""
    key = str(name or "").strip()
    if not key:
        raise ValueError("a surface needs a name")
    if not str(what or "").strip():
        raise ValueError(f"surface {key!r} needs a `what` naming its loop")
    if not callable(probe):
        raise ValueError(f"surface {key!r} needs a callable probe")
    existing = _SURFACES.get(key)
    if existing is not None and existing.probe is not probe:
        raise ValueError(f"surface {key!r} is already registered by something else")
    surface = Surface(key, str(what).strip(), probe)
    _SURFACES[key] = surface
    return surface


def registered() -> tuple:
    """Every registered surface, in registration order."""
    return tuple(_SURFACES.values())


def get(name) -> "Surface | None":
    return _SURFACES.get(str(name or "").strip())


def run_probe(name, project=None) -> Report:
    """Run one surface's probe and always come back with a Report.

    The containment is deliberate and belongs here rather than in each probe: a
    probe exists to report on a surface that may itself be broken, so "the probe
    raised" is a state this has to survive, and a probe author should not have to
    remember that. An unregistered name is the one thing that is a caller bug
    rather than a surface fault, so it raises.
    """
    surface = get(name)
    if surface is None:
        raise KeyError(f"no surface registered as {name!r}")
    try:
        report = surface.probe(project)
    except Exception as e:                      # noqa: BLE001 — see docstring
        return Report(FAILED, f"{surface.name} probe raised {type(e).__name__}: {e}")
    if not isinstance(report, Report):
        return Report(
            FAILED,
            f"{surface.name} probe returned {type(report).__name__}, not a Report")
    return report
