# tests/test_server_cli.py
"""`hive ui`, and the promise that `hive dashboard` did not change.

Two verbs over one server. `hive ui` opens the panel and lets it go away when
the tab does; `hive dashboard` starts the 24/7 panel and is expected to still
be there tomorrow. They share `dashboard.serve`, which now knows how to shut
itself down -- so the test that matters most in this file is the one asserting
that `hive dashboard` still calls it the old way, with no lifecycle argument
at all.

`--idle-cutoff` is offered on BOTH, and that is deliberate rather than
symmetrical-for-its-own-sake: an operator who wants an ephemeral panel on a
box where `hive ui` is not the habit should not have to learn a second verb,
and the flag being absent is what keeps the default untouched.
"""
import pytest

from mindhive import __main__ as cli
from mindhive.commands import dashboard as cmds
from mindhive.server import app


def _run(*argv):
    return cli.main(list(argv))


@pytest.fixture
def served(monkeypatch):
    """Capture `serve_ui`'s arguments instead of serving."""
    calls = []
    monkeypatch.setattr(app, "serve_ui", lambda **kw: calls.append(kw))
    return calls


@pytest.fixture
def raw_serve(monkeypatch):
    """Capture `dashboard.serve`'s arguments instead of serving."""
    from mindhive import dashboard
    calls = []
    monkeypatch.setattr(dashboard, "serve",
                        lambda *a, **k: calls.append((a, k)))
    return calls


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def test_the_verb_is_on_the_real_parser():
    import argparse
    parser = cli._build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    assert "ui" in sub.choices


def test_the_help_renders_and_exits_zero():
    with pytest.raises(SystemExit) as e:
        _run("ui", "--help")
    assert e.value.code == 0


def test_the_help_says_what_makes_it_different_from_dashboard(capsys):
    with pytest.raises(SystemExit):
        _run("ui", "--help")
    text = capsys.readouterr().out.lower()
    assert "close" in text or "idle" in text


def test_the_handler_is_exported_under_its_own_name():
    assert cmds.cmd_ui is cli.cmd_ui
    assert "cmd_ui" in cmds.__all__


# --------------------------------------------------------------------------
# `hive ui`
# --------------------------------------------------------------------------

def test_the_defaults_are_the_blueprints(served):
    assert _run("ui") == 0
    (kw,) = served
    assert kw["port"] is None            # serve_ui resolves it to DEFAULT_PORT
    assert kw["open_browser"] is True
    assert kw["idle_cutoff_s"] == 60.0
    assert kw["asgi"] is False


def test_the_port_is_passed_through(served):
    assert _run("ui", "--port", "54471") == 0
    assert served[0]["port"] == 54471


def test_no_browser_is_passed_through(served):
    assert _run("ui", "--no-browser") == 0
    assert served[0]["open_browser"] is False


def test_the_idle_cutoff_is_passed_through(served):
    assert _run("ui", "--idle-cutoff", "5") == 0
    assert served[0]["idle_cutoff_s"] == 5.0


def test_the_asgi_flag_is_passed_through(served):
    assert _run("ui", "--asgi") == 0
    assert served[0]["asgi"] is True


def test_a_short_cutoff_is_a_short_run_not_a_usage_error(served):
    """The first thing anyone tries. `HeartbeatMonitor` refuses an interval
    that cannot fit inside its cutoff, so a 5 s cutoff against the default
    15 s beat would be an error — which is why `serve_ui` derives the
    interval rather than defaulting it."""
    assert _run("ui", "--idle-cutoff", "5") == 0
    assert served[0]["idle_cutoff_s"] == 5.0


def test_a_cutoff_that_cannot_work_is_a_usage_error_not_a_traceback(capsys):
    """Zero is the cutoff no derivation can rescue. A CLI must report that as
    a sentence, not as a ValueError traceback from three frames down."""
    assert _run("ui", "--idle-cutoff", "0", "--no-browser") == 2
    err = capsys.readouterr().err
    assert "idle_cutoff_s" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------
# --asgi without the extras
# --------------------------------------------------------------------------

def test_asgi_without_the_extras_names_the_install(monkeypatch, capsys):
    def _missing(**kw):
        raise RuntimeError("the ASGI adapter needs extras: pip install -e .[ui]")

    monkeypatch.setattr(app, "serve_ui", _missing)
    assert _run("ui", "--asgi") == 2
    err = capsys.readouterr().err
    assert "pip install -e .[ui]" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------
# `hive dashboard` is the same command it was
# --------------------------------------------------------------------------

def test_a_plain_dashboard_calls_serve_exactly_as_before(raw_serve):
    """THE promise of this phase. No `lifecycle` kwarg -- not `None`, ABSENT
    -- so a 24/7 panel cannot acquire a way to shut itself down through a
    default that someone later changes."""
    assert _run("dashboard") == 0
    (args, kw), = raw_serve
    assert args == ()
    assert kw == {"port": None, "open_browser": True}


def test_a_dashboard_with_an_idle_cutoff_gets_a_monitor(raw_serve):
    from mindhive.server.lifecycle import HeartbeatMonitor
    assert _run("dashboard", "--idle-cutoff", "30") == 0
    (_args, kw), = raw_serve
    assert isinstance(kw["lifecycle"], HeartbeatMonitor)
    assert kw["lifecycle"].idle_cutoff_s == 30.0


def test_the_dashboards_other_flags_still_reach_serve(raw_serve):
    assert _run("dashboard", "--port", "0", "--no-browser") == 0
    (_args, kw), = raw_serve
    assert kw["port"] == 0 and kw["open_browser"] is False


def test_a_dashboard_cutoff_that_cannot_work_is_a_usage_error(capsys):
    assert _run("dashboard", "--idle-cutoff", "0", "--no-browser") == 2
    err = capsys.readouterr().err
    assert "idle_cutoff_s" in err
    assert "Traceback" not in err


def test_a_short_dashboard_cutoff_derives_its_interval_too(raw_serve):
    """The two verbs must not disagree about what `--idle-cutoff 5` means."""
    assert _run("dashboard", "--idle-cutoff", "5") == 0
    (_args, kw), = raw_serve
    assert kw["lifecycle"].idle_cutoff_s == 5.0
    assert kw["lifecycle"].interval_s == 1.25


# --------------------------------------------------------------------------
# packaging
# --------------------------------------------------------------------------

def _pyproject():
    import pathlib
    import tomllib
    root = pathlib.Path(__file__).resolve().parents[1]
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_ui_extra_carries_both_packages_and_nothing_else():
    """`--asgi` needs fastapi AND uvicorn, and `_require_ui` refuses when
    either is missing — so one extra installing both is the only shape where
    "install the thing it named" actually fixes it."""
    extra = _pyproject()["project"]["optional-dependencies"]["ui"]
    assert {d.split(">")[0].strip() for d in extra} == {"fastapi", "uvicorn"}


def test_the_ui_extra_did_not_become_a_runtime_dependency():
    """The invariant this whole design exists to keep. `hive ui` without
    `--asgi` serves through the stdlib, so a machine with neither package
    still gets the feature."""
    assert _pyproject()["project"]["dependencies"] == []


def test_the_mindhive_script_is_the_same_entry_point_as_hive():
    """An alias, not a second CLI. Two entry points into different functions
    would be two surfaces to keep in step."""
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["mindhive"] == scripts["hive"] == "mindhive.__main__:main"


# --------------------------------------------------------------------------
# the import graph
# --------------------------------------------------------------------------

def test_building_the_parser_does_not_import_the_server_package():
    """`hive --help` names `cmd_ui`, and `cmd_ui` must still be a name rather
    than a reason to import an HTTP stack. Asserted in a child so this
    suite's own imports cannot make it pass."""
    import subprocess
    import sys
    code = ("import sys;"
            "from mindhive import __main__ as m;"
            "m._build_parser();"
            "print(any(k.startswith('mindhive.server') for k in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
