# tests/test_loop_estafet.py
"""The baton: a run that stops with work open hands it on intact.

Two things have to survive the hop, and both used to die. The `(new)` flag dies
if the snapshot rebuilds step text without it — the resumed agent then cannot
tell evidence-backed injected work from the plan a human wrote. And a run that
ran out of WALL CLOCK dropped its marker entirely, which is the same unfinished
plan as one that ran out of money.
"""
import pytest

from mindhive import autoresume, events, replenish, roadmap
from mindhive.loop import config, engine
from mindhive.loop.driver import DriverResult, Usage

COMPLETE = ("EXIT_SIGNAL: true\nsummary: done\n"
            "all done — the work is complete and everything is finished")


class FakeDriver:
    def __init__(self, plan_path, rate_limited=False):
        self.plan_path = plan_path
        self.rate_limited = rate_limited

    def run(self, *_a, **_kw):
        body = self.plan_path.read_text(encoding="utf-8")
        self.plan_path.write_text(body.replace("- [ ] ", "- [x] "),
                                  encoding="utf-8")
        return DriverResult(text=COMPLETE, usage=Usage(input_tokens=10),
                            session_id="s1", num_turns=1,
                            rate_limited=self.rate_limited)


def _loop_dir(tmp_path):
    d = tmp_path / "proj" / ".hive-loop"
    d.mkdir(parents=True)
    (d / "PROMPT.md").write_text("# Build the parser\n", encoding="utf-8")
    (d / "fix_plan.md").write_text("# Fix plan\n- [ ] step one\n",
                                   encoding="utf-8")
    (d / "AGENT.md").write_text("run: pytest", encoding="utf-8")
    return d


def _cand(fp="fp-1"):
    return replenish.Candidate(lens="coverage", title="Bootstrap the suite",
                               fingerprint=fp, evidence=("a real pointer",),
                               severity=True)


def _run(tmp_path, *, rate_limited=False, hours=8.0, inject=True,
         jump_after=None):
    """One run. `jump_after` makes the clock leap past `max_hours` once that
    many readings have been taken, which is how the wall-clock exit is reached
    AFTER work has happened — a clock that advances from the first reading trips
    the check before any iteration, and there is nothing to hand on.

    It has to survive TWO iterations, not one. `run_loop` loads the checklist at
    the top of an iteration and evaluates `required_remaining` against that
    copy, so the box the agent ticks in iteration 1 is only seen in iteration 2
    — which is where the completion gate, and therefore the injector, is first
    reached."""
    d = _loop_dir(tmp_path)
    monkey = replenish.LENSES
    replenish.LENSES = {
        "coverage": (lambda _e, root=None: [_cand()]) if inject
        else (lambda _e, root=None: [])}
    base, reads = 1_000_000.0, [0]

    def now():
        reads[0] += 1
        if jump_after is not None and reads[0] > jump_after:
            return base + hours * 3600 + 1
        return base

    try:
        return engine.run_loop(
            project="test", loop_dir=d,
            cfg=config.LoopConfig(max_iterations=5, max_hours=hours,
                                  model="claude-haiku-4-5",
                                  min_completion_heuristics=0,
                                  replenish_dry_rounds=0),
            driver=FakeDriver(d / "fix_plan.md", rate_limited),
            now_fn=now, sleep_fn=lambda _s: None,
            progress_fn=lambda _i: True), d
    finally:
        replenish.LENSES = monkey


def _interrupt(run_id):
    return [e.payload for e in events.read_all()
            if e.type == "roadmap" and e.payload.get("plan_id") == run_id
            and e.payload.get("action") == "interrupt"]


# --- the causes --------------------------------------------------------------

def test_running_out_of_wall_clock_leaves_a_marker(tmp_path):
    # A run that ran out of clock has exactly the same unfinished plan as one
    # that ran out of money; dropping its marker is how injected work silently
    # disappeared at the end of a long run.
    result, _d = _run(tmp_path, jump_after=9)
    assert result["reason"] == "max_hours"
    assert _interrupt(result["run_id"])


def test_max_hours_is_in_the_interrupt_set():
    assert set(engine.USAGE_INTERRUPT_REASONS) == {"api_cap", "budget_cap",
                                                   "max_hours"}


def test_running_out_of_usage_leaves_a_marker(tmp_path):
    result, _d = _run(tmp_path, rate_limited=True)
    assert result["reason"] == "api_cap"
    assert _interrupt(result["run_id"])


def test_a_completed_run_leaves_no_marker(tmp_path):
    result, _d = _run(tmp_path, inject=False)
    assert result["reason"] == "completed"
    assert _interrupt(result["run_id"]) == []


# --- what the baton carries --------------------------------------------------

def test_the_snapshot_keeps_the_injected_flag(tmp_path):
    result, _d = _run(tmp_path, jump_after=9)
    rec = roadmap.get(events.read_all(), result["run_id"])
    injected = [s for s in rec["steps"] if s.get("new")]
    assert injected, "the (new) flag did not survive the snapshot"
    assert injected[0]["text"] == "Bootstrap the suite"


def test_the_interrupt_carries_a_relay_context(tmp_path):
    result, _d = _run(tmp_path, jump_after=9)
    relay = _interrupt(result["run_id"])[0]["relay_context"]
    assert result["run_id"] in relay
    assert "max_hours" in relay
    assert len(relay) <= roadmap.MAX_RELAY_CONTEXT_CHARS


def test_the_relay_context_names_the_injected_steps(tmp_path):
    result, _d = _run(tmp_path, jump_after=9)
    relay = _interrupt(result["run_id"])[0]["relay_context"]
    assert "(new)" in relay and "replenishment injector" in relay


def test_an_oversized_relay_context_is_rejected_not_truncated():
    # Truncation ends mid-sentence in the next session's auto-loaded context,
    # where nobody can tell a deliberate summary from a cut-off one.
    payload = {"action": "interrupt", "plan_id": "p-1", "cause": "api_cap",
               "relay_context": "x" * (roadmap.MAX_RELAY_CONTEXT_CHARS + 1)}
    with pytest.raises(ValueError, match="relay_context"):
        roadmap.validate_payload(payload)


def test_an_interrupt_without_a_relay_context_still_validates():
    # Every historical interrupt predates the field.
    roadmap.validate_payload({"action": "interrupt", "plan_id": "p-1",
                              "cause": "api_cap"})


def test_the_fold_exposes_the_relay_context(tmp_path):
    result, _d = _run(tmp_path, jump_after=9)
    rec = roadmap.get(events.read_all(), result["run_id"])
    assert rec["relay_context"].startswith("Resuming loop run")


def test_the_fix_plan_is_untouched_by_the_snapshot(tmp_path):
    result, d = _run(tmp_path, jump_after=9)
    body = (d / "fix_plan.md").read_text(encoding="utf-8")
    # The resumed agent parses the same file with the same marker.
    assert "(new) Bootstrap the suite" in body
    assert result["reason"] == "max_hours"


def test_a_snapshot_failure_still_closes_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_relay_context",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    result, _d = _run(tmp_path, jump_after=9)
    # A run must never be stranded as "running" in Oversee because a
    # best-effort marker could not be built.
    ends = [e for e in events.read_all() if e.type == "loop_run_end"]
    assert ends and ends[-1].payload["run_id"] == result["run_id"]


# --- the delivered prompt ----------------------------------------------------

def _rec(steps, relay=""):
    payload = {"action": "set", "plan_id": "p-1", "prompt": "Build the parser",
               "title": "Build the parser", "steps": steps}
    roadmap.validate_payload(payload)
    events.append_event("roadmap", payload, ts="2026-08-01T00:00:00Z")
    ipayload = {"action": "interrupt", "plan_id": "p-1", "cause": "api_cap"}
    if relay:
        ipayload["relay_context"] = relay
    roadmap.validate_payload(ipayload)
    events.append_event("roadmap", ipayload, ts="2026-08-02T00:00:00Z")
    return roadmap.get(events.read_all(), "p-1")


def test_the_resume_prompt_carries_the_relay_line():
    rec = _rec([{"text": "a", "status": "pending"}],
               relay="Resuming loop run abc: stopped on api_cap.")
    out = autoresume.resume_prompt(rec)
    assert "**Where it stopped:** Resuming loop run abc" in out


def test_the_resume_prompt_explains_the_new_convention():
    rec = _rec([{"text": "a", "status": "pending", "new": True}])
    out = autoresume.resume_prompt(rec)
    assert "replenishment injector" in out
    assert "(new) a" in out            # and the marker itself still renders


def test_a_session_origin_plan_is_unchanged():
    rec = _rec([{"text": "a", "status": "pending"}])
    out = autoresume.resume_prompt(rec)
    assert "Where it stopped" not in out
    assert "replenishment injector" not in out
    assert "# AUTO-RESUME" in out and "Build the parser" in out
