# tests/test_mindnet_phases.py
"""MN-1 evidence descriptors, ref containment, and the two-surface
``mindnet_phase`` contract.

Three properties carry this file:

1. **The legend has exactly one home.** Every phase has a descriptor, no
   descriptor invents a phase, and (the grep guard at the bottom) no synonym of
   a phase name appears anywhere in ``src/mindhive/``.
2. **Containment, not prefix.** ``docs/prds/../../../etc/passwd`` passes a
   ``startswith`` test — there is a test here that PROVES it does, so the
   containment check is never quietly downgraded back to one.
3. **``verified_by`` is derived, never accepted.** The append surface refuses a
   payload carrying it; the writer surface refuses one lacking it. That pair is
   the entire basis of the honesty rule.
"""
import re
import shutil
from pathlib import Path

import pytest

from mindhive import __main__ as cli_mod, events, paths
from mindhive.mindnet import phases as p_mod
from mindhive.mindnet import tasks as t_mod


# --- helpers -------------------------------------------------------------------

def _payload(action="enter", task_id="mn1-u3", **kw):
    return {"action": action, "task_id": task_id, **kw}


def _satisfy(**kw):
    base = {"action": "satisfy", "task_id": "mn1-u3", "phase": "prd",
            "epoch": 0,
            "evidence": {"kind": "file", "ref": "docs/prds/x-prd.md",
                         "result": "ok"}}
    base.update(kw)
    return base


# --- the descriptors ------------------------------------------------------------

def test_every_phase_has_an_evidence_descriptor():
    missing = [ph for ph in t_mod.PHASES if p_mod.descriptor(ph) is None]
    assert missing == [], f"phases with no exit criterion: {missing}"


def test_no_descriptor_names_a_phase_outside_the_legend():
    """The other direction, which is the one that rots: a descriptor for a phase
    that was renamed or removed keeps working locally and silently gates
    nothing."""
    stray = sorted(set(p_mod.PHASE_EVIDENCE) - set(t_mod.PHASES))
    assert stray == [], f"descriptors for non-phases: {stray}"


def test_the_legend_is_imported_not_redefined():
    """Invariant 10 — one legend, one home. A second tuple here would drift
    silently, because each copy looks correct in isolation."""
    assert p_mod.PHASES is t_mod.PHASES


def test_file_phases_require_their_stated_directories():
    assert p_mod.PHASE_EVIDENCE["brainstorm"]["required_dir"] == \
        ("docs/superpowers/specs/",)
    assert p_mod.PHASE_EVIDENCE["plan"]["required_dir"] == \
        ("docs/plans/", "docs/superpowers/plans/")
    assert p_mod.PHASE_EVIDENCE["prd"]["required_dir"] == ("docs/prds/",)
    for ph in ("brainstorm", "plan", "prd"):
        assert p_mod.PHASE_EVIDENCE[ph]["kind"] == "file"


def test_required_dir_is_always_a_tuple():
    """One shape to reason about — never "a string here, a tuple there", which
    is how a caller ends up iterating the characters of a path."""
    for ph, desc in p_mod.PHASE_EVIDENCE.items():
        assert isinstance(desc["required_dir"], tuple), ph
        assert all(isinstance(d, str) for d in desc["required_dir"]), ph


def test_brainstorm_has_a_directory_at_all():
    """It had none, so any non-empty file satisfied it — including the
    machine's own `mindnet/state.json` sidecar."""
    assert p_mod.PHASE_EVIDENCE["brainstorm"]["required_dir"]


def test_non_file_phases_carry_no_directory():
    for ph, desc in p_mod.PHASE_EVIDENCE.items():
        if desc["kind"] != "file":
            assert desc["required_dir"] == (), ph


def test_descriptor_kinds_and_verifiers_come_from_the_declared_sets():
    for ph, desc in p_mod.PHASE_EVIDENCE.items():
        assert desc["kind"] in p_mod.EVIDENCE_KINDS, ph
        assert desc["verified_by"] in p_mod.VERIFIERS, ph


def test_the_two_reviewed_audits_are_honestly_agent_reported():
    """KTD17. No review event type is registered and `hive review clear`
    appends nothing, so there is no artifact Python could read — claiming
    `python` here would be exactly the lie KTD2 exists to prevent."""
    assert p_mod.PHASE_EVIDENCE["audit-code"]["verified_by"] == "agent"
    assert p_mod.PHASE_EVIDENCE["audit-system"]["verified_by"] == "agent"


def test_descriptor_is_total_on_an_unknown_phase():
    assert p_mod.descriptor("audit_bug") is None
    assert p_mod.descriptor(None) is None


# --- ref containment -------------------------------------------------------------

TRAVERSAL = "docs/prds/../../../etc/passwd"


def test_ref_inside_the_required_directory_resolves(tmp_path):
    """The discriminating half: containment refuses escapes only — a legitimate
    ref still resolves to an absolute path the re-stat can use."""
    got, why = p_mod.resolve_ref("prd", "docs/prds/2026-07-25-x-prd.md",
                                 tmp_path)
    assert why is None
    assert got == (tmp_path / "docs" / "prds" / "2026-07-25-x-prd.md").resolve()


def test_traversal_ref_is_refused_by_containment(tmp_path):
    got, why = p_mod.resolve_ref("prd", TRAVERSAL, tmp_path)
    assert got is None
    assert "resolves outside" in why and "docs/prds" in why


def test_a_naive_prefix_test_would_have_passed_the_traversal_ref(tmp_path):
    """Proves the refused case is a REAL bug and not a strawman: the joined
    path literally starts with the required directory. If this assertion ever
    fails the containment test above has stopped discriminating."""
    joined = str(Path(tmp_path, TRAVERSAL))
    required = str(Path(tmp_path, "docs/prds"))
    assert joined.startswith(required), "the prefix test used to pass this"
    assert p_mod.resolve_ref("prd", TRAVERSAL, tmp_path)[0] is None


def test_ref_outside_a_worktree_is_refused(tmp_path):
    """`paths.worktree_root()` answers None off-repo. Resolving against the cwd
    instead would accept a file from an unrelated directory as evidence."""
    got, why = p_mod.resolve_ref("prd", "docs/prds/x-prd.md", None)
    assert got is None and "worktree" in why


def test_absolute_ref_is_refused(tmp_path):
    abs_ref = str((tmp_path / "docs" / "prds" / "x-prd.md").resolve())
    got, why = p_mod.resolve_ref("prd", abs_ref, tmp_path)
    assert got is None and "worktree-root-relative" in why


def test_ref_for_a_phase_that_takes_no_file_evidence_is_refused(tmp_path):
    got, why = p_mod.resolve_ref("execution", "docs/prds/x-prd.md", tmp_path)
    assert got is None and "not a file ref" in why


def test_ref_for_an_unknown_phase_is_refused(tmp_path):
    got, why = p_mod.resolve_ref("pr_green", "docs/prds/x-prd.md", tmp_path)
    assert got is None and "not a phase" in why


def test_empty_and_oversized_refs_are_refused(tmp_path):
    assert p_mod.resolve_ref("prd", "  ", tmp_path)[0] is None
    huge = "docs/prds/" + "x" * p_mod.MAX_REF_CHARS
    got, why = p_mod.resolve_ref("prd", huge, tmp_path)
    assert got is None and "exceeds" in why


def test_the_required_directory_itself_is_not_file_evidence(tmp_path):
    got, why = p_mod.resolve_ref("prd", "docs/prds", tmp_path)
    assert got is None and "must be a file inside" in why


@pytest.mark.parametrize("phase,ref", [
    ("prd", "docs/prds-archive/x.md"),
    ("brainstorm", "docs/superpowers/specs-archive/x.md"),
    # One per entry of the two-directory phase: a widened list must widen by
    # exactly the directories named, not by everything that shares their
    # spelling.
    ("plan", "docs/plans-archive/x.md"),
    ("plan", "docs/superpowers/plans-archive/x.md"),
])
def test_a_sibling_directory_sharing_a_name_prefix_is_refused(
        phase, ref, tmp_path):
    """`docs/plans-archive/x.md` shares a string prefix with `docs/plans` and
    is a different directory — the other half of prefix-vs-containment."""
    got, why = p_mod.resolve_ref(phase, ref, tmp_path)
    assert got is None and "resolves outside" in why


# --- the `plan` phase has two legal homes -----------------------------------------
# The repo genuinely carries both: the superpowers workflow wrote
# `docs/superpowers/plans/`, `ce-plan` writes `docs/plans/`. MN-1's own plan is
# in the latter, so a single-directory rule would have made this machine unable
# to validate the document that specified it.

MN1_PLAN = "docs/plans/2026-07-25-001-feat-mn1-phase-spine-plan.md"


def test_plan_accepts_the_ce_plan_directory_pinned_to_mn1s_own_plan():
    """Pinned to the real file on purpose: this exact ref is the case a
    single-directory descriptor would have refused."""
    if shutil.which("git") is None:                # pragma: no cover
        pytest.skip("git is not on PATH")
    root = paths.worktree_root(Path(__file__).resolve().parent)
    assert root is not None
    assert (root / MN1_PLAN).is_file(), (
        f"MN-1's own plan is no longer at {MN1_PLAN} — update this ref, and "
        f"check whether the `plan` descriptor still names the right dirs")
    got, why = p_mod.resolve_ref("plan", MN1_PLAN, root)
    assert why is None
    assert got == (root / MN1_PLAN).resolve()


def test_plan_also_accepts_the_superpowers_directory(tmp_path):
    got, why = p_mod.resolve_ref(
        "plan", "docs/superpowers/plans/2026-06-24-hive-loop.md", tmp_path)
    assert why is None
    assert got == (tmp_path / "docs" / "superpowers" / "plans"
                   / "2026-06-24-hive-loop.md").resolve()


def test_plan_does_not_accept_any_docs_directory(tmp_path):
    """Widening to two named directories must not become "anything under
    docs/" — the containment check still runs per entry."""
    for ref in ("docs/prds/x-prd.md",
                "docs/superpowers/specs/x-design.md",
                "docs/adr/0005-x.md",
                "README.md"):
        got, why = p_mod.resolve_ref("plan", ref, tmp_path)
        assert got is None, ref
        assert "resolves outside" in why


def test_traversal_out_of_a_two_directory_phase_is_still_refused(tmp_path):
    got, why = p_mod.resolve_ref("plan", "docs/plans/../../../etc/passwd",
                                 tmp_path)
    assert got is None and "resolves outside" in why
    # Both legal directories are named in the refusal, so the operator can see
    # what would have been accepted.
    assert "docs/plans/" in why and "docs/superpowers/plans/" in why


# --- the two contract surfaces -----------------------------------------------------

def test_append_surface_rejects_a_payload_carrying_verified_by():
    with pytest.raises(ValueError) as e:
        p_mod.validate_payload(_satisfy(verified_by="python"))
    assert "verified_by is derived" in str(e.value)


def test_append_surface_rejects_verified_by_even_when_it_says_human():
    """Any value at all: the field itself is the attack surface, not one of its
    spellings."""
    for value in ("python", "agent", "human", "wizard"):
        with pytest.raises(ValueError):
            p_mod.validate_payload(_satisfy(verified_by=value))


def test_writer_surface_rejects_a_payload_lacking_verified_by():
    with pytest.raises(ValueError) as e:
        p_mod.validate_stored(_satisfy())
    assert "verified_by" in str(e.value)


def test_writer_surface_rejects_a_bogus_verified_by():
    with pytest.raises(ValueError):
        p_mod.validate_stored(_satisfy(verified_by="vibes"))


def test_writer_surface_accepts_each_declared_verifier():
    for who in p_mod.VERIFIERS:
        p_mod.validate_stored(_satisfy(verified_by=who))


BAD_PAYLOADS = {
    "not an object": "just a string",
    "unknown action": _payload(action="teleport", phase="prd"),
    "unknown phase": _payload(phase="audit_bug"),
    "bad task_id": _payload(task_id="MN1 U3!", phase="prd"),
    "missing task_id": {"action": "enter", "phase": "prd"},
    "phase-less enter": _payload(action="enter"),
    "unknown evidence kind": _satisfy(
        evidence={"kind": "vibes", "ref": "docs/prds/x-prd.md"}),
    "evidence is not an object": _satisfy(evidence="a file somewhere"),
    "bad evidence result": _satisfy(
        evidence={"kind": "file", "ref": "docs/prds/x-prd.md",
                  "result": "maybe"}),
    "file evidence with no ref": _satisfy(evidence={"kind": "file"}),
    "satisfy with no epoch": {k: v for k, v in _satisfy().items()
                              if k != "epoch"},
    "satisfy with no evidence": {k: v for k, v in _satisfy().items()
                                 if k != "evidence"},
    "boolean epoch": _satisfy(epoch=True),
    "negative epoch": _satisfy(epoch=-1),
    "non-integer epoch": _satisfy(epoch="1"),
    "fall_back with no detail": _payload(action="fall_back", phase="execution"),
    "fall_back with blank detail": _payload(action="fall_back",
                                            phase="execution", detail="   "),
    "oversized detail": _payload(action="enter", phase="prd",
                                 detail="x" * (p_mod.MAX_DETAIL_CHARS + 1)),
    "oversized evidence ref": _satisfy(
        evidence={"kind": "file", "ref": "x" * (p_mod.MAX_REF_CHARS + 1)}),
}


@pytest.mark.parametrize("name", sorted(BAD_PAYLOADS))
def test_validate_payload_rejects(name):
    with pytest.raises(ValueError):
        p_mod.validate_payload(BAD_PAYLOADS[name])


@pytest.mark.parametrize("name", sorted(BAD_PAYLOADS))
def test_validate_stored_rejects_the_same_shapes(name):
    """The writer surface is the append surface plus a requirement, never
    minus one — a shape refused at the door must not be writable from inside."""
    bad = BAD_PAYLOADS[name]
    if isinstance(bad, dict):
        bad = dict(bad, verified_by="python")
    with pytest.raises(ValueError):
        p_mod.validate_stored(bad)


GOOD_PAYLOADS = {
    "declare": _payload(action="declare", detail="MN-1 phase spine"),
    "enter": _payload(action="enter", phase="plan", epoch=0),
    "satisfy": _satisfy(),
    "satisfy at a later epoch": _satisfy(
        phase="audit-bug", epoch=3,
        evidence={"kind": "command", "ref": "runs/mn-mn1-u3-1/out.txt",
                  "result": "fail", "detail": "2 failed"}),
    "fall_back": _payload(action="fall_back", phase="execution", epoch=1,
                          detail="audit-bug: 2 tests failed in test_cli.py"),
}


@pytest.mark.parametrize("name", sorted(GOOD_PAYLOADS))
def test_validate_payload_accepts_a_well_formed_payload(name):
    p_mod.validate_payload(GOOD_PAYLOADS[name])


def test_declare_needs_no_phase():
    """A task's ENTRY phase is derived by tasks.fold (the gate, or `execution`
    for a bypassed task) — a caller-named entry phase is the teleport MN-2 just
    stopped accepting."""
    p_mod.validate_payload(_payload(action="declare"))


# --- the registered contract, end to end -------------------------------------------

def test_mindnet_phase_has_a_registered_contract_row():
    assert "mindnet_phase" in events._PAYLOAD_CONTRACTS
    module, hint = events._PAYLOAD_CONTRACTS["mindnet_phase"]
    assert module == ".mindnet.phases" and "hive mindnet" in hint


def test_validate_typed_payload_dispatches_to_phases():
    with pytest.raises(ValueError) as e:
        events.validate_typed_payload("mindnet_phase", {"action": "teleport"})
    assert "hive mindnet" in str(e.value)          # the corrective hint


def test_cli_append_mindnet_phase_malformed_exits_2(capsys):
    bad = '{"action": "satisfy", "task_id": "mn1-u3", "phase": "prd"}'
    assert cli_mod.main(["append", "mindnet_phase", bad, "--json"]) == 2
    err = capsys.readouterr().err
    assert "epoch" in err                          # the validator's own reason
    assert "hive mindnet" in err                   # the corrective hint
    assert events.read_all() == []                 # nothing entered the chain


def test_cli_append_mindnet_phase_cannot_self_attest(capsys):
    """The headline: `hive append` is the untrusted surface, so it may not be
    the thing that says whether to trust it."""
    bad = ('{"action": "satisfy", "task_id": "mn1-u3", "phase": "prd", '
           '"epoch": 0, "evidence": {"kind": "file", '
           '"ref": "docs/prds/x-prd.md", "result": "ok"}, '
           '"verified_by": "python"}')
    assert cli_mod.main(["append", "mindnet_phase", bad, "--json"]) == 2
    assert "verified_by is derived" in capsys.readouterr().err
    assert events.read_all() == []


def test_cli_append_mindnet_phase_wellformed_appends():
    good = '{"action": "enter", "task_id": "mn1-u3", "phase": "plan"}'
    assert cli_mod.main(["append", "mindnet_phase", good, "--json"]) == 0
    evs = events.read_all()
    assert len(evs) == 1 and evs[0].type == "mindnet_phase"
    assert evs[0].payload["phase"] == "plan"


# --- paths.worktree_root ------------------------------------------------------------

def test_worktree_root_finds_this_checkout():
    if shutil.which("git") is None:                # pragma: no cover
        pytest.skip("git is not on PATH")
    here = Path(__file__).resolve().parent
    root = paths.worktree_root(here)
    assert root is not None
    assert (root / "src" / "mindhive" / "mindnet" / "phases.py").is_file()


def test_worktree_root_is_none_outside_a_worktree(tmp_path):
    assert paths.worktree_root(tmp_path) is None


def test_worktree_root_never_raises(monkeypatch):
    """Total: a missing git, a timeout, or a permission error answers None."""
    def boom(*a, **kw):
        raise OSError("no git here")
    monkeypatch.setattr(paths.subprocess, "run", boom)
    assert paths.worktree_root(Path(__file__).parent) is None


def test_worktree_root_is_not_detect_project():
    """Two axes, deliberately different resolutions: evidence paths are
    per-checkout, the ledger namespace is per-repo (SOP 10). Conflating them is
    the bug KTD7 exists to prevent."""
    assert paths.worktree_root is not paths.detect_project


# --- invariant 10: no phase-name synonym anywhere in src/mindhive -------------------
# Precision over reach. Two narrow scanners instead of one broad one:
#
#   * the DISTINCTIVE compound names with any separator (`audit_bug`,
#     `auditbug`, `audit bug`, `pr_green`, `prgreen`) — these are unambiguous:
#     no English sentence contains them by accident;
#   * a small set of word-choice synonyms, matched ONLY as a whole quoted
#     string literal, so `"execute"` (a phase name in disguise) is caught while
#     "the runner will execute the script" is left alone.
#
# Bare `plan` and `prd` are deliberately NOT scanned. They are ordinary English
# that appears in legitimate prose on nearly every page of this codebase, and a
# guard that cries wolf gets deleted within a week.
#
# Case-SENSITIVE on purpose: prose legitimately capitalises at a sentence start
# or in a heading, and the drift that actually breaks a string comparison is a
# different SPELLING, not a different case. (U8 did fix the one real instance —
# `mindnet/__init__.py`'s slice table used to write the terminal phase with a
# capitalised prefix — but the scanner is not what caught it, and widening it to
# case-insensitive would start flagging every capitalised sentence opener.)

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
    """Every phase-name synonym in *text*, as a sorted list of the offending
    spellings. Empty for source that uses the legend verbatim."""
    allowed = set(t_mod.PHASES)
    found = {m.group(0) for m in _COMPOUND_RE.finditer(text)} - allowed
    found |= {m.group(1) for m in _LITERAL_RE.finditer(text)
              if m.group(1) in _LITERAL_SYNONYMS}
    return sorted(found)


@pytest.mark.parametrize("planted", [
    'PHASES = ("brainstorm", "audit_bug")',
    'if rec["phase"] == "pr_green":',
    'TERMINAL = "prgreen"',
    "GATE = 'auditbug'",
    'phase = "execute"',
    'return "bug-audit"',
    '# the audit bug gate',
])
def test_the_grep_guard_catches_a_planted_synonym(planted):
    """The canary. Without it a passing invariant-10 test proves only that the
    scanner found nothing — including if it can find nothing."""
    assert _phase_synonyms(planted), f"scanner missed a synonym: {planted!r}"


def test_the_grep_guard_passes_the_real_spellings_and_ordinary_prose():
    ok = ('PHASES = ("brainstorm", "plan", "prd", "execution",\n'
          '          "audit-bug", "audit-code", "audit-system", "pr-green")\n'
          '# the plan gate; the runner will execute the validation script and\n'
          '# the PRD lands in docs/prds/. See the design doc for the audit\n'
          '# ordering: bug audit, then code review, then the system pass.\n')
    assert _phase_synonyms(ok) == []


def test_no_phase_name_synonym_appears_in_src():
    src = Path(p_mod.__file__).resolve().parents[1]
    assert src.name == "mindhive", src
    offenders = {}
    for f in sorted(src.rglob("*.py")):
        bad = _phase_synonyms(f.read_text(encoding="utf-8"))
        if bad:
            offenders[f.relative_to(src).as_posix()] = bad
    assert offenders == {}, (
        "phase-name synonyms in src/mindhive — invariant 10 says the strings "
        f"in tasks.PHASES are used verbatim everywhere: {offenders}")
