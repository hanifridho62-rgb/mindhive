# tests/test_caveman_skill.py
"""`caveman` is a VENDORED third-party skill (JuliusBrussee/caveman, MIT): an
ultra-short reply style shipped inside the mindhive plugin as a typeable
`/caveman` COMMAND plus a model-invoked SKILL. Guard the three things a careless
edit would break — the activation shape, the MIT notice that redistribution
requires, and the language-preservation clause that keeps an Indonesian question
getting an Indonesian answer."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT / "skills" / "caveman" / "SKILL.md"
_LICENSE = _ROOT / "skills" / "caveman" / "LICENSE"
_COMMAND = _ROOT / "commands" / "caveman.md"


def test_caveman_skill_exists_and_is_named():
    assert _SKILL.exists(), "caveman skill is missing"
    text = _SKILL.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: caveman" in text.split("---", 2)[1]


def test_caveman_slash_command_exists():
    # The typeable `/caveman` entry point — what the user literally types.
    assert _COMMAND.exists(), "caveman slash command is missing"
    text = _COMMAND.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "$ARGUMENTS" in text                 # the level the user passes


def test_every_intensity_level_is_documented():
    text = _SKILL.read_text(encoding="utf-8").lower()
    for level in ("lite", "full", "ultra",
                  "wenyan-lite", "wenyan-full", "wenyan-ultra"):
        assert level in text, f"caveman skill missing the {level} level"


def test_style_compresses_without_switching_language():
    # The reason this skill is usable in this project at all: it shortens the
    # prose but answers in whatever language the user wrote. Lose this clause
    # and every Indonesian question starts coming back in English.
    for f in (_SKILL, _COMMAND):
        text = f.read_text(encoding="utf-8").lower()
        assert "compress the style, not the language" in text, \
            f"{f.name} dropped the language-preservation clause"


def test_technical_content_is_never_compressed():
    # Compression is for prose only — code and errors stay byte-exact, which is
    # what makes the style safe to leave on during engineering work.
    text = _SKILL.read_text(encoding="utf-8").lower()
    assert "code blocks unchanged" in text
    assert "errors quoted exact" in text


def test_style_yields_to_safety_critical_output():
    # Terse fragments are the wrong shape for a destructive-action warning.
    text = _SKILL.read_text(encoding="utf-8").lower()
    assert "auto-clarity" in text
    assert "security warnings" in text
    assert "irreversible action confirmations" in text


def test_mit_notice_travels_with_the_vendored_copy():
    # MIT requires the copyright + permission notice ship with any substantial
    # portion. Deleting this file makes our redistribution non-compliant.
    assert _LICENSE.exists(), "vendored caveman is missing its MIT LICENSE"
    lic = _LICENSE.read_text(encoding="utf-8")
    assert "MIT License" in lic
    assert "Julius Brussee" in lic
    assert "The above copyright notice" in lic


def test_skill_records_its_upstream_provenance():
    # Without the upstream pointer a vendored copy silently becomes a fork
    # nobody can update.
    text = _SKILL.read_text(encoding="utf-8")
    assert "github.com/JuliusBrussee/caveman" in text


# --- the MindHive addendum: standing default + the teaching carve-out ---------
#
# Line breaks are irrelevant to every assertion below, so each reads a
# whitespace-normalized copy. Matching raw text would make these tests fail on a
# pure re-wrap, which is noise, not a regression.

def _flat(path):
    return " ".join(path.read_text(encoding="utf-8").lower().split())


def test_addendum_makes_the_style_standing_not_opt_in():
    text = _flat(_SKILL)
    assert "mindhive addendum" in text
    assert "standing style, not a mode the user switches on" in text
    assert "does not lapse after many turns" in text


def test_addendum_names_both_activation_mechanisms():
    # A skill body read once at session start is the instruction that decays;
    # the per-prompt pass is what makes "ACTIVE EVERY RESPONSE" true. If either
    # pointer disappears, the claim above it becomes false.
    text = _flat(_SKILL)
    assert "src/mindhive/style.py" in text
    assert "src/mindhive/directives.py" in text


def test_teaching_carve_out_is_documented_in_both_languages():
    text = _flat(_SKILL)
    assert "teaching carve-out" in text
    for english in ("explain", "teach", "walk me through"):
        assert english in text, english
    for indonesian in ("jelaskan", "ajarkan", "terangkan", "penjelasan"):
        assert indonesian in text, indonesian


def test_thoroughness_is_documented_as_not_a_teaching_request():
    # The distinction the matcher in style.py implements. If the doc and the
    # code disagree about this, the doc is what a future editor will trust.
    text = _flat(_SKILL)
    assert "thoroughness is not teaching" in text
    assert "menyeluruh" in text


def test_command_says_the_style_is_already_on():
    # Telling the user to type /caveman to switch it on would be wrong now.
    text = _flat(_COMMAND)
    assert "already the standing default" in text
    assert "you do not need this command to turn it on" in text


def test_session_directives_establish_the_style_from_message_one():
    # The SessionStart half of the mechanism, read from the live module rather
    # than from a doc — this is what actually reaches the model.
    from mindhive import directives
    text = " ".join(directives.directives().lower().split())
    assert "caveman is the standing default" in text
    assert "compress the style, not the language" in text
    assert "teaching carve-out" in text
    assert "jelaskan" in text and "ajarkan" in text        # bilingual trigger
    assert "irreversible-action confirmations" in text     # still yields to §4/§6
