# tests/test_roadmap_auto.py
"""The split's seam: `roadmap_auto` is reachable through `roadmap`, identically.

`roadmap.py` sat one line under the 1000-line cap before the plan resolver was
written, so the session auto-sync layer moved into its own module. The layer's
BEHAVIOUR is covered where it always was, in `tests/test_roadmap.py` and
`tests/test_replenish_sink.py` — those suites call it through `roadmap`, which is
exactly the access path this file is here to protect.

WHY IT NEEDS PROTECTING. This repo has already been bitten by a mechanical split
that silently moved monkeypatch addresses: splitting `dashboard.py` relocated
three patch targets, and the tests that patched the old ones kept passing while
testing nothing. `tests/test_hookcapture.py` patches `roadmap.sync_from_todos`
to prove a PostToolUse hook survives a raising writer — if that attribute ever
stops being the object the hook actually calls, the hook's most important
guarantee becomes untested and nothing goes red.
"""
import ast
import os
import pathlib
import subprocess
import sys

import pytest

from mindhive import roadmap, roadmap_auto

# Everything the split moved. `_session_prompt` and `_step_shape` are private and
# still listed: `replenish_sinks` calls `rm_mod._session_prompt`, so it is part
# of the seam whatever its underscore says.
MOVED = ("AUTO_TAG", "RESUME_WINDOW_HOURS", "_hours_before", "_session_prompt",
         "_step_shape", "auto_plan_id", "autoclose_session", "sync_from_todos",
         "todos_to_steps")


@pytest.mark.parametrize("name", MOVED)
def test_the_reexport_is_the_same_object(name):
    """Identity, not merely presence. A re-export that rebound the name to a
    wrapper would satisfy `hasattr` and defeat every monkeypatch through it."""
    assert getattr(roadmap, name) is getattr(roadmap_auto, name)


def test_the_hook_patch_address_still_resolves_to_the_writer():
    """The specific patch `tests/test_hookcapture.py` relies on, and the one the
    dashboard split broke the equivalent of."""
    from mindhive import hookcapture
    src = pathlib.Path(hookcapture.__file__).read_text(encoding="utf-8")
    assert "roadmap.sync_from_todos(" in src, (
        "hookcapture no longer calls the writer through the `roadmap` module, so "
        "monkeypatching roadmap.sync_from_todos patches nothing it uses")
    assert roadmap.sync_from_todos is roadmap_auto.sync_from_todos


def test_the_moved_layer_owns_no_second_copy_of_the_contract():
    """The split must not fork the payload contract. `roadmap_auto` reaches back
    for `validate_payload`, `fold` and the caps rather than restating them —
    a second `MAX_STEPS` is how two surfaces come to disagree about one cap."""
    tree = ast.parse(pathlib.Path(roadmap_auto.__file__).read_text(encoding="utf-8"))
    assigned = {t.id for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Name)}
    forked = assigned & {"MAX_STEPS", "MAX_STEP_CHARS", "MAX_TITLE_CHARS",
                         "MAX_PROMPT_CHARS", "STATUSES", "ACTIONS",
                         "RETIRED_STATUSES"}
    assert forked == set(), f"the contract was copied instead of imported: {forked}"


def test_the_fold_and_the_projections_did_not_move():
    """The other half of the seam: what STAYED has to have stayed. A reader
    following `roadmap.fold` must not find it re-exported from somewhere else.

    `notice` was in this list until the SECOND split (`roadmap_render`) and was
    removed then, deliberately and with an argument, because the list had been
    a snapshot rather than a rule. Two things say so. The name and docstring
    claim the boundary is "the writers, not the fold" -- but `record` is a
    writer and is still listed, so the tuple cannot have been derived from that
    sentence; it was whatever happened to remain in roadmap.py in August. And
    `notice` is neither a fold nor a projection over the registry: it takes the
    output of `interrupted()` -- a projection someone else computed -- and
    formats markdown, alongside `review.notice`, `changelog.notice` and
    `sopgate.notice`, which `projector.build_state` calls side by side.

    What actually protects callers is `test_the_reexport_is_the_same_object`,
    not `__module__`: nothing in the tree reads a `__module__` on a roadmap
    name except this test, while `projector` reaches the renderer as
    `roadmap.notice`. `tests/test_roadmap_render.py` asserts that identity for
    all seven moved names. The seven below are the rule this test was reaching
    for -- the fold, the payload contract, the writer, and every registry
    projection -- and they stay."""
    for name in ("fold", "plans", "interrupted", "record",
                 "validate_payload", "by_session", "plan_for_session"):
        fn = getattr(roadmap, name)
        assert getattr(fn, "__module__", "") == "mindhive.roadmap", (
            f"{name} moved out of roadmap.py — the split was supposed to take "
            "the writers, not the fold")


# --- F2: the cycle, which only a fresh interpreter can show -------------------

@pytest.mark.parametrize("first", ["mindhive.roadmap_auto", "mindhive.roadmap"])
def test_either_module_can_be_imported_first(first):
    """The dependency runs BOTH ways -- `roadmap_auto` needs `roadmap`'s
    contract, fold and caps; `roadmap` needs these writers to re-export -- and
    while `roadmap_auto` resolved its half at module scope, the pair worked only
    when `roadmap` happened to be touched first. `import mindhive.roadmap_auto`
    on its own died with `cannot import name 'AUTO_TAG' from partially
    initialized module`.

    This has to be a SUBPROCESS: every test in this suite imports `roadmap`
    first (the module header does), so in-process the broken order is
    unreachable and the whole file was one word-order away from being unable to
    see the bug it exists to guard.
    """
    src = pathlib.Path(roadmap.__file__).parent.parent
    code = ("import " + first + "\n"
            "from mindhive import roadmap, roadmap_auto\n"
            "assert roadmap.sync_from_todos is roadmap_auto.sync_from_todos\n"
            "assert roadmap.AUTO_TAG == roadmap_auto.AUTO_TAG\n"
            "print('ok')\n")
    env = dict(os.environ, PYTHONPATH=str(src))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_the_writer_layer_resolves_its_dependency_at_call_time():
    """The shape of the fix, pinned. A module-scope `from . import roadmap`
    here is what made the import order load-bearing, and re-adding one is an
    easy, invisible regression -- the suite would stay green because it always
    imports `roadmap` first."""
    tree = ast.parse(pathlib.Path(roadmap_auto.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    named = {a.name for n in top if isinstance(n, ast.ImportFrom)
             for a in n.names} | {a.name for n in top
                                  if isinstance(n, ast.Import) for a in n.names}
    assert "roadmap" not in named, (
        "roadmap is imported at module scope again, so whichever module the "
        "process touches first decides whether the import works at all")
