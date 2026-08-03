# tests/test_loop_prompt.py
from mindhive.loop import prompt
from mindhive.loop.task import TaskFiles


def test_loop_context_includes_briefing_and_contract():
    ctx = prompt.build_loop_context(briefing_text="# BRIEFING\nstate here")
    assert "BRIEFING" in ctx
    assert "LOOP_STATUS:" in ctx          # the contract instruction is appended
    assert "EXIT_SIGNAL" in ctx


def test_task_prompt_includes_goal_plan_and_recall():
    tasks = TaskFiles(prompt="Build the parser", fix_plan="- [ ] parse",
                      agent="run: pytest")
    hits = [{"file": "a.md", "snippet": "earlier we chose regex", "score": 2}]
    p = prompt.build_task_prompt(tasks, hits)
    assert "Build the parser" in p
    assert "- [ ] parse" in p
    assert "run: pytest" in p
    assert "earlier we chose regex" in p


def test_task_prompt_handles_no_recall():
    tasks = TaskFiles(prompt="g", fix_plan="", agent="")
    p = prompt.build_task_prompt(tasks, [])
    assert "g" in p


# --- the rotation handoff's progress delta ------------------------------------
#
# What a fresh session is told the previous slice accomplished. Built ONLY from
# sources that cannot lie — closed roadmap steps, recorded change-log entries,
# satisfied phases. The outgoing agent's own account rides along, but under an
# explicit unverified marker, because this repo already establishes (mindnet/
# agent.py) that an agent's say-so is not evidence that work happened.

def test_delta_renders_step_statuses():
    out = prompt.build_progress_delta(
        steps=[{"text": "wire the gate", "status": "done"},
               {"text": "add the table", "status": "done"},
               {"text": "hook it up", "status": "pending"}])
    assert "wire the gate" in out
    assert "add the table" in out
    assert "hook it up" in out
    assert out.count("[x]") == 2
    assert "[ ]" in out


def test_delta_renders_changes_and_phases():
    out = prompt.build_progress_delta(
        changes=[{"category": "update", "text": "fixed the occupancy formula"}],
        phases=[{"task_id": "mn8-u1", "phase": "tests-green"}])
    assert "fixed the occupancy formula" in out
    assert "mn8-u1" in out and "tests-green" in out


def test_delta_marks_the_agent_note_unverified():
    out = prompt.build_progress_delta(
        steps=[{"text": "s", "status": "pending"}],
        agent_note="I finished everything and it all works")
    assert "I finished everything" in out
    assert "UNVERIFIED" in out.upper()


def test_ledger_status_wins_over_a_contradicting_agent_note():
    # THE load-bearing case for R6. The note claims the step is done; the
    # ledger says pending. The rendered delta must show pending, and must not
    # let the claim read as recorded state.
    out = prompt.build_progress_delta(
        steps=[{"text": "ship the parser", "status": "pending"}],
        agent_note="ship the parser: DONE, verified, tests green")
    lines = out.splitlines()
    step_line = next(ln for ln in lines if "ship the parser" in ln and "[" in ln)
    assert "[ ]" in step_line
    assert "[x]" not in step_line
    # the claim survives, but only below the marker
    assert out.upper().index("UNVERIFIED") < out.index("DONE, verified")


def test_no_agent_note_produces_no_empty_section():
    out = prompt.build_progress_delta(steps=[{"text": "s", "status": "done"}])
    assert "UNVERIFIED" not in out.upper()


def test_delta_is_empty_when_nothing_happened():
    # No section headers for sections with no content — an empty scaffold reads
    # as "we looked and there was nothing", which is a different claim.
    assert prompt.build_progress_delta() == ""


def test_delta_skips_malformed_rows_without_raising():
    out = prompt.build_progress_delta(
        steps=[{"text": "good", "status": "done"}, "not a dict", None,
               {"no_text_key": 1}, {"text": "", "status": "done"}],
        changes=["junk", None, {}],
        phases=[42, {"phase": "x"}])
    assert "good" in out


def test_delta_caps_and_reports_the_truncation():
    # Cap boundary at N and N+1. A silent truncation would let a fresh session
    # believe it had the whole picture.
    n = prompt.MAX_DELTA_ITEMS
    exact = prompt.build_progress_delta(
        steps=[{"text": f"s{i}", "status": "done"} for i in range(n)])
    assert "more" not in exact.lower()

    over = prompt.build_progress_delta(
        steps=[{"text": f"s{i}", "status": "done"} for i in range(n + 1)])
    assert "more" in over.lower()
    assert f"s{n}" not in over


def test_delta_composes_into_the_loop_context():
    ctx = prompt.build_loop_context(
        briefing_text="# BRIEFING",
        progress_delta=prompt.build_progress_delta(
            steps=[{"text": "wire the gate", "status": "done"}]))
    assert "BRIEFING" in ctx
    assert "wire the gate" in ctx
    assert "LOOP_STATUS:" in ctx


def test_loop_context_without_a_delta_is_unchanged():
    # Regression guard: the pre-existing two-argument-free call shape still
    # produces exactly what it did before.
    assert prompt.build_loop_context("# BRIEFING") == prompt.build_loop_context(
        "# BRIEFING", progress_delta="")
