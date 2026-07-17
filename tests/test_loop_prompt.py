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
