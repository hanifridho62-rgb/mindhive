# tests/test_mindnet_optin.py
"""MN-6 U1 — the blast-radius gate: MindNetwork enforcement is OPT-IN.

Written TEST-FIRST. No line of `mindhive/mindnet/optin.py` existed when these
were written; the first run of this file is a collection error, and that is the
red.

## The defect this file exists to close

`enforce.py`'s own docstring states the contract: "Enforcement is always opt-in
and never fires for a signature MindHive has not escalated." MN-4 shipped two
policy checks — `gate:mindnet-phase` and `gate:mindnet-language` — into
`POLICY_CHECKS`, whose members default ON (`evaluate_policies` skips only what
the manifest explicitly disables). The MN-4 comment says so out loud: "the
adoption decision IS the opt-in, so they default ON."

That reasoning holds for `gate:no-verify`. It does not hold here, because the
two mindnet checks are the only members of that tuple whose firing depends on
mutable *projection* state rather than on the tool call alone — and
`paths.detect_project` collapses every git worktree of a repo onto ONE memory
namespace, so ONE `mindnet/state.json` decides for every checkout at once. A
task left `in_progress` at `brainstorm` in any checkout therefore denied writes
in all of them, with nobody having asked for enforcement at all.

## Why a nesting rule, and why the DEEPEST root wins

This repository's linked worktrees live UNDER the main checkout
(`<repo>/.claude/worktrees/<name>`), so plain containment would mean arming the
main checkout silently arms every worktree inside it — the same repo-wide reach,
one level removed. Roots are therefore matched segment-aligned and the DEEPEST
recorded root decides, which makes `--disable` from inside one worktree a real,
local escape from an armed ancestor. Segment alignment (not string prefix) is
the `is_phase_evidence` lesson applied: `.../mindhive-old` shares a character
prefix with `.../mindhive` and is a different tree.

## The P0 lesson, encoded

MN-4's three deadlocks all came from one root cause: guards tested against an
input shape production never emits. `Write`/`Edit` emit ABSOLUTE `file_path`s;
the tests passed relative ones, so the anti-deadlock guard returned False on
every real call and `brainstorm` denied the document that clears `brainstorm`.
Every path in this file is absolute, `test_a_relative_root_is_stored_absolute`
pins the normalization directly, and the pair
`test_the_env_var_forces_dormant_even_when_a_root_is_armed` /
`test_a_nested_disable_beats_an_armed_ancestor` proves the kill switch works
from inside the room it unlocks.
"""
import ast
import inspect
import json
import os
import subprocess
from pathlib import Path

import pytest

from mindhive import paths
from mindhive.mindnet import optin

PROJECT = "test"


# --- fixture builders: valid by default, one thing corrupted per test ----------

def _record(**roots) -> dict:
    """A valid record with *roots* mapping an absolute path -> enabled flag."""
    return {"version": 1,
            "roots": {k: {"enabled": v, "at": "2026-07-29T00:00:00Z",
                          "session": "s1"}
                      for k, v in roots.items()}}


def _write_record(obj) -> Path:
    p = optin.record_path(PROJECT)
    paths.write_text_atomic(
        p, obj if isinstance(obj, str) else json.dumps(obj, indent=2))
    return p


@pytest.fixture
def roots(tmp_path):
    """Two sibling checkouts and one nested inside the first.

    Mirrors the real layout: `main/` is a checkout, `main/.claude/worktrees/wt`
    is a linked worktree INSIDE it, and `other/` is an unrelated checkout."""
    main = tmp_path / "main"
    nested = main / ".claude" / "worktrees" / "wt"
    other = tmp_path / "other"
    for d in (main, nested, other):
        d.mkdir(parents=True)
    # Every path handed to the module is absolute — the shape the tools emit.
    assert main.is_absolute() and nested.is_absolute() and other.is_absolute()
    return {"main": main, "nested": nested, "other": other}


@pytest.fixture(autouse=True)
def no_ambient_env(monkeypatch):
    """The suite must never inherit a real `MINDNETWORK_ENFORCE` from the
    developer's shell — that would flip these assertions invisibly."""
    monkeypatch.delenv(optin.ENV_VAR, raising=False)


# --- the default: dormant ------------------------------------------------------

def test_the_default_is_dormant():
    """The whole point. Nothing on disk, nothing in the environment: the two
    mindnet policies must not be able to deny anything."""
    assert optin.armed(PROJECT) is False


def test_the_dormant_status_carries_the_documented_note():
    st = optin.status(PROJECT)
    assert st["armed"] is False
    assert st["source"] == "default"
    assert st["note"] == optin.DORMANT_NOTE
    assert optin.DORMANT_NOTE == \
        "[mindnet] enforcement dormant: opt-in required"


def test_the_dormant_status_names_no_root():
    assert optin.status(PROJECT)["root"] is None


# --- the environment variable: a session-scoped toggle -------------------------

@pytest.mark.parametrize("value", ["1", "true", "TRUE", "on", "yes", "  1  "])
def test_a_truthy_env_var_arms_enforcement(monkeypatch, value):
    monkeypatch.setenv(optin.ENV_VAR, value)
    st = optin.status(PROJECT)
    assert st["armed"] is True
    assert st["source"] == "env"


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "OFF"])
def test_a_falsy_env_var_is_dormant(monkeypatch, value):
    monkeypatch.setenv(optin.ENV_VAR, value)
    st = optin.status(PROJECT)
    assert st["armed"] is False
    assert st["source"] == "env"


def test_an_empty_env_var_is_not_a_decision(monkeypatch, roots):
    """`MINDNETWORK_ENFORCE=` (exported but blank) is what a shell leaves behind
    when a variable is cleared, and it must not out-rank the persisted opt-in.

    Discriminating on purpose: an implementation testing
    ``ENV_VAR in os.environ`` passes the two tests above and FAILS this one."""
    _write_record(_record(**{str(roots["main"]).replace("\\", "/"): True}))
    monkeypatch.setenv(optin.ENV_VAR, "")
    st = optin.status(PROJECT, cwd=roots["main"])
    assert st["armed"] is True
    assert st["source"] == "file"


@pytest.mark.parametrize("value", ["maybe", "2", "enabled-please"])
def test_an_unrecognized_env_value_is_not_a_decision(monkeypatch, roots, value):
    """Only the documented spellings decide. Anything else falls through rather
    than being guessed at in either direction."""
    _write_record(_record(**{str(roots["main"]).replace("\\", "/"): True}))
    monkeypatch.setenv(optin.ENV_VAR, value)
    assert optin.status(PROJECT, cwd=roots["main"])["source"] == "file"


def test_the_env_var_forces_dormant_even_when_a_root_is_armed(monkeypatch,
                                                              roots):
    """THE kill switch. MN-4's third P0 was a remedy locked inside the room it
    was meant to open; an operator whose repo is frozen must be able to disarm
    it without writing a file, editing a manifest, or leaving the session."""
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    assert optin.armed(PROJECT, cwd=roots["main"]) is True

    monkeypatch.setenv(optin.ENV_VAR, "0")
    st = optin.status(PROJECT, cwd=roots["main"])
    assert st["armed"] is False
    assert st["source"] == "env"


def test_status_reads_the_ambient_environment_by_default(monkeypatch, roots):
    """`env=` is injectable for tests, but the production call passes nothing
    and must still see the real variable."""
    monkeypatch.setenv(optin.ENV_VAR, "1")
    assert optin.status(PROJECT, cwd=roots["other"])["armed"] is True


def test_an_injected_env_mapping_beats_the_ambient_one(monkeypatch, roots):
    monkeypatch.setenv(optin.ENV_VAR, "1")
    st = optin.status(PROJECT, cwd=roots["other"], env={})
    assert st["armed"] is False
    assert st["source"] == "default"


# --- the persisted opt-in: per-checkout ---------------------------------------

def test_enabling_a_root_arms_that_root(roots):
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    st = optin.status(PROJECT, cwd=roots["main"])
    assert st["armed"] is True
    assert st["source"] == "file"
    assert Path(st["root"]) == roots["main"]


def test_enabling_one_checkout_does_not_arm_a_sibling(roots):
    """The blast radius, stated as a test. `detect_project` collapses every
    worktree onto one namespace, so this record is SHARED — arming one checkout
    must still leave every other checkout able to work."""
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    assert optin.armed(PROJECT, cwd=roots["other"]) is False
    assert optin.status(PROJECT, cwd=roots["other"])["note"] == \
        optin.DORMANT_NOTE


def test_a_subdirectory_of_an_armed_root_is_armed(roots):
    """A tool call's cwd is not guaranteed to be the checkout root."""
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    deep = roots["main"] / "src" / "mindhive"
    deep.mkdir(parents=True)
    assert optin.armed(PROJECT, cwd=deep) is True


def test_a_nested_worktree_inherits_an_armed_ancestor(roots):
    """Documents the inherited direction, so the test below is a real contrast
    rather than the only behaviour anyone ever checked."""
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    assert optin.armed(PROJECT, cwd=roots["nested"]) is True


def test_a_nested_disable_beats_an_armed_ancestor(roots):
    """The per-checkout escape. Linked worktrees live UNDER the main checkout
    here, so without deepest-wins an armed main checkout would re-freeze every
    worktree in it and `--disable` from inside one would do nothing."""
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    optin.set_enabled(False, project=PROJECT, root=roots["nested"])

    assert optin.armed(PROJECT, cwd=roots["nested"]) is False
    assert optin.armed(PROJECT, cwd=roots["main"]) is True, (
        "disabling a nested checkout must not disarm its parent")


def test_a_nested_enable_beats_a_disabled_ancestor(roots):
    """Deepest-wins in the other direction, so the rule is a rule and not a
    special case that only ever turns things off."""
    optin.set_enabled(False, project=PROJECT, root=roots["main"])
    optin.set_enabled(True, project=PROJECT, root=roots["nested"])

    assert optin.armed(PROJECT, cwd=roots["nested"]) is True
    assert optin.armed(PROJECT, cwd=roots["main"]) is False


def test_root_matching_is_segment_aligned_not_a_string_prefix(tmp_path):
    """`is_phase_evidence`'s lesson: `<x>/mindhive-old` shares a character
    prefix with `<x>/mindhive` and is a different tree. A `startswith` test
    arms the wrong checkout."""
    armed_root = tmp_path / "mindhive"
    lookalike = tmp_path / "mindhive-old"
    armed_root.mkdir()
    lookalike.mkdir()
    optin.set_enabled(True, project=PROJECT, root=armed_root)

    assert optin.armed(PROJECT, cwd=armed_root) is True
    assert optin.armed(PROJECT, cwd=lookalike) is False


def test_a_disable_is_recorded_rather_than_deleted(roots):
    """A deleted entry cannot out-rank an armed ancestor, which is the only
    thing that makes the nested escape work at all."""
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    optin.set_enabled(False, project=PROJECT, root=roots["main"])
    rec = json.loads(optin.record_path(PROJECT).read_text(encoding="utf-8"))
    entry = rec["roots"][str(roots["main"]).replace("\\", "/")]
    assert entry["enabled"] is False


def test_toggling_twice_leaves_one_entry_not_two(roots):
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    optin.set_enabled(False, project=PROJECT, root=roots["main"])
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    rec = json.loads(optin.record_path(PROJECT).read_text(encoding="utf-8"))
    assert len(rec["roots"]) == 1
    assert optin.armed(PROJECT, cwd=roots["main"]) is True


def test_set_enabled_reports_what_it_wrote(roots):
    out = optin.set_enabled(True, project=PROJECT, root=roots["main"])
    assert out["enabled"] is True
    assert Path(out["root"]) == roots["main"]
    assert Path(out["path"]) == optin.record_path(PROJECT)


def test_the_record_lives_beside_the_sidecar(roots):
    """One mindnet directory per namespace; the opt-in is mindnet state, not a
    second store."""
    from mindhive.mindnet import sidecar as sc_mod
    assert optin.record_path(PROJECT).parent == \
        sc_mod.sidecar_path(PROJECT).parent
    assert optin.record_path(PROJECT).name == "enforce.json"


# --- the shape production actually emits --------------------------------------

def test_a_relative_root_is_stored_absolute(roots, monkeypatch):
    """MN-4's P0 in one line: a guard that compares an absolute path against a
    relative one answers False on every real call. Normalize at the boundary so
    the comparison downstream is always absolute-to-absolute."""
    monkeypatch.chdir(roots["main"])
    out = optin.set_enabled(True, project=PROJECT, root=".")
    assert Path(out["root"]).is_absolute()
    assert Path(out["root"]) == roots["main"].resolve()
    assert optin.armed(PROJECT, cwd=roots["main"]) is True


def test_the_stored_root_key_is_always_forward_slashed(roots):
    """The cross-platform half: whatever separator the operator typed, the key on
    disk is forward-slashed, so the record is readable and stable everywhere."""
    out = optin.set_enabled(True, project=PROJECT, root=roots["main"])
    assert "\\" not in out["root"]
    rec = json.loads(optin.record_path(PROJECT).read_text(encoding="utf-8"))
    assert all("\\" not in key for key in rec["roots"])


@pytest.mark.skipif(os.name != "nt",
                    reason="separator equivalence is a Windows path rule; on "
                           "POSIX a backslash is a legal FILENAME character, so "
                           "'\\tmp\\x' really is a different path from '/tmp/x' "
                           "and asserting they match would assert a bug")
def test_a_backslash_root_matches_a_forward_slash_cwd():
    """Windows hands both spellings around; there they name one directory.

    Deliberately platform-gated rather than written to pass everywhere. The first
    version of this test asserted the equivalence unconditionally and passed on
    Windows while failing on Linux CI — the same class as the MN-4 P0: an
    assertion about a shape that only exists on one platform. The skip is not a
    coverage hole; `test_the_stored_root_key_is_always_forward_slashed` above
    carries the part that is genuinely cross-platform."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "main"
        root.mkdir()
        optin.set_enabled(True, project=PROJECT,
                          root=str(root).replace("/", "\\"))
        assert optin.armed(
            PROJECT, cwd=Path(str(root).replace("\\", "/"))) is True


def test_traversal_in_a_root_is_collapsed_before_it_is_stored(roots):
    """`main/.claude/worktrees/wt/../../..` IS `main`, and an operator who types
    it must not get a second entry that shadows the first."""
    optin.set_enabled(True, project=PROJECT,
                      root=roots["nested"] / ".." / ".." / "..")
    rec = json.loads(optin.record_path(PROJECT).read_text(encoding="utf-8"))
    assert list(rec["roots"]) == [str(roots["main"]).replace("\\", "/")]


def test_the_cwd_defaults_to_the_real_process_directory(roots, monkeypatch):
    """The hook passes no cwd — `Path.cwd()` is the production input."""
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    monkeypatch.chdir(roots["main"])
    assert optin.armed(PROJECT) is True
    monkeypatch.chdir(roots["other"])
    assert optin.armed(PROJECT) is False


def test_set_enabled_defaults_its_root_to_the_current_checkout(roots,
                                                              monkeypatch):
    monkeypatch.chdir(roots["other"])
    out = optin.set_enabled(True, project=PROJECT)
    assert Path(out["root"]) == roots["other"].resolve()
    assert optin.armed(PROJECT, cwd=roots["other"]) is True


# --- totality: a fault is dormant, never a raise ------------------------------

def test_a_missing_record_is_dormant_not_a_raise():
    assert not optin.record_path(PROJECT).exists()
    assert optin.armed(PROJECT) is False


@pytest.mark.parametrize("blob", [
    "{not json",                       # corrupt
    "[]",                              # not an object
    '"a string"',                      # not an object
    '{"roots": []}',                   # roots is not a mapping
    '{"roots": {"C:/x": true}}',       # entry is not a mapping
    '{"roots": {"": {"enabled": true}}}',   # empty root key
    "",                                # empty file
])
def test_a_malformed_record_is_dormant_not_a_raise(roots, blob):
    _write_record(blob)
    assert optin.armed(PROJECT, cwd=roots["main"]) is False
    assert optin.status(PROJECT, cwd=roots["main"])["note"] == \
        optin.DORMANT_NOTE


def test_an_unreadable_record_is_dormant_not_a_raise(roots, monkeypatch):
    _write_record(_record(**{str(roots["main"]).replace("\\", "/"): True}))

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "read_text", boom)
    assert optin.armed(PROJECT, cwd=roots["main"]) is False


def test_a_record_with_extra_keys_still_reads(roots):
    """Forward compatibility: a newer writer's field must not disarm an older
    reader, or an upgrade turns enforcement off silently."""
    rec = _record(**{str(roots["main"]).replace("\\", "/"): True})
    rec["future_field"] = {"whatever": 1}
    rec["roots"][str(roots["main"]).replace("\\", "/")]["reason"] = "because"
    _write_record(rec)
    assert optin.armed(PROJECT, cwd=roots["main"]) is True


def test_an_entry_without_an_enabled_key_is_dormant(roots):
    """Absent is not true. A half-written entry must not arm anything."""
    _write_record({"version": 1,
                   "roots": {str(roots["main"]).replace("\\", "/"): {}}})
    assert optin.armed(PROJECT, cwd=roots["main"]) is False


# --- the hot path stays cheap -------------------------------------------------

def test_status_never_shells_out_when_the_project_is_known(roots, monkeypatch):
    """`PreToolUse` fires on every tool call. `paths.worktree_root` costs a git
    subprocess with a 2-second timeout, so resolving the CURRENT checkout must
    be pure path arithmetic — the git call happens once, in the CLI, when the
    root is RECORDED."""
    optin.set_enabled(True, project=PROJECT, root=roots["main"])

    def boom(*a, **k):
        raise AssertionError("optin must not shell out on the hook path")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(paths.subprocess, "run", boom)
    assert optin.armed(PROJECT, cwd=roots["main"]) is True


def test_the_module_imports_neither_events_nor_subprocess():
    """The structural twin of the timing bound: `events.read_all` measures
    ~264 ms on the real store and this module sits on the `PreToolUse` path.
    Asserted over the source, which cannot rot the way a benchmark can."""
    tree = ast.parse(inspect.getsource(optin))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = (node.module or "").split(".")[0]
            imported.add(base)
            imported |= {a.name for a in node.names}
    assert "subprocess" not in imported
    # `events` may be reached lazily inside set_enabled (a CLI path); what must
    # never happen is a module-level import that the hook pays for.
    for node in ast.parse(inspect.getsource(optin)).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            text = (getattr(node, "module", "") or "")
            names = {a.name for a in node.names}
            assert "events" not in text and "events" not in names


def test_reading_the_record_touches_the_disk_once_per_call(roots, monkeypatch):
    """Not a caching requirement — a proof there is no accidental re-read loop
    over the roots mapping."""
    _write_record(_record(**{str(roots["main"]).replace("\\", "/"): True},
                          **{str(roots["other"]).replace("\\", "/"): True}))
    seen = {"n": 0}
    real = Path.read_text

    def counting(self, *a, **k):
        if self.name == "enforce.json":
            seen["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    optin.status(PROJECT, cwd=roots["main"])
    assert seen["n"] == 1


# --- the note is a stable contract -------------------------------------------

def test_the_armed_status_does_not_carry_the_dormant_note(monkeypatch, roots):
    """The canary for every assertion above that checks for DORMANT_NOTE: prove
    the note is absent when — and only when — enforcement is live."""
    monkeypatch.setenv(optin.ENV_VAR, "1")
    st = optin.status(PROJECT, cwd=roots["main"])
    assert st["armed"] is True
    assert optin.DORMANT_NOTE not in st["note"]
    assert st["note"], "an armed status must still explain WHY it is armed"


def test_the_env_var_is_named_exactly_once(monkeypatch):
    """One legend (invariant 10). A second spelling of the opt-in variable is a
    place for the two to drift, and the drift direction is 'enforcement was on
    and nobody could see why'."""
    assert optin.ENV_VAR == "MINDNETWORK_ENFORCE"
    src = inspect.getsource(optin)
    assert src.count('"MINDNETWORK_ENFORCE"') == 1


def test_status_and_armed_never_disagree(monkeypatch, roots):
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    for cwd in (roots["main"], roots["nested"], roots["other"]):
        for value in (None, "1", "0", ""):
            if value is None:
                monkeypatch.delenv(optin.ENV_VAR, raising=False)
            else:
                monkeypatch.setenv(optin.ENV_VAR, value)
            assert optin.armed(PROJECT, cwd=cwd) == \
                optin.status(PROJECT, cwd=cwd)["armed"]


def test_the_record_is_written_atomically(roots, monkeypatch):
    """A torn read on this file is an enforcement decision made from half a
    JSON document. `paths.write_text_atomic` is the house mechanism."""
    calls = []
    real = paths.write_text_atomic
    monkeypatch.setattr(paths, "write_text_atomic",
                        lambda p, t: (calls.append(Path(p)), real(p, t))[1])
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    assert optin.record_path(PROJECT) in calls


def test_the_record_directory_is_created_on_demand(roots):
    """`set_enabled` is often the first thing a fresh namespace ever writes."""
    assert not optin.record_path(PROJECT).parent.exists()
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    assert optin.record_path(PROJECT).exists()


def test_no_environment_variable_is_mutated_by_set_enabled(roots):
    """The persisted toggle and the session toggle are separate mechanisms; a
    `--enable` that also exported the variable would make `--disable` from a
    sibling shell look broken."""
    before = dict(os.environ)
    optin.set_enabled(True, project=PROJECT, root=roots["main"])
    assert dict(os.environ) == before
