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


def _texts(group):
    return [i["text"] for i in group]


def test_plan_items_groups_done_planned_not_yet():
    items = task.plan_items(FIX_PLAN)
    assert _texts(items["done"]) == ["Write the README"]
    assert _texts(items["planned"]) == ["Build the parser", "Add tests"]
    assert _texts(items["not_yet"]) == ["Add a GUI"]
    # Untagged items keep their text intact and are not marked.
    assert all(i["is_new"] is False for g in items.values() for i in g)


# --- the (new) provenance marker ---------------------------------------------

def test_the_loop_marker_is_the_roadmap_marker():
    # "One marker" is only checkable if the two literals are pinned together;
    # otherwise a rename on one side silently stops the other from parsing.
    from mindhive import roadmap
    assert roadmap.NEW_MARKER.strip() == task.NEW_MARKER


def test_an_injected_item_parses_to_a_flag_and_clean_text():
    items = task.plan_items("- [ ] (new) Bootstrap the suite\n")
    assert items["planned"] == [{"text": "Bootstrap the suite", "is_new": True}]


def test_the_marker_is_case_insensitive_and_tolerates_no_space():
    assert task.item("(NEW)Do it") == {"text": "Do it", "is_new": True}


def test_only_a_leading_marker_counts():
    # A line whose own wording mentions "(new)" later is describing its work.
    assert task.item("Add a (new) parser") == {"text": "Add a (new) parser",
                                               "is_new": False}


def test_a_checked_injected_item_keeps_the_flag():
    # Provenance, not recency: completing the work does not un-inject it.
    items = task.plan_items("- [x] (new) Bootstrap the suite\n")
    assert items["done"][0]["is_new"] is True


def test_an_injected_item_under_future_is_still_deferred():
    plan = "- [ ] real\n## Future / Optional\n- [ ] (new) later\n"
    items = task.plan_items(plan)
    assert items["not_yet"][0] == {"text": "later", "is_new": True}
    assert _texts(items["planned"]) == ["real"]


def test_required_remaining_does_not_care_about_markers():
    # The COUNT is what forces the loop to continue; the marker is provenance.
    plain = "- [ ] a\n- [ ] b\n"
    tagged = "- [ ] (new) a\n- [ ] (new) b\n"
    assert (task.required_remaining_count(plain)
            == task.required_remaining_count(tagged) == 2)


def test_plan_items_empty_plan():
    assert task.plan_items("") == {"done": [], "planned": [], "not_yet": []}
