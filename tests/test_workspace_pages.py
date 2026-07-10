# tests/test_workspace_pages.py
"""Requirement pinning for the Workspace pages (MindParallel / MindSwarm).

The pages are pure functions (token in, HTML string out), so — like
tests/test_graphview.py — these assert on the rendered string: every shipped
requirement of the Workspace feature is pinned here so a template edit can't
silently drop it.
"""
from mindhive.swarm_page import render_swarm
from mindhive.workspace_page import render_parallel

PARALLEL = render_parallel("tok-123")
SWARM = render_swarm("tok-123")


# --- shared page contract -----------------------------------------------------

def test_pages_embed_token_as_json():
    assert '"tok-123"' in PARALLEL
    assert '"tok-123"' in SWARM


def test_pages_are_offline_self_contained():
    for html in (PARALLEL, SWARM):
        assert "http://" not in html
        assert "https://" not in html


def test_token_cannot_break_out_of_script_block():
    for render in (render_parallel, render_swarm):
        html = render("</script><script>alert(1)")
        assert "</script><script>" not in html
        # positive assertion: the "<" was escaped, not just reshuffled
        assert "\\u003c/script>" in html


# --- R4: shared dashboard palette ----------------------------------------------

def test_parallel_uses_shared_dashboard_palette():
    assert "#0e1116" in PARALLEL          # shared page background
    assert "#30363d" in PARALLEL          # shared border
    assert "#161b22" in PARALLEL          # shared panel surface
    assert "#58a6ff" in PARALLEL          # shared accent (focus ring)
    assert "#11131a" not in PARALLEL      # old blue-tinted base is gone
    assert "#2a2f3d" not in PARALLEL      # old border is gone


def test_swarm_uses_shared_dashboard_palette():
    assert "#0e1116" in SWARM
    assert "#30363d" in SWARM
    assert "#161b22" in SWARM
    assert "#58a6ff" in SWARM
    assert "#0b0f0c" not in SWARM         # old green-tinted base is gone
    assert "#26301f" not in SWARM


def test_swarm_keeps_role_identity_colors():
    # the palette swap must not flatten the role styling: pin the ROLE_STYLE
    # map entries themselves — the bare role words also appear in labels and
    # hint text, so word-presence alone would pass with the styling deleted
    assert "coordinator: '#ff7b54'" in SWARM
    assert "builder: '#7ee787'" in SWARM
    assert "scout: '#e8b64c'" in SWARM
    assert "reviewer: '#f0883e'" in SWARM


# --- R5: 16 panes, split in any direction --------------------------------------

def test_parallel_has_16_pane_cap():
    assert ">= 16" in PARALLEL
    assert "16-pane cap" in PARALLEL


def test_parallel_supports_both_split_directions():
    assert "splitPane(n.pid, 'h')" in PARALLEL
    assert "splitPane(n.pid, 'v')" in PARALLEL


# --- R6: drag a skill onto a pane ----------------------------------------------

def test_parallel_skill_drag_payload_and_source():
    assert "kind:'skill'" in PARALLEL
    assert "/api/plugins/inventory" in PARALLEL   # skills sidebar source
    assert "dragstart" in PARALLEL


# --- R7: kanban dispatches agents -----------------------------------------------

def test_parallel_kanban_columns_and_dispatch():
    for col in ("backlog", "running", "done"):
        assert col in PARALLEL
    assert "kind:'task'" in PARALLEL
    assert "/api/workspace/board_dispatch" in PARALLEL


# --- R2: multi-account profiles --------------------------------------------------

def test_parallel_manages_profiles():
    assert "/api/workspace/profile_add" in PARALLEL
    assert "/api/workspace/profile_remove" in PARALLEL
    assert "CLAUDE_CONFIG_DIR" in PARALLEL


# --- R8: mission tree ------------------------------------------------------------

def test_swarm_styles_all_four_roles():
    for role in ("coordinator", "builder", "scout", "reviewer"):
        assert role in SWARM


def test_swarm_mission_lifecycle_endpoints():
    assert "/api/workspace/mission" in SWARM
    assert "/api/workspace/mission_stop" in SWARM


def test_swarm_inspector_streams_agent_output():
    assert "/api/workspace/output" in SWARM


# --- R9: command bar — one agent or all ------------------------------------------

def test_swarm_command_bar_targets():
    assert "/api/workspace/command" in SWARM
    assert '<option value="all">@all</option>' in SWARM


def test_swarm_parses_at_mentions():
    # typed "@Name <text>" resolves the named agent; longest name match wins
    assert "resolveMention" in SWARM
    assert "best.nm.length" in SWARM
