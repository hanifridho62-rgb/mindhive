# tests/test_dashboard_query.py
"""The request-boundary predicates, as pure functions.

`tests/test_dashboard_http.py` proves each of these changes what comes back on
the wire. This file pins the edges that a wire test cannot reach cheaply — the
spellings of a Host header, the shapes of a project name — one case per line so a
failure names the input that broke.

Every negative here is paired: for each value the function must refuse there is a
neighbouring value it must ACCEPT, because a predicate that refuses everything
also passes every "this is rejected" assertion.
"""
import pytest

from mindhive import paths
from mindhive.dashboard_query import (BadRequest, host_is_local, is_safe_project,
                                      qs_int)


# --- qs_int ----------------------------------------------------------------

def test_qs_int_reads_the_value_and_falls_back_to_the_default():
    assert qs_int({"limit": ["25"]}, "limit", 200) == 25
    assert qs_int({}, "limit", 200) == 200
    assert qs_int({"limit": [""]}, "limit", 200) == 200      # ?limit= with no value
    assert qs_int({"limit": ["-3"]}, "limit", 200) == -3     # range is the caller's business


def test_qs_int_refuses_by_the_callers_parameter_name():
    """DISCRIMINATING on the MESSAGE. A bare `int()` failure already raised —
    the defect was that it raised something whose text is our interpreter's
    (`invalid literal for int() with base 10: 'abc'`), reported as a 500. So
    "it raises" is not the assertion; "it raises OUR sentence" is."""
    with pytest.raises(BadRequest) as exc:
        qs_int({"limit": ["abc"]}, "limit", 200)
    assert str(exc.value) == "invalid limit"
    assert "literal" not in str(exc.value)

    with pytest.raises(BadRequest) as exc:
        qs_int({"since": ["1.5"]}, "since", 0)               # not an int either
    assert str(exc.value) == "invalid since"


def test_bad_request_is_a_value_error():
    """The handlers that already guard a conversion with `except ValueError`
    keep catching it; only the routing layer tells the two apart."""
    assert issubclass(BadRequest, ValueError)


# --- is_safe_project -------------------------------------------------------

USABLE = ["mindhive", "test", "a", "my-project", "Project_2", "proj.v2",
          "UNSAFE", "_private", "__hivemind__", "a" * 128]

REFUSED = [
    "..", ".", "../escaped", "..\\escaped", "a/b", "a\\b", "/etc", "C:/Windows",
    "C:relative", "~", "~/.ssh", "name:stream", "star*", "quest?", 'quo"te',
    "pipe|d", "less<than", "more>than", "nul\x00byte", "bell\x07", "", "   ",
    # Contained, and refused all the same: the sink stores each under ANOTHER
    # name, so a 200 would read a different directory under the name asked for.
    "проект", "my project", "unsafe-0000000000000000", "UNSAFE-0000000000000000",
    "-dash", ".hidden", " lead", "a" * 129,
    "LIVE_LOCK", "autosync.json", "Autosync.JSON",
]


@pytest.mark.parametrize("name", USABLE)
def test_a_name_the_store_keeps_verbatim_is_a_usable_project(name):
    """The paired positives. `paths.normalize` only ever mints `[a-z0-9-]+`, but
    a namespace directory somebody created by hand is a real namespace too, as
    long as `paths.namespace_name` keeps it as it is. `a` * 128 is the length
    boundary, beside the 129 refused below."""
    assert is_safe_project(name)


@pytest.mark.parametrize("name", REFUSED)
def test_a_name_the_store_would_not_keep_verbatim_is_refused(name):
    """Separators and `..` once left the store; `paths.project_dir` quarantines
    them now, so the defect a 200 carries here is the second one -- the answer
    comes from another directory than the one named. A non-ASCII name, a space,
    the reserved prefix, a leading `-` or `.`, an over-long name and a store-root
    file name are all quarantined by the sink, and all passed the old shape
    test."""
    assert not is_safe_project(name)


def test_a_project_that_is_not_a_string_is_refused():
    for value in (None, 3, ["test"], {"name": "test"}):
        assert not is_safe_project(value)


@pytest.mark.parametrize("name", USABLE + REFUSED + ["CON", "nul.txt", "abc.",
                                                     "com1"])
def test_the_http_check_accepts_exactly_what_the_sink_keeps(name):
    """The drift guard. The HTTP check and `paths.namespace_name` were two rules
    and the sink outgrew the check; this holds them to one answer, on this
    platform's own device-name and trailing-dot rules."""
    assert is_safe_project(name) == (paths.namespace_name(name) == name)


# --- host_is_local ---------------------------------------------------------

@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:54461", "localhost", "localhost:54461",
    "LOCALHOST:54461", "[::1]", "[::1]:54461", "::1", "127.0.0.53:9",
])
def test_loopback_by_every_spelling_a_browser_uses(host):
    assert host_is_local(host)


@pytest.mark.parametrize("host", [
    "evil.example.com", "evil.example.com:54461", "mindhive.local",
    "127.0.0.1.evil.com", "notlocalhost", "localhost.evil.com",
    "0.0.0.0", "10.0.0.4:54461",
])
def test_a_name_an_attacker_could_own_is_not_loopback(host):
    """Rebinding needs a domain the attacker controls. None of these can be
    `localhost` or a 127/8 literal, which is exactly why an allow-list of names
    closes the hole that a bind address cannot."""
    assert not host_is_local(host)


def test_an_absent_host_is_allowed():
    """HTTP/1.0 clients send none, several raw-socket tests in this repo predate
    the header, and a request with no Host is not one a rebound browser can
    make — `fetch` always sends it."""
    assert host_is_local(None)
    assert host_is_local("")
    assert host_is_local("   ")
