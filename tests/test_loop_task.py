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


def _texts(items):
    return [i.text for i in items]


def test_plan_items_groups_done_planned_not_yet():
    items = task.plan_items(FIX_PLAN)
    assert _texts(items["done"]) == ["Write the README"]
    assert _texts(items["planned"]) == ["Build the parser", "Add tests"]
    assert _texts(items["not_yet"]) == ["Add a GUI"]
    # The groups are unchanged; the values gained a type. An untagged plan parses
    # with every marker flag false, which is what keeps historical files identical.
    assert not any(i.is_new for group in items.values() for i in group)


def test_plan_items_empty_plan():
    assert task.plan_items("") == {"done": [], "planned": [], "not_yet": []}


# --- the (new) provenance marker ----------------------------------------------
# One marker, spelled one way, on both surfaces. The checklist file is the loop's
# serialisation of the same fact a roadmap step carries as a boolean.

def test_the_marker_matches_the_one_roadmap_renders():
    # The two live in different modules so `loop` stays free of a `roadmap`
    # import. This is what stops them drifting apart.
    from mindhive import roadmap
    assert task._NEW_MARKER.match(roadmap.NEW_MARKER)
    assert task.parse_item(roadmap.NEW_MARKER + "a thing").is_new


def test_an_untagged_item_parses_unchanged():
    item = task.parse_item("Build the parser")
    assert item.text == "Build the parser"
    assert item.is_new is False


def test_a_tagged_item_parses_with_the_marker_stripped():
    item = task.parse_item("(new) Add tests: cover the fold")
    assert item.is_new is True
    assert item.text == "Add tests: cover the fold"


def test_marker_matching_is_case_insensitive_and_space_tolerant():
    for raw in ("(new) x", "(NEW) x", "(New)   x", "( new ) x"):
        parsed = task.parse_item(raw)
        assert parsed.is_new is True, raw
        assert parsed.text == "x", raw


def test_a_marker_that_is_not_at_the_start_is_left_in_the_text():
    item = task.parse_item("Refactor the (new) parser")
    assert item.is_new is False
    assert item.text == "Refactor the (new) parser"


def test_a_description_containing_parentheses_is_untouched():
    item = task.parse_item("(new) Fix score(x) so it returns an int")
    assert item.text == "Fix score(x) so it returns an int"


def test_a_completed_injected_item_keeps_its_marker():
    # Provenance is permanent: the operator must still be able to see that the
    # loop added this to its own plan after the work is done.
    items = task.plan_items("- [x] (new) mined work\n- [ ] planned work\n")
    assert items["done"][0].is_new is True
    assert items["done"][0].status == "done"
    assert items["planned"][0].is_new is False


def test_a_tagged_future_item_still_lands_in_not_yet():
    items = task.plan_items(
        "- [ ] required\n## Future / Optional\n- [ ] (new) someday\n")
    assert [i.text for i in items["planned"]] == ["required"]
    assert items["not_yet"][0].is_new is True


def test_required_remaining_count_ignores_markers():
    # The count is the forcing mechanism and the marker is provenance. If the
    # marker changed the count, injected work would be double-counted.
    plain = "- [ ] one\n- [ ] two\n## Future\n- [ ] later\n"
    tagged = "- [ ] (new) one\n- [ ] (new) two\n## Future\n- [ ] (new) later\n"
    assert (task.required_remaining_count(plain)
            == task.required_remaining_count(tagged) == 2)


def test_as_record_is_the_json_safe_shape():
    record = task.parse_item("(new) mined").as_record()
    assert record == {"text": "mined", "new": True, "status": "pending"}
