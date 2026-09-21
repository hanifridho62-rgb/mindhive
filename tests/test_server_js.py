# tests/test_server_js.py
"""The heartbeat fragment: what it must contain, and what it must not.

The script is three lines of JavaScript and every one of them is a decision
that was wrong in an obvious way first. It is asserted here at the TEXT level
-- does the fragment carry the timer, the keepalive, the fallback -- and at the
BEHAVIOUR level under node in `tests/test_server_js_node.py`, which actually
runs it. Text-level tests catch a fragment that lost a clause in an edit;
behaviour tests catch one that is present and wrong.

**The two things it must NOT do**, both pinned:

* **No `document.hidden` guard.** The instinct is to pause the beat in a
  background tab to "save work". But a background tab is still an OPEN tab,
  and pausing the beat there means the server shuts down under a page the user
  has merely switched away from. When they come back the panel is dead. The
  hidden tab is precisely the case this feature must survive.
* **No bare `setInterval` as the only timer.** Chrome applies intensive
  throttling to timers in a tab hidden for more than five minutes: they fire
  about once a minute. At a 60 s cutoff that is a coin flip, every minute, for
  as long as the tab stays in the background. A dedicated Worker's timer is
  not on that budget, which is why the beat is driven from one -- with
  `setInterval` kept as the fallback for an environment where Workers or
  `blob:` URLs are unavailable.
"""
import pytest

from mindhive.server import js


def _frag(ms=15000):
    return js.heartbeat_script(ms)


# --------------------------------------------------------------------------
# the request it makes
# --------------------------------------------------------------------------

def test_it_posts_to_the_heartbeat_route():
    frag = _frag()
    assert "/api/heartbeat" in frag
    assert "'POST'" in frag or '"POST"' in frag


def test_it_goes_through_the_pages_own_url_helper():
    """`u()` appends `?token=`, which is how every other call in the page is
    authorised. Spelling the URL by hand here would be a second place the
    token has to be got right."""
    assert "u('/api/heartbeat')" in _frag()


def test_it_also_sends_the_token_as_a_header():
    """Belt and braces, and cheap: `_check_token` reads `X-Hive-Token` first,
    so a proxy or a redirect that eats the query string still authenticates."""
    assert "X-Hive-Token" in _frag()


def test_the_beat_is_keepalive_so_a_closing_tab_still_delivers_it():
    assert "keepalive" in _frag()


def test_the_interval_is_the_number_it_was_given():
    assert "15000" in _frag(15000)
    assert "2500" in _frag(2500)
    assert "15000" not in _frag(2500)


def test_the_interval_is_named_so_the_page_can_be_inspected():
    assert "HEARTBEAT_MS" in _frag()


@pytest.mark.parametrize("bad", [0, -1, -15000])
def test_an_interval_that_cannot_beat_is_refused(bad):
    """A zero or negative interval produces a `setInterval` that either never
    fires or fires in a tight loop. Neither is a heartbeat."""
    with pytest.raises(ValueError):
        js.heartbeat_script(bad)


def test_the_interval_is_rendered_as_an_integer_not_a_float():
    """`setInterval(f, 15000.0)` works, but the number also lands in the page
    where a human reads it, and `HEARTBEAT_MS = 15000.0` invites the question
    of what the fraction is for."""
    assert "15000.0" not in js.heartbeat_script(15000.0)
    assert "HEARTBEAT_MS = 15000;" in js.heartbeat_script(15000.0)


# --------------------------------------------------------------------------
# the timer
# --------------------------------------------------------------------------

def test_the_timer_lives_in_a_worker():
    frag = _frag()
    assert "new Worker" in frag
    assert "createObjectURL" in frag and "Blob" in frag


def test_there_is_a_setinterval_fallback():
    assert "setInterval(beat" in _frag()


def test_the_worker_failing_asynchronously_also_falls_back():
    """A CSP that forbids `blob:` does not make `new Worker` throw -- the
    construction succeeds and the worker fails later, through `onerror`. A
    try/catch alone would leave the page silently beatless."""
    assert "onerror" in _frag()


def test_the_fallback_cannot_double_schedule():
    """`onerror` can fire after a catch already installed the fallback, and
    two timers means two beats per interval -- harmless for the server,
    confusing for anyone reading `pings`."""
    frag = _frag()
    assert frag.count("setInterval(beat") == 1


def test_it_beats_once_immediately():
    """The first beat cannot wait for the first interval: `serve` starts the
    monitor when the socket binds, and on a slow first paint 15 s of that
    grace is already spent before the page is interactive."""
    assert "beat();" in _frag()


# --------------------------------------------------------------------------
# what it must not contain
# --------------------------------------------------------------------------

def test_there_is_no_visibility_guard():
    frag = _frag()
    assert "document.hidden" not in frag
    assert "visibilitychange" not in frag
    assert "visibilityState" not in frag


def test_it_does_not_reach_for_the_ledger_or_a_project():
    """The beat says "someone is still here" and nothing else. A body that
    carried a project would make an idle ping a namespace-scoped request and
    give HIVE MIND's read-only guard something to refuse."""
    assert "project" not in _frag()


# --------------------------------------------------------------------------
# it has to survive being spliced into the page
# --------------------------------------------------------------------------

def test_it_is_a_single_expression_statement_that_leaks_no_globals():
    frag = _frag().strip()
    assert frag.startswith("(function()")
    assert frag.endswith("})();")


def test_it_closes_every_brace_it_opens():
    frag = _frag()
    assert frag.count("{") == frag.count("}")
    assert frag.count("(") == frag.count(")")


def test_it_carries_no_marker_the_page_would_try_to_splice():
    """`render_page` runs a chain of `.replace` calls over marker comments. A
    fragment carrying one would be rewritten by a later link in that chain."""
    assert "/*__" not in _frag()
    assert "<!--__" not in _frag()


def test_it_carries_no_script_tag_that_would_close_the_pages_own():
    assert "</script" not in _frag().lower()


def test_the_module_is_a_leaf():
    """A string builder. It must not import the dashboard, the ledger or an
    HTTP stack -- `server/routes.py` is where those meet."""
    import ast
    from pathlib import Path
    src = Path(js.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == set()
