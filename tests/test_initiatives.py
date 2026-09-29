# tests/test_initiatives.py — the `initiative_declared` type and the one listing
# of every initiative (blueprint BAB 5.3.2; plan phase P1b).
"""`initiatives.py`: the declaration writer, the type's own contract (which
refuses the ``work_session`` shape and any reason or ref that is not in the
cleaned form its tag carries), the LEDGER body, and `initiatives`, which lists
declarations AND ``[INITIATIVE: …]`` steps -- so a step written through the
§8.4 API is never an initiative nobody can see."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from mindhive import contracts, events, initiatives
from mindhive import worksessions as ws

TS = "2026-09-10T15:20:00Z"


def _clock(iso=TS):
    return lambda: datetime.strptime(iso, ws.TS_FORMAT).replace(tzinfo=timezone.utc)


def _rows(etype="initiative_declared"):
    return [e for e in events.read_all("test") if e.type == etype]


# --- 1. the writer ------------------------------------------------------------------

def test_declare_initiative_appends_the_row_and_returns_its_tag():
    tag = initiatives.declare_initiative("sess-1", "circuit breaker lokal",
                                         "incident:tool:ripgrep-timeout", project="test")
    assert tag == "[INITIATIVE: circuit breaker lokal | Ref: incident:tool:ripgrep-timeout]"
    (row,) = _rows()
    assert row.session_id == "sess-1" and row.tags == ["initiative"]
    assert row.payload == {"reason": "circuit breaker lokal",
                           "ref": "incident:tool:ripgrep-timeout", "tag": tag}
    contracts.validate_typed_payload("initiative_declared", row.payload)


def test_declare_initiative_stores_the_cleaned_reason_and_ref():
    forged = initiatives.declare_initiative("s", "audit | Ref: INC-999", project="test")
    honest = initiatives.declare_initiative("s", "audit", "INC-999", project="test")
    assert forged == "[INITIATIVE: audit Ref: INC-999]" != honest
    spaced = initiatives.declare_initiative("s", "  x  ", "  a]\n[b ", project="test")
    assert spaced == "[INITIATIVE: x | Ref: a b]"
    payloads = [r.payload for r in _rows()]
    assert payloads[0] == {"reason": "audit Ref: INC-999", "ref": "", "tag": forged}
    assert payloads[2] == {"reason": "x", "ref": "a b", "tag": spaced}
    for p in payloads:
        contracts.validate_typed_payload("initiative_declared", p)


@pytest.mark.parametrize("reason", ["   ", "", " | [ ] ", None, 3])
def test_declare_initiative_refuses_a_reason_that_cleans_to_nothing(reason):
    with pytest.raises(ValueError, match="'reason'"):
        initiatives.declare_initiative("s", reason, project="test")
    assert _rows() == []


# --- 2. the contract ------------------------------------------------------------------

def _valid(**over):
    p = {"reason": "r", "ref": "i:1", "tag": "[INITIATIVE: r | Ref: i:1]"}
    p.update(over)
    return p


def test_validate_payload_accepts_the_two_written_shapes():
    initiatives.validate_payload(_valid())
    initiatives.validate_payload({"reason": "r", "ref": "", "tag": "[INITIATIVE: r]"})
    initiatives.validate_payload({"reason": "r", "tag": "[INITIATIVE: r]"})   # ref optional


@pytest.mark.parametrize("bad, needle", [
    ({"tag": "[INITIATIVE: other]"}, "'tag'"),
    ({"tag": None}, "'tag'"),
    ({"reason": None}, "'reason'"),
    ({"reason": "   "}, "'reason'"),
    ({"ref": 3}, "'ref'"),
    ({"reason": "a | Ref: b", "tag": "[INITIATIVE: a | Ref: b]"}, "'reason'"),
    ({"reason": "a  b"}, "'reason'"),
    ({"reason": "x" * 201}, "'reason'"),
    ({"ref": "a]b"}, "'ref'"),
    ({"ref": " i:1"}, "'ref'"),
])
def test_validate_payload_names_the_offending_key(bad, needle):
    with pytest.raises(ValueError) as ei:
        initiatives.validate_payload(_valid(**bad))
    assert needle in str(ei.value)


def test_validate_payload_refuses_the_work_session_shape_and_non_objects():
    with pytest.raises(ValueError, match=r"\['action', 'session_id', 'status', 'step_id'\] "
                                         r"milik work_session"):
        initiatives.validate_payload({"action": "step_status", "session_id": "x",
                                      "step_id": "y", "status": "active"})
    with pytest.raises(ValueError, match=r"\['phase'\] milik work_session"):
        initiatives.validate_payload(_valid(phase="plan"))
    with pytest.raises(ValueError, match="objek JSON"):
        initiatives.validate_payload(["r"])


def test_a_forgotten_json_flag_is_named():
    with pytest.raises(ValueError, match=r"tanpa --json, `hive append`") as ei:
        contracts.validate_typed_payload("initiative_declared", {"text": '{"reason": "r"}'})
    assert "(use: hive worksession initiative" in str(ei.value)


def test_ledger_body_prefers_the_stored_tag():
    assert initiatives.ledger_body(_valid()) == "[initiative] [INITIATIVE: r | Ref: i:1]"
    assert initiatives.ledger_body({"reason": "r"}) == "[initiative] [INITIATIVE: r]"
    assert initiatives.ledger_body("text") == \
        f"[initiative] [INITIATIVE: {ws.INITIATIVE_DEFAULT_REASON}]"


# --- 3. the listing -----------------------------------------------------------------------

def test_initiatives_lists_declarations_in_ledger_order():
    initiatives.declare_initiative("s1", "first", "i:1", project="test")
    initiatives.declare_initiative("s2", "second", project="test")
    rows = initiatives.initiatives(events.read_all("test"))
    assert [r["reason"] for r in rows] == ["first", "second"]
    assert rows[0] == {"session_id": "s1", "reason": "first", "ref": "i:1",
                       "tag": "[INITIATIVE: first | Ref: i:1]", "ts": rows[0]["ts"],
                       "step_id": ""}
    assert rows[1]["ref"] == "" and rows[1]["tag"] == "[INITIATIVE: second]"


def test_an_initiative_step_written_through_the_api_is_listed():
    step = ws.create_work_session_step("quality assessment", "TDD_GREEN", True,
                                       initiative_reason="autonomous extra",
                                       initiative_ref="finding:3", now_fn=_clock())
    ws.record_work_session_step("S", step, project="test")
    ws.record_work_session_step("S", replace(
        ws.create_work_session_step("plan", "user asked", False, now_fn=_clock()),
        step_id="spec-step"), project="test")
    initiatives.declare_initiative("S", "not one step", project="test")
    rows = initiatives.initiatives(events.read_all("test"))
    assert [(r["reason"], r["ref"], r["step_id"]) for r in rows] == [
        ("autonomous extra", "finding:3", step.step_id), ("not one step", "", "")]
    assert rows[0]["tag"] == step.initiative_tag and rows[0]["session_id"] == "S"
    assert _rows() and len(_rows("work_session")) == 2


def test_the_listing_skips_what_the_fold_would_not_keep():
    step = ws.create_work_session_step("plan", "x", True, initiative_reason="once",
                                       now_fn=_clock())
    payload = {"action": "step_append", "session_id": "s1", "step_id": step.step_id,
               "phase": "plan", "ws_tag": step.technical_action,
               "initiative_tag": step.initiative_tag, "usage_tag": step.usage_tag,
               "status": "pending", "timestamp": TS}
    events.append_event("work_session", payload, session_id="s1", project="test")
    events.append_event("work_session", payload, session_id="s1", project="test")  # repeat id
    events.append_event("work_session", dict(payload, session_id="s2", step_id="other"),
                        session_id="s1", project="test")                         # foreign
    events.append_event("work_session", dict(payload, step_id="bad", phase="nope"),
                        session_id="s1", project="test")                         # off-axis
    events.append_event("work_session", dict(payload, step_id="forged",
                                             initiative_tag="[INITIATIVE: a  b]"),
                        session_id="s1", project="test")                         # not a tag
    events.append_event("initiative_declared", {"ref": "x"}, session_id="s1",
                        project="test")                                          # no reason
    events.append_event("initiative_declared", "text", session_id="s1", project="test")
    rows = initiatives.initiatives(events.read_all("test"))
    assert [(r["session_id"], r["step_id"], r["reason"]) for r in rows] == \
        [("s1", step.step_id, "once")]


def test_a_declaration_without_a_stored_tag_gets_the_built_one():
    events.append_event("initiative_declared", {"reason": "r", "ref": "i:2"},
                        session_id="s1", project="test")
    (row,) = initiatives.initiatives(events.read_all("test"))
    assert row["tag"] == "[INITIATIVE: r | Ref: i:2]"
