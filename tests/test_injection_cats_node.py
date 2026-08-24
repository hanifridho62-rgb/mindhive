# tests/test_injection_cats_node.py
"""Tier-1 node test of the injection row: the three verdicts, executed.

`tests/test_injection_cats.py` asserts the three verdict classes exist in the
CSS and that their rule bodies differ. That is a check on the STYLESHEET. It
cannot see whether `paintInjections` ever reaches those classes, whether a click
replays the turn the operator clicked, or whether the replayed block arrives as
text -- and the founding ask was to see "apa saja yang berhasil di injeksi dan
yang tidak", which is a claim about what renders, not about what is spelled.

WHY THE `node` MARKER IS NOT COSMETIC. `pyproject.toml` carries
``addopts = -m "not node"`` so that `hive selftest` -- which shells pytest from
inside the running dashboard's own POST handler -- never forks a JS runtime out
of a web request. Run it deliberately:

    pytest -m node tests/test_injection_cats_node.py

The script under test is `cat_board.script()` itself, written out and handed to
node on argv. A harness that retyped the painter would keep passing after the
page's own copy changed, which is the vacuous shape this repo already has scars
from.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mindhive import cat_board

pytestmark = pytest.mark.node

HARNESS = Path(__file__).parent / "node" / "injection_cats.test.mjs"
NODE_TIMEOUT_S = 60
DOT = " · "


# --- fixture builders: valid by default, one thing varied per case ----------

def _inj(name, verdict, chars=100):
    return {"name": name, "verdict": verdict, "chars": chars,
            "tokens": chars // 4, "ms": 1, "reason": f"{name} says {verdict}"}


def _turn(sig, verdicts, ts="2026-08-24T01:02:03Z", repeats=1):
    rows = [_inj(f"p{n}", v) for n, v in enumerate(verdicts)]
    return {"ts": ts, "signature": sig, "repeats": repeats,
            "total_chars": 100 * len(rows), "total_tokens": 25 * len(rows),
            "injectors": rows}


# One turn per verdict the row can render. `turnVerdict` is a precedence rule --
# partial beats verbatim beats silent -- so the partial turn deliberately also
# carries a verbatim pass: a naive "first verdict wins" would render it green.
V_TURN = _turn("sig-v", ["verbatim", "silent"])
P_TURN = _turn("sig-p", ["verbatim", "partial"])
S_TURN = _turn("sig-s", ["silent", "silent"])

BLOCK = "the whole composed turn, replayed"
XSS_BLOCK = "<script>alert('pwned')</script>"

CASES = [
    {"label": "three_verdicts",
     "paints": [{"recent": [V_TURN, P_TURN, S_TURN], "current": {}}]},
    {"label": "empty_ring",
     "paints": [{"recent": [], "current": {}}]},
    {"label": "click_the_current_turn",
     "paints": [{"recent": [V_TURN],
                 "current": {"signature": "sig-v", "block": BLOCK}}],
     "clicks": [0]},
    {"label": "click_an_older_turn",
     "paints": [{"recent": [V_TURN, P_TURN],
                 "current": {"signature": "sig-v", "block": BLOCK}}],
     "clicks": [1]},
    {"label": "click_the_same_cat_twice",
     "paints": [{"recent": [V_TURN],
                 "current": {"signature": "sig-v", "block": BLOCK}}],
     "clicks": [0, 0]},
    {"label": "click_two_different_cats",
     "paints": [{"recent": [V_TURN, P_TURN], "current": {}}],
     "clicks": [0, 1]},
    {"label": "repainted_with_the_same_data",
     "paints": [{"recent": [V_TURN, P_TURN, S_TURN], "current": {}},
                {"recent": [V_TURN, P_TURN, S_TURN], "current": {}}]},
    {"label": "a_repeated_turn",
     "paints": [{"recent": [_turn("sig-r", ["verbatim"], repeats=4)],
                 "current": {}}]},
    {"label": "a_block_that_contains_markup",
     "paints": [{"recent": [V_TURN],
                 "current": {"signature": "sig-v", "block": XSS_BLOCK}}],
     "clicks": [0]},
]


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    node = shutil.which("node")
    if not node:                                    # pragma: no cover
        pytest.skip("node is not on PATH -- these cases were REQUESTED "
                    "(`-m node`) and did not run; nothing here passed")
    work = tmp_path_factory.mktemp("injection-cats")
    src = work / "cat_board.js"
    src.write_text(cat_board.script(), encoding="utf-8")
    cases = work / "cases.json"
    cases.write_text(json.dumps({"cases": CASES}), encoding="utf-8")

    proc = subprocess.run(
        [node, str(HARNESS), str(src), str(cases)],
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
    assert not data.get("harness_error"), data["harness_error"]
    # Every case named below must have RUN. The blanket "no harness error" says
    # nothing about a case the loop silently stopped reaching.
    missing = [c["label"] for c in CASES if c["label"] not in data["cases"]]
    assert not missing, f"the harness ran no case named {missing}"
    return data["cases"]


def test_the_page_serves_the_painter_this_file_executes():
    """The seam. A painter that is tested but not spliced is a test of nothing;
    this repo has already shipped a page whose producer was missing."""
    from mindhive.dashboard_page import render_page
    page = render_page("test-token")
    assert cat_board.script() in page, (
        "the page no longer inlines cat_board.script() verbatim -- the painter "
        "the browser runs and the painter this file executes have diverged")


def test_the_three_verdicts_render_as_three_distinct_classes(report):
    """T5's verify line, executed. The CSS test proves three rules differ; this
    proves the painter actually reaches all three."""
    cats = report["three_verdicts"]["cats"]
    assert [c["className"] for c in cats] == [
        "mn-inj verbatim", "mn-inj partial", "mn-inj silent"]
    assert len({c["className"] for c in cats}) == 3


def test_partial_wins_over_verbatim_within_one_turn(report):
    """The discriminating half of the case above. The partial turn's FIRST pass
    is verbatim, so an implementation that took the first verdict -- or the last
    -- renders it green and still passes a "three classes exist" check."""
    partial = report["three_verdicts"]["cats"][1]
    assert "partial" in partial["className"]
    assert "verbatim" not in partial["className"]


def test_every_cat_carries_the_reduced_motion_hook(report):
    """`.mn-cat-sprite` is what the `prefers-reduced-motion` rule keys on. The
    default tier can only check that `catSVG` is mentioned; this checks the
    class reached the markup the row actually emits."""
    for cat in report["three_verdicts"]["cats"]:
        assert "mn-cat-sprite" in cat["sprite"], cat["className"]


def test_hovering_gives_the_title_that_was_asked_for(report):
    """"ketika di arahkan ke gambar kucing maka akan menampilkan title" -- the
    hover answer names the turn, its verdict, how many passes spoke, and how
    much they delivered."""
    title = report["three_verdicts"]["cats"][0]["title"]
    assert title == ("injection 2026-08-24T01:02:03Z" + DOT + "verbatim"
                     + DOT + "1 of 2 spoke" + DOT + "200 chars")


def test_a_silent_turn_is_drawn_and_says_so(report):
    """E2: absence cannot express failure. A turn where nothing spoke is still
    a cat -- greyed, and honest about the count."""
    silent = report["three_verdicts"]["cats"][2]
    assert "silent" in silent["className"]
    assert "0 of 2 spoke" in silent["title"]


def test_an_empty_ring_says_so_rather_than_rendering_nothing(report):
    case = report["empty_ring"]
    assert case["cats"] == []
    assert case["empty"] == "nothing injected yet"
    assert case["label"] == "injection"


def test_a_repeated_turn_shows_its_count_on_the_badge(report):
    assert report["a_repeated_turn"]["cats"][0]["badge"] == "100c x4"


def test_clicking_the_current_turn_replays_the_whole_block(report):
    """"ketika di klik akan menampilkan keseluruhan konteks yang di injeksi"."""
    panel = report["click_the_current_turn"]["panel"]
    assert panel["display"] == "block"
    assert panel["bodyText"] == BLOCK
    assert panel["heading"] == "injection 2026-08-24T01:02:03Z"
    assert ["total", "200 chars / 50 tokens"] in panel["rows"]
    assert panel["warning"] == ""


def test_clicking_an_older_turn_says_why_it_has_no_block(report):
    """The ring is the index; only the current turn keeps its text. Saying so
    is the difference between a turn that injected nothing and a turn whose
    text this sidecar no longer holds -- the distinction E2 is about."""
    panel = report["click_an_older_turn"]["panel"]
    assert panel["bodyText"] is None
    assert panel["warning"] == "whole turn text is kept for the current turn only"


def test_the_replayed_block_reaches_the_dom_as_text_not_markup(report):
    """The injected block is arbitrary prompt and SOP text. An innerHTML path
    would make every injected block a script vector in the operator's own
    dashboard."""
    panel = report["a_block_that_contains_markup"]["panel"]
    assert panel["bodyText"] == XSS_BLOCK
    assert panel["bodyHTML"] == ""
    assert panel["bodyClass"] == "mn-body"


def test_clicking_the_open_cat_again_closes_the_panel(report):
    case = report["click_the_same_cat_twice"]
    assert case["panel"]["display"] == "none"
    assert case["selected"] == 0


def test_exactly_one_cat_is_selected_when_a_second_is_clicked(report):
    """`mnOpen`'s sweep, executed. The faculty handler once forgot it and two
    cats read as selected at the same time."""
    case = report["click_two_different_cats"]
    assert case["selected"] == 1
    assert case["cats"][1]["selected"] is True
    assert case["cats"][0]["selected"] is False


def test_a_second_paint_of_the_same_data_does_not_duplicate_the_row(report):
    """The repaint guard. Without it a 2.5 s poll rebuilds the row on every
    tick, closing a panel someone is reading and restarting the bob animation
    mid-cycle; with a guard keyed wrongly it would instead never repaint."""
    assert len(report["repainted_with_the_same_data"]["cats"]) == 3
    assert report["repainted_with_the_same_data"]["sig"], (
        "the guard never recorded a signature, so it can never suppress a "
        "repaint at all")
