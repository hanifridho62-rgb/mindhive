# tests/test_dashboard_jobbar_node.py
"""Tier-1 node test of the job pane's progress bar: `jobText`, the exact bytes.

`progress_pct` reaches the page off the wire: `jobs.OutputRing` reads it out
of a sync frame's `done/total`, and the pane draws the bar with
`String.prototype.repeat` on `Math.round(pct / 5)` filled cells and on
`20 - filled` empty ones. `repeat` throws `RangeError` on a negative count,
so a value outside 0..100 took the whole poll render down with it -- and a
render that throws mid-poll leaves the pane frozen on its last paint while
the job runs on. The one producer keeps `done <= total` today; the pane does
not get to depend on that, so it clamps.

`dashboard_js_actions.JS` is the whole implementation, and this file runs
those exact bytes. The `node` marker is not cosmetic: `pyproject.toml`
carries ``addopts = -m "not node"``, so `hive selftest` -- which shells pytest
from inside the running dashboard's own POST handler -- never forks a JS
runtime out of a web request. Run it deliberately:

    pytest -m node tests/test_dashboard_jobbar_node.py
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mindhive import dashboard_js_actions

pytestmark = pytest.mark.node

HARNESS = Path(__file__).parent / "node" / "jobbar.test.mjs"
NODE_TIMEOUT_S = 60

FULL, EMPTY = "█", "·"


def _running(pct, **more):
    job = {"state": "running", "label": "Embedding", "seconds": 3,
           "progress_pct": pct}
    job.update(more)
    return {"job": job, "output": "line one\nline two", "dropped": 0}


# label -> the case. Each one names the wrong implementation it kills.
CASES = {
    # THE two that used to throw: `repeat(20 - 50)` and `repeat(-1)`.
    "past_a_hundred": _running(250),
    "below_zero": _running(-5),
    # The edges of the range the cells can draw, exactly.
    "exactly_a_hundred": _running(100),
    "exactly_zero": _running(0),
    # A fraction in the middle: `Math.round(8.5)` is 9 in JavaScript, so the
    # bar is 9 filled and 11 empty, and the label keeps the fraction.
    "in_the_middle": _running(42.5),
    # Neither an infinity nor NaN is a number the pane can draw: no bar,
    # rather than an `Infinity%` or a `NaN%` one.
    "infinite": _running("Infinity"),
    "negative_infinite": _running("-Infinity"),
    "not_a_number": _running("NaN"),
    "a_string": _running("75"),
    # A finished job has no live frame, so no bar, whatever the number says.
    "finished_past_a_hundred": {
        "job": {"state": "finished", "outcome": "ok", "summary": "done",
                "seconds": 9, "progress_pct": 250},
        "output": "", "dropped": 0},
}


def _slice(js: str) -> str:
    """The constant and the function, out of the fragment, verbatim. The
    fragment cannot be evaluated whole -- its tail starts a poll timer and a
    boot IIFE that touch the DOM -- and a retyped copy would test the copy."""
    const = next(line for line in js.splitlines()
                 if line.strip().startswith("const JOB_TAIL_LINES"))
    start = js.index("function jobText(")
    end = js.index("\n  }\n", start) + len("\n  }\n")
    return const + "\n" + js[start:end]


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    node = shutil.which("node")
    if not node:                                        # pragma: no cover
        pytest.skip("node is not installed")
    d = tmp_path_factory.mktemp("jobbar")
    src = d / "jobbar.js"
    src.write_text(_slice(dashboard_js_actions.JS), encoding="utf-8")
    cases = d / "cases.json"
    cases.write_text(json.dumps([dict(label=k, **v) for k, v in CASES.items()]),
                     encoding="utf-8")
    proc = subprocess.run([node, str(HARNESS), str(src), str(cases)],
                          capture_output=True, text=True,
                          # Explicit, not inherited: node writes UTF-8 and the
                          # Windows host decodes subprocess text as cp1252 by
                          # default, which turns the bar's cells into mojibake.
                          encoding="utf-8", timeout=NODE_TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _bar(text: str) -> str:
    """The `[cells] pct%` line, or '' when the text carries no bar."""
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("%"):
            return line
    return ""


@pytest.mark.parametrize("label", list(CASES))
def test_no_case_throws(rendered, label):
    assert not rendered[label].startswith("THREW:"), rendered[label]


def test_a_frame_past_a_hundred_draws_a_full_bar(rendered):
    assert _bar(rendered["past_a_hundred"]) == f"[{FULL * 20}] 100%"


def test_a_frame_below_zero_draws_an_empty_bar(rendered):
    assert _bar(rendered["below_zero"]) == f"[{EMPTY * 20}] 0%"


def test_the_edges_of_the_range_are_drawn_exactly(rendered):
    assert _bar(rendered["exactly_a_hundred"]) == f"[{FULL * 20}] 100%"
    assert _bar(rendered["exactly_zero"]) == f"[{EMPTY * 20}] 0%"


def test_a_fraction_keeps_its_label_and_rounds_its_cells(rendered):
    assert _bar(rendered["in_the_middle"]) == f"[{FULL * 9}{EMPTY * 11}] 42.5%"


def test_a_value_that_is_not_a_finite_number_gets_no_bar(rendered):
    for label in ("infinite", "negative_infinite", "not_a_number", "a_string"):
        assert _bar(rendered[label]) == "", (label, rendered[label])
        assert "%" not in rendered[label], (label, rendered[label])


def test_a_finished_job_gets_no_bar_whatever_the_number_says(rendered):
    assert _bar(rendered["finished_past_a_hundred"]) == ""
    assert "%" not in rendered["finished_past_a_hundred"]


def test_the_page_carries_this_exact_function_once():
    """The seam: a helper that is tested but not spliced is a test of nothing."""
    from mindhive import taskview
    from mindhive.dashboard_page import render_page
    page = render_page("test-token", tasks_css=taskview.CARDS_CSS,
                       tasks_js=taskview.CARDS_JS)
    assert page.count("function jobText(") == 1
    assert _slice(dashboard_js_actions.JS).split("\n", 1)[1] in page
