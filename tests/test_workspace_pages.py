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


def test_parallel_spawn_seeds_agents_before_repaint():
    # regression: dropping a skill on an empty pane spawned an agent, but the
    # stale AGENTS map made the repaint treat it as vanished, null the pane,
    # and the follow-up send failed with "unknown agent" — every spawn path
    # goes through the one spawnAgent helper, which seeds AGENTS itself
    assert PARALLEL.count("AGENTS[res.agent.id] = res.agent") == 1
    assert "async function spawnAgent" in PARALLEL
    # spawn button, card drop on an empty pane, and Run on an empty pane all
    # use the helper
    assert PARALLEL.count("await spawnAgent(") >= 3


def test_parallel_drop_send_uses_pinned_agent_id():
    # the send after an auto-spawn must not re-read the mutable layout node
    assert "const agentId = n.agent" in PARALLEL
    assert "{id:agentId," in PARALLEL


def test_parallel_pane_reset_needs_two_consecutive_misses():
    # one stale poll (request in flight during the spawn) must not wipe a pane
    assert "MISSES[n.agent] < 2" in PARALLEL


# --- stage-then-run (2026-07-11 plan R1) -----------------------------------------

def _drop_handler():
    # the onDrop function body sits between its declaration and the live-agent
    # section comment — slice it out so we can assert on the drop path alone
    return PARALLEL.split("async function onDrop", 1)[1] \
                   .split("// --- live agent state", 1)[0]


def test_parallel_skill_drop_stages_instead_of_running():
    drop = _drop_handler()
    assert "STAGED[key]" in drop                  # skill drops stage…
    assert "/api/workspace/send" not in drop      # …and never send directly
    assert "board_dispatch" in drop               # kanban cards still dispatch


def test_parallel_run_posts_recipe_to_server_composer():
    # composition moved server-side: Run posts the staged chips + command to
    # run_recipe, which enriches them with MindHive history before launching
    assert "/api/workspace/run_recipe" in PARALLEL
    assert "skills: STAGED[agentId] || []" in PARALLEL
    assert "composePrompt" not in PARALLEL        # no client-side composer left
    assert 'data-r="run"' in PARALLEL
    # the consumed recipe clears so the next Run can't double-invoke it
    assert "delete STAGED[agentId]" in PARALLEL


def test_parallel_recipe_is_persistent_and_chips_removable():
    assert "mh_ws_staged" in PARALLEL
    assert 'title="unstage"' in PARALLEL


# --- tabs view (2026-07-11 plan R2) -----------------------------------------------

def test_parallel_has_tab_view_toggle():
    assert "toggleView" in PARALLEL
    assert "mh_ws_view" in PARALLEL               # view choice persists
    assert "mh_ws_tab" in PARALLEL                # active tab persists
    assert "tabbar" in PARALLEL


def test_parallel_tab_spawn_offers_account_profiles():
    # the + tab reuses the empty-pane spawn form, which lists PROFILES
    assert "PROFILES.map(pr =>" in PARALLEL
    assert "tab-new" in PARALLEL


# --- orphan adoption + clear idle (2026-07-11 plan R3) ----------------------------

def test_parallel_adopts_orphaned_agents():
    assert "adoptOrphans" in PARALLEL
    # adoption is splits-only (tabs render from server truth by construction)
    assert "VIEW !== 'splits' || !POLLED" in PARALLEL
    # a just-spawned agent already claimed by adoption is unclaimed before the
    # spawning pane takes it — two panes must never share one agent
    assert "l.agent === agent.id" in PARALLEL


def test_parallel_clear_idle_button():
    assert "/api/workspace/clear_idle" in PARALLEL
    assert "Clear idle" in PARALLEL


# --- run data reflected in the pane header (2026-07-11 plan R5) --------------------

def test_parallel_shows_last_run_stats():
    assert "last_turns" in PARALLEL
    assert "last_cost" in PARALLEL


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


# --- cat nodes (2026-07-11 plan R4) ------------------------------------------------

def test_swarm_nodes_are_pixel_cat_sprites():
    assert "catSVG" in SWARM
    assert "CAT_PALETTES" in SWARM
    assert "crispEdges" in SWARM                  # pixel-art rendering
    for cat in ("white", "calico", "orange", "tabby", "siamese", "black"):
        assert cat in SWARM


def test_swarm_cat_choice_is_deterministic_from_agent_id():
    # random per new node but stable across refreshes: derived from the id,
    # never from Math.random
    assert "parseInt(String(id).slice(0,4), 16)" in SWARM
    assert "% 6" in SWARM
    assert "Math.random" not in SWARM


# --- node inspector: skills used + run info (2026-07-11 plan R5) -------------------

def test_swarm_inspector_shows_skills_used_and_run_info():
    assert "skills_used" in SWARM
    assert "iskills" in SWARM
    assert "last_turns" in SWARM
    assert "last_cost" in SWARM


# --- model picker + account auth (2026-07-11 auth/skills round) --------------------

def test_parallel_offers_model_picker():
    assert 'data-r="model"' in PARALLEL
    assert "/api/workspace/set_model" in PARALLEL
    assert "modelOptions" in PARALLEL
    assert '"__custom"' in PARALLEL               # free-typed model ids allowed


def test_parallel_shows_profile_auth_health():
    assert "/api/workspace/auth" in PARALLEL
    for state in ("ok", "expired", "none"):
        assert ".adot." + state in PARALLEL       # green / amber / red dots


def test_parallel_has_login_and_trust_buttons():
    assert "/api/workspace/profile_login" in PARALLEL
    assert "/api/workspace/profile_trust" in PARALLEL
    assert "loginProfile" in PARALLEL and "trustProfile" in PARALLEL
