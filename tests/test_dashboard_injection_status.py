# tests/test_dashboard_injection_status.py
"""The SOP-injection verdict on the dashboard, and the one way it could lie.

`sopgate.notice` already puts this verdict into STATE.md and the MEMORY.md
digest. The panel was the surface it had not reached: `grep injection
src/mindhive/dashboard*.py` answered 0, so the only way to see a degradation
while looking at the dashboard was to open the State tab and read prose.

The interesting test here is not that the field appears — it is
`test_the_verdict_is_not_frozen_by_the_overview_memo`. `/api/overview` is now
memoized on the ledger's stat, and this verdict is a fact about the SOP sidecar
and `PROMPT_BUDGET` instead. Computed inside that memo it would sit at whatever
it said when the ledger last moved: a stale answer on the one field whose entire
job is to report that something has gone stale. That test holds the ledger
still, flips the probe, and requires the next poll to report the flip.

Store isolation comes from the autouse `isolate_store` fixture in
tests/conftest.py, like every other dashboard test.
"""
import collections
import json
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, events, sopgate

from tests.serving import serve_in_thread

OVERVIEW = "/api/overview?project=test"
OVERVIEW_ALL = "/api/overview?project=*"


def _clear_memos():
    for name, value in vars(dashboard).items():
        if name.endswith("_MEMO") and isinstance(value, collections.OrderedDict):
            value.clear()


@pytest.fixture(autouse=True)
def clean_module_state():
    _clear_memos()
    yield
    _clear_memos()


@pytest.fixture
def dashboard_server():
    token = "test-token-123"
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(token))
    with serve_in_thread(server):
        yield f"http://127.0.0.1:{server.server_address[1]}", token


def _get(base, path, token):
    sep = "&" if "?" in path else "?"
    with urllib.request.urlopen(f"{base}{path}{sep}token={token}", timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _seed(project="test"):
    events.append_event("user_prompt", {"text": "do the thing"},
                        session_id="sess-1", project=project)


# The shape `sopgate.probe` returns, for the two states that matter. Built as
# data rather than by calling the real probe: the variable under test is what
# the dashboard does with a verdict, not whether this machine's sidecar happens
# to be degraded today — which it is, and which would make a green run mean
# nothing on a machine where it is not.
DEGRADED = {
    "verdict": "pointers_only", "budget": 3000, "mandatory": 1, "bodies": 0,
    "pointers": 1, "largest_chars": 3414, "body_chars": 0,
    "sops": [{"sop_id": "execution-discipline", "title": "Execution discipline",
              "delivered": "pointer", "chars": 3414}],
}
HEALTHY = {
    "verdict": "verbatim", "budget": 3000, "mandatory": 1, "bodies": 1,
    "pointers": 0, "largest_chars": 2601, "body_chars": 2601,
    "sops": [{"sop_id": "execution-discipline", "title": "Execution discipline",
              "delivered": "body", "chars": 2601}],
}


def test_a_degraded_verdict_reaches_the_panel(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed()
    monkeypatch.setattr(sopgate, "probe", lambda *_a, **_kw: DEGRADED)

    status, body = _get(base, OVERVIEW, token)
    assert status == 200
    inj = body["injection"]
    assert inj["verdict"] == "pointers_only"
    assert "3000-char prompt budget" in inj["sentence"], (
        f"the sentence is not sopgate's own: {inj['sentence']!r}")
    assert inj["pointers"] == ["execution-discipline"], \
        "the SOPs that arrived as pointers are what a reader has to go and read"
    assert inj["scope"] == "global", (
        "the response must say the verdict is machine-wide — it sits beside "
        "per-namespace counts, and a global number read as a local one is worse "
        "than no number")


def test_a_healthy_verdict_renders_zero_lines(dashboard_server, monkeypatch):
    """`verbatim` is not news. A strip in every healthy panel is a strip every
    reader learns to skip, which is how the degraded case gets missed."""
    base, token = dashboard_server
    _seed()
    monkeypatch.setattr(sopgate, "probe", lambda *_a, **_kw: HEALTHY)

    _status, body = _get(base, OVERVIEW, token)
    assert body["injection"] is None, \
        f"a healthy verdict still produced a panel line: {body['injection']!r}"


def test_the_verdict_is_not_frozen_by_the_overview_memo(
        dashboard_server, monkeypatch):
    """DISCRIMINATING, and the reason this field is computed outside
    `overview_stats`.

    The ledger does not move here — not one event is appended between the two
    polls — so the overview memo scores a hit and the counts come back cached,
    which is exactly what it is for. The verdict must NOT come back cached with
    them: it is a fact about the SOP sidecar, so nothing that fingerprints the
    event segments can ever notice it changing."""
    base, token = dashboard_server
    _seed()
    monkeypatch.setattr(sopgate, "probe", lambda *_a, **_kw: HEALTHY)
    _status, warm = _get(base, OVERVIEW, token)
    assert warm["injection"] is None

    monkeypatch.setattr(sopgate, "probe", lambda *_a, **_kw: DEGRADED)
    status, after = _get(base, OVERVIEW, token)
    assert status == 200
    assert after["events"] == warm["events"], \
        "this test is only meaningful while the ledger stayed still"
    assert after["injection"] is not None, (
        "the injection verdict was served from the overview memo — it degraded "
        "and the panel went on reporting health until the next append")
    assert after["injection"]["verdict"] == "pointers_only"


def test_a_probe_that_raises_costs_one_line_not_the_overview(
        dashboard_server, monkeypatch):
    """`probe` documents that it never raises. The guard exists anyway, because
    the cost of being wrong about that is a 500 on the section a human opens
    first, in exchange for one missing line."""
    base, token = dashboard_server
    _seed()

    def _boom(*_a, **_kw):
        raise RuntimeError("sidecar unreadable")
    monkeypatch.setattr(sopgate, "probe", _boom)

    status, body = _get(base, OVERVIEW, token)
    assert status == 200, "a failing probe took the whole overview down"
    assert body["injection"] is None
    assert body["events"] == 1, "the counts beside it must be unaffected"


def test_hive_mind_carries_the_same_global_verdict(dashboard_server, monkeypatch):
    """The verdict is machine-wide, so the unified lens reports it too — it is
    the one number on that page that is NOT an aggregate of the namespaces."""
    base, token = dashboard_server
    _seed()
    _seed(project="other")
    monkeypatch.setattr(sopgate, "probe", lambda *_a, **_kw: DEGRADED)

    status, body = _get(base, OVERVIEW_ALL, token)
    assert status == 200
    assert body["injection"]["verdict"] == "pointers_only"


def test_the_class_the_page_emits_is_a_class_the_stylesheet_defines():
    """The seam between the two halves of this feature.

    Weaker than the behavioural cases above and labelled as such: it proves the
    page ships a strip whose class is styled, not that the strip appears for a
    degraded verdict and vanishes for a healthy one. That claim needs a real
    cascade and belongs in the browser tier (`-m browser`), where it is NOT yet
    covered — recorded rather than implied."""
    from mindhive import taskview
    from mindhive.dashboard_page import render_page
    page = render_page("test-token", tasks_css=taskview.CARDS_CSS,
                       tasks_js=taskview.CARDS_JS)
    assert "o.injection" in page, \
        "the page never reads the field the server now sends"
    assert page.count("injwarn") >= 2, (
        "the strip's class appears fewer than twice — the JS emits it and the "
        "stylesheet defines it, so one of the two halves is missing")
