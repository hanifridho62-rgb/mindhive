# tests/test_paint_guard_node.py
"""Tier-1 node test of the paint guard — the decision, executed.

`tests/test_dashboard.py` already asserts the guard's TEXT is in the page. This
file asserts what the text is for, by running the emitted page under node and
driving the race the user reported: a poll of the section they were on is still
in flight, they click another section, and the older response lands last. The
plan classifies this codepath as a SILENT failure mode — "the idiom applied to
some renders but not others" — and a source-string assertion cannot see the two
things that decide whether it is actually fixed: whether the comparison is
re-read AFTER the await, and whether a discarded render really writes nothing.

WHY THE `node` MARKER IS NOT COSMETIC. `hive selftest` shells
``[sys.executable, -m, pytest, -q, tests/]`` from inside the dashboard's own
POST handler (`verify.run_and_record`, reached from `/api/selftest`). Without a
marker that deselects this file, pressing Self-test in the dashboard would spawn
node from inside the running web server — a web request forking a JS runtime.
`pyproject.toml` therefore carries ``addopts = -m "not node"``, so the default
run (and every caller that inherits it, `hive selftest` included) skips this
file. Run it deliberately:

    pytest -m node tests/test_paint_guard_node.py

The generated artifacts are the point of the seam: the page and the guard are
written out from the real module, never retyped here. A fixture that retyped the
comparison would keep passing after the page's own copy changed, which is the
vacuous shape this repo's ledger already has scars from.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import taskview
from mindhive.dashboard_page import PAINT_GUARD_JS, render_page

pytestmark = pytest.mark.node

HARNESS = Path(__file__).parent / "node" / "paint_guard.test.mjs"

# Every scenario gets its own vm context — ~40 compiles of a 118 KB page — and
# the whole run measures 0.13 s. The ceiling is three orders of magnitude above
# that on purpose: it exists so a harness that hangs (an unresolved promise, a
# scenario waiting on a fetch nobody answers) fails this test instead of the
# session, not to police performance.
NODE_TIMEOUT_S = 180


def _page_script() -> str:
    """The <script> body of the page the server actually serves.

    Split here rather than in JS so "the page has exactly one script block" is
    checked by an assertion instead of by a regex that would quietly pick the
    wrong one if that ever stopped being true."""
    page = render_page("test-token", tasks_css=taskview.CARDS_CSS,
                       tasks_js=taskview.CARDS_JS)
    assert page.count("<script>") == 1 and page.count("</script>") == 1, (
        "the page grew a second <script> block — this splitter would silently "
        "test only part of it")
    return page.split("<script>", 1)[1].split("</script>", 1)[0]


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    """Run the harness once; every test below reads one case out of it."""
    node = shutil.which("node")
    if not node:                                    # pragma: no cover
        pytest.skip("node is not on PATH — these cases were REQUESTED (`-m node`) "
                    "and did not run; nothing here passed")
    work = tmp_path_factory.mktemp("paint-guard")
    page_js = work / "page.js"
    guard_js = work / "guard.js"
    page_js.write_text(_page_script(), encoding="utf-8")
    guard_js.write_text(PAINT_GUARD_JS, encoding="utf-8")

    proc = subprocess.run(
        [node, str(HARNESS), str(page_js), str(guard_js)],
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
    assert not data.get("harness_error"), data.get("harness_error")

    # The harness's own verdict is the verdict. An earlier version of this
    # fixture read the report and threw the exit code away, on the reasoning
    # that "exit 1 means cases failed and the report is still the evidence" —
    # which is true of the report and false of the run. The tests below name
    # the cases they care about, so any case NOT named by one of them failed
    # into silence: `pytest -m node` reported 40 passed while this same harness
    # exited 1 on `paints_when_the_user_stayed:renderTaskCards`. A test suite
    # that cannot go red is not a test suite, and this one exists precisely to
    # stop a vacuous fixture reaching the dashboard.
    failed = [c for c in data["cases"] if not c.get("ok")]
    assert not failed, (
        "the node harness reported %d failing case(s) (exit %s):\n%s"
        % (len(failed), proc.returncode,
           "\n".join("  %s :: %s" % (c["name"], c.get("detail", "")) for c in failed)))
    assert proc.returncode == 0, (
        f"the harness exited {proc.returncode} with no failing case — the report "
        f"and the exit code disagree, which means the harness itself is broken")

    # Cases by name, plus the harness's own discovery under a `_`-prefixed key
    # so it can never collide with a case name.
    return {c["name"]: c for c in data["cases"]} | {"_renderers": data["renderers"]}


def test_the_page_ships_the_bytes_the_guard_test_executes():
    """The seam, checked from this side too.

    The tier-1 cases run PAINT_GUARD_JS on its own. That is only meaningful
    while the page's inlined copy IS PAINT_GUARD_JS — if the two ever drift, a
    perfectly green unit test would be exercising a function the dashboard does
    not ship, which is the classic way a suite goes quietly vacuous."""
    assert PAINT_GUARD_JS in _page_script(), (
        "the page no longer inlines PAINT_GUARD_JS verbatim — the guard the "
        "browser runs and the guard this file unit-tests have diverged")


def _case(report, name):
    if name not in report:
        # Only the same family is listed. Dumping all ~40 case names buries the
        # one fact that matters — which variant of this case the harness did
        # run — under a wall of names that are all fine.
        family = name.split(":")[0]
        siblings = sorted(k for k in report if k.startswith(family))
        pytest.fail(f"the harness ran no case named {name!r}; from the same "
                    f"family it ran {siblings or 'nothing'}")
    c = report[name]
    assert c["ok"], f"{name}: {c['detail']}"


# Renderers whose paint the page CAN guard. renderSessions / renderTaskCards are
# the documented gap (the paint happens inside taskview.py, past an await this
# module does not own) and are checked by the subset test instead, which stays
# green when that gap is closed.
GUARDED = ["renderOverview", "renderMemory", "renderImprovements", "renderOversee",
           "renderLog", "renderSops", "renderState", "renderPlugins"]


def test_the_harness_saw_the_renderers_the_page_declares(report):
    """If discovery breaks, every parametrized test below loops over nothing."""
    _case(report, "the_page_still_declares_renderers")
    missing = [r for r in GUARDED if r not in report["_renderers"]]
    assert not missing, (
        f"renderers this file expects are no longer in the page: {missing} — "
        f"found {report['_renderers']}")


def test_the_documented_gap_still_names_real_renderers(report):
    _case(report, "the_documented_gap_still_names_real_renderers")


@pytest.mark.parametrize("case", [
    "guard_paints_for_the_render_the_user_is_waiting_for",
    "guard_discards_a_render_the_user_left",
    "guard_discards_when_no_section_is_live",
    "guard_compares_identity_not_equality",
])
def test_the_guard_function_itself(report, case):
    """PAINT_GUARD_JS executed on its own — the decision with no page around it."""
    _case(report, case)


@pytest.mark.parametrize("renderer", GUARDED)
@pytest.mark.parametrize("via", ["run", "poll"])
def test_a_late_render_writes_nothing_once_the_user_has_moved_on(report, renderer, via):
    """The reported bug, both ways it can be started.

    `poll` is refreshLive() — the path that actually bit, and the one a
    run()-scoped generation counter could never have seen. `run` is the manual
    click, where a stale throw would reach fail() and paint an error over the
    section the user just picked."""
    _case(report, f"discards_late_paint:{renderer}:{via}")


@pytest.mark.parametrize("renderer", GUARDED)
def test_the_same_race_still_paints_when_the_user_stayed_put(report, renderer):
    """Anti-vacuity: a renderer that returned unconditionally would pass the
    discard test above and fail this one."""
    _case(report, f"paints_when_the_user_stayed:{renderer}")


@pytest.mark.parametrize("renderer", GUARDED)
def test_removing_the_guard_reintroduces_the_clobber(report, renderer):
    """The canary, run every time rather than once at authoring time: the same
    scenario against a copy of the page with THIS renderer's guard stripped must
    clobber. A scenario that cannot be made to fail is measuring nothing."""
    _case(report, f"removing_the_guard_reintroduces_the_clobber:{renderer}")


def test_no_renderer_outside_the_documented_gap_can_clobber(report):
    """The answer to the SILENT failure mode: the harness enumerates the page's
    own renderers, so one added later without the guard fails here without
    anybody remembering to add it to a list."""
    _case(report, "no_renderer_outside_the_documented_gap_can_clobber")
