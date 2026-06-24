# tests/test_loop_task.py
from mindhive.loop import task

FIX_PLAN = """# Fix plan
- [ ] Build the parser
- [x] Write the README
- [ ] Add tests

## Future / Optional
- [ ] Add a GUI
"""


def test_required_remaining_counts_only_unchecked_above_future():
    assert task.required_remaining_count(FIX_PLAN) == 2  # parser + tests


def test_required_remaining_zero_when_all_checked():
    plan = "- [x] a\n- [x] b\n## Future / Optional\n- [ ] later\n"
    assert task.required_remaining_count(plan) == 0


def test_load_tasks_reads_files(tmp_path):
    d = tmp_path / ".hive-loop"
    d.mkdir()
    (d / "PROMPT.md").write_text("goal", encoding="utf-8")
    (d / "fix_plan.md").write_text(FIX_PLAN, encoding="utf-8")
    (d / "AGENT.md").write_text("run: pytest", encoding="utf-8")
    t = task.load_tasks(d)
    assert t.prompt == "goal"
    assert "run: pytest" in t.agent
    assert task.required_remaining_count(t.fix_plan) == 2


def test_load_tasks_missing_files_are_empty(tmp_path):
    d = tmp_path / ".hive-loop"
    d.mkdir()
    t = task.load_tasks(d)
    assert t.prompt == ""
    assert t.fix_plan == ""
