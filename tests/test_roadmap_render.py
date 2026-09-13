# tests/test_roadmap_render.py
"""The second seam: `roadmap_render` is reachable through `roadmap`, identically.

`roadmap.py` hit the 1000-line cap (`tests/test_file_size_cap.py`) for the
second time, so the rendering layer moved into its own module — the same
treatment `roadmap_auto` got, and this file is the same guard
`tests/test_roadmap_auto.py` is, for the new cut.

The layer's BEHAVIOUR is covered where it always was: `tests/test_roadmap.py`
for the marker's content, `tests/test_provenance_surfaces.py` for the fact that
a plan cannot forge a heading or a step through it, `tests/test_projector.py`
for the block reaching STATE.md. Every one of those calls it through `roadmap`,
which is exactly the access path this file protects.

WHY IT NEEDS PROTECTING, restated because the hazard is specific and this repo
has already been bitten by it: splitting `dashboard.py` relocated three
monkeypatch targets and the tests that patched the old addresses kept passing
while testing nothing. A re-export makes `roadmap.notice` keep working for
callers; it does NOT make `roadmap._step_line` a patch address for what
`notice` actually calls. `test_the_patch_address_moved_with_the_code` below
makes that rule fail loudly instead of silently, before someone writes the
patch that would quietly assert nothing.
"""
import ast
import os
import pathlib
import subprocess
import sys

import pytest

from mindhive import events, outcomes, roadmap, roadmap_render

# Everything the split moved. The private names are listed too: `_step_line`
# and the two `_NOTICE_*` caps have no external reader today, and they are here
# precisely so the address rule is checkable before one appears.
MOVED = ("NEW_MARKER", "_NOTICE_DONE_TAIL", "_NOTICE_PENDING_HEAD",
         "_step_line", "ledger_body", "new_prefix", "notice")


def _plan_and_interrupt(steps=("do the thing",), title="A plan"):
    """One interrupted plan, which is the only state `notice` renders."""
    events.append_event(
        "roadmap",
        {"action": "set", "plan_id": "p1", "prompt": "the ask",
         "title": title, "steps": list(steps)},
        session_id="human", tags=["roadmap"], ts="2026-01-01T00:00:00Z")
    events.append_event(
        "roadmap", {"action": "interrupt", "plan_id": "p1",
                    "cause": "session_end"},
        session_id="human", tags=["roadmap"], ts="2026-01-02T00:00:00Z")
    return events.read_all()


# --- the seam itself ---------------------------------------------------------

@pytest.mark.parametrize("name", MOVED)
def test_the_reexport_is_the_same_object(name):
    """Identity, not merely presence. A re-export that rebound the name to a
    wrapper would satisfy `hasattr` and defeat every monkeypatch through it."""
    assert getattr(roadmap, name) is getattr(roadmap_render, name)


def test_the_moved_layer_owns_no_second_copy_of_the_contract():
    """The split must not fork the caps. `roadmap_render` reaches back for
    `MAX_PROMPT_CHARS` rather than restating it — a second copy is how one
    surface comes to truncate at a different width than the validator that
    accepted the value."""
    tree = ast.parse(pathlib.Path(roadmap_render.__file__)
                     .read_text(encoding="utf-8"))
    assigned = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Name)}
    forked = assigned & {"MAX_PROMPT_CHARS", "MAX_STEPS", "MAX_STEP_CHARS",
                         "MAX_TITLE_CHARS", "MAX_NOTE_CHARS", "STATUSES",
                         "ACTIONS", "RETIRED_STATUSES"}
    assert forked == set(), (
        f"the contract was copied instead of imported: {forked}")


def test_the_renderer_resolves_its_dependency_at_call_time():
    """The shape of the fix, pinned — the lesson `roadmap_auto` had to learn.
    A module-scope `from . import roadmap` here makes the pair loadable in only
    one order, and the suite would stay green because it always imports
    `roadmap` first."""
    tree = ast.parse(pathlib.Path(roadmap_render.__file__)
                     .read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    named = {a.name for n in top if isinstance(n, ast.ImportFrom)
             for a in n.names} | {a.name for n in top
                                  if isinstance(n, ast.Import)
                                  for a in n.names}
    assert "roadmap" not in named, (
        "roadmap is imported at module scope again, so whichever module the "
        "process touches first decides whether the import works at all")
    assert "autoresume" not in named, (
        "autoresume reaches back into roadmap; importing it at module scope "
        "here re-opens the cycle `notice` keeps lazy on purpose")


def test_the_fold_did_not_follow_the_renderer_out():
    """What STAYED has to have stayed. The cut takes the presentation layer;
    a reader following `roadmap.fold` or `roadmap.validate_payload` must still
    find them in roadmap.py."""
    for name in ("fold", "plans", "interrupted", "record", "validate_payload",
                 "next_step", "describe_work_link", "_probe"):
        fn = getattr(roadmap, name)
        assert getattr(fn, "__module__", "") == "mindhive.roadmap", (
            f"{name} left roadmap.py — the split was supposed to take the "
            "renderers, not the registry")


def test_the_dependency_fell_cleanly_to_the_render_side():
    """The evidence that this seam is a boundary and not a line-count
    convenience: every `provenance` use in the old file was inside `_step_line`
    and `notice`, so roadmap.py no longer imports it at all.

    TRIPWIRE, deliberately. If you are adding sanitization, it belongs beside
    the other renderers in `roadmap_render`. If it genuinely belongs in the
    fold or the validator, delete this assertion and say why in the
    change-log — do not re-add the import and leave this passing by accident,
    because it cannot: it will fail, and that is the conversation."""
    tree = ast.parse(pathlib.Path(roadmap.__file__)
                     .read_text(encoding="utf-8"))
    named = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
             for a in n.names}
    assert "provenance" not in named, (
        "roadmap.py imports provenance again — sanitization belongs in "
        "roadmap_render with the renderers that need it")


# --- the address rule, made checkable ----------------------------------------

def test_the_patch_address_moved_with_the_code():
    """THE `dashboard.py` FAILURE, as an assertion.

    `roadmap._step_line` is still the same object as `roadmap_render._step_line`
    (asserted above), so patching it satisfies every naive check — and changes
    nothing, because `notice` resolves `_step_line` through `roadmap_render`'s
    own globals. A future test that stubbed the step renderer at the old
    address would pass while testing nothing. This is the discriminating pair:
    the old address must NOT take effect and the new one must."""
    evs = _plan_and_interrupt()

    def sentinel(s, marker):
        return "- SENTINEL"

    real = roadmap_render._step_line
    try:
        roadmap._step_line = sentinel          # the OLD address
        assert "- SENTINEL" not in roadmap.notice(evs), (
            "patching roadmap._step_line appeared to work — if this ever "
            "passes, the address rule this file documents is wrong")
        roadmap_render._step_line = sentinel   # the NEW one
        assert "- SENTINEL" in roadmap.notice(evs)
    finally:
        roadmap_render._step_line = real
        roadmap._step_line = real


# --- canaries: the guards above must not pass on a renderer that died --------

def test_the_marker_still_renders_its_content():
    """Without this, a `notice` that returned "" would satisfy the address
    test's first half and every import assertion in the file."""
    out = roadmap.notice(_plan_and_interrupt(steps=["ship the thing"]))
    assert "⚠ Interrupted plan" in out
    assert "A plan" in out
    assert "the ask" in out
    assert "ship the thing" in out


def test_ledger_body_still_reaches_the_work_link_through_roadmap():
    """`ledger_body`'s one outward edge. It calls `describe_work_link`, which
    stayed behind, so this is the assertion that the call-time import actually
    resolves rather than raising NameError at the first LEDGER write."""
    body = roadmap.ledger_body({"action": "complete", "plan_id": "p1",
                                "work_pr": 130, "work_branch": "feat/x"})
    assert "work: PR #130 · branch feat/x" in body


def test_the_new_marker_still_rides_both_renderers():
    """`new_prefix` moved with them; both readers must still show it, which is
    the whole point of it being one helper rather than three literals."""
    evs = _plan_and_interrupt(
        steps=[{"text": "injected work", "status": "pending", "new": True}])
    assert roadmap.NEW_MARKER + "injected work" in roadmap.notice(evs)
    body = roadmap.ledger_body(
        {"action": "set", "plan_id": "p1",
         "steps": [{"text": "injected work", "status": "pending",
                    "new": True}]})
    assert roadmap.NEW_MARKER + "injected work" in body


# --- import order, which only a fresh interpreter can show --------------------

@pytest.mark.parametrize("first", ["mindhive.roadmap_render", "mindhive.roadmap",
                                   "mindhive.replenish_sinks"])
def test_any_of_the_three_can_be_imported_first(first):
    """Three, not two. `replenish_sinks` binds `_NEW = rm_mod.NEW_MARKER` at
    MODULE SCOPE, so it reads a name that now exists only after roadmap.py's
    bottom re-export has run — it is the one caller a bottom import can break,
    and the `roadmap_auto` guard does not cover it.

    A SUBPROCESS, for the reason that file gives: every test here imports
    `roadmap` at module scope, so in-process the broken order is unreachable
    and this suite would be one word-order away from being unable to see the
    bug it exists to guard."""
    src = pathlib.Path(roadmap.__file__).parent.parent
    code = ("import " + first + "\n"
            "from mindhive import roadmap, roadmap_render\n"
            "assert roadmap.notice is roadmap_render.notice\n"
            "assert roadmap.NEW_MARKER == roadmap_render.NEW_MARKER\n"
            "print('ok')\n")
    env = dict(os.environ, PYTHONPATH=str(src))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_importing_roadmap_alone_still_registers_the_retirement_surface():
    """`outcomes.register` has no discovery mechanism — no scanning, no
    import_module — so the import side effect IS the whole contract, and
    `tests/test_outcomes.py` imports `roadmap` for exactly that. The probe
    stayed in roadmap.py, but a split that had moved it could have severed this
    with nothing going red."""
    assert outcomes.get("roadmap-retirement") is not None
    src = pathlib.Path(roadmap.__file__).parent.parent
    code = ("import mindhive.roadmap\n"
            "from mindhive import outcomes\n"
            "assert outcomes.get('roadmap-retirement') is not None\n"
            "print('ok')\n")
    env = dict(os.environ, PYTHONPATH=str(src))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
