# tests/test_server_js_node.py
"""Tier-1 node test of the heartbeat: the fragment RUN, not string-matched.

`tests/test_server_js.py` asserts on the text of `heartbeat_script`. Text is
not behaviour: a fragment with a missing brace, a `Worker` path that never
ticks, or a `document.hidden` guard smuggled back in as a different spelling
would all pass a substring check. This file executes the exact bytes the page
gets and drives the seams a browser would -- the worker tick, its `onerror`,
the fallback interval -- then asserts on what the page DID.

Two properties here have no other test anywhere:

* **It parses.** Nothing else in this repo ever hands the fragment to a
  JavaScript engine, so a syntax error would ship, and the browser would
  report it in a console nobody is watching -- with the panel shutting down a
  minute later for reasons that look like a server bug.
* **A hidden tab still beats.** The design decision the whole feature rests
  on, asserted by running the fragment with `document.hidden === true`.

The `node` marker is not cosmetic: `pyproject.toml` carries
``addopts = -m "not node"``, so `hive selftest` -- which shells pytest from
inside the running dashboard's own POST handler -- never forks a JS runtime
out of a web request. Run it deliberately:

    pytest -m node tests/test_server_js_node.py
"""
import json
import pathlib
import shutil
import subprocess

import pytest

from mindhive.server.js import heartbeat_script

pytestmark = pytest.mark.node

HARNESS = pathlib.Path(__file__).parent / "node" / "heartbeat.test.mjs"
NODE_TIMEOUT_S = 60
MS = 15000
TOKEN = "node-token"

# name -> what the browser does. Each runs the fragment in its own fresh vm
# context, so no scenario can see another's timers or fetches.
CASES = [
    # The ordinary browser: a Worker that ticks twice.
    {"name": "worker", "token": TOKEN, "fireOnMessage": 2},
    # The same, in a tab the user switched away from.
    {"name": "hidden", "token": TOKEN, "hidden": True, "fireOnMessage": 2},
    # No Worker at all -- an old runtime, or a CSP that forbids blob: workers.
    {"name": "no_worker", "token": TOKEN, "noWorker": True, "fireTimers": 2},
    # A Worker that constructs and then fails: `onerror` must install the
    # fallback, and the fallback must then beat.
    # `onerror` twice, because it can fire more than once and a second
    # fallback would double the beat rate.
    {"name": "worker_dies", "token": TOKEN, "fireOnError": 2,
     "fireTimers": 2},
    # The network is down. The beat must not throw and must not leave an
    # unhandled rejection.
    {"name": "fetch_rejects", "token": TOKEN, "fetchRejects": True,
     "fireOnMessage": 1},
    # `fetch` itself missing or throwing synchronously (a blocked scheme).
    {"name": "fetch_throws", "token": TOKEN, "fetchThrows": True,
     "fireOnMessage": 1},
]


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    node = shutil.which("node")
    if not node:                                        # pragma: no cover
        pytest.skip("node is not installed")
    d = tmp_path_factory.mktemp("heartbeat")
    src = d / "heartbeat.js"
    src.write_text(heartbeat_script(MS), encoding="utf-8")
    cases = d / "cases.json"
    cases.write_text(json.dumps(CASES), encoding="utf-8")
    proc = subprocess.run([node, str(HARNESS), str(src), str(cases)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=NODE_TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("name", [c["name"] for c in CASES])
def test_the_fragment_parses_and_nothing_escapes_it(ran, name):
    """Every scenario, including the two where the network fails: a beat that
    throws out of the fragment would take the page's own scripts with it."""
    assert ran[name]["threw"] is None


# --------------------------------------------------------------------------
# it beats
# --------------------------------------------------------------------------

def test_it_beats_once_on_load(ran):
    """Before any timer: the server starts its idle clock at construction, so
    a page that waited a full interval to say hello would burn a quarter of
    the default cutoff doing nothing."""
    assert ran["worker"]["afterLoad"] == 1


def test_every_worker_tick_is_a_beat(ran):
    assert ran["worker"]["afterOnMessage"] == 3          # load + two ticks


def test_the_beat_goes_where_the_server_listens(ran):
    beat = ran["worker"]["fetches"][0]
    assert beat["url"].startswith("/api/heartbeat?token=")
    assert beat["method"] == "POST"
    assert beat["keepalive"] is True
    assert beat["body"] == "{}"
    assert beat["headers"]["X-Hive-Token"] == TOKEN
    assert beat["headers"]["Content-Type"] == "application/json"


def test_a_hidden_tab_keeps_beating(ran):
    """THE design decision. Pausing in a hidden tab would shut the server
    down under a page the user merely switched away from, and they would come
    back to a dead panel. Hidden is not closed.

    Asserted by RUNNING the fragment with `document.hidden === true` rather
    than by grepping for the string, so a guard spelled
    `visibilityState !== 'visible'` would fail here too."""
    assert ran["hidden"]["afterOnMessage"] == ran["worker"]["afterOnMessage"]


# --------------------------------------------------------------------------
# the timer lives in a Worker
# --------------------------------------------------------------------------

def test_the_timer_is_in_the_worker_not_on_the_main_thread(ran):
    """Chrome throttles a hidden tab's `setInterval` to ~1/min after five
    minutes; against a 60 s cutoff that is a coin flip every minute. A worker
    is not on that budget — so when one exists, the main thread must hold no
    interval at all."""
    assert ran["worker"]["intervals"] == []
    assert ran["worker"]["workerCount"] == 1


def test_the_worker_ticks_at_the_configured_interval(ran):
    assert f"{MS}" in ran["worker"]["workerSource"]
    assert "setInterval" in ran["worker"]["workerSource"]
    assert "postMessage" in ran["worker"]["workerSource"]


def test_the_token_never_enters_the_worker(ran):
    """The worker only TICKS. Keeping the fetch on the main thread is what
    keeps the token out of a second scope and out of a blob: URL."""
    assert TOKEN not in ran["worker"]["workerSource"]
    assert "fetch" not in ran["worker"]["workerSource"]


# --------------------------------------------------------------------------
# and falls back when there is no Worker
# --------------------------------------------------------------------------

def test_without_a_worker_it_falls_back_to_a_main_thread_timer(ran):
    assert ran["no_worker"]["intervals"] == [MS]
    assert ran["no_worker"]["afterTimers"] == 3          # load + two ticks


def test_a_worker_that_dies_later_falls_back_too(ran):
    """`onerror` fires for a worker that constructed and then failed — a CSP
    that allows the blob: URL and blocks its execution, say. Without this the
    page would have a heartbeat that silently stopped."""
    assert ran["worker_dies"]["intervals"] == [MS]
    assert ran["worker_dies"]["afterTimers"] > ran["worker_dies"]["afterLoad"]


def test_the_fallback_cannot_stack_two_timers(ran):
    """`onerror` can fire more than once. Two intervals would double the beat
    rate and the server would never learn why."""
    assert len(ran["worker_dies"]["intervals"]) == 1


# --------------------------------------------------------------------------
# a network that is not there
# --------------------------------------------------------------------------

def test_a_rejected_beat_is_swallowed_and_the_next_one_still_fires(ran):
    assert ran["fetch_rejects"]["afterOnMessage"] == 2
    assert ran["fetch_rejects"]["threw"] is None


def test_a_throwing_fetch_is_swallowed_too(ran):
    """A rejected promise and a synchronous throw are different failures and
    need different guards — `.catch()` handles neither of the second kind."""
    assert ran["fetch_throws"]["afterOnMessage"] == 2
    assert ran["fetch_throws"]["threw"] is None


# --------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------

def test_the_served_page_carries_these_exact_bytes():
    """A fragment that is tested but not spliced is a test of nothing."""
    from mindhive import dashboard
    from mindhive.server.lifecycle import HeartbeatMonitor
    from mindhive.server.routes import with_lifecycle
    handler = with_lifecycle(dashboard.make_handler("t"), HeartbeatMonitor())
    assert handler._extra_js == heartbeat_script(MS)
    # `_render_index` called with the CLASS as `self`: it reads
    # `self._extra_js` and module globals, nothing instance-bound, so the
    # splice can be asserted without standing up a server.
    assert handler._extra_js in handler._render_index(handler)
