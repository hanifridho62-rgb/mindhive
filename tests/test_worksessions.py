# tests/test_worksessions.py — Work Sessions and the four tag variants
# (blueprint BAB 5 + section 8.4; MEB SES-01; plan phase P1b).
"""The ``work_session`` owner: every builder, parser, the contract, the
writers, the fold, and the `hive worksession` CLI, against the document's own
literals. Companions: `tests/test_initiatives.py` (``initiative_declared``),
`tests/test_worksession_usage.py` (the context bar) and
`tests/test_worksession_migrate.py` (the roadmap bridge). The store is the
conftest-isolated one, so the append tests write real rows and read them back
through `events.read_all`."""
import argparse
import io
import json
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from mindhive import consolidate, contracts, dedupe, events, initiatives, projector
from mindhive import worksessions as ws
from mindhive.mindnet import tasks as mn_tasks

TS = "2026-09-10T15:20:00Z"


def _clock(iso=TS):
    return lambda: datetime.strptime(iso, ws.TS_FORMAT).replace(tzinfo=timezone.utc)


def _action_of(tag):
    return ws.parse_work_session_tag(tag)[1]


# --- 1. the phase axis -----------------------------------------------------------

def test_the_ten_canonical_phases_are_the_documents_in_order():
    assert ws.CANONICAL_PHASES == (
        "brainstorm", "plan", "observation", "understand-and-converge",
        "PRD", "recommendation matrix", "kanban", "execution",
        "quality assessment", "learning")


def test_work_stages_is_the_same_tuple_object_and_not_the_mindnet_spine():
    assert ws.WORK_STAGES is ws.CANONICAL_PHASES
    assert ws.WORK_STAGES != mn_tasks.PHASES
    assert len(ws.WORK_STAGES) == 10 and len(mn_tasks.PHASES) == 8


# --- 2. the action catalog (BAB 5.4 + section 8.4) ----------------------------------

BAB5_KEYS = ("REF_AST_PARSE", "FIX_PATH_TRAVERSAL", "LOCK_MUTEX_ACQUIRE", "VEC_BATCH_EMBED",
             "TDD_RED_SUITE", "TDD_GREEN_REFACTOR", "GIT_WT_ISOLATE", "CTX_TRIM_BUDGET")
BAB8_KEYS = ("AST_PARSE", "REGEX_SANITIZE", "LOCK_ACQUIRE", "VEC_EMBED_CPU", "TDD_RED",
             "TDD_GREEN", "WT_ISOLATE", "TAIL_TRIM", "CI_INTERCEPT")


def test_both_catalogs_are_present_and_merged():
    assert tuple(ws.ACTION_CATALOG_BAB5) == BAB5_KEYS
    assert tuple(ws.ACTION_CATALOG_BAB8) == BAB8_KEYS
    assert ws.ACTION_CATALOG == {**ws.ACTION_CATALOG_BAB5, **ws.ACTION_CATALOG_BAB8}
    assert len(ws.ACTION_CATALOG) == 17


def test_the_documents_descriptions_are_verbatim():
    assert ws.ACTION_CATALOG["FIX_PATH_TRAVERSAL"] == \
        "Sanitasi regex whitelist pada parameter session_id"
    assert ws.ACTION_CATALOG["CTX_TRIM_BUDGET"] == \
        "Pemotongan konteks adaptif sesuai plafon byte dan baris MEMORY.md"
    assert ws.ACTION_CATALOG["AST_PARSE"] == \
        "Analisis statis AST untuk ekstraksi fungsi, kelas, dan siklomatik"
    assert ws.ACTION_CATALOG["CI_INTERCEPT"] == \
        "Intersepsi siklus hidup status penggabungan PR dan pipeline CI"


def test_every_catalog_text_fits_the_ws_tag_unchanged():
    for text in ws.ACTION_CATALOG.values():
        assert ws.clean_tag_text(text, ws.WS_ACTION_MAX_CHARS) == text


def test_expand_action_expands_a_key_and_passes_text_through():
    assert ws.expand_action("FIX_PATH_TRAVERSAL") == \
        "Sanitasi regex whitelist pada parameter session_id"
    assert ws.expand_action("hand-written action") == "hand-written action"


# --- 3. the four tag builders and their parsers ----------------------------------------

def test_clean_tag_text_breaks_nothing_and_cuts_at_the_limit():
    assert ws.clean_tag_text("a|b [c]\r\nd") == "a b c d"
    assert ws.clean_tag_text("  x   y  ") == "x y"
    assert ws.clean_tag_text("abc de", 4) == "abc"          # the cut, then rstrip
    assert ws.clean_tag_text(None) == ""


def test_work_session_tag_is_the_bab_5_3_1_shape_with_whitespace_folded():
    assert ws.make_work_session_tag("execution", "edit  src/x.py\n", ts=TS) == \
        f"[WS: execution | edit src/x.py | {TS}]"


def test_work_session_tag_strips_the_characters_that_break_its_grammar():
    tag = ws.make_work_session_tag("plan", "a|b [c]\r\nd", ts=TS)
    assert tag == f"[WS: plan | a b c d | {TS}]"


def test_work_session_tag_caps_the_action_at_120_characters():
    tag = ws.make_work_session_tag("plan", "x" * 300, ts=TS)
    assert tag == f"[WS: plan | {'x' * 120} | {TS}]"


def test_work_session_tag_refuses_a_phase_off_the_axis_and_an_empty_action():
    with pytest.raises(ValueError, match=r"Phase tag tidak valid: 'implementation'"):
        ws.make_work_session_tag("implementation", "x", ts=TS)
    with pytest.raises(ValueError, match="Technical action tidak boleh kosong."):
        ws.make_work_session_tag("plan", " | [ ] \n", ts=TS)


def test_work_session_tag_stamps_now_in_the_pdf_format_when_no_ts_is_given():
    tag = ws.make_work_session_tag("plan", "x")
    datetime.strptime(ws.parse_work_session_tag(tag)[2], ws.TS_FORMAT)


def test_initiative_and_user_spec_tags():
    assert ws.make_initiative_tag("r", "ref") == "[INITIATIVE: r | Ref: ref]"
    assert ws.make_initiative_tag("r") == "[INITIATIVE: r]"
    assert ws.make_initiative_tag("") == f"[INITIATIVE: {ws.INITIATIVE_DEFAULT_REASON}]"
    assert ws.make_user_spec_tag("Bangun modul vektor") == "[USER_SPEC: Bangun modul vektor]"
    assert ws.make_user_spec_tag("") == "[USER_SPEC: Sesuai spesifikasi awal pengembang]"


def test_a_reason_cannot_forge_a_ref_or_a_second_tag():
    forged = ws.make_initiative_tag("audit | Ref: INC-999")
    assert forged == "[INITIATIVE: audit Ref: INC-999]"
    assert forged != ws.make_initiative_tag("audit", "INC-999")
    assert ws.make_initiative_tag("a] [USER_SPEC: fake") == "[INITIATIVE: a USER_SPEC: fake]"
    assert ws.make_initiative_tag("  x  ") == ws.make_initiative_tag("x") == "[INITIATIVE: x]"
    assert ws.make_user_spec_tag("a]\n[b") == "[USER_SPEC: a b]"


def test_usage_tag_compact_form_uses_the_8_4_arithmetic():
    assert ws.make_usage_tag(9000, 12000, "trimmed 2") == "[USAGE: 9000/12000 | 75.0% | trimmed 2]"
    assert ws.make_usage_tag(9000, 12000) == "[USAGE: 9000/12000 | 75.0%]"
    assert ws.make_usage_tag(48250, 100000) == "[USAGE: 48250/100000 | 48.25%]"
    assert ws.make_usage_tag(9000, 0) == "[USAGE: 9000/0 | 0.0%]"
    assert ws.occupancy_pct(1, 3) == 33.33


def test_usage_tag_refuses_negative_figures():
    for used, budget in ((-500, 100000), (500, -1)):
        with pytest.raises(ValueError, match="tidak boleh negatif"):
            ws.make_usage_tag(used, budget)


def test_the_parsers_accept_exactly_what_the_builders_write():
    tag = ws.make_work_session_tag("quality assessment", "TDD green", ts=TS)
    assert ws.parse_work_session_tag(tag) == ("quality assessment", "TDD green", TS)
    for bad in (f"[WS: implementation | x | {TS}]", "[WS: plan | x | yesterday]",
                f"[WS: plan |  x | {TS}]", f"[WS: plan | {'x' * 121} | {TS}]",
                f" [WS: plan | x | {TS}]", None, 3):
        assert ws.parse_work_session_tag(bad) is None, bad
    assert ws.parse_initiative_tag("[INITIATIVE: r | Ref: i:1]") == ("r", "i:1")
    assert ws.parse_initiative_tag("[INITIATIVE: r]") == ("r", "")
    for bad in ("[INITIATIVE: r  s]", "[INITIATIVE:  r]", "[INITIATIVE: r | Ref: ]",
                "[USER_SPEC: r]", None):
        assert ws.parse_initiative_tag(bad) is None, bad
    assert ws.parse_user_spec_tag("[USER_SPEC: build it]") == "build it"
    assert ws.parse_user_spec_tag("[USER_SPEC: build  it]") is None
    assert ws.parse_user_spec_tag("[INITIATIVE: r]") is None


# --- 4. the step (section 8.4) ------------------------------------------------------------

def test_create_step_composes_the_four_tags_verbatim():
    step = ws.create_work_session_step("execution", "FIX_PATH_TRAVERSAL", False,
                                       tokens_used=9000, token_budget=12000,
                                       now_fn=_clock())
    assert isinstance(step, ws.WorkSessionStep)
    assert step.phase_tag == "execution"
    assert step.technical_action == \
        f"[WS: execution | Sanitasi regex whitelist pada parameter session_id | {TS}]"
    assert step.initiative_tag == "[USER_SPEC: Sesuai spesifikasi awal pengembang]"
    assert step.usage_tag == "[USAGE: 9000/12000 | 75.0%]"
    assert step.status == "pending" and step.timestamp == TS
    ms = int(_clock()().timestamp() * 1000) % 1000000
    assert step.step_id == f"step-{ms}"


def test_create_step_initiative_uses_the_reason_or_the_default_and_the_ref():
    a = ws.create_work_session_step("plan", "x", True, initiative_reason="fix flaky lock",
                                    now_fn=_clock())
    b = ws.create_work_session_step("plan", "x", True, now_fn=_clock())
    c = ws.create_work_session_step("plan", "x", True, initiative_reason="r",
                                    initiative_ref="incident:7", now_fn=_clock())
    assert a.initiative_tag == "[INITIATIVE: fix flaky lock]"
    assert b.initiative_tag == "[INITIATIVE: Penanganan temuan audit otonom]"
    assert c.initiative_tag == "[INITIATIVE: r | Ref: incident:7]"


def test_every_writer_path_runs_the_tags_through_their_builders():
    step = ws.create_work_session_step("plan", "x" * 300 + " | y\nz", True,
                                       initiative_reason="a] [USER_SPEC: fake",
                                       now_fn=_clock())
    action = _action_of(step.technical_action)
    assert len(action) <= ws.WS_ACTION_MAX_CHARS and "|" not in action and "\n" not in action
    forged = ws.create_work_session_step("execution", f"one | 2020-01-01T00:00:00Z] {'x' * 5}",
                                         False, now_fn=_clock())
    assert ws.parse_work_session_tag(forged.technical_action)[2] == TS
    assert step.initiative_tag == "[INITIATIVE: a USER_SPEC: fake]"
    assert ws.create_work_session_step("plan", "x", False, tokens_used=500, token_budget=0,
                                       now_fn=_clock()).usage_tag == "[USAGE: 500/0 | 0.0%]"
    with pytest.raises(ValueError, match="negatif"):
        ws.create_work_session_step("plan", "x", False, tokens_used=-500, now_fn=_clock())


def test_create_step_refuses_a_bogus_phase_with_the_documents_message():
    with pytest.raises(ValueError) as ei:
        ws.create_work_session_step("bogus", "x", False)
    assert str(ei.value) == (f"Phase tag tidak valid: 'bogus'. Harus salah satu dari: "
                             f"{ws.CANONICAL_PHASES}")


def test_the_step_is_frozen_and_the_session_is_not():
    step = ws.create_work_session_step("plan", "x", False, now_fn=_clock())
    with pytest.raises(Exception):
        step.status = "active"        # frozen dataclass
    sess = ws.WorkSession(session_id="s", project="p", title="t")
    assert sess.steps == [] and sess.active_step_index == 0 and sess.status == "active"
    sess.steps.append(step)
    assert len(sess.steps) == 1
    assert ws.STEP_STATUSES == ("active", "completed", "pending", "blocked")


# --- 5. the writers --------------------------------------------------------------------

def _rows(etype, project="test"):
    return [e for e in events.read_all(project) if e.type == etype]


def test_record_step_appends_the_verbatim_payload_and_returns_the_step():
    step = ws.create_work_session_step("execution", "TDD_RED", False, now_fn=_clock())
    assert ws.record_work_session_step("sess-1", step, project="test") == step
    (ev,) = _rows("work_session")
    assert ev.session_id == "sess-1"
    assert ev.tags == ["work_session", "execution"]
    assert ev.payload == {
        "action": "step_append", "session_id": "sess-1", "step_id": step.step_id,
        "phase": "execution", "ws_tag": step.technical_action,
        "initiative_tag": step.initiative_tag, "usage_tag": step.usage_tag,
        "status": "pending", "timestamp": TS}
    contracts.validate_typed_payload("work_session", ev.payload)   # accepted
    assert _rows("initiative_declared") == []


def test_record_step_status_appends_a_stamped_move():
    ev = ws.record_step_status("sess-1", "step-1", "completed", project="test",
                               now_fn=_clock())
    assert ev.payload == {"action": "step_status", "session_id": "sess-1",
                          "step_id": "step-1", "status": "completed", "timestamp": TS}
    contracts.validate_typed_payload("work_session", ev.payload)
    with pytest.raises(ValueError, match="status"):
        ws.record_step_status("sess-1", "step-1", "finished", project="test")


def test_a_colliding_step_id_is_suffixed_and_moves_only_its_own_step():
    one = ws.create_work_session_step("plan", "one", False, now_fn=_clock())
    two = ws.create_work_session_step("execution", "two", False, now_fn=_clock())
    three = ws.create_work_session_step("kanban", "three", False, now_fn=_clock())
    assert one.step_id == two.step_id == three.step_id
    got = [ws.record_work_session_step("c1", s, project="test") for s in (one, two, three)]
    assert [s.step_id for s in got] == [one.step_id, f"{one.step_id}-2", f"{one.step_id}-3"]
    # Another session holds its own ids: no suffix there.
    assert ws.record_work_session_step("c2", one, project="test").step_id == one.step_id
    ws.record_step_status("c1", got[1].step_id, "completed", project="test")
    steps = ws.fold(events.read_all("test"))["c1"].steps
    assert [(_action_of(s.technical_action), s.status) for s in steps] == \
        [("one", "pending"), ("two", "completed"), ("three", "pending")]


def test_session_events_reads_one_session_in_append_order():
    for sid, action in (("a", "one"), ("b", "other"), ("a", "two")):
        ws.record_work_session_step(sid, replace(
            ws.create_work_session_step("plan", action, False, now_fn=_clock()),
            step_id=f"id-{action}"), project="test")
    got = ws.session_events("a", "test")
    assert [e.payload["step_id"] for e in got] == ["id-one", "id-two"]
    assert ws.session_events("nobody", "test") == []


# --- 6. the append-surface contract ------------------------------------------------------

def _valid_step_payload(**over):
    step = ws.create_work_session_step("execution", "x", False, now_fn=_clock())
    p = {"action": "step_append", "session_id": "s", "step_id": step.step_id,
         "phase": "execution", "ws_tag": step.technical_action,
         "initiative_tag": step.initiative_tag, "usage_tag": step.usage_tag,
         "status": "pending", "timestamp": TS}
    p.update(over)
    return p


@pytest.mark.parametrize("bad, key", [
    ({"phase": None}, "phase"),
    ({"phase": "implementation"}, "phase"),
    ({"phase": "plan"}, "ws_tag"),                                   # tag names execution
    ({"action": "step_delete"}, "action"),
    ({"is_initiative": "yes"}, "is_initiative"),
    ({"status": "finished"}, "status"),
    ({"ws_tag": "execution | x"}, "ws_tag"),
    ({"ws_tag": f"[WS: execution | x | 2020-01-01T00:00:00Z]"}, "ws_tag"),  # stamp differs
    ({"ws_tag": f"[WS: execution | a | b | {TS}]"}, "ws_tag"),
    ({"initiative_tag": "[FOO: x]"}, "initiative_tag"),
    ({"initiative_tag": "[INITIATIVE: a  b]"}, "initiative_tag"),
    ({"usage_tag": "9000/12000"}, "usage_tag"),
    ({"step_id": ""}, "step_id"),
    ({"step_id": "a b"}, "step_id"),
    ({"step_id": "x" * 129}, "step_id"),
    ({"session_id": 3}, "session_id"),
    ({"timestamp": "yesterday"}, "timestamp"),
    ({"timestamp": None}, "timestamp"),
])
def test_validate_payload_names_the_offending_key(bad, key):
    p = _valid_step_payload(**bad)
    with pytest.raises(ValueError) as ei:
        ws.validate_payload(p)
    assert f"'{key}'" in str(ei.value)


def test_validate_payload_accepts_both_actions_with_their_keys():
    ws.validate_payload(_valid_step_payload())
    ws.validate_payload(_valid_step_payload(
        initiative_tag="[INITIATIVE: r | Ref: i:1]", is_initiative=True))
    ws.validate_payload({"action": "step_status", "session_id": "s", "step_id": "x",
                         "status": "blocked", "timestamp": TS})
    with pytest.raises(ValueError, match="'timestamp'"):
        ws.validate_payload({"action": "step_status", "session_id": "s", "step_id": "x",
                             "status": "blocked"})
    with pytest.raises(ValueError, match="'phase'"):
        ws.validate_payload({"action": "step_append", "session_id": "s", "step_id": "x",
                             "status": "pending", "timestamp": TS})


def test_validate_payload_refuses_the_initiative_declared_shape():
    with pytest.raises(ValueError, match=r"milik initiative_declared") as ei:
        ws.validate_payload({"reason": "r", "ref": "", "tag": "[INITIATIVE: r]"})
    assert "['reason', 'ref', 'tag']" in str(ei.value)
    with pytest.raises(ValueError, match=r"\['tag'\] milik initiative_declared"):
        ws.validate_payload(_valid_step_payload(tag="[INITIATIVE: r]"))


def test_validate_payload_names_what_it_needs_on_empty_and_on_a_stranger():
    with pytest.raises(ValueError) as ei:
        ws.validate_payload({})
    assert str(ei.value).startswith("payload kosong: ") and "'action'" in str(ei.value)
    with pytest.raises(ValueError) as ei:
        ws.validate_payload({"note": "x", "k": 1})
    msg = str(ei.value)
    assert msg.startswith("payload tidak dikenali, kunci diterima: ['k', 'note']")
    assert "'action'" in msg and "--json" not in msg
    # The shape `hive append work_session '{...}'` sends when --json is forgotten.
    with pytest.raises(ValueError, match=r"tanpa --json, `hive append`") as ei:
        contracts.validate_typed_payload("work_session", {"text": '{"action": "step_status"}'})
    assert "(use: hive worksession record" in str(ei.value)
    with pytest.raises(ValueError, match="objek JSON"):
        ws.validate_payload("nope")


def test_ledger_bodies_carry_every_tag():
    p = _valid_step_payload()
    body = ws.ledger_body(p)
    for part in ("[step_append]", p["step_id"], "execution", p["ws_tag"],
                 p["initiative_tag"], p["usage_tag"], "pending"):
        assert part in body
    assert ws.ledger_body({"action": "step_status", "step_id": "s1", "session_id": "s",
                           "status": "completed"}) == "[step_status] s1 (s) -> completed"


# --- 7. registration (docs/DATA_MODEL.md §6, all steps) --------------------------------

def test_both_types_are_registered_each_with_its_own_contract_module():
    for t in ("work_session", "initiative_declared"):
        assert t in events.SEMANTIC_TYPES and t in events.VALID_TYPES
        assert t in consolidate.NEVER_CONSOLIDATE and t in dedupe.NEVER_COLLAPSE
        assert "hive worksession" in contracts._PAYLOAD_CONTRACTS[t][1]
        with pytest.raises(ValueError, match=r"use: hive worksession"):
            contracts.validate_typed_payload(t, {})
    assert contracts._PAYLOAD_CONTRACTS["work_session"][0] == ".worksessions"
    assert contracts._PAYLOAD_CONTRACTS["initiative_declared"][0] == ".initiatives"
    assert "record --phase" in contracts._PAYLOAD_CONTRACTS["work_session"][1]
    assert "initiative --reason" in contracts._PAYLOAD_CONTRACTS["initiative_declared"][1]


def test_neither_type_accepts_the_others_payload():
    step = _valid_step_payload()
    with pytest.raises(ValueError, match="milik work_session"):
        contracts.validate_typed_payload("initiative_declared", step)
    with pytest.raises(ValueError, match="milik work_session"):
        contracts.validate_typed_payload("initiative_declared", {
            "action": "step_status", "session_id": "x", "step_id": "y", "status": "active"})
    with pytest.raises(ValueError, match="milik initiative_declared"):
        contracts.validate_typed_payload("work_session",
                                         {"reason": "hand", "tag": "[INITIATIVE: hand]"})


def test_work_session_rows_stay_out_of_the_briefing_timeline():
    from mindhive import briefing
    assert {"work_session", "initiative_declared"}.isdisjoint(briefing._TIMELINE_TYPES)
    events.append_event("decision", {"text": "keep the ledger append-only"},
                        session_id="s1", project="test")
    import re
    count = re.compile(r"_events: \d+_")        # the footer counts every row
    before = briefing.build_briefing(events.read_all("test"))
    ws.record_work_session_step("s1", ws.create_work_session_step(
        "plan", "x", True, now_fn=_clock()), project="test")
    initiatives.declare_initiative("s1", "r", project="test")
    after = briefing.build_briefing(events.read_all("test"))
    assert "_events: 3_" in after
    assert count.sub("", after) == count.sub("", before)


def test_projector_renders_non_empty_ledger_bodies_for_both_types():
    step = ws.create_work_session_step("kanban", "x", True, initiative_reason="r",
                                       now_fn=_clock())
    ws.record_work_session_step("s9", step, project="test")
    initiatives.declare_initiative("s9", "r", "inc:1", project="test")
    entries = projector.build_ledger_entries(events.read_all("test"))
    by_type = {ev.type: content for _f, content, ev in entries}
    assert "kanban" in by_type["work_session"] and step.technical_action in by_type["work_session"]
    assert "[INITIATIVE: r | Ref: inc:1]" in by_type["initiative_declared"]


# --- 8. the fold -------------------------------------------------------------------------

def _step(phase, action, status="pending", step_id=None, **kw):
    s = ws.create_work_session_step(phase, action, False, now_fn=_clock(), **kw)
    return replace(s, status=status, step_id=step_id or s.step_id)


def test_fold_builds_one_session_per_id_in_append_order():
    a = _step("plan", "one", status="completed", step_id="step-a")
    b = _step("execution", "two", step_id="step-b")
    ws.record_work_session_step("s1", a, project="test")
    ws.record_work_session_step("s1", b, project="test")
    ws.record_work_session_step("s2", _step("brainstorm", "three", status="active"),
                                project="test")
    folded = ws.fold(events.read_all("test"), "test")
    assert set(folded) == {"s1", "s2"}
    s1 = folded["s1"]
    assert isinstance(s1, ws.WorkSession) and s1.project == "test"
    assert s1.title == a.technical_action
    assert [s.step_id for s in s1.steps] == ["step-a", "step-b"]
    assert s1.active_step_index == 1 and s1.status == "active"
    assert folded["s2"].active_step_index == 0 and folded["s2"].status == "active"


def test_fold_applies_step_status_by_replacing_the_frozen_step():
    a = _step("plan", "one")
    ws.record_work_session_step("s1", a, project="test")
    ws.record_step_status("s1", a.step_id, "active", project="test")
    folded = ws.fold(events.read_all("test"))
    assert folded["s1"].steps[0].status == "active"
    assert folded["s1"].active_step_index == 0
    ws.record_step_status("s1", a.step_id, "completed", project="test")
    folded = ws.fold(events.read_all("test"))
    assert folded["s1"].status == "completed"
    assert folded["s1"].steps[0].status == "completed"


def test_the_active_step_is_the_last_active_one_not_the_last_appended():
    for i, phase in enumerate(("plan", "execution", "quality assessment")):
        ws.record_work_session_step("t", _step(phase, f"step {i}", step_id=f"st-{i}"),
                                    project="test")
    ws.record_step_status("t", "st-0", "completed", project="test")
    ws.record_step_status("t", "st-1", "active", project="test")
    w = ws.fold(events.read_all("test"))["t"]
    assert w.active_step_index == 1 and w.steps[1].phase_tag == "execution"
    (row,) = ws.session_rows(events.read_all("test"), "test")
    assert row["phase"] == "execution" and _action_of(row["ws_tag"]) == "step 1"


def test_the_events_session_is_authoritative():
    good = _step("plan", "mine", step_id="k1")
    ws.record_work_session_step("s1", good, project="test")
    forged = {"action": "step_append", "session_id": "s2", "step_id": "k2", "phase": "plan",
              "ws_tag": good.technical_action, "initiative_tag": good.initiative_tag,
              "usage_tag": good.usage_tag, "status": "pending", "timestamp": TS}
    events.append_event("work_session", forged, session_id="s1", project="test")
    events.append_event("work_session", {"action": "step_status", "session_id": "s2",
                                         "step_id": "k1", "status": "completed",
                                         "timestamp": TS},
                        session_id="s1", project="test")
    folded = ws.fold(events.read_all("test"))
    assert set(folded) == {"s1"}
    assert [(s.step_id, s.status) for s in folded["s1"].steps] == [("k1", "pending")]


def test_the_first_row_for_a_step_id_wins():
    first = _step("plan", "first", step_id="dup")
    second = _step("execution", "second", step_id="dup")
    for s in (first, second):     # straight to the ledger: the writer would suffix
        events.append_event("work_session", {
            "action": "step_append", "session_id": "s1", "step_id": s.step_id,
            "phase": s.phase_tag, "ws_tag": s.technical_action,
            "initiative_tag": s.initiative_tag, "usage_tag": s.usage_tag,
            "status": s.status, "timestamp": s.timestamp}, session_id="s1", project="test")
    (step,) = ws.fold(events.read_all("test"))["s1"].steps
    assert _action_of(step.technical_action) == "first"


def test_fold_is_total_over_malformed_history():
    events.append_event("work_session", {"action": "step_status", "session_id": "ghost",
                                         "step_id": "x", "status": "completed"},
                        session_id="ghost", project="test")          # unknown session
    events.append_event("work_session", {"action": "step_append", "session_id": "s1",
                                         "step_id": "k", "phase": "not-a-phase",
                                         "status": "pending"},
                        session_id="s1", project="test")             # off-axis phase
    events.append_event("work_session", {"action": "step_append", "phase": "plan",
                                         "ws_tag": 3}, session_id="s1", project="test")
    events.append_event("work_session", "just text", session_id="s1", project="test")
    ws.record_work_session_step("s1", _step("plan", "ok"), project="test")
    ws.record_step_status("s1", "never-appended", "completed", project="test")
    folded = ws.fold(events.read_all("test"))
    assert set(folded) == {"s1"} and len(folded["s1"].steps) == 1


def test_session_rows_summarise_each_session_newest_first():
    early = ws.create_work_session_step("plan", "a", False,
                                        now_fn=_clock("2026-09-10T10:00:00Z"))
    late = ws.create_work_session_step("execution", "TDD_RED", True, initiative_reason="r",
                                       now_fn=_clock("2026-09-11T10:00:00Z"))
    ws.record_work_session_step("old", early, project="test")
    ws.record_work_session_step("new", late, project="test")
    ws.record_step_status("new", late.step_id, "active", project="test")
    rows = ws.session_rows(events.read_all("test"), "test")
    assert [r["session_id"] for r in rows] == ["new", "old"]
    assert rows[0] == {"session_id": "new", "project": "test", "title": late.technical_action,
                       "status": "active", "steps": 1, "active_step_index": 0,
                       "phase": "execution", "ws_tag": late.technical_action,
                       "initiative_tag": "[INITIATIVE: r]", "usage_tag": late.usage_tag,
                       "last_ts": "2026-09-11T10:00:00Z"}
    assert ws.session_rows([], "test") == []


def test_session_rows_break_a_timestamp_tie_newest_folded_first():
    for sid in ("first", "second", "third"):
        ws.record_work_session_step(sid, _step("plan", sid), project="test")
    rows = ws.session_rows(events.read_all("test"), "test")
    assert [r["session_id"] for r in rows] == ["third", "second", "first"]


# --- 9. the CLI handler and its parser -------------------------------------------

def _ns(**kw):
    base = dict(ws_cmd=None, phase=None, action=None, initiative=False, reason=None,
                ref=None, tokens_used=0, token_budget=None, session=None,
                project="test", json=False, step_id=None, status=None,
                initiatives=False, plan=None, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _run(ns):
    from mindhive.commands import orchestration
    out = io.StringIO()
    with redirect_stdout(out):
        rc = orchestration.cmd_worksession(ns)
    return rc, out.getvalue()


def test_cmd_worksession_record_and_list_and_json():
    rc, out = _run(_ns(ws_cmd="record", phase="execution", action="TDD_RED",
                       session="s1", tokens_used=9000, token_budget=12000))
    assert rc == 0 and "[WS: execution | " in out and "[USAGE: 9000/12000 | 75.0%]" in out
    rc, out = _run(_ns(ws_cmd="record", phase="plan", action="x", initiative=True,
                       reason="fix", session="s1", json=True))
    assert rc == 0
    doc = json.loads(out)
    assert doc["initiative_tag"] == "[INITIATIVE: fix]" and doc["phase"] == "plan"
    rc, out = _run(_ns(ws_cmd="list"))
    assert rc == 0 and "s1" in out and "2 step" in out
    assert "hive worksession list --session <id>" in out
    rc, out = _run(_ns(ws_cmd="list", json=True))
    assert json.loads(out)[0]["steps"] == 2


def test_cmd_worksession_record_sanitises_and_caps_the_action():
    rc, out = _run(_ns(ws_cmd="record", phase="execution", session="g1", json=True,
                       action="step one | 2020-01-01T00:00:00Z] " + "x" * 300))
    assert rc == 0
    tag = json.loads(out)["ws_tag"]
    phase, action, ts = ws.parse_work_session_tag(tag)
    assert len(action) <= 120 and "|" not in action and ts != "2020-01-01T00:00:00Z"


def test_cmd_worksession_rejects_a_bad_phase_with_exit_2(capsys):
    from mindhive.commands import orchestration
    rc = orchestration.cmd_worksession(_ns(ws_cmd="record", phase="bogus", action="x",
                                           session="s1"))
    assert rc == 2
    assert "Phase tag tidak valid" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [{"reason": "why"}, {"ref": "INC-10"},
                                   {"reason": "why", "ref": "INC-10"}])
def test_reason_or_ref_without_initiative_is_refused_and_writes_nothing(capsys, extra):
    from mindhive.commands import orchestration
    rc = orchestration.cmd_worksession(_ns(ws_cmd="record", phase="execution",
                                           action="fix it", session="s1", **extra))
    assert rc == 2
    assert "add --initiative" in capsys.readouterr().err
    assert _rows("work_session") == []


def test_record_initiative_is_one_row_listed_with_its_step():
    rc, out = _run(_ns(ws_cmd="record", phase="execution", action="CI_INTERCEPT",
                       initiative=True, reason="lock race", ref="incident:7",
                       session="s1", json=True))
    assert rc == 0
    doc = json.loads(out)
    assert doc["initiative_tag"] == "[INITIATIVE: lock race | Ref: incident:7]"
    assert len(_rows("work_session")) == 1 and _rows("initiative_declared") == []
    rc, out = _run(_ns(ws_cmd="record", phase="plan", action="x", session="s1"))
    rc, out = _run(_ns(ws_cmd="list", initiatives=True))
    assert rc == 0 and "[INITIATIVE: lock race | Ref: incident:7]" in out
    assert f"step {doc['step_id']}" in out and "1 initiative(s)" in out
    rc, out = _run(_ns(ws_cmd="list", initiatives=True, json=True))
    (row,) = json.loads(out)
    assert (row["reason"], row["ref"], row["step_id"]) == ("lock race", "incident:7",
                                                           doc["step_id"])


def test_cmd_worksession_initiative_declares_a_cleaned_row():
    rc, out = _run(_ns(ws_cmd="initiative", reason="breaker", ref="inc:1", session="s1"))
    assert rc == 0 and out.strip() == "[INITIATIVE: breaker | Ref: inc:1]"
    rc, out = _run(_ns(ws_cmd="initiative", reason="audit | Ref: INC-999", session="s1",
                       json=True))
    assert json.loads(out) == {"session_id": "s1", "reason": "audit Ref: INC-999", "ref": "",
                               "tag": "[INITIATIVE: audit Ref: INC-999]"}
    assert len(_rows("initiative_declared")) == 2


def test_cmd_worksession_status_moves_a_listed_step():
    rc, out = _run(_ns(ws_cmd="record", phase="plan", action="x", session="s1", json=True))
    step_id = json.loads(out)["step_id"]
    rc, out = _run(_ns(ws_cmd="list", session="s1"))
    assert rc == 0 and f"* {step_id}  [pending]" in out
    assert "hive worksession status --session s1 --step-id <id>" in out
    rc, out = _run(_ns(ws_cmd="status", session="s1", step_id=step_id, status="completed"))
    assert rc == 0 and "completed" in out
    assert ws.fold(events.read_all("test"))["s1"].status == "completed"
    rc, out = _run(_ns(ws_cmd="list", session="s1", json=True))
    doc = json.loads(out)
    assert (doc["session_id"], doc["project"], doc["status"]) == ("s1", "test", "completed")
    assert [s["step_id"] for s in doc["steps"]] == [step_id]


def test_cmd_worksession_refuses_unknown_steps_and_plans(capsys):
    from mindhive.commands import orchestration
    assert orchestration.cmd_worksession(
        _ns(ws_cmd="status", session="s1", step_id="nope", status="completed")) == 2
    err = capsys.readouterr().err
    assert "not in work session 's1'" in err and "hive worksession list --session s1" in err
    assert _rows("work_session") == []
    assert orchestration.cmd_worksession(_ns(ws_cmd="migrate", plan="P9")) == 2
    assert "no roadmap plan 'P9'" in capsys.readouterr().err


def test_cmd_worksession_migrate_reports_and_is_idempotent():
    events.append_event("roadmap", {
        "action": "set", "plan_id": "P1", "prompt": "p", "title": "t",
        "steps": [{"text": "a", "status": "done"}, {"text": "b", "status": "pending"}]},
        session_id="rs", project="test")
    rc, out = _run(_ns(ws_cmd="migrate", dry_run=True))
    assert rc == 0 and "P1 -> rs: 2 step(s), would write 2, 0 already present" in out
    assert _rows("work_session") == []                # dry run wrote nothing
    rc, out = _run(_ns(ws_cmd="migrate"))
    assert rc == 0 and "wrote 2" in out and len(_rows("work_session")) == 2
    rc, out = _run(_ns(ws_cmd="migrate", json=True))  # a re-run writes nothing
    assert json.loads(out) == [{"plan_id": "P1", "session_id": "rs", "steps": 2,
                                "present": 2, "stale": 0, "pending": 0, "drifted": 0,
                                "orphaned": 0, "settle": 0, "hand_moved": 0,
                                "written": 0, "settled": 0, "dry_run": False}]
    assert len(_rows("work_session")) == 2


def test_cmd_worksession_migrate_settles_a_moved_roadmap_and_says_so():
    events.append_event("roadmap", {
        "action": "set", "plan_id": "P1", "prompt": "p", "title": "t",
        "steps": [{"text": "a", "status": "active"}, {"text": "b", "status": "pending"},
                  {"text": "c", "status": "pending"}]},
        session_id="rs", project="test")
    _run(_ns(ws_cmd="migrate"))
    ws.record_step_status("rs", "P1-2", "blocked", project="test")          # by hand
    events.append_event("roadmap", {
        "action": "set", "plan_id": "P1", "prompt": "p", "title": "t",
        "steps": [{"text": "a", "status": "done"}, {"text": "b", "status": "done"}]},
        session_id="rs", project="test")
    rc, out = _run(_ns(ws_cmd="migrate", dry_run=True))
    assert rc == 0
    assert ("P1 -> rs: 2 step(s), would write 0, 2 already present, 2 drifted (the "
            "roadmap status moved), 1 orphaned (the step left the roadmap), would "
            "settle 2 status move(s)") in out
    assert "1 copy(ies) whose status was moved by hand left as they are" in out
    assert len([r for r in _rows("work_session")
                if r.payload["action"] == "step_status"]) == 1     # dry run wrote nothing
    rc, out = _run(_ns(ws_cmd="migrate", json=True))
    (doc,) = json.loads(out)
    assert (doc["drifted"], doc["orphaned"], doc["settle"], doc["settled"],
            doc["hand_moved"]) == (2, 1, 2, 2, 1)
    steps = {s.step_id: s.status for s in ws.fold(events.read_all("test"))["rs"].steps}
    assert steps == {"P1-1": "completed", "P1-2": "blocked", "P1-3": "completed"}


def test_concurrent_writers_to_one_session_never_lose_a_step():
    """The review's race: eight threads, one pinned clock, so every step is
    minted with the same §8.4 id. Each writer must get a distinct id back, and
    the fold must hold all eight."""
    import threading
    from mindhive import worksession_lock
    got, errors = [], []
    barrier = threading.Barrier(8)

    def writer(i):
        try:
            step = ws.create_work_session_step("execution", f"lane {i}", i == 3,
                                               initiative_reason="race", now_fn=_clock())
            barrier.wait()
            got.append(ws.record_work_session_step("race", step, project="test").step_id)
        except Exception as e:                              # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert len(set(got)) == 8
    folded = ws.fold(events.read_all("test"))["race"]
    assert sorted(s.step_id for s in folded.steps) == sorted(got)
    assert [i["reason"] for i in initiatives.initiatives(events.read_all("test"))] == ["race"]
    from mindhive import paths
    assert not (paths.project_dir("test") / worksession_lock.LOCK_FILE).exists()


def test_a_held_record_lock_fails_closed_with_a_named_timeout(monkeypatch):
    from mindhive import paths, worksession_lock
    from mindhive.core.locks import LockPolicy, LockTimeoutError
    monkeypatch.setattr(worksession_lock, "RECORD_POLICY",
                        LockPolicy(timeout_s=0.05, stale_s=60.0))
    lock = paths.project_dir("test") / worksession_lock.LOCK_FILE
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("another writer", encoding="utf-8")
    with pytest.raises(LockTimeoutError, match="Gagal memperoleh worksession_lock"):
        ws.record_work_session_step("s1", _step("plan", "x"), project="test")
    assert _rows("work_session") == []                    # nothing appended unlocked
    assert isinstance(LockTimeoutError("x"), OSError)     # the CLI's exit-2 path
    lock.unlink()
    ws.record_work_session_step("s1", _step("plan", "x"), project="test")
    assert len(_rows("work_session")) == 1


@pytest.mark.parametrize("stamp", ["2026-9-1T0:0:0Z", "2026-09- 7T01:02:03Z",
                                   "2026-09-07T1:02:03Z"])
def test_a_stamp_strptime_would_take_is_refused_unless_zero_padded(stamp):
    """`strptime` accepts one-digit and space-padded fields; `session_rows`
    sorts stamps as strings, so such a stamp listed September after December."""
    tag = f"[WS: execution | x | {stamp}]"
    assert ws.parse_work_session_tag(tag) is None
    with pytest.raises(ValueError, match="timestamp"):
        ws.validate_payload(_valid_step_payload(timestamp=stamp, ws_tag=tag))
    assert ws.parse_work_session_tag("[WS: plan | x | 2026-09-07T01:02:03Z]") is not None


def test_cmd_worksession_list_session_json_uses_the_record_key_names():
    rc, out = _run(_ns(ws_cmd="record", phase="plan", action="abc", session="m1", json=True))
    recorded = json.loads(out)
    rc, out = _run(_ns(ws_cmd="list", session="m1", json=True))
    (listed,) = json.loads(out)["steps"]
    assert listed == {k: v for k, v in recorded.items() if k != "session_id"}
    assert list(listed) == ["step_id", "phase", "ws_tag", "initiative_tag", "usage_tag",
                            "status", "timestamp"]


def test_cmd_worksession_list_initiatives_with_a_session_filters_the_initiatives():
    _run(_ns(ws_cmd="record", phase="plan", action="x", initiative=True,
             reason="fix audit", ref="INC-1", session="s9"))
    _run(_ns(ws_cmd="initiative", reason="decide", session="s8"))
    rc, out = _run(_ns(ws_cmd="list", session="s9", initiatives=True, json=True))
    rows = json.loads(out)
    assert rc == 0 and [(r["session_id"], r["reason"]) for r in rows] == [("s9", "fix audit")]
    rc, out = _run(_ns(ws_cmd="list", session="s9", initiatives=True))
    assert "[INITIATIVE: fix audit | Ref: INC-1]" in out and "decide" not in out
    rc, out = _run(_ns(ws_cmd="list", session="nobody", initiatives=True))
    assert rc == 0 and "no initiatives in work session 'nobody'" in out


def test_cmd_worksession_empty_listings_name_the_writer():
    rc, out = _run(_ns(ws_cmd="list"))
    assert rc == 0 and "hive worksession record" in out
    rc, out = _run(_ns(ws_cmd="list", initiatives=True))
    assert rc == 0 and "hive worksession initiative" in out and "--initiative" in out
    rc, out = _run(_ns(ws_cmd="list", session="nobody"))
    assert rc == 0 and "no steps in work session 'nobody'" in out
    rc, out = _run(_ns(ws_cmd="migrate"))
    assert rc == 0 and "no roadmap plans" in out


def test_list_resolves_the_namespace_it_reports(monkeypatch):
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    ws.record_work_session_step("s1", _step("plan", "x"), project="test")
    rc, out = _run(_ns(ws_cmd="list", project=None, json=True))
    assert rc == 0 and json.loads(out)[0]["project"] == "test"


def test_the_worksession_parser_is_wired_to_the_handler():
    from mindhive import __main__ as cli
    from mindhive.commands import orchestration
    parser = cli._build_parser()
    ns = parser.parse_args(["worksession", "record", "--phase", "quality assessment",
                            "--action", "TDD_GREEN", "--initiative", "--reason", "r",
                            "--tokens-used", "10", "--token-budget", "40"])
    assert ns.func is orchestration.cmd_worksession and ns.ws_cmd == "record"
    assert (ns.phase, ns.tokens_used, ns.token_budget, ns.initiative) == \
        ("quality assessment", 10, 40, True)
    ns = parser.parse_args(["worksession", "status", "--step-id", "step-1",
                            "--status", "blocked", "--session", "s1"])
    assert (ns.step_id, ns.status, ns.session) == ("step-1", "blocked", "s1")
    assert parser.parse_args(["worksession", "list", "--session", "s1"]).session == "s1"
    for bad in (["worksession", "status", "--step-id", "x", "--status", "done"],
                ["worksession", "initiative", "--reason", "r", "--session", "../x"],
                ["worksession", "list", "--project", "../x"],
                ["worksession", "list", "--session", "../x"],
                ["worksession", "record", "--phase", "plan", "--action", "x",
                 "--tokens-used", "-1"],
                ["worksession", "record", "--phase", "plan", "--action", "x",
                 "--token-budget", "0"],
                ["worksession", "record", "--phase", "plan", "--action", "x",
                 "--token-budget", "lots"],
                ["worksession"]):
        with pytest.raises(SystemExit) as ei:
            parser.parse_args(bad)
        assert ei.value.code == 2


def test_hive_worksession_runs_end_to_end_through_main(capsys):
    from mindhive import __main__ as cli
    assert cli.main(["worksession", "record", "--phase", "kanban", "--action", "WT_ISOLATE",
                     "--session", "e2e", "--project", "test"]) in (0, None)
    assert "[WS: kanban | " in capsys.readouterr().out
    assert cli.main(["worksession", "list", "--project", "test", "--json"]) in (0, None)
    (row,) = json.loads(capsys.readouterr().out)
    assert row["session_id"] == "e2e" and row["phase"] == "kanban"
    assert cli.main(["worksession", "record", "--phase", "plan", "--action", "x",
                     "--reason", "r", "--session", "e2e", "--project", "test"]) == 2
