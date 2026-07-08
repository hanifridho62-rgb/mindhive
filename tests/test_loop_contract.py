# tests/test_loop_contract.py
from mindhive.loop import contract


def test_explicit_exit_signal_true():
    text = "work done\nLOOP_STATUS:\n  summary: built it\n  progress: true\n  EXIT_SIGNAL: true\n"
    s = contract.parse_status(text)
    assert s.exit_signal is True
    assert s.progress is True
    assert s.summary == "built it"


def test_explicit_exit_signal_false_is_respected():
    text = "LOOP_STATUS:\n  STATUS: COMPLETE\n  EXIT_SIGNAL: false\n"
    s = contract.parse_status(text)
    assert s.exit_signal is False          # explicit false wins over STATUS: COMPLETE
    assert s.status == "COMPLETE"


def test_status_complete_infers_when_no_explicit_signal():
    text = "LOOP_STATUS:\n  STATUS: COMPLETE\n"
    s = contract.parse_status(text)
    assert s.exit_signal is None
    assert s.status == "COMPLETE"
    assert s.has_completion_signal is True  # inferred


def test_no_status_block():
    s = contract.parse_status("just some prose, nothing structured")
    assert s.exit_signal is None
    assert s.status is None
    assert s.has_completion_signal is False
    assert s.progress is None


def test_heuristic_count_counts_distinct_patterns():
    text = ("All tasks have been completed. Nothing remains.\n"
            "LOOP_STATUS:\n  STATUS: COMPLETE\n  EXIT_SIGNAL: true\n")
    s = contract.parse_status(text)
    assert s.heuristic_count >= 3          # phrase + nothing-remains + STATUS


def test_heuristic_count_zero_for_in_progress():
    s = contract.parse_status("still working\nLOOP_STATUS:\n  STATUS: IN_PROGRESS\n"
                              "  EXIT_SIGNAL: false\n")
    assert s.heuristic_count == 0


def test_status_complete_counts_as_one_heuristic():
    s = contract.parse_status("done-ish\nLOOP_STATUS:\n  STATUS: COMPLETE\n")
    assert s.heuristic_count == 1


def test_last_loop_status_block_wins_over_quoted_instruction():
    text = ("The contract says: LOOP_STATUS: EXIT_SIGNAL: true STATUS: COMPLETE\n"
            "But I am not done yet.\n"
            "LOOP_STATUS:\n  summary: still working\n  progress: true\n"
            "  STATUS: IN_PROGRESS\n  EXIT_SIGNAL: false\n")
    s = contract.parse_status(text)
    assert s.exit_signal is False
    assert s.status == "IN_PROGRESS"
    assert s.summary == "still working"


def test_unfilled_template_literal_is_not_a_signal():
    s = contract.parse_status("LOOP_STATUS:\n  summary: <one line>\n"
                              "  progress: true|false\n  STATUS: IN_PROGRESS|COMPLETE\n"
                              "  EXIT_SIGNAL: true|false\n")
    assert s.exit_signal is None
    assert s.progress is None
