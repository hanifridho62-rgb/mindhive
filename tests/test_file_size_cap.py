# tests/test_file_size_cap.py
"""The 1000-line file cap, enforced instead of merely recorded.

The cap was already a real number in this codebase — `replenish.LINE_THRESHOLD`,
which the tech-debt lens scores against and `docs/session-legend.md` cites — but
nothing FAILED when a module crossed it. `dashboard.py` reached 2043 lines and
`dashboard_page.py` 2396, and the only consequence was a suggestion card the
lens could raise and a human could dismiss. A number that no run can fail is a
preference.

## The allowlist is the whole design

An unconditional "no module may exceed 1000 lines" would have been RED on the
day it was written, five times over, and a permanently red test gets marked
xfail or deleted. So the rule is: **an over-cap module must be listed, by name,
with the count it had when it was listed.** Three separate assertions ride on
that, and they close different holes:

* an unlisted module going over → fail. This is the cap.
* a LISTED module growing past its recorded count → fail. This is what keeps the
  allowlist from turning into a licence. `__main__.py` is allowed to be 4082
  lines; it is not allowed to become 4083.
* a listed module dropping under the cap → fail, asking for its removal. Without
  this the list silently accumulates modules that were fixed years ago, and a
  future reader cannot tell which entries are live debt.

Shrinking a listed module is therefore a two-line change here (edit the number),
and that is deliberate: the number is a ratchet, and a ratchet has to be turned.

## Why it counts lines with `review._line_count`

Because that is what the cap already means elsewhere. `replenish._line_count`
delegates to it for the same stated reason, so "how long is this file" has one
answer in this codebase rather than three subtly different ones (`len(read())`,
`splitlines()`, and a trailing-newline-sensitive `count("\\n")` all disagree on
the same file). Importing both the threshold and the counter means this test
cannot drift from the lens that scores against them.

## The scan is recursive; the lens is not

`replenish._python_modules` is deliberately non-recursive and skips `__`-prefixed
names, so the lens can see neither `__main__.py` nor `mindnet/controller.py` —
both of which are over the cap. This test walks the whole tree, which is why its
allowlist is longer than anything the lens has ever proposed.
"""
from pathlib import Path

import pytest

from mindhive import replenish, review

SRC = Path(__file__).resolve().parents[1] / "src" / "mindhive"
CAP = replenish.LINE_THRESHOLD

# Modules that were already over the cap when it became enforceable, with the
# count each had at that moment. Add an entry ONLY with a reason; the point of
# the list is that it shrinks.
#
# `__main__.py` and `replenish.py` are recorded in the ledger as known debt of
# exactly this kind and are being left alone here on purpose: this change split
# the two dashboard files, and folding two more multi-thousand-line refactors
# into it would make the diff unreviewable.
ALLOWED = {
    "__main__.py": 4082,          # the CLI surface; every command's argparse wiring
    "replenish.py": 2781,         # the suggestion lenses; a split is already deferred
    "workspace.py": 1462,         # MindParallel orchestration
    "research.py": 1353,          # Self-Reliant research runs
    "mindnet/controller.py": 1016,  # the phase spine's command surface
}


def _counts() -> dict:
    """`{repo-relative posix name: line count}` for every module under src."""
    return {str(p.relative_to(SRC)).replace("\\", "/"): review._line_count(str(p))
            for p in sorted(SRC.rglob("*.py"))}


def test_the_scan_finds_the_tree_at_all():
    """CANARY. Every assertion below is over `_counts()`, and an empty or
    all-zero mapping would satisfy all of them while checking nothing — the
    precise failure a moved `src/` layout or a broken counter would produce."""
    counts = _counts()
    assert len(counts) > 40, f"only {len(counts)} modules found under {SRC}"
    assert "dashboard.py" in counts, "the scan did not reach the package root"
    assert "mindnet/controller.py" in counts, "the scan did not recurse"
    # Tied to file SIZE, not to the count: `loop/__init__.py` is a genuinely
    # empty file, so "every module has lines" is false on this tree and a test
    # asserting it would be wrong rather than strict.
    silent = [name for name, n in counts.items()
              if n == 0 and (SRC / name).stat().st_size > 0]
    assert silent == [], f"the counter returned 0 for non-empty files: {silent}"


def test_no_unlisted_module_exceeds_the_cap():
    """THE CAP. A module going over without being listed fails here."""
    over = {name: n for name, n in _counts().items()
            if n > CAP and name not in ALLOWED}
    assert over == {}, (
        f"module(s) past the {CAP}-line cap and not in ALLOWED: {over}. Split "
        f"the file, or add it to ALLOWED with its count and a reason — but "
        f"adding it is a debt entry, not a fix.")


def test_every_allowlisted_module_still_exists():
    """A renamed or deleted module must not leave a dead entry behind: the
    allowlist would then silently permit nothing while looking like a real
    record of debt."""
    counts = _counts()
    ghosts = [name for name in ALLOWED if name not in counts]
    assert ghosts == [], f"ALLOWED names modules that no longer exist: {ghosts}"


def test_no_allowlisted_module_has_grown():
    """The ratchet. Being on the list buys amnesty for the lines already there,
    not for the next thousand."""
    counts = _counts()
    grown = {name: (recorded, counts[name]) for name, recorded in ALLOWED.items()
             if name in counts and counts[name] > recorded}
    assert grown == {}, (
        f"allowlisted module(s) grew past their recorded count "
        f"(name: recorded -> now): {grown}. The allowlist is a ceiling, not a "
        f"licence — shrink the file, or justify raising the number.")


def test_no_allowlisted_module_is_secretly_under_the_cap():
    """The other end of the ratchet. A module that got fixed must come OFF the
    list, or the list stops describing the debt that actually remains."""
    counts = _counts()
    fixed = {name: counts[name] for name in ALLOWED
             if name in counts and counts[name] <= CAP}
    assert fixed == {}, (
        f"module(s) in ALLOWED are now under the {CAP}-line cap: {fixed}. "
        f"Delete their entries — that is the win being recorded.")


def test_the_cap_check_actually_fails_on_a_violation(monkeypatch):
    """CANARY for the cap itself. Every assertion above is an emptiness claim,
    and a `_counts()` that returned `{}`, or a comparison that never fired,
    would pass all of them. Plant a violation and require a failure."""
    monkeypatch.setitem(ALLOWED, "workspace.py", 99999)   # keep the real one quiet
    planted = dict(_counts(), **{"not_a_real_module.py": CAP + 1})
    over = {name: n for name, n in planted.items()
            if n > CAP and name not in ALLOWED}
    assert "not_a_real_module.py" in over, \
        "a module a full line over the cap was not detected"


def test_the_cap_is_precise_at_the_boundary():
    """One line under is fine, one line over is not — asserted rather than
    assumed, because `>` vs `>=` here is the difference between a 1000-line cap
    and a 999-line one."""
    counts = {"under.py": CAP, "over.py": CAP + 1}
    over = {name: n for name, n in counts.items() if n > CAP}
    assert over == {"over.py": CAP + 1}


@pytest.mark.parametrize("name", [
    "dashboard.py", "dashboard_page.py",
    "dashboard_get.py", "dashboard_post.py", "dashboard_jobs.py",
    "dashboard_data.py", "dashboard_memo.py", "dashboard_locks.py",
    "dashboard_css.py", "dashboard_js_core.py", "dashboard_js_improve.py",
    "dashboard_js_oversee.py", "dashboard_js_views.py",
    "dashboard_js_skills.py", "dashboard_js_actions.py",
])
def test_the_dashboard_split_landed_and_stays_landed(name):
    """The specific regression this file was written alongside. `dashboard.py`
    and `dashboard_page.py` were 2043 and 2396 lines; naming every module the
    split produced means a future change cannot quietly re-merge them or grow
    one of the halves back over the cap without saying so here."""
    counts = _counts()
    assert name in counts, f"{name} is missing — was the dashboard split undone?"
    assert counts[name] <= CAP, (
        f"{name} is {counts[name]} lines, past the {CAP}-line cap the dashboard "
        f"split existed to respect")
