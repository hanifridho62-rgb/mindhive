# tests/test_replenish_fix_plan_sink.py
"""The fix-plan sink: where replenished work lands on the LOOP surface.

The forcing mechanism is the COUNT, not the marker. An unchecked item above the
Future/Optional heading raises `loop.task.required_remaining_count`, and
`loop.safeguards.should_exit` already refuses to exit while any remains — so the
loop continues because of arithmetic, never because a prompt asked it to
(SOP `agentic-loops` §1). The marker is provenance for a reader.

The second thing protected here is that a candidate title cannot forge structure.
Titles are derived from ledger text and file paths, so they are not hostile — but
they are not controlled either, and a title able to invent a second checklist
item would let one candidate refuse the exit forever.
"""
from mindhive import events, replenish, replenish_sinks
from mindhive.loop import safeguards, task as task_mod
from mindhive.loop.contract import parse_status

SCAFFOLD = ("# Fix plan\n"
            "- [x] already done\n"
            "- [ ] the required step\n"
            "\n"
            "## Future / Optional\n"
            "- [ ] a nice-to-have that must not block exit\n")

COMPLETING_REPLY = (
    "Everything is complete and verified; no work remains.\n"
    "LOOP_STATUS:\n  summary: done\n  progress: true\n"
    "  STATUS: COMPLETE\n  EXIT_SIGNAL: true\n")


def _cand(fp="fp-1", title="Run the test suite", lens="evidence-gap",
          evidence=("a real pointer",)):
    return replenish.Candidate(lens=lens, title=title, fingerprint=fp,
                               evidence=evidence)


def _loop_dir(tmp_path, text=SCAFFOLD):
    d = tmp_path / ".hive-loop"
    d.mkdir()
    (d / "fix_plan.md").write_text(text, encoding="utf-8")
    return d


def _plan_text(loop_dir):
    return (loop_dir / "fix_plan.md").read_text(encoding="utf-8")


# --- the count, which is what forces continuation -----------------------------

def test_two_candidates_raise_the_required_count_by_exactly_two(tmp_path):
    d = _loop_dir(tmp_path)
    before = task_mod.required_remaining_count(_plan_text(d))
    sink = replenish_sinks.fix_plan_sink(d, run_id="run-1")
    placements, dropped = sink([_cand("fp-a", "First mined task"),
                                _cand("fp-b", "Second mined task")])
    assert dropped == 0 and len(placements) == 2
    assert task_mod.required_remaining_count(_plan_text(d)) == before + 2


def test_the_exit_gate_lets_a_clean_run_leave_and_refuses_after_injection(tmp_path):
    """The whole feature in one test: the same reply, opposite verdicts.

    Asserting only the refusal would pass even if the gate refused
    unconditionally — the before-state is what makes this discriminating."""
    d = _loop_dir(tmp_path, "# Fix plan\n- [x] all done\n\n## Future\n")
    status = parse_status(COMPLETING_REPLY)

    done_before, _ = safeguards.should_exit(
        status, task_mod.required_remaining_count(_plan_text(d)),
        json_mode=True, min_heuristics=0)
    assert done_before is True          # nothing outstanding: free to leave

    replenish_sinks.fix_plan_sink(d, run_id="run-1")([_cand("fp-a", "Mined work")])

    done_after, reason = safeguards.should_exit(
        status, task_mod.required_remaining_count(_plan_text(d)),
        json_mode=True, min_heuristics=0)
    assert done_after is False
    assert reason == "required_items_remain"


# --- placement ----------------------------------------------------------------

def test_the_line_carries_the_marker_the_parser_reads_back(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d)([_cand("fp-a", "Mined work")])
    planned = task_mod.plan_items(_plan_text(d))["planned"]
    mined = [i for i in planned if i.text.startswith("Mined work")]
    assert len(mined) == 1
    assert mined[0].is_new is True


def test_items_land_above_the_future_heading(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d)([_cand("fp-a", "Mined work")])
    text = _plan_text(d)
    assert text.index("Mined work") < text.index("## Future / Optional")


def test_the_future_items_stay_under_the_heading(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d)([_cand("fp-a", "Mined work")])
    items = task_mod.plan_items(_plan_text(d))
    assert [i.text for i in items["not_yet"]] == [
        "a nice-to-have that must not block exit"]


def test_a_plan_with_no_future_heading_appends_at_the_end(tmp_path):
    d = _loop_dir(tmp_path, "# Fix plan\n- [ ] only step\n")
    replenish_sinks.fix_plan_sink(d)([_cand("fp-a", "Mined work")])
    text = _plan_text(d)
    assert "Mined work" in text
    assert task_mod.required_remaining_count(text) == 2


def test_placement_indices_count_required_items_not_lines(tmp_path):
    # A raw line number would move whenever anything above it was edited.
    d = _loop_dir(tmp_path)
    placements, _dropped = replenish_sinks.fix_plan_sink(d, run_id="run-1")(
        [_cand("fp-a"), _cand("fp-b")])
    assert [p.step_index for p in placements] == [2, 3]
    assert all(p.plan_id == "run-1" for p in placements)


# --- refusing to write --------------------------------------------------------

def test_a_missing_fix_plan_drops_everything_and_writes_nothing(tmp_path):
    # It must not create the file: an absent checklist means this sink was
    # pointed somewhere that is not a loop, and inventing one there would put
    # required work where no run will ever read it.
    d = tmp_path / ".hive-loop"
    d.mkdir()
    placements, dropped = replenish_sinks.fix_plan_sink(d)([_cand("fp-a")])
    assert placements == [] and dropped == 1
    assert not (d / "fix_plan.md").exists()


def test_dropped_candidates_keep_their_fingerprints_unspent(tmp_path):
    d = tmp_path / ".hive-loop"
    d.mkdir()
    res = replenish.run_round([], surface="loop",
                              sink=replenish_sinks.fix_plan_sink(d),
                              lenses={"l": lambda _c: [_cand("fp-a")]})
    assert res.landed == []
    assert "fp-a" not in replenish.history(events.read_all()).fingerprints


def test_append_false_places_without_touching_the_file(tmp_path):
    d = _loop_dir(tmp_path)
    original = (d / "fix_plan.md").read_bytes()
    placements, dropped = replenish_sinks.fix_plan_sink(
        d, run_id="run-1", append=False)([_cand("fp-a"), _cand("fp-b")])
    assert len(placements) == 2 and dropped == 0
    assert (d / "fix_plan.md").read_bytes() == original


def test_an_empty_kept_list_writes_nothing(tmp_path):
    d = _loop_dir(tmp_path)
    original = (d / "fix_plan.md").read_bytes()
    assert replenish_sinks.fix_plan_sink(d)([]) == ([], 0)
    assert (d / "fix_plan.md").read_bytes() == original


# --- a title cannot forge structure -------------------------------------------

def test_a_title_containing_a_checklist_marker_cannot_forge_an_item(tmp_path):
    d = _loop_dir(tmp_path)
    before = task_mod.required_remaining_count(_plan_text(d))
    replenish_sinks.fix_plan_sink(d)(
        [_cand("fp-a", "- [ ] sneaky\n- [ ] sneakier")])
    assert task_mod.required_remaining_count(_plan_text(d)) == before + 1


def test_a_title_containing_a_heading_cannot_move_the_future_boundary(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d)([_cand("fp-a", "## Future\nsneaky")])
    assert len(task_mod.plan_items(_plan_text(d))["not_yet"]) == 1


def test_repeated_markers_are_all_stripped(tmp_path):
    # One pass would leave the second marker intact and still forge an item.
    line = replenish_sinks.fix_plan_line(_cand("fp-a", "- [ ] - [ ] doubly"))
    assert line.count("- [ ]") == 1


def test_an_oversized_title_is_capped_before_it_reaches_the_file(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d)([_cand("fp-a", "t" * 900)])
    longest = max(len(line) for line in _plan_text(d).splitlines())
    assert longest <= replenish_sinks.MAX_LINE_CHARS


# --- the line's own shape -----------------------------------------------------

def test_the_line_names_the_lens_and_its_first_evidence():
    line = replenish_sinks.fix_plan_line(
        _cand("f", "Patch the cause", lens="repair",
              evidence=("signature: edit:not-read", "extra")))
    assert line == ("- [ ] (new) Patch the cause: repair lens "
                    "— signature: edit:not-read")


def test_a_candidate_with_no_evidence_still_produces_a_readable_line():
    line = replenish_sinks.fix_plan_line(
        _cand("f", "Write tests", lens="coverage", evidence=()))
    assert line == "- [ ] (new) Write tests: coverage lens"


def test_a_blank_evidence_item_is_skipped_for_the_why():
    line = replenish_sinks.fix_plan_line(
        _cand("f", "Write tests", lens="coverage", evidence=("   ", "real one")))
    assert "real one" in line


def test_an_untitled_candidate_still_produces_a_valid_item():
    line = replenish_sinks.fix_plan_line(_cand("f", "   ", lens="audit"))
    assert line.startswith("- [ ] (new) (untitled):")


def test_a_trailing_newline_is_preserved(tmp_path):
    d = _loop_dir(tmp_path)
    replenish_sinks.fix_plan_sink(d)([_cand("fp-a")])
    assert _plan_text(d).endswith("\n")


def test_a_file_with_no_trailing_newline_does_not_gain_one(tmp_path):
    d = _loop_dir(tmp_path, "# Fix plan\n- [ ] step")
    replenish_sinks.fix_plan_sink(d)([_cand("fp-a")])
    assert not _plan_text(d).endswith("\n")


def test_the_sink_reuses_the_task_modules_future_regex():
    # A second copy of this regex would diverge the first time the scaffold's
    # wording moved, and the symptom would be injected work landing BELOW the
    # line that makes it non-blocking — an injector that no longer forces
    # anything.
    assert replenish_sinks._FUTURE_HEADING is task_mod._FUTURE_HEADING


def test_the_marker_written_is_the_marker_roadmap_renders():
    from mindhive import roadmap
    assert replenish_sinks._NEW == roadmap.NEW_MARKER
