# tests/test_replenish_fix_plan_sink.py
"""The loop surface's sink, and the forcing mechanism it exists to trip.

The assertion that matters is not "a line was appended" — it is that
`safeguards.should_exit` refuses to exit afterwards. A sink that wrote a
perfectly-formatted line BELOW the Future/Optional heading would pass every
formatting check and force nothing, because `required_remaining_count` stops
counting there.
"""
from mindhive import replenish, replenish_sinks
from mindhive.loop import safeguards, task
from mindhive.loop.contract import parse_status

PLAN = ("# Fix plan\n"
        "- [x] scaffold\n"
        "- [ ] wire the API\n"
        "\n"
        "## Future / Optional\n"
        "- [ ] polish\n")


def _loop_dir(tmp_path, body=PLAN):
    d = tmp_path / ".hive-loop"
    d.mkdir()
    if body is not None:
        (d / "fix_plan.md").write_text(body, encoding="utf-8")
    return d


def _cand(fp="fp-1", title="Bootstrap the suite"):
    return replenish.Candidate(lens="coverage", title=title, fingerprint=fp,
                               evidence=("a real pointer",), severity=True)


def _body(d):
    return (d / "fix_plan.md").read_text(encoding="utf-8")


def test_landed_items_raise_the_required_count(tmp_path):
    d = _loop_dir(tmp_path)
    before = task.required_remaining_count(_body(d))
    placements, dropped = replenish_sinks.fix_plan_sink(d, "run-a")(
        [_cand("fp-1", "one"), _cand("fp-2", "two")])
    assert len(placements) == 2 and dropped == 0
    assert task.required_remaining_count(_body(d)) == before + 2


def test_the_exit_gate_refuses_after_an_injection(tmp_path):
    # The whole feature: the run does not end because the count rose.
    d = _loop_dir(tmp_path)
    status = parse_status("EXIT_SIGNAL: true\nall done, everything is complete, "
                          "the work is finished")
    replenish_sinks.fix_plan_sink(d, "run-a")([_cand()])
    done, reason = safeguards.should_exit(
        status, task.required_remaining_count(_body(d)), json_mode=True)
    assert done is False and reason == "required_items_remain"


def test_items_land_above_the_future_heading(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d, "run-a")([_cand(title="injected")])
    lines = _body(d).splitlines()
    assert lines.index("- [ ] (new) injected") < lines.index(
        "## Future / Optional")
    # Deferred work stays deferred.
    assert task.plan_items(_body(d))["not_yet"] == [{"text": "polish",
                                                     "is_new": False}]


def test_a_plan_with_no_future_heading_appends_at_the_end(tmp_path):
    d = _loop_dir(tmp_path, "# Fix plan\n- [ ] wire the API\n")
    replenish_sinks.fix_plan_sink(d, "run-a")([_cand(title="injected")])
    assert _body(d).splitlines()[-1] == "- [ ] (new) injected"


def test_the_marker_parses_back_out(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d, "run-a")([_cand(title="injected")])
    planned = task.plan_items(_body(d))["planned"]
    assert {"text": "injected", "is_new": True} in planned
    assert {"text": "wire the API", "is_new": False} in planned


def test_a_missing_fix_plan_drops_everything_and_writes_nothing(tmp_path):
    d = _loop_dir(tmp_path, body=None)
    placements, dropped = replenish_sinks.fix_plan_sink(d, "run-a")([_cand()])
    # The file is the run's only source of required work; inventing one would
    # hand the loop a plan nobody wrote.
    assert placements == [] and dropped == 1
    assert not (d / "fix_plan.md").exists()


def test_append_false_computes_the_same_placements_and_writes_nothing(tmp_path):
    d = _loop_dir(tmp_path)
    before = _body(d)
    live = replenish_sinks.fix_plan_sink(d, "run-a", append=False)
    placements, dropped = live([_cand("fp-1", "one"), _cand("fp-2", "two")])
    assert [p.step_index for p in placements] == [1, 2]
    assert dropped == 0
    assert _body(d) == before


def test_placements_index_the_required_list_not_the_line_number(tmp_path):
    # `steps_from_plan_items` orders by required position; a line number would
    # point the audit row at a heading or a blank line.
    d = _loop_dir(tmp_path)
    placements, _ = replenish_sinks.fix_plan_sink(d, "run-a")([_cand()])
    assert placements[0].plan_id == "run-a"
    assert placements[0].step_index == 1        # after "wire the API"


def test_a_title_with_a_newline_cannot_split_the_item(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d, "run-a")(
        [_cand(title="first line\nsecond line")])
    lines = [ln for ln in _body(d).splitlines() if "first line" in ln]
    assert lines == ["- [ ] (new) first line second line"]


def test_a_title_that_is_already_a_checklist_item_is_flattened(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d, "run-a")([_cand(title="- [ ] do it")])
    assert "- [ ] (new) do it" in _body(d)
    assert "- [ ] (new) - [ ]" not in _body(d)


def test_a_title_already_carrying_the_marker_is_not_doubled(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d, "run-a")([_cand(title="(new) do it")])
    assert "- [ ] (new) do it" in _body(d)
    assert "(new) (new)" not in _body(d)


def test_a_blank_title_lands_nothing(tmp_path):
    d = _loop_dir(tmp_path)
    before = _body(d)
    placements, dropped = replenish_sinks.fix_plan_sink(d, "run-a")(
        [_cand(title="   ")])
    assert placements == [] and dropped == 1
    assert _body(d) == before


def test_a_write_failure_reports_everything_unplaced(tmp_path, monkeypatch):
    # Windows: os.replace raises PermissionError on the DESTINATION while the
    # dashboard holds a handle to it. A half-written fix_plan.md would corrupt
    # the run's only source of required work.
    d = _loop_dir(tmp_path)
    before = _body(d)

    def boom(_path, _text):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(replenish_sinks.paths_mod, "write_text_atomic", boom)
    placements, dropped = replenish_sinks.fix_plan_sink(d, "run-a")([_cand()])
    assert placements == [] and dropped == 1
    assert _body(d) == before


def test_a_full_round_through_the_real_sink_lands_and_records(tmp_path):
    d = _loop_dir(tmp_path)
    res = replenish.run_round(
        [], surface="loop", sink=replenish_sinks.fix_plan_sink(d, "run-a"),
        lenses={"l": lambda _e: [_cand()]}, run_id="run-a")
    assert len(res.landed) == 1
    assert task.required_remaining_count(_body(d)) == 2
    from mindhive import events
    row = [e.payload for e in events.read_all()
           if e.type == "work_injected" and e.payload["outcome"] == "landed"][0]
    assert row["run_id"] == "run-a" and row["plan_id"] == "run-a"


def test_the_file_keeps_its_trailing_newline(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d, "run-a")([_cand()])
    assert _body(d).endswith("\n")
