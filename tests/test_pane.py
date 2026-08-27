"""The pane guard reaches every document that needs it.

`pane.PANE_JS` is substituted into the three workspace documents at import, the
way `theme.STATUS_CSS` is. What this file protects is the wiring, not the
behaviour: a marker that is never substituted leaves `/*__PANE__*/` sitting in
the served HTML and `paneHidden` undefined, so the first tick throws a
ReferenceError and the loop dies silently — a page that never polls at all,
which looks from the outside exactly like the fix working.

The BEHAVIOUR — hidden frame goes quiet, visible frame keeps polling — is proven
in the browser tier by `probe_a_hidden_workspace_frame_stops_polling` and its
mutant, which cuts the guard out of MindParallel and requires the probe to go
red. Nothing here duplicates that; these are the cheap structural facts that
tier is too slow to be the only guard for.
"""
import pytest

from mindhive import (dashboard_get, pane, research_page, swarm_page,
                      workspace_page)

# (name, renderer, the module that owns the document). The renderers are listed
# here rather than discovered, so ADDING a fourth workspace document and
# forgetting its marker is a failure someone has to notice — an auto-discovering
# test would simply not know about the new page.
DOCUMENTS = [
    ("parallel", workspace_page.render_parallel),
    ("swarm", swarm_page.render_swarm),
    ("research", research_page.render_research),
]


@pytest.mark.parametrize("name,render", DOCUMENTS, ids=[d[0] for d in DOCUMENTS])
def test_every_workspace_document_defines_the_guard_once(name, render):
    html = render("tok")
    assert html.count("function paneHidden") == 1, (
        f"{name} defines paneHidden {html.count('function paneHidden')} times")
    assert pane.MARKER not in html, (
        f"{name} still carries {pane.MARKER} verbatim: the import-time "
        f"substitution did not run, so paneHidden is undefined and the poll "
        f"loop throws on its first tick")


@pytest.mark.parametrize("name,render", DOCUMENTS, ids=[d[0] for d in DOCUMENTS])
def test_every_workspace_poll_loop_asks_before_it_fetches(name, render):
    html = render("tok")
    assert "if (!paneHidden())" in html, (
        f"{name} has the guard defined but does not consult it, which is the "
        f"shape that passes a 'the function exists' check and still polls "
        f"while nobody is looking")
    # The loop must still be scheduled unconditionally: skipping the TICK is the
    # design, stopping the TIMER would mean the section never resumes.
    assert "setTimeout(loop," in html, f"{name} no longer re-arms its loop"


def test_the_guard_is_fail_open():
    """A page with no frame, or one it may not read, must keep polling.

    `window.frameElement` is null for a document opened on its own and throws
    across an origin. Either way the answer has to be "not hidden": these pages
    are served standalone during development, and a guard that defaulted to
    hidden would make them look dead.
    """
    body = pane.PANE_JS
    assert "catch (e) { return false; }" in body, (
        "the guard no longer defaults to 'keep polling' when it cannot see its "
        "own frame")
    assert "window.frameElement" in body


def test_the_workspace_renderers_are_reachable_where_the_server_looks():
    """The browser tier's mutants patch these names on `dashboard_get`.

    They are imported there by name, so patching them on their defining module
    would leave the server's own binding pointing at the real function and the
    mutant would silently test nothing.
    """
    for name, render in DOCUMENTS:
        attr = f"render_{'parallel' if name == 'parallel' else name}"
        assert getattr(dashboard_get, attr) is render, (
            f"dashboard_get.{attr} is not {render}, so a mutation aimed at "
            f"{name} would patch a name the server does not use")
