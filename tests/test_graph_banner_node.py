# tests/test_graph_banner_node.py
"""Tier-1 node test of the Graph page's semantic-failure banner.

`tests/test_graphview.py` asserts the banner's text is in the renderer. This
file runs the banner block -- extracted from `graphview._RENDERER_JS`, the
script every rendered graph page carries -- under node against a recording
`document`, and checks what reaches the stage: one `#semerr` alert holding the
message as text when the build failed, nothing when it did not. The harness
also runs a copy with the `appendChild` removed and requires the failure case
to go wrong there.

Deselected by default, like every `node` test (see
`tests/test_paint_guard_node.py`). Run it deliberately:

    pytest -m node tests/test_graph_banner_node.py
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mindhive import graphview

pytestmark = pytest.mark.node

HARNESS = Path(__file__).parent / "node" / "graph_banner.test.mjs"
NODE_TIMEOUT_S = 60
OPENER = "if (data.semantic_error) {"
CASES = ("a_failed_build_shows_its_error_as_text",
         "a_build_that_did_not_fail_shows_nothing",
         "the_banners_append_is_removable",
         "without_the_append_nothing_is_shown")


def _banner_block() -> str:
    """The `if (data.semantic_error) {...}` block, by brace matching."""
    js = graphview._RENDERER_JS
    assert js.count(OPENER) == 1, f"{js.count(OPENER)} banner blocks in the renderer"
    start = js.index(OPENER)
    depth = 0
    for j in range(js.index("{", start), len(js)):
        depth += {"{": 1, "}": -1}.get(js[j], 0)
        if depth == 0:
            return js[start:j + 1]
    raise AssertionError("banner block: unbalanced braces")


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    node = shutil.which("node")
    if not node:                                    # pragma: no cover
        pytest.skip("node is not on PATH — these cases were REQUESTED (`-m node`) "
                    "and did not run; nothing here passed")
    src = tmp_path_factory.mktemp("graph-banner") / "banner.js"
    src.write_text(_banner_block(), encoding="utf-8")
    proc = subprocess.run([node, str(HARNESS), str(src)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=NODE_TIMEOUT_S,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        data = json.loads(proc.stdout)
    except ValueError:                              # pragma: no cover
        pytest.fail(f"node produced no report (exit {proc.returncode})\n"
                    f"{proc.stdout[:2000]}\n{proc.stderr[:2000]}")
    failed = [c for c in data["cases"] if not c["ok"]]
    assert not failed, "\n".join(f"{c['name']} :: {c['detail']}" for c in failed)
    assert proc.returncode == 0, proc.stderr
    return {c["name"]: c for c in data["cases"]}


@pytest.mark.parametrize("case", CASES)
def test_the_graph_banner(report, case):
    assert case in report, f"the harness ran no case named {case!r}"
    assert report[case]["ok"], report[case]["detail"]
