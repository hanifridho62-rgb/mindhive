# tests/test_sop_golden_rules_doc.py — blueprint P11 (spec §9.3): the rules
# document and the SOP body that is actually registered.
"""`docs/SOP_FIVE_GOLDEN_RULES.md` told the operator to register itself with
`hive sop add --file docs/SOP_FIVE_GOLDEN_RULES.md`. Run for the first time
(2026-09-29) the command was refused: an SOP body is capped at
`sop.MAX_TEXT_CHARS`, because every active SOP auto-loads into every session,
and the document is about three times that. So the registered body is the
compact form, `docs/SOP_FIVE_GOLDEN_RULES_COMPACT.md`, and the long document
points at it.

These tests hold the two files together: the compact form fits the cap and
names the long form; the long form's registration command names the compact
form; the five rule titles are the same in both; and the reason the compact
form exists -- the long form is over the cap -- is pinned, so a future edit
that shrinks the long form under the cap is told the split is no longer
needed.
"""
import re
from pathlib import Path

from mindhive import sop

REPO = Path(__file__).resolve().parents[1]
LONG = REPO / "docs" / "SOP_FIVE_GOLDEN_RULES.md"
COMPACT = REPO / "docs" / "SOP_FIVE_GOLDEN_RULES_COMPACT.md"
SOP_ID = "five-golden-rules"


def _long() -> str:
    return LONG.read_text(encoding="utf-8")


def _compact() -> str:
    return COMPACT.read_text(encoding="utf-8")


def test_the_compact_body_fits_the_sop_cap_and_names_the_long_form():
    body = _compact()
    assert 0 < len(body) <= sop.MAX_TEXT_CHARS, \
        f"{len(body)} chars against a cap of {sop.MAX_TEXT_CHARS}"
    assert "docs/SOP_FIVE_GOLDEN_RULES.md" in body


def test_the_long_form_is_over_the_cap_which_is_why_the_compact_form_exists():
    assert len(_long()) > sop.MAX_TEXT_CHARS, (
        "the long form now fits the cap: register it directly and retire the "
        "compact form")


def test_the_registration_command_names_the_compact_form_and_the_sop_id():
    commands = [line.strip() for line in _long().splitlines()
                if line.strip().startswith("hive sop add ")]
    assert len(commands) == 1, commands
    cmd = commands[0]
    assert f"hive sop add {SOP_ID} " in cmd
    assert "--file docs/SOP_FIVE_GOLDEN_RULES_COMPACT.md" in cmd
    assert "--file docs/SOP_FIVE_GOLDEN_RULES.md" not in cmd
    assert "--no-mandatory" in cmd
    refresh = [line for line in _long().splitlines() if "hive sop update" in line]
    assert refresh and all("SOP_FIVE_GOLDEN_RULES_COMPACT.md" in line for line in refresh)


def test_the_five_rule_titles_are_the_same_in_both_forms():
    headings = re.findall(r"^## Rule (\d) — (.+)$", _long(), flags=re.M)
    assert [n for n, _ in headings] == ["1", "2", "3", "4", "5"]
    compact = _compact()
    for number, title in headings:
        assert f"\n{number}. {title}." in compact, \
            f"rule {number} ({title!r}) is missing or retitled in the compact form"
