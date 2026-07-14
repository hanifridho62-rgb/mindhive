# tests/test_directives.py
from mindhive import directives
from mindhive import __main__ as cli_mod


def test_directives_returns_nonempty_string():
    text = directives.directives()
    assert isinstance(text, str)
    assert text.strip()


def test_directives_has_a_recognizable_marker():
    # The assistant must be able to spot these as operating directives.
    assert "MINDHIVE ASSISTANT" in directives.directives()


def test_directives_carry_core_safety_invariants():
    """If a future edit drops a core safety directive, this test fails loud.
    These phrases encode the non-negotiable guardrails from the spec."""
    text = directives.directives().lower()
    for phrase in [
        "confirm with the user first",   # §4 mutate only after confirmation
        "report failures honestly",      # §5 no hidden failures
        "append-only",                   # §6 auditable memory
        "do not fabricate",              # §2 no hallucinated history
    ]:
        assert phrase in text, f"missing core directive: {phrase!r}"


def test_directives_is_stable():
    # Pure constant accessor — repeated calls return identical text.
    assert directives.directives() == directives.directives()


def test_cli_directives_prints_spec(capsys):
    ret = cli_mod.main(["directives"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "MINDHIVE ASSISTANT" in out
    assert "confirm with the user first" in out


def test_directives_prohibit_memory_meta_comments():
    text = directives.directives()
    assert "Memory voice" in text
    assert "based on my memory" in text
    assert "as we discussed" in text
