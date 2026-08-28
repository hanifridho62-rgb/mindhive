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


# Every renderer the page declares — there is no longer an excused set.
# renderSessions / renderTaskCards used to be "the documented gap", on the
# grounds that their paint happens inside taskview.py past an await this module
# does not own. taskview.py had already shipped that guard in the same commit,
# so the excuse outlived the gap: the one guard living outside dashboard_page.py
# was the only guard in the change with no discard case, no anti-vacuity case
# and no mutant, and deleting it would have kept every test green.
GUARDED = ["renderOverview", "renderMemory", "renderImprovements", "renderOversee",
           "renderLog", "renderSops", "renderState", "renderPlugins",
           "renderSessions", "renderTaskCards"]


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


@pytest.mark.parametrize("case", [
    "the_run_generation_is_read_through_one_expression",
    "a_superseded_runs_timers_paint_nothing",
    "an_older_load_of_the_same_section_cannot_paint_its_error",
    "removing_the_run_generation_lets_the_stale_timers_paint",
    "removing_the_run_generation_lets_the_stale_error_paint",
])
def test_the_run_scoped_generation(report, case):
    """The half `shouldPaint` cannot answer for.

    Two loads of the SAME section both compare equal to liveFn, so the paint
    guard passes for both — and everything run() does around the render (the
    skeleton timer, the "still loading" status, the closing announce, fail()'s
    error panel) is outside the section question entirely. RUN_GEN covers that,
    and neither of its two paint sites is reachable through the race scenario:
    one needs a timer to elapse, the other needs two loads of one section.

    Named here, not left to the fixture's blanket "no case failed": that assert
    catches a case that RAN and failed, and says nothing about one the harness
    silently stopped running."""
    _case(report, case)


@pytest.mark.parametrize("case", [
    "a_poll_that_never_returns_stops_the_chip_claiming_live",
    "a_stuck_poll_never_issues_a_second_request",
    "removing_the_stuck_check_lets_the_chip_go_on_lying",
    "the_chip_stays_green_without_the_stuck_check",
])
def test_a_wedged_poll_cannot_keep_claiming_live(report, case):
    """`liveInFlight` is released in a `finally`, which covers every way a fetch
    can END. A fetch that never ends is not one of them, and `fetch` has no
    default timeout — so a wedged server thread (the failure the server-side
    locks in this change exist for) held the latch forever while the chip went
    on asserting freshness. The second case is the other half: the answer is to
    keep skipping and tell the truth, NOT to retry, because a replacement
    request every 2.5 s is exactly the pile-up the latch was added to stop."""
    _case(report, case)


@pytest.mark.parametrize("case", [
    "the_cats_poll_issues_one_request_and_then_skips",
    "the_cats_poll_resumes_once_its_request_lands",
    "both_halves_of_the_cats_latch_are_removable",
    "removing_the_cats_skip_lets_the_ticks_queue",
    "removing_the_cats_release_wedges_the_poller",
])
def test_the_cats_poller_skips_rather_than_queues(report, case):
    """The second poller, which had no executed cover at all.

    `pollCats` carries the same latch as `refreshLive` and was asserted only by
    substring-matching the served page, including a whitespace-exact
    `finally { catsInFlight = false; }`. That check cannot tell a latch that
    works from one that is merely spelled correctly — and this is the poller
    with the cheaper endpoint, so it is the one likeliest to be waved through."""
    _case(report, case)


@pytest.mark.parametrize("case", [
    "a_render_started_for_another_project_paints_nothing",
    "the_scope_check_is_one_removable_expression",
    "removing_the_scope_check_paints_the_other_projects_data",
])
def test_a_project_switch_discards_the_previous_namespaces_answer(report, case):
    """SCOPE, which section identity cannot express.

    `shouldPaint` asks which SECTION is live. A project switch changes neither
    the section nor the renderer — `onProjChange` re-enters Overview — so a user
    already on Overview has `liveFn === renderOverview` on both sides and an
    answer fetched for the previous namespace compares equal. It painted another
    project's numbers under this project's label with nothing on screen looking
    wrong. The case asserts liveFn was identical across the switch, so a pass
    cannot be credited to the section guard by accident."""
    _case(report, case)


@pytest.mark.parametrize("case", [
    "an_answer_asked_before_the_picker_settled_still_paints",
    "the_empty_scope_allowance_is_one_removable_expression",
    "removing_the_empty_scope_allowance_discards_the_first_answer",
])
def test_the_first_answer_of_a_session_is_not_read_as_a_project_switch(report, case):
    """The boot half of the same guard, which the switch cases cannot reach.

    A section clicked before the picker settles asks with NO project -- `withProj`
    omits the parameter entirely -- so the server answers from `default_project()`,
    and `loadProjects` then sets PROJECT to that very project. That transition is
    an acquisition, not a switch, and treating it as one threw away the reader's
    own first click. The removability case is what keeps the pass honest: with the
    allowance taken back out, the same answer must be discarded."""
    _case(report, case)


@pytest.mark.parametrize("case", [
    "a_remembered_project_that_still_exists_is_kept",
    "a_remembered_project_the_server_no_longer_offers_is_dropped",
    "the_offered_check_in_loadProjects_is_removable",
    "without_the_offered_check_the_picker_and_the_board_disagree",
])
def test_the_picker_and_the_board_name_the_same_project(report, case):
    """A remembered project the server no longer lists used to win outright.

    `loadProjects` restored the reader's last pick from localStorage, and a
    deleted namespace is still a string. A `<select>` whose value matches no
    option falls back to its FIRST -- here `*`, the unified view -- so the picker
    read HIVE MIND, the widest scope there is, while every request carried the
    vanished project and the board stayed empty. Nothing said so, and nothing
    rewrote the remembered value, so it came back on every reload.

    The first case is what stops the fix from being "ignore what was remembered":
    a project the server DOES offer has to survive."""
    _case(report, case)


@pytest.mark.parametrize("case", [
    "the_refusal_check_is_removable",
    "the_stamp_check_is_removable",
    "the_paused_check_is_removable",
    "a_refused_action_is_not_announced_as_ready",
    "the_refusal_reason_still_reaches_the_panel",
    "a_discarded_live_tick_does_not_stamp_the_page_as_updated",
    "pausing_replaces_the_stale_message_it_can_no_longer_update",
    "without_the_refusal_check_the_status_says_ready",
    "without_the_liveFn_recheck_a_discarded_tick_stamps",
    "without_the_paused_branch_the_stale_message_survives_the_pause",
])
def test_the_page_does_not_report_what_did_not_happen(report, case):
    """Three ways the page said something that was not so, none of them a paint.

    A server-side refusal (`{ok:false, error:…}` with HTTP 200 — how the six
    one-at-a-time locks decline work) was rendered and then reported as a
    completed operation. A live tick whose render was DISCARDED still moved the
    "updated HH:MM:SS" stamp, which is the one widget a user checks to decide
    whether what they are reading is current. And a chip that went stale and was
    then paused kept the "not refreshing" message, which nothing could correct
    because markLiveHealth is unreachable while paused.

    Each is checked against the real page and against a copy with that one
    check removed, which must fail."""
    _case(report, case)


@pytest.mark.parametrize("case", [
    "a_first_paint_writes_the_panel",
    "an_identical_repaint_writes_nothing",
    "a_changed_payload_still_repaints",
    "a_matching_payload_still_puts_the_pane_back_on_screen",
    "a_re_arrival_repaints_even_when_the_payload_is_unchanged",
    "the_repaint_guard_is_one_removable_expression",
    "removing_the_repaint_guard_writes_the_same_markup_again",
])
def test_an_unchanged_panel_is_not_repainted_every_two_and_a_half_seconds(report, case):
    """The 2.5 s poll re-runs the whole renderer whether or not anything moved.

    Eight of the nine live sections end that render in showText/showHTML with a
    COMPLETE payload, so an idle board re-parsed its entire panel every tick:
    the reader's text selection collapsed, focus on a control inside the panel
    fell back to <body>, :hover reset, and every entrance animation replayed
    from 0%. Overview already avoided it with a data-shape signature and the cat
    strips with dataset.sig; the list sections now avoid it by stamping what was
    painted and skipping a byte-identical write.

    Executed rather than asserted from the source, because the branch has three
    distinct ways to be wrong and only one of them is visible in the text: not
    skipping at all, skipping a payload that CHANGED, and mistaking "already
    painted" for "already visible" — the last of which is the two-click blank
    board arriving through a new door. The final case removes the comparison and
    requires the identical repaint to come back."""
    _case(report, case)
