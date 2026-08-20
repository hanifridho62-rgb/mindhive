# tests/test_dashboard_clock_node.py
"""Tier-1 node test of the dashboard clock: UTC and WIB, from one artifact.

Every stamp the dashboard prints comes off an append-only ledger and is UTC.
Every reader of this dashboard is in Jakarta. The page used to print the UTC
string raw, which left the reader converting in their head -- and the one case
that conversion gets wrong is the one that matters, because 20:00Z and 03:00 WIB
are the same instant on DIFFERENT DAYS.

`dashboard_js_core.CLOCK_JS` is the whole implementation, and this file runs
those exact bytes. The `node` marker is not cosmetic: `pyproject.toml` carries
``addopts = -m "not node"``, so `hive selftest` -- which shells pytest from
inside the running dashboard's own POST handler -- never forks a JS runtime out
of a web request. Run it deliberately:

    pytest -m node tests/test_dashboard_clock_node.py
"""
import json
import shutil
import subprocess
import sys

import pytest

from mindhive import taskview
from mindhive.dashboard_js_core import CLOCK_JS
from mindhive.dashboard_page import render_page

pytestmark = pytest.mark.node

HARNESS = __import__("pathlib").Path(__file__).parent / "node" / "clock.test.mjs"
NODE_TIMEOUT_S = 60

# label -> (input, expected). The input is separate from the label because
# `None` and `""` are both cases and would collide as one JSON key.
CASES = {
    "same_day": ("2026-08-20T05:12:00Z", "2026-08-20 05:12Z · 12:12 WIB"),
    # THE discriminating pair. 16:59Z is 23:59 the same day in Jakarta and
    # 17:00Z is midnight the next -- an implementation that prints the WIB date
    # only when the hour wraps, or that leans on the runtime's local zone,
    # passes every other case here and fails exactly these two.
    "last_minute_of_the_day": ("2026-08-20T16:59:00Z",
                               "2026-08-20 16:59Z · 23:59 WIB"),
    "midnight_in_jakarta": ("2026-08-20T17:00:00Z",
                            "2026-08-20 17:00Z · 2026-08-21 00:00 WIB"),
    "rolls_into_tomorrow": ("2026-08-20T20:00:00Z",
                            "2026-08-20 20:00Z · 2026-08-21 03:00 WIB"),
    "end_of_month": ("2026-08-31T20:00:00Z",
                     "2026-08-31 20:00Z · 2026-09-01 03:00 WIB"),
    # A stamp the page cannot read is still evidence. Blanking it would hide a
    # malformed row; throwing would take out the render that found it.
    "unparseable_is_verbatim": ("not a date", "not a date"),
    "empty_stays_empty": ("", ""),
    "missing_stays_empty": (None, ""),
}

# `fmtHm` is the clock-FACE form: the same two zones, no date. It exists for
# the skill trail, whose marker sits inline beside other text and is five
# characters wide by design -- the date there is already established by the
# group the marker sits in.
HM_CASES = {
    "hm_same_day": ("2026-08-20T05:12:00Z", "05:12Z · 12:12 WIB"),
    # THE case that documents the trade. This instant is 2026-08-21 in
    # Jakarta and this form does not say so -- deliberately, because the
    # surrounding group carries the date. Asserted rather than left
    # implicit, so a later reader can tell an omission from an oversight.
    "hm_drops_the_day_roll": ("2026-08-20T17:00:00Z",
                              "17:00Z · 00:00 WIB"),
    "hm_late_evening": ("2026-08-20T20:00:00Z", "20:00Z · 03:00 WIB"),
    # Same totality rule as fmtTs: a row the page cannot read is still
    # evidence, so it comes back verbatim rather than blanked.
    "hm_unparseable_is_verbatim": ("not a date", "not a date"),
    "hm_empty_stays_empty": ("", ""),
    "hm_missing_stays_empty": (None, ""),
}

# label -> which clock form runs it. One flat table so the harness gets a
# single list and the two verdict tests stay independent.
ALL_CASES = {**{k: (v, "fmtTs") for k, v in CASES.items()},
             **{k: (v, "fmtHm") for k, v in HM_CASES.items()}}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    node = shutil.which("node")
    if not node:                                        # pragma: no cover
        pytest.skip("node is not installed")
    d = tmp_path_factory.mktemp("clock")
    src = d / "clock.js"
    src.write_text(CLOCK_JS, encoding="utf-8")
    cases = d / "cases.json"
    cases.write_text(json.dumps(
        [{"label": k, "input": v[0][0], "fn": v[1]}
         for k, v in ALL_CASES.items()]),
        encoding="utf-8")
    proc = subprocess.run([node, str(HARNESS), str(src), str(cases)],
                          capture_output=True, text=True,
                          # Explicit, not inherited: node writes UTF-8 and this
                          # host decodes subprocess text as cp1252 by default,
                          # which turned the separator into mojibake and failed
                          # five cases for a reason that had nothing to do with
                          # the clock.
                          encoding="utf-8", timeout=NODE_TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("label", list(CASES))
def test_every_stamp_reads_in_both_zones(rendered, label):
    assert rendered[label] == CASES[label][1]


@pytest.mark.parametrize("label", list(HM_CASES))
def test_the_clock_face_reads_in_both_zones_too(rendered, label):
    """The trail used to print `k.ts.slice(11, 16)` -- five characters of
    a UTC string, with nothing saying so. Same two zones as the full form,
    same totality, one fewer field."""
    assert rendered[label] == HM_CASES[label][1]


def test_the_page_serves_this_exact_helper():
    """The seam. A helper that is tested but not spliced is a test of nothing,
    and this repo has already shipped a page whose producer was missing."""
    page = render_page("test-token", tasks_css=taskview.CARDS_CSS,
                       tasks_js=taskview.CARDS_JS)
    assert page.count("function fmtTs(") == 1, (
        "the clock helper is not on the page exactly once")
    assert page.count("function fmtHm(") == 1, (
        "the clock FACE helper is not on the page exactly once")
    assert "/*__CLOCK__*/" not in page, "the splice placeholder survived"
    assert CLOCK_JS.strip() in page
