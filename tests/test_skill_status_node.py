# tests/test_skill_status_node.py
"""Tier-1 node test of the Skill panel's four status states, executed.

A skill invoked ONLY by slash command has calls but no observed outcome:
nothing fired a tool, so no response was ever read and nothing can ever mark
it failed. `plugins.skill_stats` therefore reports ``last_outcome: null`` and
``completion_rate: null`` for it, and the panel has to say "called, outcome
unknown" — not "last call succeeded" (which it used to, on a hardcoded
``ok: True``) and not "never called" (which a naive fallthrough gives, about a
skill that ran).

That status is DERIVED, through three fallbacks, and the headline rate is a
division whose denominator is the entire question. Grepping the script for the
word 'unobserved' would pass on a page that computes the number wrongly, so
these cases run the panel's own code.

WHY THE `node` MARKER IS NOT COSMETIC. `pyproject.toml` carries
``addopts = -m "not node"`` so that `hive selftest` -- which shells pytest from
inside the running dashboard's own POST handler -- never forks a JS runtime out
of a web request. Run it deliberately:

    pytest -m node tests/test_skill_status_node.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mindhive import dashboard_js_core, dashboard_js_skills

pytestmark = pytest.mark.node

HARNESS = Path(__file__).parent / "node" / "skill_status.test.mjs"
NODE_TIMEOUT_S = 60


# --- fixture builders: the shapes plugins.skill_stats actually emits --------

def _observed(calls, ok):
    """A skill whose calls all had a witnessed outcome (invoke/hook path)."""
    fail = calls - ok
    return {"calls": calls, "ok": ok, "fail": fail, "unobserved": 0,
            "completion_rate": (ok / calls) if calls else None,
            "last_outcome": "fail" if fail else "ok",
            "last_used": "2026-07-01T10:00:00Z",
            "streak": 0 if fail else ok, "fail_streak": fail,
            "provenance": {"invoke": calls}, "sources": ["invoke_result"]}


def _unobserved(calls):
    """A slash-command-only skill: real calls, no outcome ever witnessed."""
    return {"calls": calls, "ok": 0, "fail": 0, "unobserved": calls,
            "completion_rate": None, "last_outcome": None,
            "last_used": "2026-07-01T10:00:00Z", "streak": 0, "fail_streak": 0,
            "provenance": {"passive": calls}, "sources": ["skill_used"]}


def _mixed(unobs, ok):
    """Typed slash invocations AND a real observed completion."""
    st = _unobserved(unobs)
    st.update({"ok": ok, "completion_rate": 1.0, "last_outcome": "ok",
               "streak": ok, "calls": unobs + ok})
    st["sources"] = ["invoke_result", "skill_used"]
    return st


def _inv(*names):
    return {"plugins": [], "counts": {"plugins": 0, "skills_total": len(names)},
            "local_skills": [{"name": n, "trigger": ""} for n in names]}


CASES = {
    # One skill per status, so a collapse of any two shows up as a duplicate.
    "four_states": {
        "inv": _inv("green", "red", "typed", "never"),
        "stats": {"green": _observed(3, 3), "red": _observed(3, 2),
                  "typed": _unobserved(4)},
    },
    # The headline rate must divide by observed calls only.
    "rate_excludes_unobserved": {
        "inv": _inv("typed", "real"),
        "stats": {"typed": _unobserved(3), "real": _observed(1, 1)},
    },
    "nothing_observed_at_all": {
        "inv": _inv("typed"),
        "stats": {"typed": _unobserved(3)},
    },
    "no_calls_at_all": {
        "inv": _inv("never"),
        "stats": {},
    },
    # A skill with both kinds of call keeps the outcome it really has.
    "mixed_record": {
        "inv": _inv("both"),
        "stats": {"both": _mixed(3, 1)},
    },
    # The invoke-channel status map still wins over a bare call count: it is a
    # real observed outcome from /api/skills/status.
    "invoke_channel_status_still_wins": {
        "inv": _inv("typed"),
        "stats": {"typed": _unobserved(2)},
        "status": {"typed": "fail"},
    },
}


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    node = shutil.which("node")
    if not node:                                    # pragma: no cover
        pytest.skip("node is not on PATH -- these cases were REQUESTED "
                    "(`-m node`) and did not run; nothing here passed")
    work = tmp_path_factory.mktemp("skill-status")
    # Both fragments, verbatim and in page order: the panel calls `esc` and
    # `escAttr` from the core fragment, and the served page is one script.
    core = work / "core.js"
    core.write_text(dashboard_js_core.JS, encoding="utf-8")
    src = work / "skills.js"
    src.write_text(dashboard_js_skills.JS, encoding="utf-8")
    cases = work / "cases.json"
    cases.write_text(json.dumps(CASES), encoding="utf-8")

    proc = subprocess.run(
        [node, str(HARNESS), str(core), str(src), str(cases)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=NODE_TIMEOUT_S,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        data = json.loads(proc.stdout)
    except ValueError:                              # pragma: no cover
        pytest.fail(f"node produced no report (exit {proc.returncode})\n"
                    f"--- stdout ---\n{proc.stdout[:4000]}\n"
                    f"--- stderr ---\n{proc.stderr[:4000]}")
    # Every case named above must have RUN. A report that silently stopped
    # short would otherwise pass every assertion it never reached.
    missing = [k for k in CASES if k not in data]
    assert not missing, f"the harness ran no case named {missing}"
    return data


def _status(rep, case, name):
    return {e["name"]: e["status"] for e in rep[case]["entries"]}[name]


def test_the_page_serves_the_panel_this_file_executes():
    """The seam. A fragment that is tested but not spliced is a test of
    nothing, and this repo has already shipped a page whose producer was
    missing."""
    from mindhive.dashboard_page import render_page
    page = render_page("test-token")
    assert dashboard_js_skills.JS in page, (
        "the page no longer inlines dashboard_js_skills.JS verbatim -- the "
        "panel the browser runs and the panel this file executes have diverged")


def test_a_typed_only_skill_is_not_reported_as_a_success(report):
    """It carried ``ok: True`` on every slash row before the harvest fix, so
    the dot read green and the tooltip said 'last call succeeded' about a run
    nobody observed.

    Kills 'an unwitnessed call renders as ok'."""
    assert _status(report, "four_states", "typed") == "unobserved"


def test_a_typed_only_skill_is_not_reported_as_never_called(report):
    """The other direction, and the regression the outcome_observed fix would
    have introduced on its own: ``last_outcome`` goes null, the status
    fallthrough reaches 'uncalled', and a skill that ran four times renders
    grey with the tooltip 'never called'.

    Kills 'no outcome means no call'."""
    assert _status(report, "four_states", "typed") != "uncalled"
    assert _status(report, "four_states", "never") == "uncalled"


def test_the_four_states_render_as_four_distinct_dots(report):
    """CANARY against collapsing the new state into a neighbour: four states
    that share a class or a tooltip are three states with extra words."""
    dots = report["four_states"]["dots"]
    assert len(set(dots.values())) == 4, dots
    assert "sunobs" in dots["unobserved"]
    assert "outcome never observed" in dots["unobserved"]


def test_the_class_the_panel_emits_exists_in_the_stylesheet(report):
    """A dot class with no CSS rule is an invisible dot."""
    from mindhive import dashboard_css
    css = dashboard_css.CSS if hasattr(dashboard_css, "CSS") else ""
    assert ".statusDot.sunobs" in css or ".sunobs" in css


def test_observed_outcomes_still_render_as_before(report):
    """CANARY: the new state must not swallow the two that already worked."""
    assert _status(report, "four_states", "green") == "ok"
    assert _status(report, "four_states", "red") == "fail"


def test_the_headline_rate_divides_by_observed_calls_only(report):
    """One real completion beside three typed invocations is 100%, not 25%.

    Kills 'count unobserved calls in the denominator', which would drag the
    strip toward 0% as slash invocations accumulate -- reporting failures
    nobody observed, the mirror image of the inflation being fixed."""
    strip = report["rate_excludes_unobserved"]["strip"]
    assert ">100%<" in strip, strip
    assert "1/1 observed call" in strip
    assert "3 unobserved" in strip


def test_a_record_with_no_observed_call_shows_no_rate_and_says_why(report):
    """Not a bare dash that looks like 'nothing has run yet'."""
    strip = report["nothing_observed_at_all"]["strip"]
    assert "none with an observed outcome" in strip
    assert "3 call" in strip
    assert "100%" not in strip and "0%" not in strip


def test_a_cold_start_still_says_no_calls_recorded_yet(report):
    """CANARY for the branch above: with zero calls the label must not claim
    calls exist."""
    strip = report["no_calls_at_all"]["strip"]
    assert "no calls recorded yet" in strip


def test_a_mixed_record_keeps_the_outcome_it_really_has(report):
    """An observed completion alongside typed invocations is still 'ok'."""
    assert _status(report, "mixed_record", "both") == "ok"


def test_the_invoke_channel_status_still_outranks_the_call_count(report):
    """/api/skills/status carries real observed outcomes; the call-count
    fallback is the LAST resort, not the first."""
    assert _status(report, "invoke_channel_status_still_wins", "typed") == "fail"


def test_needs_attention_sorting_places_the_new_state_deliberately(report):
    """`status` sort is 'needs-attention first'. A failure leads; an unknown
    outcome sits behind 'never called' and ahead of a clean run, rather than
    landing on the default rank by accident."""
    rank = report["four_states"]["rank"]
    assert rank["fail"] < rank["uncalled"] < rank["unobserved"] < rank["ok"]
    assert report["four_states"]["sorted"] == ["red", "never", "typed", "green"]


def test_the_legend_offers_the_new_state_as_a_filter(report):
    """The legend items double as one-click filters; a state absent from it is
    a state the operator cannot filter to."""
    legend = report["four_states"]["legend"]
    assert "called, outcome not observed" in legend
    assert legend.count("legitem") == 4
