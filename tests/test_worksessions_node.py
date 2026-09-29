# tests/test_worksessions_node.py
"""Tier-1 node test of the Oversee work-session renderers (blueprint P1b, BAB 5.5).

`tests/test_dashboard_get_v1.py` asserts the functions are IN the served page.
This file asserts what they draw, by extracting `wsUsage`, `wsPills` and
`wsInitiatives` -- with the page's own `esc`, `escAttr`, `ovRow` and `ovGroup`
-- out of the script the server actually serves, and running them under node:
the usage bar's arithmetic and clamping, the HIVE MIND lookup, the online-only
blip, the eight-row cap, and the escaping of every ledger-derived string (a
phase, a tag, a reason, a session id and a project name are all text some
agent wrote).

WHY THE `node` MARKER IS NOT COSMETIC: see `tests/test_paint_guard_node.py`.
`pyproject.toml` deselects it by default so `hive selftest`, which runs pytest
from inside the dashboard's POST handler, never forks a JS runtime. Run it
deliberately:

    pytest -m node tests/test_worksessions_node.py
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mindhive import taskview
from mindhive.dashboard_page import render_page

pytestmark = pytest.mark.node

HARNESS = Path(__file__).parent / "node" / "worksessions.test.mjs"
NODE_TIMEOUT_S = 60
FUNCTIONS = ("esc", "escAttr", "ovRow", "ovGroup", "wsUsage", "wsPills", "wsInitiatives")
CONSTS = ("OV_NEW_PILL", "WS_INIT_SHOW")


def _page_script() -> str:
    page = render_page("test-token", tasks_css=taskview.CARDS_CSS,
                       tasks_js=taskview.CARDS_JS)
    assert page.count("<script>") == 1
    return page.split("<script>", 1)[1].split("</script>", 1)[0]


def _function(script: str, name: str) -> str:
    """`function <name>(...){...}` by brace matching, from the one definition."""
    starts = [m.start() for m in re.finditer(rf"\bfunction {name}\(", script)]
    assert len(starts) == 1, f"{name}: {len(starts)} definitions in the page"
    i = script.index("{", starts[0])
    depth = 0
    for j in range(i, len(script)):
        if script[j] == "{":
            depth += 1
        elif script[j] == "}":
            depth -= 1
            if depth == 0:
                return script[starts[0]:j + 1]
    raise AssertionError(f"{name}: unbalanced braces")


def _const(script: str, name: str) -> str:
    found = re.findall(rf"\bconst {name} = .*?;\n", script, re.S)
    assert len(found) == 1, f"{name}: {len(found)} definitions in the page"
    return found[0]


def test_the_extractor_refuses_a_missing_or_doubled_definition():
    with pytest.raises(AssertionError):
        _function("function a(){}\nfunction a(){}", "a")
    with pytest.raises(AssertionError):
        _function("function b(){}", "a")
    assert _function("x; function a(q){ return {k: 1}; } y", "a") == \
        "function a(q){ return {k: 1}; }"


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    node = shutil.which("node")
    if not node:                                    # pragma: no cover
        pytest.skip("node is not on PATH — these cases were REQUESTED (`-m node`) "
                    "and did not run; nothing here passed")
    script = _page_script()
    body = "\n".join([_const(script, c) for c in CONSTS]
                     + [_function(script, f) for f in FUNCTIONS])
    work = tmp_path_factory.mktemp("worksessions-js")
    src = work / "ws.js"
    src.write_text(body, encoding="utf-8")
    proc = subprocess.run([node, str(HARNESS), str(src)], capture_output=True,
                          text=True, encoding="utf-8", timeout=NODE_TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_nothing_is_drawn_without_data(report):
    assert report["none"] == {"usage": "", "pills": "", "inits": ""}


def test_the_usage_bar_draws_occupancy_and_the_safe_tick(report):
    html = report["usage"]
    assert 'class="usagebar"' in html
    assert 'class="usagefill" style="width:25.0%"' in html
    assert 'class="usagesafe" style="left:80.0%"' in html
    assert "25.0% of 200,000 tokens" in html
    assert "50,000 of 200,000 tokens" in html and "being truncated" in html
    assert report["defaultProject"] == html          # no namespace: the page's PROJECT


def test_the_usage_bar_clamps_and_refuses_an_unknown_budget(report):
    assert 'style="width:100.0%"' in report["overflow"]
    assert "100.0% of 200,000 tokens" in report["overflow"]
    assert "being truncated" not in report["overflow"]
    assert report["zeroBudget"] == ""
    assert 'style="width:0.0%"' in report["bare"] and 'style="left:0.0%"' in report["bare"]
    assert "NaN" not in report["bare"]


def test_an_unmeasured_digest_draws_no_bar(report):
    # "we could not measure" must never read as "0% used" -- and a row that
    # does not say it was measured is not taken as measured either.
    assert report["unmeasured"] == ""
    assert report["legacy"] == ""


def test_hive_mind_reads_each_cards_own_namespace(report):
    assert report["hiveMindOwn"] == report["overflow"]
    assert report["hiveMindNone"] == ""


def test_pills_escape_ledger_text_and_blip_only_online(report):
    on = report["pillsOnline"]
    assert "<img" not in on and "<script>" not in on
    assert "&lt;img src=x onerror=alert(1)&gt;" in on
    assert 'title="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in on
    assert on.count('class="pill wsinit"') == 2 and 'title="plain"' in on
    assert '<span class="blip">●</span>' in on
    assert 'class="usagebar"' in on                  # project 'test' has usage
    off = report["pillsOffline"]
    assert 'class="pill wsphase"' in off and "kanban" in off
    assert "blip" not in off and "usagebar" not in off


def test_the_initiatives_group_caps_at_eight_and_escapes_every_field(report):
    hive = report["initsHive"]
    assert "Autonomous Initiatives" in hive and "newest 8 of 10" in hive
    assert hive.count("<li>") == 8
    assert "<b>" not in hive and "reason &lt;b&gt;0&lt;/b&gt;" in hive
    assert "s&lt;0&gt;" in hive and "p&amp;0 · " in hive
    assert "Ref: inc:1" in hive and "Ref: ·" not in hive
    assert "T(2026-09-17T00:00:00Z)" in hive
    assert "reason &lt;b&gt;8&lt;/b&gt;" not in hive  # the ninth is past the cap
    assert "step step-&lt;3&gt; · " in hive and hive.count("step ") == 1
    one = report["initsOne"]
    assert "p&amp;0" not in one and one.count("<li>") == 8
