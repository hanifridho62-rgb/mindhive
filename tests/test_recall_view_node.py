# tests/test_recall_view_node.py
"""Tier-1 node test of the Recall view's failure path.

`tests/test_recall.py` asserts the guard's text is in `runRecall`. This file
runs `runRecall` -- extracted from the script the server actually serves --
under node, with `apiGet` answering each shape `/api/recall` can answer, and
checks what reaches the panel and the announcement: a failed search ends as a
failed run with "Recall failed" painted once, an empty one says "No matches",
hits are listed. The harness also runs the guard painting by hand (the shape
it had, which announced "ready" over the error) and no guard at all, and
requires the failure case to go wrong in both.

Deselected by default, like every `node` test (see
`tests/test_paint_guard_node.py`). Run it deliberately:

    pytest -m node tests/test_recall_view_node.py
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

HARNESS = Path(__file__).parent / "node" / "recall_view.test.mjs"
NODE_TIMEOUT_S = 60
CASES = ("a_failed_search_says_it_failed",
         "a_search_with_no_answer_says_it_failed",
         "an_empty_search_says_no_matches",
         "hits_are_listed",
         "a_partial_failure_keeps_the_hits_and_names_the_namespace",
         "a_failure_beside_no_matches_says_both",
         "the_guard_is_mutable",
         "painting_the_failure_by_hand_announces_ready",
         "without_the_guard_the_error_is_lost")


def _run_recall() -> str:
    """`function runRecall(){...}` out of the served page, by brace matching."""
    page = render_page("test-token", tasks_css=taskview.CARDS_CSS,
                       tasks_js=taskview.CARDS_JS)
    assert page.count("<script>") == 1
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    starts = [m.start() for m in re.finditer(r"\bfunction runRecall\(", script)]
    assert len(starts) == 1, f"runRecall: {len(starts)} definitions in the page"
    depth = 0
    for j in range(script.index("{", starts[0]), len(script)):
        depth += {"{": 1, "}": -1}.get(script[j], 0)
        if depth == 0:
            return script[starts[0]:j + 1]
    raise AssertionError("runRecall: unbalanced braces")


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    node = shutil.which("node")
    if not node:                                    # pragma: no cover
        pytest.skip("node is not on PATH — these cases were REQUESTED (`-m node`) "
                    "and did not run; nothing here passed")
    src = tmp_path_factory.mktemp("recall-view") / "runRecall.js"
    src.write_text(_run_recall(), encoding="utf-8")
    proc = subprocess.run([node, str(HARNESS), str(src)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=NODE_TIMEOUT_S,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        data = json.loads(proc.stdout)
    except ValueError:                              # pragma: no cover
        pytest.fail(f"node produced no report (exit {proc.returncode})\n"
                    f"{proc.stdout[:2000]}\n{proc.stderr[:2000]}")
    assert not data.get("harness_error"), data.get("harness_error")
    failed = [c for c in data["cases"] if not c["ok"]]
    assert not failed, "\n".join(f"{c['name']} :: {c['detail']}" for c in failed)
    assert proc.returncode == 0, proc.stderr
    return {c["name"]: c for c in data["cases"]}


@pytest.mark.parametrize("case", CASES)
def test_the_recall_view(report, case):
    assert case in report, f"the harness ran no case named {case!r}"
    assert report[case]["ok"], report[case]["detail"]
