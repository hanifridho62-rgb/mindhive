# tests/test_research_page.py
"""Self-Reliant Phase 3: page pinning for research_page.py (U7, mirroring
tests/test_workspace_pages.py) and live /api/research/* route coverage (U8)
against a real dashboard server with the controller swapped for a test-wired
instance (mock trainer + stubbed agent — no WSL, GPU, or CLI)."""
import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from mindhive import dashboard, research
from mindhive.research_page import render_research
from mindhive.dashboard_page import render_page

MOCK = Path(__file__).resolve().parents[1] / "scripts" / "self_reliant" / "mock_trainer.py"
PAGE = render_research("tok-123")
PARENT = render_page("tok-parent")


class _ReportingStub:
    """A learn-mode researcher that returns an analysis report each turn."""

    def turn(self, prompt):
        return {"ok": True, "report": "lower the lr to 0.03 next run"}

    def stop(self):
        pass


class _PRDStub:
    """A PRD-mode researcher that returns a PRD (TITLE:/SUMMARY: header + body)."""

    def turn(self, prompt):
        return {"ok": True, "report":
                "TITLE: Ship the widget\nSUMMARY: builds the widget\n\n# PRD\nbody"}

    def stop(self):
        pass


# --- U7: page contract --------------------------------------------------------

def test_page_embeds_token_as_json():
    assert '"tok-123"' in PAGE


def test_page_is_offline_self_contained():
    assert "http://" not in PAGE
    assert "https://" not in PAGE


def test_token_cannot_break_out_of_script_block():
    html = render_research("</script><script>alert(1)")
    assert "</script><script>" not in html
    assert "\\u003c/script>" in html


def test_page_uses_shared_dashboard_palette():
    assert "#0e1116" in PAGE
    assert "#30363d" in PAGE
    assert "#161b22" in PAGE
    assert "#58a6ff" in PAGE


def test_page_skill_drag_stages_into_recipe():
    assert "kind:'skill'" in PAGE                # same payload as MindParallel
    assert "/api/plugins/inventory" in PAGE      # same sidebar source
    assert "dragstart" in PAGE
    assert "mh_sr_staged" in PAGE                # staged recipe persists
    assert 'title="unstage"' in PAGE             # chips removable


def test_page_start_posts_goal_caps_profile_failover():
    assert "/api/research/start" in PAGE
    for field in ("goal", "skills: STAGED", "max_experiments",
                  "time_budget_s", "failover", "profile"):
        assert field in PAGE


def test_page_has_stop_and_live_surfaces():
    assert "/api/research/stop" in PAGE
    assert "/api/research/status" in PAGE
    assert "/api/research/leaderboard" in PAGE
    assert "/api/research/output?since=" in PAGE


def test_page_states_explicit_start_and_guidance_contract():
    # the section's two contracts are user-visible: runs only on explicit
    # Start (R3), and skills are guidance, not executable code (KTD4)
    assert "explicit Start" in PAGE
    assert "guidance" in PAGE


def test_page_has_model_picker_fed_by_profiles_models():
    # the researcher's model is switchable per run; the option list is the
    # same MODEL_CHOICES the workspace already serves via /api/workspace/profiles
    assert 'id="model"' in PAGE
    assert "res.models" in PAGE               # reads the served model list
    assert "mh_sr_model" in PAGE              # last pick remembered across runs


def test_page_start_posts_model():
    assert "/api/research/start" in PAGE
    assert "model:" in PAGE                   # model sent in the start body


def test_page_has_mode_selector_posting_prd_and_improve():
    # the PRD/improve choice is a start-form control sent in the body (U5).
    # the read-only mode is relabeled 'PRD' but still posts value="learn".
    assert 'id="mode"' in PAGE
    assert "mode:" in PAGE                     # mode sent in the start body
    assert 'value="learn"' in PAGE            # PRD mode still posts learn
    assert "PRD" in PAGE
    assert "improve (edit" in PAGE


def test_page_has_prd_panel_reading_the_prds_route():
    assert 'id="prdpanel"' in PAGE
    assert "/api/research/prds" in PAGE
    assert "PRD" in PAGE


def test_page_prd_card_press_shows_backing_memory_via_recall():
    # pressing a PRD card recalls its backing ledger events inline — the
    # section is connected to the memory it wrote
    assert "/api/recall?q=" in PAGE


def test_page_prd_card_jumps_to_memory_section_via_postmessage():
    # 'View in Memory' asks the parent dashboard to open the Memory view
    # filtered to this PRD's events
    assert "postMessage" in PAGE
    assert "mh-self-reliant" in PAGE
    assert "show-memory" in PAGE
    assert "View in Memory" in PAGE


def test_parent_opens_memory_when_self_reliant_requests_it():
    # the parent listens for the Self-Reliant iframe's jump-to-memory message,
    # validates it is from our iframe on the same origin, then opens the
    # filtered Memory view
    assert "addEventListener('message'" in PARENT
    assert "mh-self-reliant" in PARENT
    assert "show-memory" in PARENT
    assert "contentWindow" in PARENT          # only accept our iframe's messages
    assert "location.origin" in PARENT        # same-origin guard
    assert "showMemory()" in PARENT


def test_page_has_accounts_panel_reusing_workspace_endpoints():
    # full account switching in-section, reusing the tested workspace routes
    assert "Accounts" in PAGE
    for ep in ("/api/workspace/profile_add", "/api/workspace/profile_login",
               "/api/workspace/profile_trust", "/api/workspace/profile_remove",
               "/api/workspace/auth"):
        assert ep in PAGE


def test_page_auto_connects_accounts_without_a_terminal():
    # zero-terminal login: the page verifies each account with the CLI-backed
    # auth_verify endpoint on load, shows who is connected, and the connect
    # button falls back to the browser OAuth flow — never "type /login"
    assert "/api/workspace/auth_verify" in PAGE
    assert "verifyAll" in PAGE
    assert "connectProfile" in PAGE
    assert "browser" in PAGE
    assert "/login" not in PAGE


def test_page_has_trusted_sources_panel():
    # transparency: the page shows exactly what a PRD run's allowlist grants
    assert "Trusted sources" in PAGE
    assert "/api/research/sources" in PAGE
    assert "MINDHIVE_RESEARCH_WEB" in PAGE      # the kill switch is explained


def test_page_labels_self_reliant_output_and_offers_prd_copy():
    # every PRD card carries the provenance badge and a copy affordance
    assert "self-reliant" in PAGE
    assert "pill sr" in PAGE
    assert "copyPRD" in PAGE
    assert "copy-btn" in PAGE


def test_page_gates_stop_button_on_run_state():
    assert 'id="stopbtn"' in PAGE
    assert "stopbtn').disabled" in PAGE


# --- U8: routes ---------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    """Dashboard on an ephemeral port with research.CONTROLLER swapped for a
    test-wired controller; restores the singleton on teardown."""
    token = "test-token-123"
    handler = dashboard.make_handler(token)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class StubResearcher:
        def turn(self, prompt):
            return {"ok": True}

        def stop(self):
            pass

    (tmp_path / "train.py").write_text("lr = 0.01\n", encoding="utf-8")
    ctl = research.ResearchController(
        trainer_cmd=[sys.executable, str(MOCK), "--mode", "ok",
                     "--seconds", "0.05"],
        agent_factory=lambda profile, model=None: StubResearcher(),
        train_path=tmp_path / "train.py",
        leaderboard=research.Leaderboard(tmp_path / "lb.json"),
        append_event=lambda *a, **k: None,
        preflight=lambda profile, mode: [],
        cap_s=10, turn_timeout_s=5)
    old = research.CONTROLLER
    research.CONTROLLER = ctl
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", token, ctl
    finally:
        ctl.stop()
        ctl.wait(10)
        research.CONTROLLER = old
        srv.shutdown()


def _get(base, path, token):
    sep = "&" if "?" in path else "?"
    with urllib.request.urlopen(f"{base}{path}{sep}token={token}", timeout=10) as r:
        return r.status, r.read().decode("utf-8")


def _post(base, path, token, body):
    req = urllib.request.Request(
        f"{base}{path}?token={token}", method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_research_page_route_serves_html(server):
    base, token, _ = server
    status, body = _get(base, "/api/research/page", token)
    assert status == 200
    assert "SELF-RELIANT" in body
    assert '"test-token-123"' in body


def test_research_status_idle_before_any_run(server):
    base, token, _ = server
    status, body = _get(base, "/api/research/status", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["ok"] and obj["state"] == "idle" and obj["experiments"] == 0


def test_research_start_runs_and_leaderboard_reflects(server):
    base, token, ctl = server
    status, res = _post(base, "/api/research/start",
                        token, {"goal": "g", "max_experiments": 2})
    assert status == 200 and res["ok"]
    assert ctl.wait(30)
    _, body = _get(base, "/api/research/leaderboard", token)
    obj = json.loads(body)
    assert obj["ok"] and len(obj["experiments"]) == 2
    assert obj["best"]["val_bpb"] == pytest.approx(1.2345)


def test_research_start_malformed_body_structured_error(server):
    base, token, _ = server
    status, res = _post(base, "/api/research/start", token, {"goal": ""})
    assert status == 200
    assert res["ok"] is False and "goal" in res["error"]


def test_research_learnings_route_reachable_and_serves_reports(server):
    # regression guard: /api/research/learnings must be a registered GET route
    # (a missing entry returned 404 and left the LEARNINGS panel permanently
    # empty). A learn run persists reports the route then serves.
    base, token, ctl = server
    ctl._factory = lambda profile, model=None: _ReportingStub()
    status, res = _post(base, "/api/research/start", token,
                        {"goal": "analyze", "max_experiments": 1, "mode": "learn"})
    assert status == 200 and res["ok"]
    assert ctl.wait(30)
    status, body = _get(base, "/api/research/learnings", token)
    assert status == 200                         # route registered (was 404)
    obj = json.loads(body)
    assert obj["ok"] and "lower the lr" in obj["learnings"]
    _, sbody = _get(base, "/api/research/status", token)
    assert json.loads(sbody)["mode"] == "learn"  # status drives the panel


def test_research_prds_route_reachable_and_serves_cards(server):
    # regression guard mirroring the learnings-route test: /api/research/prds
    # must be a registered GET route (a missing entry returns 404 and leaves the
    # PRD panel permanently empty). A PRD run persists cards the route then serves.
    base, token, ctl = server
    ctl._factory = lambda profile, model=None: _PRDStub()
    status, res = _post(base, "/api/research/start", token,
                        {"goal": "spec it", "max_experiments": 1, "mode": "learn"})
    assert status == 200 and res["ok"]
    assert ctl.wait(30)
    status, body = _get(base, "/api/research/prds", token)
    assert status == 200                         # route registered (not 404)
    obj = json.loads(body)
    assert obj["ok"]
    assert obj["prds"][0]["title"] == "Ship the widget"
    assert obj["prds"][0]["summary"] == "builds the widget"


def test_research_stop_halts_active_run(server):
    base, token, ctl = server
    ctl._trainer_cmd = [sys.executable, str(MOCK), "--mode", "hang"]
    _, res = _post(base, "/api/research/start", token, {"goal": "g"})
    assert res["ok"]
    time.sleep(0.5)                              # let the hang trainer spawn
    _, res = _post(base, "/api/research/stop", token, {})
    assert res["ok"]
    assert ctl.wait(10)
    _, body = _get(base, "/api/research/status", token)
    assert json.loads(body)["state"] == "stopped"
