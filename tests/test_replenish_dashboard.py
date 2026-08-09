# tests/test_replenish_dashboard.py
"""The `new` marker where the operator actually looks.

Two surfaces have to agree and both are asserted: the Oversee API must carry the
flag off the roadmap fold, and the served page must contain the render hook. A
page test alone would pass against an API that never sends the field, and an API
test alone would pass against a page that never draws it.
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, events, replenish_sinks, roadmap
from mindhive.replenish import Candidate

SID = "sess-dash-01"


@pytest.fixture
def server():
    token = "test-token-123"
    handler = dashboard.make_handler(token)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", token
    finally:
        srv.shutdown()


def _get(base, path, token=None):
    url = base + path
    if token:
        url += ("&" if "?" in url else "?") + "token=" + token
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def _inject():
    events.append_event("user_prompt", {"text": "add the parser"},
                        session_id=SID)
    replenish_sinks.roadmap_sink(SID)([
        Candidate(lens="evidence-gap", title="Run the test suite",
                  fingerprint="fp-1", evidence=("a real pointer",),
                  severity=True)])


def test_the_oversee_api_carries_the_new_flag(server):
    base, token = server
    _inject()
    status, body = _get(base, "/api/oversee", token)
    assert status == 200
    data = json.loads(body)
    steps = [s for plan in (data.get("plans") or [])
             for s in plan.get("steps") or []]
    steps += [s for card in (data.get("runs") or [])
              for s in ((card.get("roadmap") or {}).get("steps") or [])]
    assert steps, "the injected plan reached neither a card nor the plans list"
    assert any(s.get("new") is True for s in steps)


def test_an_ordinary_step_is_not_marked_new(server):
    base, token = server
    payload = {"action": "set", "plan_id": SID, "prompt": "ask",
               "title": "ask", "steps": [{"text": "from the plan",
                                          "status": "active"}]}
    roadmap.validate_payload(payload)
    events.append_event("roadmap", payload, session_id=SID,
                        tags=["roadmap", "auto"])
    _status, body = _get(base, "/api/oversee", token)
    data = json.loads(body)
    steps = [s for plan in (data.get("plans") or [])
             for s in plan.get("steps") or []]
    steps += [s for card in (data.get("runs") or [])
              for s in ((card.get("roadmap") or {}).get("steps") or [])]
    assert steps and all(s.get("new") is False for s in steps)


def test_the_served_page_draws_the_marker(server):
    base, _token = server
    _status, body = _get(base, "/")
    assert "rmnew" in body
    # The pre-existing Oversee hooks must still be there — a render change that
    # took the section out with it is the failure mode worth catching.
    assert "showOversee" in body and "renderOversee" in body and "rmPanel" in body


# --- the progress list, not just the roadmap panel ---------------------------
# Master-prompt 4.1/4.2/4.3: the card's own done/planned/not_yet groups carry
# the flag too, so a reader who never opens the roadmap panel still sees that
# the plan grew.

def test_a_chat_card_carries_landed_work_as_a_marked_planned_item():
    from mindhive import oversee, replenish
    events.append_event("user_prompt", {"text": "add the parser"},
                        session_id=SID)
    replenish.run_round(
        events.read_all(), surface="session",
        sink=replenish_sinks.roadmap_sink(SID),
        lenses={"l": lambda _e: [Candidate(
            lens="evidence-gap", title="Run the test suite",
            fingerprint="fp-card", evidence=("a real pointer",),
            severity=True)]},
        session_id=SID)
    cards = oversee.interactive_sessions()
    card = next(c for c in cards if c["run_id"] == SID)
    assert {"text": "Run the test suite", "is_new": True} in card["planned"]


def test_a_refused_row_does_not_reach_the_card():
    from mindhive import oversee
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    events.append_event(
        "work_injected",
        {"outcome": "refused", "round": 1, "surface": "session", "lens": "l",
         "title": "not worth doing", "fingerprint": "fp-r",
         "reason": "no evidence"}, session_id=SID, tags=["replenish"])
    card = next(c for c in oversee.interactive_sessions()
                if c["run_id"] == SID)
    assert card["planned"] == []


def test_a_malformed_work_injected_row_does_not_break_the_card():
    from mindhive import oversee
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    events.append_event("work_injected", ["not", "a", "dict"], session_id=SID)
    card = next(c for c in oversee.interactive_sessions()
                if c["run_id"] == SID)
    assert card["planned"] == []


def test_the_row_renderer_tolerates_both_shapes_and_escapes_the_text(server):
    base, _token = server
    _status, body = _get(base, "/")
    # ovRow must accept a bare string (chat cards) and {text, is_new} (loop
    # cards). Escaping stays on the text; the badge is the only markup.
    assert "function ovRow(" in body
    assert "typeof entry === 'object'" in body
    assert "obj.is_new" in body
    assert "esc(obj.text" in body
