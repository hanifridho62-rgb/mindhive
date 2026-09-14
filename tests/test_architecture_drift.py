# tests/test_architecture_drift.py
"""Guards against the two drifts this codebase has actually suffered.

Not a style checker. Each assertion here corresponds to a divergence that
already happened, silently, between two places that must agree:

1. **The merge exemptions.** DATA_MODEL.md §6.3 says a registry/audit type must
   be exempted in *both* `consolidate.NEVER_CONSOLIDATE` and
   `dedupe.NEVER_COLLAPSE`. "Both" is easy to half-do: `learning` and
   `state_set` were exempt from the dreaming pass and still collapsible in the
   display path for months, because nothing compared the lists.

2. **The phase legend.** Invariant 10 says `tasks.PHASES` is used verbatim in
   code, UI *and* docs. The src side is covered by
   `test_mindnet_phases.test_no_phase_name_synonym_appears_in_src`; this file
   covers the docs, by PARSING the legend out of the Markdown and comparing it
   to the tuple rather than eyeballing it.

## Why the doc guard is narrow on purpose

A guard that cries wolf gets deleted within a week, so this one refuses to
guess. It scans exactly three documents, and inside them it looks only at
things that are unambiguously a legend:

* the fenced ``PHASES = (...)`` literal — parsed with `ast`, not a regex over
  prose;
* the numbered legend TABLE rows — anchored on ``| <digit> | `<name>` |``;
* explicit ENUMERATIONS running from `brainstorm` to `pr-green` with nothing
  between them but phase names and separators;
* the distinctive compound spellings (`audit_bug`, `prgreen`, `audit code`) and
  a small set of quoted word-synonyms.

Bare `plan` and `prd` are never scanned. They are ordinary English on nearly
every page here. Every parser below is paired with a CANARY that plants a bad
legend and asserts the parser rejects it — without that, a green run would only
prove the parser found nothing, including if it can find nothing at all.
"""
import ast
import re
from pathlib import Path

import pytest

from mindhive import chain, consolidate, dedupe, events, pgmirror, projector
from mindhive.mindnet import phases as p_mod
from mindhive.mindnet import tasks as t_mod

REPO = Path(__file__).resolve().parents[1]
DATA_MODEL = REPO / "docs" / "DATA_MODEL.md"
SPEC_DIR = REPO / "docs" / "superpowers" / "specs"
MN1_SPEC = SPEC_DIR / "2026-07-25-mn1-phase-spine-design.md"
MN_DECOMP = SPEC_DIR / "2026-07-25-mindnetwork-architecture-decomposition.md"

# The MN-1 PLAN (`docs/plans/…-mn1-phase-spine-plan.md`) is deliberately NOT in
# this list. It is a planning artifact — a record of what was intended at a
# moment in time, including a mermaid state diagram whose node labels are not
# prose — and freezing a historical document against the current legend would
# make every future legend change rewrite history. The specs are normative; the
# plan is not.
LEGEND_DOCS = (DATA_MODEL, MN1_SPEC, MN_DECOMP)


def test_the_documents_this_file_guards_actually_exist():
    """A renamed doc must fail loudly, not silently disable four tests."""
    missing = [str(p.relative_to(REPO)) for p in LEGEND_DOCS if not p.is_file()]
    assert missing == [], f"guarded documents are missing: {missing}"


# --- 1. the merge exemptions -------------------------------------------------
#
# Scoped to types that can ACTUALLY reach `dedupe.collapse`, which is the only
# honest scope for an agreement assertion: exempting a type from a function it
# never reaches proves nothing. Two callers collapse, and the wider one decides
# the scope — `dashboard_get._get_memory` filters on `events.MEMORY_VIEW_TYPES`
# and feeds every surviving row straight into `collapse`, so every SEMANTIC type
# reaches it, `state_set` included (`briefing._collapsed_entries` passes a
# strict subset).

COLLAPSE_REACHABLE = events.MEMORY_VIEW_TYPES


def test_state_set_really_does_reach_collapse():
    """The scoping premise, asserted rather than assumed. If `state_set` ever
    stops reaching `collapse`, exempting it there becomes dead configuration
    and this file should say so instead of quietly keeping it."""
    assert "state_set" in COLLAPSE_REACHABLE
    assert "state_set" in events.SEMANTIC_TYPES
    assert "state_set" not in projector.LEDGER_TYPES, (
        "state_set is excluded from the LEDGER but still rides the Memory view")


def test_the_two_merge_exemption_lists_agree():
    """The guard the plan called for. It failed on TWO pre-existing omissions,
    not one: `learning` and `state_set` were exempt from consolidation and NOT
    from display dedupe."""
    consolidated_out = set(consolidate.NEVER_CONSOLIDATE)
    collapsed_out = set(dedupe.NEVER_COLLAPSE)
    assert consolidated_out == collapsed_out, (
        "merge exemptions disagree (DATA_MODEL.md §6.3 requires BOTH). "
        f"exempt from consolidation only: {sorted(consolidated_out - collapsed_out)}; "
        f"exempt from display dedupe only: {sorted(collapsed_out - consolidated_out)}")


def test_every_exempt_type_is_a_registered_type():
    """A typo in either list exempts nothing at all and reports no error —
    `collapse` just never sees that string. Pin them to the registry."""
    for name in set(consolidate.NEVER_CONSOLIDATE) | set(dedupe.NEVER_COLLAPSE):
        assert name in events.VALID_TYPES, f"{name!r} is not a registered type"


def test_every_exempt_type_can_reach_the_function_it_is_exempt_from():
    for name in dedupe.NEVER_COLLAPSE:
        assert name in COLLAPSE_REACHABLE, (
            f"{name!r} is exempted from dedupe.collapse but cannot reach it")


def test_consolidate_types_is_exactly_the_semantic_set_minus_the_exemptions():
    assert consolidate.CONSOLIDATE_TYPES == (
        events.SEMANTIC_TYPES - set(consolidate.NEVER_CONSOLIDATE))


def test_the_agreement_guard_would_fail_on_a_planted_divergence():
    """The canary. Compares the same way the real test does, on a list that is
    short by one — so a future refactor cannot make the comparison vacuous."""
    planted = set(dedupe.NEVER_COLLAPSE) - {"learning"}
    assert planted != set(consolidate.NEVER_CONSOLIDATE)


# --- the exemptions, as behaviour rather than configuration ------------------

def _item(type_, text):
    return {"type": type_, "project": None, "text": text}


def test_two_identical_learnings_no_longer_collapse():
    """THE BEHAVIOUR CHANGE this unit lands. Before it, two Self-Reliant PRDs
    with near-identical wording merged into one row in the dashboard Memory
    view and the briefing timeline — each PRD is a distinct build artifact, and
    two PRDs about one goal read alike while prescribing different builds."""
    text = "rewrote the retry loop to back off exponentially on 429"
    reps = dedupe.collapse([_item("learning", text), _item("learning", text)])
    assert len(reps) == 2
    assert [r["similar_count"] for r in reps] == [0, 0]


def test_two_identical_caveats_still_collapse():
    """The discriminating half: the exemption is a list, not a switch that
    turned collapsing off."""
    text = "never redirect the live dashboard with PYTHONPATH"
    reps = dedupe.collapse([_item("caveat", text), _item("caveat", text)])
    assert len(reps) == 1
    assert reps[0]["similar_count"] == 1


def test_state_set_rows_survive_collapse_individually():
    """state_set carries no `text`, so it never matched anything anyway — the
    exemption closes the list-agreement gap without changing behaviour. Asserted
    so that stays true if state_set ever gains a text field."""
    rows = [{"type": "state_set", "project": None, "text": ""} for _ in range(3)]
    assert len(dedupe.collapse(rows)) == 3


# --- 2. the phase legend, parsed out of the Markdown -------------------------

_PHASES = t_mod.PHASES


def _flat(text):
    """Whitespace-collapsed, backtick-stripped text. Legends wrap across lines
    in Markdown tables and code fences; matching line-by-line would miss them."""
    return " ".join(text.replace("`", " ").split())


# A legend ENUMERATION: starts at `brainstorm`, ends at `pr-green`, and contains
# NOTHING between them but lowercase phase-shaped words and separators. The
# character class is the precision: an uppercase letter, a quote, a colon or a
# bracket ends the candidate, so ordinary prose that happens to mention both
# words cannot masquerade as a legend.
_ENUM_RE = re.compile(r"\bbrainstorm\b[a-z0-9 ,|>→-]*?\bpr-green\b")
_SPLIT_RE = re.compile(r"\s*(?:\||,|→|->)\s*")


def _legend_enumerations(text):
    out = []
    for m in _ENUM_RE.finditer(_flat(text)):
        out.append(tuple(p for p in _SPLIT_RE.split(m.group(0).strip()) if p))
    return out


# The numbered legend TABLE: `| 0 | `brainstorm` | … |`.
_TABLE_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*`([a-z][a-z0-9-]*)`\s*\|",
                           re.MULTILINE)


def _legend_table(text):
    rows = [(int(n), name) for n, name in _TABLE_ROW_RE.findall(text)]
    return tuple(name for _n, name in sorted(rows))


# The fenced Python tuple — parsed, not regex-compared.
_PHASES_LITERAL_RE = re.compile(r"PHASES\s*=\s*(\([^)]*\))")


def _legend_literals(text):
    out = []
    for m in _PHASES_LITERAL_RE.finditer(text):
        try:
            value = ast.literal_eval(m.group(1))
        except (SyntaxError, ValueError):
            out.append(("<unparseable>",))
            continue
        out.append(tuple(value) if isinstance(value, tuple) else ("<not-a-tuple>",))
    return out


def test_the_legend_in_tasks_is_the_one_the_evidence_table_covers():
    """Both halves of the src-side legend, so the doc guard below is comparing
    against something that is itself internally consistent."""
    assert tuple(p_mod.PHASE_EVIDENCE) == _PHASES
    assert p_mod.PHASES is _PHASES


@pytest.mark.parametrize("doc", LEGEND_DOCS, ids=lambda p: p.name)
def test_every_legend_enumeration_in_the_docs_matches_phases(doc):
    """Every prose run a doc carries must match the legend — but a doc is not
    required to carry a prose run at all.

    The MN-1 spec states the legend as a quoted `PHASES = (...)` literal and a
    numbered table, and `_ENUM_RE` deliberately stops at a quote character so
    ordinary prose cannot masquerade as a legend. Demanding a prose run from
    every document would therefore fail a document that is not merely correct
    but MORE precisely checkable than one, and the two tests below already
    guard both of those forms.

    What is refused is a document carrying the legend in NO machine-readable
    form: that is how a legend silently stops being checkable, which is the
    failure this whole file exists to prevent."""
    text = doc.read_text(encoding="utf-8")
    found = _legend_enumerations(text)
    assert found or _legend_literals(text) or _legend_table(text), (
        f"{doc.name} carries the legend in no machine-readable form — no prose "
        "brainstorm-to-pr-green run, no `PHASES = (...)` literal, no numbered "
        "table. Either the legend was reworded out of checkable shape, or this "
        "parser stopped working. Both need a human.")
    for run in found:
        assert run == _PHASES, (
            f"{doc.name} enumerates the phases as {run}, but tasks.PHASES is "
            f"{_PHASES} (invariant 10: verbatim in code, UI and docs)")


def test_the_mn1_spec_python_literal_matches_phases():
    found = _legend_literals(MN1_SPEC.read_text(encoding="utf-8"))
    assert found, "the MN-1 spec no longer carries a `PHASES = (...)` block"
    for value in found:
        assert value == _PHASES


def test_the_mn1_spec_legend_table_matches_phases():
    assert _legend_table(MN1_SPEC.read_text(encoding="utf-8")) == _PHASES


@pytest.mark.parametrize("planted", [
    "brainstorm | plan | prd | execution | audit-bug | audit-code | pr-green",
    "brainstorm, plan, prd, execution, audit-bug, audit-code, audit-system, "
    "audit-docs, pr-green",
    "brainstorm -> plan -> prd -> execute -> audit-bug -> audit-code -> "
    "audit-system -> pr-green",
    "brainstorm | prd | plan | execution | audit-bug | audit-code | "
    "audit-system | pr-green",
])
def test_the_enumeration_parser_rejects_a_planted_bad_legend(planted):
    """Canary: a dropped phase, an invented one, a synonym, and a reorder."""
    found = _legend_enumerations(planted)
    assert found, f"parser found no enumeration at all in {planted!r}"
    assert all(run != _PHASES for run in found)


def test_the_enumeration_parser_accepts_the_real_legend_in_three_spellings():
    for spelling in (" | ".join(_PHASES), ", ".join(_PHASES),
                     " → ".join(_PHASES)):
        assert _legend_enumerations(spelling) == [_PHASES]


def test_the_enumeration_parser_ignores_ordinary_prose():
    """Precision, asserted. Prose that mentions both endpoints in one paragraph
    is not a legend, and must not be read as a malformed one."""
    prose = ("The brainstorm gate is where intent is explored; much later, "
             "once CI has something to chew on, pr-green closes the task.")
    assert _legend_enumerations(prose) == []


def test_the_table_parser_rejects_a_planted_reorder():
    planted = "| 0 | `plan` | x |\n| 1 | `brainstorm` | y |\n"
    assert _legend_table(planted) == ("plan", "brainstorm") != _PHASES


def test_the_literal_parser_rejects_a_planted_edit():
    planted = 'PHASES = ("brainstorm", "plan", "prd", "execution")'
    assert _legend_literals(planted) == [("brainstorm", "plan", "prd",
                                          "execution")]


# --- 3. no phase-name SYNONYM in the normative docs --------------------------
#
# Same two-scanner approach as `test_mindnet_phases`, for the same reason:
# distinctive compounds are unambiguous, bare `plan`/`prd` are not. Applied to
# prose, one extra rule matters — the compound scan stays case-SENSITIVE, so a
# heading that capitalises a sentence is left alone while `audit_bug` is not.

_COMPOUND_RE = re.compile(r"audit[-_ ]?(?:bug|code|system)|pr[-_ ]?green")
_LITERAL_RE = re.compile(r"""["']([A-Za-z][A-Za-z0-9_-]*)["']""")
_LITERAL_SYNONYMS = {
    "execute", "executing", "executed", "exec",     # execution
    "brainstorming", "brainstorms",                 # brainstorm
    "planning",                                     # plan
    "bug-audit", "code-audit", "system-audit",      # the three audits
    "green-pr", "pr-check", "ci-green",             # pr-green
}


def _phase_synonyms(text):
    allowed = set(_PHASES)
    found = {m.group(0) for m in _COMPOUND_RE.finditer(text)} - allowed
    found |= {m.group(1) for m in _LITERAL_RE.finditer(text)
              if m.group(1) in _LITERAL_SYNONYMS}
    return sorted(found)


@pytest.mark.parametrize("planted", [
    "the audit_bug gate",
    "PHASES ends at prgreen",
    "run the audit code pass",
    'the phase is "execute"',
    "we call it 'bug-audit' internally",
])
def test_the_doc_synonym_scanner_catches_a_planted_synonym(planted):
    assert _phase_synonyms(planted), f"scanner missed a synonym: {planted!r}"


def test_the_doc_synonym_scanner_passes_the_real_spellings():
    ok = ("brainstorm -> plan -> prd -> execution -> audit-bug -> audit-code "
          "-> audit-system -> pr-green. The plan file lands in docs/plans/ and "
          "the PRD in docs/prds/; the runner will execute the script.")
    assert _phase_synonyms(ok) == []


@pytest.mark.parametrize("doc", LEGEND_DOCS, ids=lambda p: p.name)
def test_no_phase_name_synonym_in_the_normative_docs(doc):
    bad = _phase_synonyms(doc.read_text(encoding="utf-8"))
    assert bad == [], (
        f"phase-name synonyms in {doc.relative_to(REPO).as_posix()} — "
        f"invariant 10 says the tasks.PHASES strings are used verbatim in "
        f"docs too: {bad}")


# --- 4. every text-less semantic type has a LEDGER body renderer -------------
#
# `build_ledger_entries` defaults to `payload.get("text", "")`. For a structured
# payload that means an empty body: invisible to keyword recall (which searches
# the projected markdown) AND filename-colliding, because the entry name hashes
# `ts + type + body`. Two same-second events then write the same file.

_TEXTLESS_SEMANTIC_TYPES = ("sop", "roadmap", "mindnet_task", "mindnet_phase")


@pytest.mark.parametrize("type_name", _TEXTLESS_SEMANTIC_TYPES)
def test_every_structured_semantic_type_has_a_ledger_body(type_name):
    src = Path(projector.__file__).read_text(encoding="utf-8")
    assert type_name in events.SEMANTIC_TYPES
    assert f'ev.type == "{type_name}"' in src, (
        f"{type_name} has no branch in projector.build_ledger_entries, so it "
        "projects an empty LEDGER body and same-second events collide")


# --- 5. one decision about where a ledger row ends ---------------------------
#
# Drift 3, and like the two above it already happened. Four readers each carried
# their own `str.splitlines()`. Because `canonical_json` writes with
# `ensure_ascii=False`, a raw U+0085/U+2028/U+2029 in a payload then split an
# INTACT row in two: `read_all` dropped the event, `corrupt_rows` invented two
# torn rows, and every later line index in that segment shifted by one --
# `seg_line`, the mirror's stored natural key, among them. `chain._scan` split
# correctly and so disagreed with all four: on a three-row segment it sealed one
# event while recording a count of three, and the two rows that fell out could
# be rewritten with `hive verify` still green.
#
# The fix was to make the split ONE function, `events._segment_parts`. This
# guard is what keeps it one, and it pins the RULE rather than an example of it.
# A fixture test cannot do that job: nothing compares the chain's count with the
# mirror's index at runtime, each is self-consistent on its own, and a forward
# edit applied to one splitter and not the other leaves the whole suite green
# while re-opening exactly that hole.
#
# SCOPE, narrow on purpose in the spirit of this file: a module is a ledger
# reader if it asks for the segment directory, i.e. its source mentions
# `events_dir(`. That is the set that could fork, and it catches a SIXTH reader
# on the day someone writes one. Modules that split Claude Code TRANSCRIPTS
# (`harvest`, `failures`, `taskview`, `rotation.occupancy`) are a different file
# family with a different contract, and are deliberately out of scope.

_SPLIT_OWNER = "_segment_parts"
_BS = chr(92)      # the planted sources below must carry a LITERAL backslash-n,
_Q = chr(34)       # not a real newline: ast.parse must see SOURCE, not text


def _line_splitters(src_text):
    """Every `.splitlines()` and every `.split()` on a literal containing a
    newline escape, as (lineno, what, enclosing def). Parsed with `ast`, so a
    mention inside a docstring or a comment -- of which these modules have many,
    explaining this very rule -- is not a hit."""
    tree = ast.parse(src_text)
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                owner.setdefault(id(n), fn.name)
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        what = None
        if n.func.attr == "splitlines":
            what = "splitlines()"
        elif n.func.attr == "split" and n.args:
            a = n.args[0]
            if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                    and chr(10) in a.value):
                what = "split(%r)" % a.value
        if what:
            out.append((n.lineno, what, owner.get(id(n), "<module>")))
    return out


def _ledger_reader_modules():
    root = Path(events.__file__).parent
    return sorted(f for f in root.rglob("*.py")
                  if "events_dir(" in f.read_text(encoding="utf-8")
                  and f.name != "paths.py")


def test_there_are_ledger_reader_modules_to_guard():
    """Without this, a glob that silently matched nothing would let the guard
    below pass by looking nowhere."""
    mods = [f.name for f in _ledger_reader_modules()]
    assert {"events.py", "chain.py", "pgmirror.py"} <= set(mods), mods


def test_only_one_function_decides_where_a_ledger_row_ends():
    bad = []
    for f in _ledger_reader_modules():
        for lineno, what, fn in _line_splitters(f.read_text(encoding="utf-8")):
            if fn != _SPLIT_OWNER:
                bad.append("%s:%d %s in %s()" % (f.name, lineno, what, fn))
    assert not bad, (
        "a ledger-segment reader splits lines outside events._segment_parts, "
        "which is how the chain and the mirror last disagreed about seg_line: "
        + "; ".join(bad))


def test_the_split_owner_is_real_and_is_what_the_readers_call():
    """The rule above goes vacuous if `_segment_parts` stops existing, stops
    splitting, or stops being what the readers go through."""
    assert hasattr(events, _SPLIT_OWNER)
    assert [w for _, w, fn in _line_splitters(
        Path(events.__file__).read_text(encoding="utf-8"))
        if fn == _SPLIT_OWNER], "the split owner no longer splits anything"
    assert "events._segment_parts(" in Path(
        chain.__file__).read_text(encoding="utf-8"), (
        "chain._scan no longer calls the shared splitter")
    # `scan_segments` reads through `_segment_events`, the ordered row stream it
    # shares with `read_all` and `read_types`, and that stream reads through
    # `_segment_lines`. Both links are asserted: either one dropped forks the
    # mirror's split, or its order, from the file readers'.
    assert "_events._segment_events(" in Path(
        pgmirror.__file__).read_text(encoding="utf-8"), (
        "pgmirror.scan_segments no longer goes through the shared generator")
    import inspect
    assert "_segment_lines(" in inspect.getsource(events._segment_events), (
        "events._segment_events no longer goes through the shared splitter")


@pytest.mark.parametrize("planted", [
    "def _scan(seg):{nl}    return seg.read_text().splitlines(){nl}",
    "def _scan(seg):{nl}    return seg.read_text().split({q}{bs}n{q}){nl}",
    "def helper(t):{nl}    return t.split({q}{bs}r{bs}n{q}){nl}",
], ids=["splitlines", "split-lf", "split-crlf"])
def test_the_splitter_scanner_catches_a_planted_reader(planted):
    """CANARY. A green run above must mean the scanner looked and found
    nothing, not that it cannot find anything at all."""
    src = planted.format(nl=chr(10), q=_Q, bs=_BS)
    hits = _line_splitters(src)
    assert hits and all(fn != _SPLIT_OWNER for _, _, fn in hits), (src, hits)


def test_the_splitter_scanner_ignores_prose_about_splitting():
    """The modules it scans are full of docstrings naming `splitlines()` --
    that is the whole subject of the rule. A scanner that flagged those would be
    deleted within a week."""
    prose = ("def f():{nl}    {q}{q}{q}Never use splitlines() here, nor "
             "x.split({q}{bs}n{q}) -- see DATA_MODEL.{q}{q}{q}{nl}    return 1{nl}")
    assert _line_splitters(prose.format(nl=chr(10), q=_Q, bs=_BS)) == []
