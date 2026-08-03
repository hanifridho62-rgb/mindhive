# tests/test_sop_mandatory.py
"""The MANDATORY SOP tier: the event contract, the fold's no-silent-demotion
rule, un-elided STATE delivery, the hot-path sidecar, and the `hive sop`
CLI surface (`--mandatory`, `preflight`).

Kept beside `tests/test_sop.py` rather than inside it: that file pins the
registry as it was before the tier existed, and several tests here exist
specifically to prove those older guarantees still hold.
"""
import json

import pytest

from mindhive import __main__ as cli_mod, events, projector, sop

BODY = ("Read the exact file before editing it; scope every Grep; pass "
        "absolute paths on Windows.")


def _add(sop_id="edit-discipline", title="Edit discipline", text=BODY,
         session="s1", ts=None, mandatory=None):
    """Raw append — deliberately NOT `sop.record`, so the fold is exercised on
    events rather than on the writer's convenience layer."""
    payload = {"action": "add", "sop_id": sop_id, "title": title, "text": text}
    if mandatory is not None:
        payload["mandatory"] = mandatory
    return events.append_event("sop", payload, session_id=session, ts=ts,
                               tags=["sop"])


# --- the event contract --------------------------------------------------------

@pytest.mark.parametrize("bad", [1, 0, "yes", "true", "false", [], {}])
def test_validate_payload_rejects_non_boolean_mandatory(bad):
    with pytest.raises(ValueError):
        sop.validate_payload({"action": "add", "sop_id": "x-1",
                              "title": "Title", "text": BODY, "mandatory":bad})


@pytest.mark.parametrize("good", [True, False])
def test_validate_payload_accepts_boolean_mandatory(good):
    sop.validate_payload({"action": "add", "sop_id": "x-1", "title": "Title",
                          "text": BODY, "mandatory": good})


# --- the fold ------------------------------------------------------------------

def test_add_without_the_key_is_not_mandatory():
    """Backward compatibility for every SOP written before the tier existed."""
    _add()
    evs = events.read_all()
    assert sop.get(evs, "edit-discipline")["mandatory"] is False
    assert sop.mandatory(evs) == []


def test_add_with_mandatory_folds_true():
    _add(mandatory=True)
    evs = events.read_all()
    assert sop.get(evs, "edit-discipline")["mandatory"] is True
    assert [r["sop_id"] for r in sop.mandatory(evs)] == ["edit-discipline"]


def test_title_only_update_never_demotes_a_mandatory_sop():
    """The regression this tier is most likely to acquire: a routine
    `sop update --title` quietly dropping the obligation."""
    _add(mandatory=True, ts="2026-07-01T10:00:00Z")
    events.append_event("sop", {"action": "update", "sop_id": "edit-discipline",
                                "title": "Edit discipline, revised"},
                        session_id="s2", ts="2026-07-02T10:00:00Z")
    rec = sop.get(events.read_all(), "edit-discipline")
    assert rec["mandatory"] is True
    assert rec["title"].endswith("revised")


def test_explicit_demote_then_repromote():
    _add(mandatory=True, ts="2026-07-01T10:00:00Z")
    events.append_event("sop", {"action": "update", "sop_id": "edit-discipline",
                                "mandatory": False},
                        session_id="s2", ts="2026-07-02T10:00:00Z")
    assert sop.get(events.read_all(), "edit-discipline")["mandatory"] is False
    events.append_event("sop", {"action": "update", "sop_id": "edit-discipline",
                                "mandatory": True},
                        session_id="s3", ts="2026-07-03T10:00:00Z")
    rec = sop.get(events.read_all(), "edit-discipline")
    assert rec["mandatory"] is True
    assert rec["version"] == 3


def test_revival_without_the_key_keeps_the_prior_tier():
    """On a REVIVAL the key's absence means "unchanged", not "unset" — the same
    never-lose-data rule the title/text fields already follow."""
    _add(mandatory=True, ts="2026-07-01T10:00:00Z")
    events.append_event("sop", {"action": "delete", "sop_id": "edit-discipline"},
                        session_id="s2", ts="2026-07-02T10:00:00Z")
    _add(ts="2026-07-03T10:00:00Z", session="s3")          # re-add, no key
    assert sop.get(events.read_all(), "edit-discipline")["mandatory"] is True


def test_mandatory_order_is_created_then_id():
    _add("b-sop", "B", BODY, ts="2026-07-01T10:00:00Z", mandatory=True)
    _add("a-sop", "A", BODY, ts="2026-07-02T10:00:00Z", mandatory=True)
    assert [r["sop_id"] for r in sop.mandatory(events.read_all())] == [
        "b-sop", "a-sop"]


def test_ledger_body_marks_mandatory():
    marked = sop.ledger_body({"action": "add", "sop_id": "x-1", "title": "T",
                              "text": BODY, "mandatory": True})
    plain = sop.ledger_body({"action": "add", "sop_id": "x-1", "title": "T",
                             "text": BODY})
    assert "(MANDATORY)" in marked
    assert "MANDATORY" not in plain


# --- STATE.md block: mandatory first, un-elided --------------------------------

def test_block_is_unchanged_when_nothing_is_mandatory():
    """Regression guard for the SOPs already in the live registry."""
    _add("a-sop", "A", BODY, ts="2026-07-01T10:00:00Z")
    _add("b-sop", "B", BODY, ts="2026-07-02T10:00:00Z")
    out = sop.block(events.read_all())
    assert "MANDATORY" not in out
    assert out.index("## SOP 1: A") < out.index("## SOP 2: B")


def test_mandatory_renders_first_even_when_created_last():
    _add("ordinary", "Ordinary", BODY, ts="2026-07-01T10:00:00Z")
    _add("must", "Must", BODY, ts="2026-07-09T10:00:00Z", mandatory=True)
    out = sop.block(events.read_all())
    assert out.index("MANDATORY SOP 1: Must") < out.index("## SOP 1: Ordinary")


def test_mandatory_body_is_exempt_from_the_block_budget():
    big = "M" * (sop.BLOCK_FULL_BUDGET + 100)
    _add("must", "Must", big, ts="2026-07-01T10:00:00Z", mandatory=True)
    _add("ordinary", "Ordinary", BODY, ts="2026-07-02T10:00:00Z")
    out = sop.block(events.read_all())
    assert big in out, "a mandatory body must never be elided"
    # ...and the ordinary SOP still renders in full: the mandatory body did not
    # spend a budget it is exempt from.
    assert BODY in out


def test_ordinary_sops_still_elide_past_the_budget():
    _add("must", "Must", BODY, ts="2026-07-01T10:00:00Z", mandatory=True)
    _add("big", "Big", "O" * (sop.BLOCK_FULL_BUDGET - 10),
         ts="2026-07-02T10:00:00Z")
    _add("later", "Later", "P" * 100, ts="2026-07-03T10:00:00Z")
    out = sop.block(events.read_all())
    assert "hive sop show later" in out


# --- the sidecar ---------------------------------------------------------------

def test_record_writes_the_sidecar():
    sop.record("add", "must", title="Must", text=BODY, mandatory=True)
    data = sop.read_sidecar()
    assert [s["sop_id"] for s in data["sops"]] == ["must"]
    assert data["sops"][0]["text"] == BODY


def test_sidecar_omits_ordinary_sops():
    sop.record("add", "ordinary", title="Ordinary", text=BODY)
    assert sop.read_sidecar()["sops"] == []


def test_delete_removes_it_from_the_sidecar():
    sop.record("add", "must", title="Must", text=BODY, mandatory=True)
    sop.record("delete", "must", text="superseded by a newer procedure")
    assert sop.read_sidecar()["sops"] == []


@pytest.mark.parametrize("junk", ["{not json", '"a string"', "[1,2]",
                                  '{"sops": "nope"}'])
def test_read_sidecar_is_total(junk):
    p = sop.sidecar_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(junk, encoding="utf-8")
    assert sop.read_sidecar() == {}


def test_read_sidecar_missing_is_empty():
    assert sop.read_sidecar() == {}


def test_a_failing_sidecar_write_still_appends_the_event(monkeypatch):
    """The ledger is the source of truth; a hot-path cache must never be able
    to fail an append that already landed."""
    def boom(*a, **kw):
        raise OSError("read-only volume")

    monkeypatch.setattr(sop, "write_sidecar", boom)
    ev = sop.record("add", "must", title="Must", text=BODY, mandatory=True)
    assert ev.type == "sop"
    assert sop.get(events.read_all(), "must")["mandatory"] is True


def test_project_rebuild_refreshes_the_sidecar():
    _add("must", "Must", BODY, mandatory=True)      # raw append: no sidecar yet
    assert sop.read_sidecar() == {}
    projector.project()
    assert [s["sop_id"] for s in sop.read_sidecar()["sops"]] == ["must"]


def test_hive_state_self_heals_a_corrupt_sidecar(capsys):
    _add("must", "Must", BODY, mandatory=True)
    projector.project()
    sop.sidecar_path().write_text("{corrupt", encoding="utf-8")
    cli_mod.main(["state"])
    capsys.readouterr()
    assert [s["sop_id"] for s in sop.read_sidecar()["sops"]] == ["must"]


# --- CLI ------------------------------------------------------------------------

def test_cli_add_mandatory():
    assert cli_mod.main(["sop", "add", "must", "--title", "Must",
                         "--text", BODY, "--mandatory"]) == 0
    assert sop.get(events.read_all(), "must")["mandatory"] is True


def test_cli_update_without_the_flag_keeps_the_tier():
    cli_mod.main(["sop", "add", "must", "--title", "Must", "--text", BODY,
                  "--mandatory"])
    cli_mod.main(["sop", "update", "must", "--title", "Must, revised"])
    assert sop.get(events.read_all(), "must")["mandatory"] is True


def test_cli_no_mandatory_demotes():
    cli_mod.main(["sop", "add", "must", "--title", "Must", "--text", BODY,
                  "--mandatory"])
    cli_mod.main(["sop", "update", "must", "--no-mandatory"])
    assert sop.get(events.read_all(), "must")["mandatory"] is False


def test_cli_list_marks_mandatory(capsys):
    cli_mod.main(["sop", "add", "must", "--title", "Must", "--text", BODY,
                  "--mandatory"])
    capsys.readouterr()
    cli_mod.main(["sop", "list"])
    out = capsys.readouterr().out
    assert "MANDATORY [must v1]" in out
    assert "1 mandatory" in out


def test_cli_list_json_carries_the_flag(capsys):
    cli_mod.main(["sop", "add", "must", "--title", "Must", "--text", BODY,
                  "--mandatory"])
    capsys.readouterr()
    cli_mod.main(["sop", "list", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["mandatory"] is True


# --- preflight: the checkable half ---------------------------------------------

def test_preflight_on_an_empty_store_says_new(capsys):
    assert cli_mod.main(["sop", "preflight"]) == 0
    assert "Continuity: NEW" in capsys.readouterr().out


def test_preflight_sees_a_prior_session(capsys, monkeypatch):
    events.append_event("decision", {"text": "earlier work"},
                        session_id="old-1")
    monkeypatch.setenv("MINDHIVE_SESSION", "new-2")
    cli_mod.main(["sop", "preflight"])
    out = capsys.readouterr().out
    assert "Continuity: CONTINUATION" in out
    assert "old-1" in out
    assert "absorb the prior context" in out


def test_preflight_reports_an_interrupted_plan(capsys):
    """The branch most likely to crash on real data: `interrupted()` records
    are read for their title and next step, and nothing else in the suite
    exercises that read."""
    from mindhive import roadmap
    roadmap.record("set", "resume-me", prompt="finish the migration",
                   title="Finish the migration",
                   steps=["write the backfill", "run it", "verify counts"])
    roadmap.record("interrupt", "resume-me", cause="usage_limit")
    cli_mod.main(["sop", "preflight"])
    out = capsys.readouterr().out
    assert "Continuity: CONTINUATION" in out
    assert "resume-me" in out
    assert "write the backfill" in out, "the NEXT step is the actionable part"


def test_preflight_ignores_the_current_session(capsys, monkeypatch):
    monkeypatch.setenv("MINDHIVE_SESSION", "here")
    events.append_event("decision", {"text": "my own work"}, session_id="here")
    cli_mod.main(["sop", "preflight"])
    assert "Continuity: NEW" in capsys.readouterr().out


def test_preflight_json_is_parseable_and_lists_mandatory_sops(capsys):
    cli_mod.main(["sop", "add", "must", "--title", "Must", "--text", BODY,
                  "--mandatory"])
    capsys.readouterr()
    assert cli_mod.main(["sop", "preflight", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["verdict"] in ("NEW", "CONTINUATION")
    assert [s["sop_id"] for s in rep["mandatory_sops"]] == ["must"]
    assert "skills" in rep["catalog"]
    assert isinstance(rep["evidence"], list)
