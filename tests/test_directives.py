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


# --- §0: absolute honesty is the core rule -----------------------------------

def test_honesty_rule_leads_the_document():
    """Position is the claim. The user asked for a CORE rule, and a rule
    buried under Role is not core — so §0 must exist AND precede §1."""
    text = directives.directives()
    assert "## 0." in text, "no §0 section"
    assert text.index("## 0.") < text.index("## 1. Role")


def test_honesty_rule_covers_the_four_distinct_failures():
    """Four failures that fail differently, so four separate assertions.
    Dropping any one leaves a hole the remaining three do not cover."""
    text = directives.directives()
    for phrase in [
        "Never claim work is done that is not done",   # claimed completion
        "Never report a result you did not observe",   # unobserved results
        "Never accept a premise you did not verify",   # unverified premises
        "Ambition never softens a claim",              # the pairing the user asked for
    ]:
        assert phrase in text, f"missing honesty directive: {phrase!r}"


def test_honesty_rule_states_the_ambition_claim_split():
    """The user granted ambition and demanded honesty in one sentence. The
    rule has to say which one governs what, or the licence reads as a licence
    to overstate."""
    text = directives.directives()
    assert "ATTEMPT" in text
    assert "REPORT" in text


def test_honesty_rule_names_a_command_that_actually_exists(tmp_path, capsys):
    """The rule tells the assistant to verify premises with `hive premise
    check`. A core honesty directive that names a nonexistent command is
    itself the failure it prohibits, so the CLI is asked to prove it — by
    running, not by being listed in a help string."""
    assert "hive premise check" in directives.directives()
    doc = tmp_path / "plain.md"
    doc.write_text("nothing checkable in here", encoding="utf-8")
    assert cli_mod.main(["premise", "check", "--file", str(doc)]) == 0
    assert "no checkable claims" in capsys.readouterr().out.lower()


def test_existing_sections_survive_the_insertion():
    """§0 is prepended; nothing else may be clobbered on the way in."""
    text = directives.directives()
    for heading in [
        "## 1. Role",
        "## 2. Using injected context",
        "## 2b. Search the record BEFORE asking the user",
        "## 3. Cold start",
        "## 4. Plugins and Skills",
        "## 5. Failure handling",
        "## 6. Writing to memory",
        "## 7. Omitted by design",
        "## 8. Methodology",
        "## 9. Reply style",
    ]:
        assert heading in text, f"clobbered section: {heading!r}"
