# SPDX-License-Identifier: Apache-2.0
"""The per-lane budget and the door that enforces it (blueprint P1f; MEB K21).

`LaneQuota` shipped with no caller anywhere in `src/` -- the same defect the
review found in `IntegrationBreaker.manual_reset`, which is what `hive ci
breaker reset` closed. A ceiling nothing consults is not a ceiling. This file
covers the door: `quota.LaneLedger` accumulates per lane, `LaneLedger.guard`
wraps the scheduler's runner, and `orchestrator/cli` wires both from two flags.

Three properties carry the weight, and each of them is a way the naive version
would be WRONG rather than merely unfinished:

* **Unmeasured is not zero.** `agenttask._RunOutcome` carries no token or cost
  figure, so on the default path nothing is measured. A ledger that read that
  as "this lane has spent 0" would print a confident zero for a mission it
  never measured -- and worse, a ceiling of 0 would then refuse every lane in
  the name of a number nobody read.
* **A quota refusal is not an environment failure.** `environment.
  environment_failure` reads a refusal's text for `_USAGE_HINTS` ("usage
  limit", "rate limit", "quota", ...). A refusal that matched would halt EVERY
  lane and tell the operator their ACCOUNT's usage limit is exhausted -- a
  claim about their billing that nothing here checked.
* **The default run is untouched.** With no flag there is no ledger, no
  wrapper and no report line; `hive lintester` makes byte for byte the call it
  always made, the same contract `_limits` keeps for the fence.
"""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from mindhive.orchestrator import agenttask, breaker as brk, cli as orch_cli
from mindhive.orchestrator import environment as env_mod
from mindhive.orchestrator import quota as q


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MINDHIVE_EVENT_BACKEND", raising=False)
    return tmp_path


def _lane(lane_id=0):
    return SimpleNamespace(lane_id=lane_id, worktree_path="")


def _entry(task_id="T-1"):
    return SimpleNamespace(task=SimpleNamespace(task_id=task_id), attempts=0)


def _args(**kw):
    kw.setdefault("max_lane_tokens", None)
    kw.setdefault("max_lane_cost", None)
    kw.setdefault("project", None)
    return SimpleNamespace(**kw)


# --- the two modules are one surface ---------------------------------------------------

def test_the_carve_out_is_by_identity():
    """K21 names these on `orchestrator/breaker`; they live in `quota.py` so
    neither file passes the MEB's 500-line ceiling. Identity is what makes that
    a carve-out rather than a second copy that can drift."""
    assert brk.LaneQuota is q.LaneQuota
    assert brk.LaneUsage is q.LaneUsage
    assert brk.LaneLedger is q.LaneLedger
    assert brk.usage_of is q.usage_of
    assert brk.QUOTA_REFUSED == q.QUOTA_REFUSED


def test_neither_import_order_breaks():
    """`quota` imports nothing from `breaker`, so there is no cycle."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(agenttask.__file__).resolve().parents[2]
    for first in ("mindhive.orchestrator.quota", "mindhive.orchestrator.breaker"):
        done = subprocess.run(
            [sys.executable, "-c", f"import {first}; print('ok')"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(root)})
        assert done.returncode == 0, f"{first}: {done.stderr}"


# --- unmeasured is not zero ------------------------------------------------------------

def test_an_outcome_that_reports_no_spend_reads_as_unknown_not_as_zero():
    """DISCRIMINATING. This is the real `_RunOutcome` the default runner
    answers -- it carries no usage at all. `LaneUsage()` here would be a claim
    that the unit was free."""
    assert q.usage_of(agenttask._RunOutcome(ok=True, result="ok")) is None
    assert q.usage_of(None) is None
    assert q.usage_of(object()) is None


def test_a_ceiling_of_zero_still_admits_a_run_nothing_measured():
    """DISCRIMINATING, and the sharpest form of the rule above: with usage read
    as zero, `max_tokens=0` would refuse every unit of every lane forever."""
    led = brk.LaneLedger(brk.LaneQuota(max_tokens=0))
    ran = []
    guarded = led.guard(lambda entry, lane: ran.append(lane.lane_id)
                        or agenttask._RunOutcome(ok=True, result="ok"))
    for _ in range(3):
        guarded(_entry(), _lane(0))
    assert ran == [0, 0, 0]
    assert led.measured() is False
    assert led.check(0).allowed is True


def test_the_two_shapes_a_spend_can_arrive_in():
    """An outcome that reports its own numbers, and `claude_cli`'s envelope."""
    assert q.usage_of({"tokens": 7, "cost_usd": 0.25}) == brk.LaneUsage(7, 0.25)
    assert q.usage_of(SimpleNamespace(tokens=4)) == brk.LaneUsage(4, 0.0)
    got = q.usage_of({"usage": {"input_tokens": 100, "output_tokens": 20,
                                "cache_read_input_tokens": 9_000},
                      "total_cost_usd": 1.5})
    # The cache fields are `mindnet.agent`'s OCCUPANCY signal, not a spend:
    # counting them would bill a lane for a prompt it only read.
    assert got == brk.LaneUsage(120, 1.5)


def test_a_zero_spend_that_was_really_measured_is_measured():
    """The other side: an explicit 0 is a figure, and `measured()` says so."""
    led = brk.LaneLedger(brk.LaneQuota(max_tokens=0))
    assert led.record(0, {"tokens": 0}) is True
    assert led.measured() is True
    assert led.check(0).allowed is True          # 0 is AT the ceiling, so inside


# --- the door ---------------------------------------------------------------------------

def test_with_no_ceiling_the_guard_hands_back_the_runner_itself():
    """Identity, not a wrapper that happens to be transparent: the default
    `hive lintester` run makes the call it always made."""
    def runner(entry, lane):
        return agenttask._RunOutcome(ok=True, result="ok")

    assert brk.LaneLedger(None).guard(runner) is runner
    assert brk.LaneLedger(brk.LaneQuota()).guard(runner) is runner


def test_the_lane_that_blew_its_budget_takes_no_further_unit():
    ran = []

    def runner(entry, lane):
        ran.append(entry.task.task_id)
        return {"tokens": 60}

    led = brk.LaneLedger(brk.LaneQuota(max_tokens=100))
    guarded = led.guard(runner)
    first = guarded(_entry("T-1"), _lane(0))
    second = guarded(_entry("T-2"), _lane(0))
    third = guarded(_entry("T-3"), _lane(0))
    assert ran == ["T-1", "T-2"]                 # the third never reached a child
    assert first == {"tokens": 60} and second == {"tokens": 60}
    assert third.ok is False and third.executed is False
    assert third.result == brk.QUOTA_REFUSED
    assert "kuota lane terlampaui" in third.reason
    assert led.spent(0) == brk.LaneUsage(120, 0.0)
    assert led.refusals() == ((0, "T-3", third.reason),)


def test_a_budget_is_per_lane_so_the_others_keep_running():
    """DISCRIMINATING. A ledger that kept one global total would stop the whole
    mission for one expensive lane."""
    led = brk.LaneLedger(brk.LaneQuota(max_tokens=50))
    guarded = led.guard(lambda entry, lane: {"tokens": 80})
    guarded(_entry("T-1"), _lane(0))             # lane 0 goes over
    assert guarded(_entry("T-2"), _lane(0)).executed is False
    out = guarded(_entry("T-3"), _lane(1))       # lane 1 is untouched by it
    assert out == {"tokens": 80}
    assert led.spent(1) == brk.LaneUsage(80, 0.0)


def test_the_cost_ceiling_refuses_on_its_own_axis():
    led = brk.LaneLedger(brk.LaneQuota(max_cost_usd=1.0))
    guarded = led.guard(lambda entry, lane: {"cost_usd": 0.75})
    guarded(_entry("T-1"), _lane(0))
    guarded(_entry("T-2"), _lane(0))
    refused = guarded(_entry("T-3"), _lane(0))
    assert refused.executed is False
    assert "biaya" in refused.reason and "USD" in refused.reason


def test_a_coroutine_runner_stays_a_coroutine_runner():
    """`queue._run_one` dispatches on `inspect.iscoroutinefunction`; a sync
    wrapper around an async runner would be awaited as a plain value."""
    import inspect

    async def runner(entry, lane):
        return {"tokens": 90}

    led = brk.LaneLedger(brk.LaneQuota(max_tokens=50))
    guarded = led.guard(runner)
    assert inspect.iscoroutinefunction(guarded)
    assert asyncio.run(guarded(_entry("T-1"), _lane(0))) == {"tokens": 90}
    refused = asyncio.run(guarded(_entry("T-2"), _lane(0)))
    assert refused.executed is False and refused.result == brk.QUOTA_REFUSED


def test_the_accumulation_survives_the_threads_the_scheduler_runs_lanes_on():
    led = brk.LaneLedger(brk.LaneQuota(max_tokens=10_000_000))
    guarded = led.guard(lambda entry, lane: {"tokens": 1, "cost_usd": 0.01})
    threads = [threading.Thread(target=lambda: [guarded(_entry(), _lane(0))
                                                for _ in range(25)])
               for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert led.spent(0).tokens == 300
    assert led.spent(0).cost_usd == pytest.approx(3.0)


def test_a_reader_that_raises_does_not_take_a_lane_down():
    """DISCRIMINATING. The bookkeeping runs on the scheduler's own path, so a
    reader that threw would turn a budget that cannot MEASURE into a mission
    that cannot RUN -- and the unit's real result would be lost with it."""
    def boom(out):
        raise RuntimeError("the reader blew up")

    led = brk.LaneLedger(brk.LaneQuota(max_tokens=1), reader=boom)
    guarded = led.guard(lambda entry, lane: {"tokens": 5})
    assert guarded(_entry("T-1"), _lane(0)) == {"tokens": 5}
    assert led.record(0, {"tokens": 5}) is False
    assert led.measured() is False                 # nothing was ever read
    assert led.check(0).allowed is True            # so nothing may be refused


def test_the_shipped_reader_is_total_over_what_it_cannot_read():
    assert q.usage_of(SimpleNamespace(tokens="not a number")) is None
    assert q.usage_of(SimpleNamespace(tokens=True)) is None      # bools are not counts
    assert q.usage_of({"usage": {"input_tokens": float("nan")}}) == brk.LaneUsage(0, 0.0)
    assert q.usage_of({"usage": "not a mapping"}) is None


# --- the refusal is not a claim about the operator's account ---------------------------

def test_a_quota_refusal_is_not_mistaken_for_the_accounts_usage_limit():
    """DISCRIMINATING, and the reason `QUOTA_REFUSED` is `refused` and the
    reason text is worded as it is. `_USAGE_HINTS` holds "quota"; if this
    classified as USAGE, `RepairLoop` would halt every lane and `describe`
    would tell the operator that their account's usage limit is exhausted."""
    led = brk.LaneLedger(brk.LaneQuota(max_tokens=1))
    guarded = led.guard(lambda entry, lane: {"tokens": 5})
    guarded(_entry("T-1"), _lane(0))
    refused = guarded(_entry("T-2"), _lane(0))

    assert refused.result in env_mod.NEVER_RAN      # no child was created
    kind = env_mod.environment_failure(SimpleNamespace(result=refused))
    assert kind == "", f"a lane budget was read as the machine failing: {kind}"
    for hint in env_mod._USAGE_HINTS + env_mod._AUTH_HINTS + env_mod._CLI_HINTS:
        assert hint not in refused.reason.lower()


# --- the front door ---------------------------------------------------------------------

def test_no_flag_means_no_ledger_and_no_report():
    assert orch_cli._quota(_args()) is None
    assert orch_cli._ledger(_args()) is None


def test_the_flags_reach_the_parser_and_build_the_ceiling():
    from mindhive import __main__ as cli_mod

    got = cli_mod._build_parser().parse_args(
        ["lintester", "x", "--max-lane-tokens", "500", "--max-lane-cost", "2.5"])
    assert (got.max_lane_tokens, got.max_lane_cost) == (500, 2.5)
    quota = orch_cli._quota(got)
    assert (quota.max_tokens, quota.max_cost_usd) == (500, 2.5)


def test_a_negative_ceiling_is_clamped_rather_than_refused():
    """`_limits` makes the same choice: a typo costs the budget, not the run."""
    quota = orch_cli._quota(_args(max_lane_tokens=-5, max_lane_cost=-1.0))
    assert (quota.max_tokens, quota.max_cost_usd) == (0, 0.0)


def test_the_report_says_unavailable_rather_than_printing_a_confident_zero(capsys):
    """DISCRIMINATING. `0 token(s)` here would read as a measured fact."""
    led = orch_cli._ledger(_args(max_lane_tokens=100))
    orch_cli._print_quota(led)
    out = capsys.readouterr().out
    assert "lane quota: 100 token(s)" in out
    assert "usage: unavailable" in out
    assert "0 token(s)," not in out


def test_the_report_names_what_was_spent_and_what_was_refused(capsys):
    led = orch_cli._ledger(_args(max_lane_tokens=50, max_lane_cost=1.0))
    guarded = led.guard(lambda entry, lane: {"tokens": 80, "cost_usd": 0.5})
    guarded(_entry("T-1"), _lane(2))
    guarded(_entry("T-2"), _lane(2))
    orch_cli._print_quota(led)
    out = capsys.readouterr().out
    assert "lane quota: 50 token(s), $1" in out
    assert "lane 2: 80 token(s), $0.5" in out
    assert "refused T-2 on lane 2" in out
    assert "usage: unavailable" not in out

    report = orch_cli._quota_report(led)
    assert report["measured"] is True
    assert report["max_tokens"] == 50
    assert report["lanes"]["2"] == {"tokens": 80, "cost_usd": 0.5}
    assert report["refused"][0]["task_id"] == "T-2"
    json.dumps(report)                                   # the `--json` shape is JSON


def test_nothing_is_printed_for_a_run_that_asked_for_no_ceiling(capsys):
    orch_cli._print_quota(None)
    assert capsys.readouterr().out == ""


def test_a_refused_lane_is_recorded_where_hive_rules_can_see_it(monkeypatch):
    """`record_anomaly` is the existing `tool_failed` door; a parallel record
    only this package could read would be the worse choice."""
    from mindhive.orchestrator import resilience as res_mod

    seen = []
    monkeypatch.setattr(res_mod, "record_anomaly",
                        lambda key, sig, detail="", project=None:
                        seen.append((key, sig, detail, project)) or True)
    led = orch_cli._ledger(_args(max_lane_tokens=1))
    guarded = led.guard(lambda entry, lane: {"tokens": 5})
    guarded(_entry("T-1"), _lane(3))
    guarded(_entry("T-2"), _lane(3))
    guarded(_entry("T-3"), _lane(3))
    orch_cli._record_quota(led, project="p")

    assert len(seen) == 1, "one anomaly per LANE, not per refused unit"
    key, signature, detail, project = seen[0]
    assert (key, signature, project) == ("lane-3", "lane_quota_exceeded", "p")
    assert "2 unit(s) refused" in detail and "T-2" in detail and "T-3" in detail


def test_a_run_that_refused_nothing_records_nothing(monkeypatch):
    from mindhive.orchestrator import resilience as res_mod

    seen = []
    monkeypatch.setattr(res_mod, "record_anomaly",
                        lambda *a, **k: seen.append(a) or True)
    orch_cli._record_quota(orch_cli._ledger(_args(max_lane_tokens=5)), project="p")
    orch_cli._record_quota(None, project="p")
    assert seen == []


# --- the refusal message is read by a person ---------------------------------------

def test_the_token_refusal_names_the_spend_then_the_ceiling():
    """By VALUE, in order. A substring check (`"1001" in reason and "1000" in
    reason`) holds whichever operand is printed where, so swapping them --
    reporting the ceiling as the spend and the spend as the ceiling -- is
    silent, and the operator reads a number that says the opposite of what
    happened."""
    v = brk.LaneQuota(max_tokens=1000).check(brk.LaneUsage(tokens=1001))
    assert v.allowed is False
    assert v.reason == "kuota lane terlampaui: 1001 token melebihi batas 1000"


def test_the_cost_refusal_names_the_spend_then_the_ceiling():
    """`"2.5" in reason` is also a substring of "2.51", so the cost axis is
    pinned the same way."""
    v = brk.LaneQuota(max_cost_usd=2.5).check(brk.LaneUsage(cost_usd=2.51))
    assert v.allowed is False
    assert v.reason == ("kuota lane terlampaui: biaya 2.51 USD "
                        "melebihi batas 2.5 USD")
