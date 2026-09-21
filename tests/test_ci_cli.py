# SPDX-License-Identifier: Apache-2.0
"""`hive ci breaker` (blueprint P1f).

The command exists because the state does. An adversarial review found that
QUARANTINED was reachable, that `IntegrationBreaker.manual_reset` was its only
exit, and that `manual_reset` had no caller anywhere in `src/` -- so the verdict
told the operator to run `hive ci breaker reset` and argparse answered "invalid
choice". The recovery left was deleting the sidecar by hand.

Two properties carry the weight here:

* **`status` does not move the circuit.** `check()` is a transition, not a
  question: an OPEN circuit whose cooldown has elapsed writes HALF_OPEN and
  appends an event as it answers. A status command built on it would change
  what it was asked to report.
* **`reset` is total.** From any state, including CLOSED, so a script does not
  have to know the state before asking for one.
"""
import json
import time

import pytest

from mindhive import __main__ as cli_mod
from mindhive import events, models
from mindhive.ci import cli as ci_cli
from mindhive.orchestrator import breaker as brk


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MINDHIVE_EVENT_BACKEND", raising=False)
    return tmp_path


def _args(**kw):
    from types import SimpleNamespace
    kw.setdefault("project", "p")
    kw.setdefault("json", False)
    return SimpleNamespace(**kw)


def _quarantine(project="p"):
    """Three trips inside the window: the state that has no cooldown out."""
    now = {"t": 1_000_000.0}
    b = brk.IntegrationBreaker(project=project, clock=lambda: now["t"])
    for _ in range(3):                       # trip 1
        b.record_failure("boom")
    for _ in range(2):                       # a probe, then trips 2 and 3
        now["t"] += b.COOLDOWN_PERIOD_S + 1.0
        b.check()
        b.record_failure("boom")
    return b


# --- the parser is really wired -------------------------------------------------------

def test_the_group_is_on_the_live_parser_with_both_verbs():
    parser = cli_mod._build_parser()
    got = parser.parse_args(["ci", "breaker", "status", "--project", "p", "--json"])
    assert got.func is ci_cli.cmd_ci_breaker_status
    assert (got.project, got.json) == ("p", True)
    reset = parser.parse_args(["ci", "breaker", "reset"])
    assert reset.func is ci_cli.cmd_ci_breaker_reset
    assert (reset.project, reset.json) == (None, False)


def test_a_hostile_project_is_refused_at_the_keyboard():
    """`cliparse.project_arg`, like every other `--project` on the surface."""
    with pytest.raises(SystemExit):
        cli_mod._build_parser().parse_args(["ci", "breaker", "status",
                                            "--project", "../../x"])


def test_the_group_is_required_to_name_a_verb():
    for argv in (["ci"], ["ci", "breaker"]):
        with pytest.raises(SystemExit):
            cli_mod._build_parser().parse_args(argv)


# --- status reads, and only reads ------------------------------------------------------

def test_status_on_an_untouched_project_says_so(capsys):
    code = ci_cli.cmd_ci_breaker_status(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (report["state"], report["exists"]) == (models.CLOSED, False)
    assert report["cooldown_left_s"] == 0
    assert report["path"].endswith(brk.STATE_FILENAME)


def test_status_never_moves_an_open_circuit_whose_cooldown_has_elapsed(capsys):
    """DISCRIMINATING, and stamped in the real epoch so the default clock the
    command uses agrees with it (that epoch is itself a round-2 fix).

    The cooldown here has ELAPSED, which is exactly when `check()` transitions:
    built on it this test fails twice over -- the state on disk becomes
    HALF_OPEN and a `state_transition` event appears -- a question that answered
    by changing its subject."""
    stale = time.time() - (brk.IntegrationBreaker.COOLDOWN_PERIOD_S + 100.0)
    b = brk.IntegrationBreaker(project="p", clock=lambda: stale)
    for _ in range(3):
        b.record_failure("boom")
    assert b.state().state == models.OPEN
    before = len(events.read_all("p"))

    code = ci_cli.cmd_ci_breaker_status(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 1                                     # OPEN: a script can gate
    assert report["state"] == models.OPEN
    assert report["cooldown_left_s"] == 0                # elapsed, and unmoved
    assert brk.IntegrationBreaker(project="p").state().state == models.OPEN
    assert len(events.read_all("p")) == before
    # and `check` really would have moved it, which is what makes this a test
    assert brk.IntegrationBreaker(project="p").check().state == models.HALF_OPEN


def test_status_names_the_cooldown_still_to_wait(capsys):
    """The other side of the same field: a trip the default clock sees as
    FRESH still has its whole cooldown ahead of it."""
    b = brk.IntegrationBreaker(project="p")           # the real, persisted epoch
    for _ in range(3):
        b.record_failure("boom")
    ci_cli.cmd_ci_breaker_status(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert report["cooldown_left_s"] == pytest.approx(
        brk.IntegrationBreaker.COOLDOWN_PERIOD_S, abs=30.0)


def test_status_exits_two_while_quarantined(capsys):
    _quarantine()
    code = ci_cli.cmd_ci_breaker_status(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["state"] == models.QUARANTINED
    assert report["quarantine_reason"]
    assert report["trips_in_window"] == brk.IntegrationBreaker.MAX_TRIPS_IN_WINDOW


def test_the_human_report_names_the_record_and_the_reason(capsys):
    _quarantine()
    ci_cli.cmd_ci_breaker_status(_args())
    out = capsys.readouterr().out
    assert f"state: {models.QUARANTINED}" in out
    assert "quarantined:" in out
    assert brk.STATE_FILENAME in out


# --- reset is the exit, from anywhere --------------------------------------------------

def test_reset_is_the_way_out_of_quarantine(capsys):
    """The finding, closed: before this the only recovery was deleting
    `integration_breaker.json` by hand."""
    _quarantine()
    code = ci_cli.cmd_ci_breaker_reset(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (report["was"], report["state"]) == (models.QUARANTINED, models.CLOSED)
    assert report["changed"] is True
    assert report["trips_in_window"] == 0 and report["quarantine_reason"] is None
    # and a fresh process agrees, which is the whole point of the sidecar
    after = brk.IntegrationBreaker(project="p")
    assert after.check().allowed is True


def test_reset_of_a_closed_circuit_is_a_named_no_op(capsys):
    code = ci_cli.cmd_ci_breaker_reset(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (report["was"], report["state"]) == (models.CLOSED, models.CLOSED)
    assert report["changed"] is False


def test_the_human_reset_says_which_way_it_went(capsys):
    _quarantine()
    ci_cli.cmd_ci_breaker_reset(_args())
    out = capsys.readouterr().out
    assert f"{models.QUARANTINED} -> {models.CLOSED}" in out
    ci_cli.cmd_ci_breaker_reset(_args())
    assert "nothing to reset" in capsys.readouterr().out


def test_the_verdict_names_a_command_that_now_runs():
    """The other half of the finding: the refusal text told the operator to run
    something argparse answered `invalid choice` to."""
    named = _quarantine().check().reason
    assert "hive ci breaker reset" in named
    argv = named.split("jalankan ", 1)[1].split(" setelah", 1)[0].split()
    assert argv[0] == "hive"
    got = cli_mod._build_parser().parse_args(argv[1:])
    assert got.func is ci_cli.cmd_ci_breaker_reset


# --- the group costs nothing to offer ---------------------------------------------------

def test_the_package_init_imports_nothing():
    import ast
    from pathlib import Path
    src = Path(ci_cli.__file__).resolve().parent / "__init__.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    assert [n for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))] == []


def test_building_the_parser_does_not_import_the_orchestrator():
    """`hive --help` must not pay for the orchestrator package: the breaker is
    imported inside the handlers, not at module load."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(ci_cli.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [a.name for n in top for a in n.names]
    assert "orchestrator" not in " ".join(names)
    assert sorted(names) == ["cliparse", "json", "paths"]
