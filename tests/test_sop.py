# tests/test_sop.py
"""Managed SOP registry: the event contract, the deterministic fold, registry
guards, STATE/LEDGER/briefing delivery, and the `hive sop` CLI."""
import pytest
from mindhive import __main__ as cli_mod, events, paths, projector, sop
from mindhive.briefing import build_briefing

BODY = ("Read the exact file before editing it; scope every Grep; pass "
        "absolute paths on Windows.")


def _add(sop_id="edit-discipline", title="Edit discipline", text=BODY,
         session="s1", ts=None, **kw):
    return events.append_event(
        "sop", {"action": "add", "sop_id": sop_id, "title": title,
                "text": text, **kw}, session_id=session, ts=ts, tags=["sop"])


# --- payload contract ---------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "free text",                                              # not a dict
    {"action": "promote", "sop_id": "x-1", "title": "T", "text": BODY},
    {"action": "add", "sop_id": "Bad_ID!", "title": "T", "text": BODY},
    {"action": "add", "sop_id": "x-1", "text": BODY},         # no title
    {"action": "add", "sop_id": "x-1", "title": "T", "text": "too short"},
    {"action": "add", "sop_id": "x-1", "title": "T", "text": "y" * 4001},
    {"action": "update", "sop_id": "x-1"},                    # nothing to change
    {"action": "update", "sop_id": "x-1", "text": "tiny"},
])
def test_validate_payload_rejects(payload):
    with pytest.raises(ValueError):
        sop.validate_payload(payload)


def test_validate_payload_accepts_delete_with_reason_only():
    sop.validate_payload({"action": "delete", "sop_id": "x-1",
                          "text": "superseded by y-2"})


# --- fold / registry -----------------------------------------------------------

def test_add_update_delete_lifecycle():
    _add(ts="2026-07-01T10:00:00Z")
    events.append_event("sop", {"action": "update", "sop_id": "edit-discipline",
                                "text": "Revised body: " + BODY},
                        session_id="s2", ts="2026-07-02T10:00:00Z")
    evs = events.read_all()
    rec = sop.get(evs, "edit-discipline")
    assert rec["version"] == 2
    assert rec["created"] == "2026-07-01T10:00:00Z"
    assert rec["updated"] == "2026-07-02T10:00:00Z"
    assert rec["title"] == "Edit discipline"          # carried forward
    assert rec["text"].startswith("Revised body:")
    events.append_event("sop", {"action": "delete",
                                "sop_id": "edit-discipline"},
                        session_id="s2", ts="2026-07-03T10:00:00Z")
    evs = events.read_all()
    assert sop.get(evs, "edit-discipline") is None
    assert sop.current(evs) == []
    _, deleted = sop.fold(evs)
    assert deleted["edit-discipline"]["deleted_at"] == "2026-07-03T10:00:00Z"


def test_readd_revives_with_original_created_and_version_continuity():
    _add(ts="2026-07-01T10:00:00Z")
    events.append_event("sop", {"action": "delete",
                                "sop_id": "edit-discipline"},
                        session_id="s1", ts="2026-07-02T10:00:00Z")
    events.append_event("sop", {"action": "add", "sop_id": "edit-discipline",
                                "title": "Edit discipline v2", "text": BODY},
                        session_id="s1", ts="2026-07-03T10:00:00Z")
    rec = sop.get(events.read_all(), "edit-discipline")
    assert rec is not None
    assert rec["created"] == "2026-07-01T10:00:00Z"
    assert rec["version"] == 3
    assert rec["title"] == "Edit discipline v2"


def test_fold_is_total_on_junk_history():
    # Partial update on an unknown id, delete of an unknown id: skipped, never
    # raised — historical data must always fold.
    events.append_event("sop", {"action": "update", "sop_id": "ghost",
                                "text": BODY}, session_id="s1")
    events.append_event("sop", {"action": "delete", "sop_id": "ghost2"},
                        session_id="s1")
    evs = events.read_all()
    assert sop.current(evs) == []


def test_current_orders_by_created_then_id():
    _add("b-proc", title="B proc", ts="2026-07-02T10:00:00Z")
    _add("a-proc", title="A proc", ts="2026-07-01T10:00:00Z")
    _add("c-proc", title="C proc", ts="2026-07-01T10:00:00Z")
    ids = [r["sop_id"] for r in sop.current(events.read_all())]
    assert ids == ["a-proc", "c-proc", "b-proc"]


def test_entries_newest_first():
    _add(ts="2026-07-01T10:00:00Z")
    events.append_event("sop", {"action": "update", "sop_id": "edit-discipline",
                                "text": "Second body revision " + BODY},
                        session_id="s1", ts="2026-07-02T10:00:00Z")
    ents = sop.entries(events.read_all())
    assert [e["action"] for e in ents] == ["update", "add"]


# --- record guards -------------------------------------------------------------

def test_record_add_then_duplicate_add_is_an_error():
    sop.record("add", "memo-hygiene", title="Memo hygiene", text=BODY)
    with pytest.raises(ValueError, match="hive sop update"):
        sop.record("add", "memo-hygiene", title="Memo hygiene", text=BODY)
    assert len(events.read_all()) == 1


def test_record_update_or_delete_of_missing_id_is_an_error():
    with pytest.raises(ValueError, match="no active SOP"):
        sop.record("update", "nope", text=BODY)
    with pytest.raises(ValueError, match="no active SOP"):
        sop.record("delete", "nope")
    assert events.read_all() == []


def test_record_stamps_sop_tag():
    ev = sop.record("add", "memo-hygiene", title="Memo hygiene", text=BODY,
                    tags=["quality", "sop"])
    assert ev.tags[0] == "sop"
    assert ev.tags.count("sop") == 1
    assert "quality" in ev.tags


# --- delivery: STATE / LEDGER / briefing ----------------------------------------

def test_block_empty_without_sops():
    assert sop.block([]) == ""


def test_block_renders_title_and_body_in_order():
    _add("a-proc", title="A proc", text="Body of procedure A, in full.",
         ts="2026-07-01T10:00:00Z")
    _add("b-proc", title="B proc", text="Body of procedure B, in full.",
         ts="2026-07-02T10:00:00Z")
    blk = sop.block(events.read_all())
    assert "STANDARD OPERATING PROCEDURES" in blk
    assert blk.index("SOP 1: A proc") < blk.index("SOP 2: B proc")
    assert "Body of procedure A, in full." in blk
    assert "`[a-proc v1]`" in blk


def test_block_elides_bodies_beyond_budget(monkeypatch):
    monkeypatch.setattr(sop, "BLOCK_FULL_BUDGET", 60)
    _add("a-proc", title="A proc", text="A" * 50, ts="2026-07-01T10:00:00Z")
    _add("b-proc", title="B proc", text="B" * 50, ts="2026-07-02T10:00:00Z")
    blk = sop.block(events.read_all())
    assert "A" * 50 in blk                       # first fits the budget
    assert "B" * 50 not in blk                   # second elided…
    assert "hive sop show b-proc" in blk         # …with a pointer


def test_projector_delivers_block_and_ledger_entry():
    _add()
    projector.project()
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "STANDARD OPERATING PROCEDURES" in state
    assert "SOP 1: Edit discipline" in state
    assert BODY in state
    ledger = list((paths.project_dir() / "LEDGER").glob("*-sop-*.md"))
    assert len(ledger) == 1
    body = ledger[0].read_text(encoding="utf-8")
    assert "[add] SOP edit-discipline — Edit discipline" in body
    assert BODY in body


def test_memory_md_variant_keeps_the_block():
    # sync embeds compose_state(include_rules=False) into MEMORY.md — the SOP
    # block must ride that variant or sessions never auto-load it.
    _add()
    evs = events.read_all()
    assert "STANDARD OPERATING PROCEDURES" in \
        projector.compose_state(evs, include_rules=False, suggestions="")


def test_briefing_lists_active_sops():
    _add()
    text = build_briefing(events.read_all())
    assert "## Standard operating procedures (active)" in text
    assert "`edit-discipline` v1: Edit discipline" in text


def test_deleted_sop_leaves_state_but_stays_in_ledger():
    _add(ts="2026-07-01T10:00:00Z")
    events.append_event("sop", {"action": "delete",
                                "sop_id": "edit-discipline"},
                        session_id="s1", ts="2026-07-02T10:00:00Z")
    projector.project()
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "STANDARD OPERATING PROCEDURES" not in state
    # both lifecycle events remain projected in the LEDGER (audit trail)
    assert len(list((paths.project_dir() / "LEDGER").glob("*-sop-*.md"))) == 2


# --- registry content is audit-grade: never merged ------------------------------

def test_sop_exempt_from_consolidation():
    from mindhive import consolidate
    assert "sop" not in consolidate.CONSOLIDATE_TYPES
    assert "decision" in consolidate.CONSOLIDATE_TYPES  # sanity: set intact


def test_display_dedupe_never_collapses_sop_events():
    from mindhive import dedupe
    items = [
        {"type": "sop", "project": "p", "ts": "2026-07-02T10:00:00Z",
         "text": "Read the exact file before editing it, always."},
        {"type": "sop", "project": "p", "ts": "2026-07-01T10:00:00Z",
         "text": "Read the exact file before editing it, always."},
    ]
    out = dedupe.collapse(items)
    assert len(out) == 2 and all(i["similar_count"] == 0 for i in out)


# --- CLI ------------------------------------------------------------------------

def test_cli_sop_add_list_show(capsys):
    assert cli_mod.main(["sop", "add", "edit-discipline",
                         "--title", "Edit discipline", "--text", BODY]) == 0
    assert cli_mod.main(["sop", "list"]) == 0
    out = capsys.readouterr().out
    assert "[edit-discipline v1] Edit discipline" in out
    assert cli_mod.main(["sop", "show", "edit-discipline"]) == 0
    out = capsys.readouterr().out
    assert BODY in out and "history (newest first):" in out


def test_cli_sop_add_body_from_file(tmp_path):
    f = tmp_path / "body.md"
    f.write_text(BODY, encoding="utf-8")
    assert cli_mod.main(["sop", "add", "edit-discipline",
                         "--title", "Edit discipline",
                         "--file", str(f)]) == 0
    assert sop.get(events.read_all(), "edit-discipline")["text"] == BODY


def test_cli_sop_rejects_conflicting_body_sources(tmp_path, capsys):
    f = tmp_path / "body.md"
    f.write_text(BODY, encoding="utf-8")
    ret = cli_mod.main(["sop", "add", "edit-discipline",
                        "--title", "Edit discipline",
                        "--text", BODY, "--file", str(f)])
    assert ret == 2
    assert "only one of" in capsys.readouterr().err
    assert events.read_all() == []


def test_cli_sop_update_and_delete(capsys):
    cli_mod.main(["sop", "add", "edit-discipline",
                  "--title", "Edit discipline", "--text", BODY])
    assert cli_mod.main(["sop", "update", "edit-discipline",
                         "--text", "Sharpened: " + BODY]) == 0
    assert cli_mod.main(["sop", "delete", "edit-discipline",
                         "--reason", "superseded"]) == 0
    evs = events.read_all()
    assert [e.payload["action"] for e in evs] == ["add", "update", "delete"]
    assert sop.current(evs) == []


def test_cli_sop_duplicate_add_exits_2(capsys):
    cli_mod.main(["sop", "add", "edit-discipline",
                  "--title", "Edit discipline", "--text", BODY])
    ret = cli_mod.main(["sop", "add", "edit-discipline",
                        "--title", "Edit discipline", "--text", BODY])
    assert ret == 2
    assert "already exists" in capsys.readouterr().err


def test_cli_append_enforces_sop_contract(capsys):
    # `hive append sop …` must not smuggle a malformed lifecycle event past
    # the contract (mirrors the code_change gate).
    assert cli_mod.main(["append", "sop", "free text, no action"]) == 2
    assert "hive sop" in capsys.readouterr().err
    good = ('{"action": "add", "sop_id": "edit-discipline", '
            '"title": "Edit discipline", "text": "' + BODY + '"}')
    assert cli_mod.main(["append", "sop", good, "--json"]) == 0
    evs = events.read_all()
    assert len(evs) == 1 and evs[0].type == "sop"
