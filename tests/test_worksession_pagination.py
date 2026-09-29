# tests/test_worksession_pagination.py — `hive worksession list --page/--page-size`
# (blueprint P8: the §10.8.2 envelope, `pgshape.paginate_records`, on the CLI).
"""The three `list` surfaces -- work sessions, ``--initiatives`` and one
session's steps (``--session S``) -- through `cli.main`, against the
conftest-isolated store. Without either flag the output is the pre-P8 output
(`tests/test_worksessions.py` pins its literals; here a listing longer than
`pgshape.DEFAULT_PAGE_SIZE` pins that nothing is cut). With a flag the page,
its footer and the ``--json`` envelope are exactly what
`pgshape.paginate_records` answers for the same raw values, clamping included:
the CLI hands the flags to the helper untouched, so there is one authority for
the page arithmetic and no second copy of it to drift."""
import argparse
import json
import re
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from mindhive import events, initiatives, pgshape
from mindhive import worksessions as ws
from mindhive.__main__ import main

N = 25                      # one row more than a DEFAULT_PAGE_SIZE (20) page holds
KEYS = ["page", "page_size", "total_items", "total_pages", "has_next", "has_prev", "items"]
STEP_KEYS = ["step_id", "phase", "ws_tag", "initiative_tag", "usage_tag", "status",
             "timestamp"]


def _clock(iso):
    return lambda: datetime.strptime(iso, ws.TS_FORMAT).replace(tzinfo=timezone.utc)


def _stamp(i):
    """Distinct ascending stamps: hour *i* of 1 September, spilling into the 2nd."""
    return f"2026-09-{1 + i // 24:02d}T{i % 24:02d}:00:00Z"


def _record(sid, i):
    step = ws.create_work_session_step("plan", f"step {i}", False, now_fn=_clock(_stamp(i)))
    ws.record_work_session_step(sid, replace(step, step_id=f"st-{i:02d}"), project="test")


def _sessions(n=N):
    """*n* one-step work sessions; the listing (newest first) runs ``ws-{n-1}``
    down to ``ws-00``."""
    for i in range(n):
        _record(f"ws-{i:02d}", i)
    return [f"ws-{i:02d}" for i in reversed(range(n))]


def _steps(n=N, sid="s1"):
    """*n* steps in one session, in append order ``st-00`` .. ``st-{n-1}``."""
    for i in range(n):
        _record(sid, i)
    return [f"st-{i:02d}" for i in range(n)]


def _initiatives(n=N, sid="i-1"):
    for i in range(n):
        initiatives.declare_initiative(sid, f"reason {i}", project="test")


def _rows():
    return ws.session_rows(events.read_all("test"), "test")


def _all_initiatives():
    return initiatives.initiatives(events.read_all("test"))


def _list(capsys, *argv):
    rc = main(["worksession", "list", "--project", "test", *argv])
    captured = capsys.readouterr()
    assert rc in (0, None), captured.err
    return captured.out


def _session_ids(out):
    return [line.split()[0] for line in out.splitlines() if line.startswith("  ws-")]


def _step_ids(out):
    return [line[2:].split()[0] for line in out.splitlines() if line[2:].startswith("st-")]


def _marked(out):
    return [line[2:].split()[0] for line in out.splitlines() if line.startswith("* ")]


def _reasons(out):
    return re.findall(r"\[INITIATIVE: (reason \d+)\]", out)


# --- 1. without a flag, nothing changes ------------------------------------------------

def test_without_a_flag_the_sessions_listing_prints_every_row_as_before(capsys):
    order = _sessions()
    out = _list(capsys)
    assert _session_ids(out) == order
    assert out.endswith(f"\n\n{N} work session(s); step ids: hive worksession list "
                        "--session <id>\n")
    assert "page" not in out
    listed = json.loads(_list(capsys, "--json"))
    assert isinstance(listed, list) and [r["session_id"] for r in listed] == order


def test_without_a_flag_a_sessions_steps_are_all_listed_as_before(capsys):
    steps = _steps()
    out = _list(capsys, "--session", "s1")
    assert _step_ids(out) == steps and _marked(out) == [steps[-1]]
    assert out.endswith(f"\n\n{N} step(s), session active; move one with: hive worksession "
                        "status --session s1 --step-id <id> --status <status>\n")
    assert "page" not in out
    doc = json.loads(_list(capsys, "--session", "s1", "--json"))
    assert isinstance(doc["steps"], list) and [s["step_id"] for s in doc["steps"]] == steps


def test_without_a_flag_the_initiatives_are_all_listed_as_before(capsys):
    _initiatives()
    out = _list(capsys, "--initiatives")
    assert len(_reasons(out)) == N and out.endswith(f"\n\n{N} initiative(s)\n")
    assert "page" not in out
    listed = json.loads(_list(capsys, "--initiatives", "--json"))
    assert isinstance(listed, list) and len(listed) == N


# --- 2. one page and its footer ----------------------------------------------------------

def test_page_size_alone_prints_page_one_and_names_the_next_page(capsys):
    order = _sessions()
    out = _list(capsys, "--page-size", "10")
    assert _session_ids(out) == order[:10]
    assert ("\n\npage 1 of 3, 10 of 25 work session(s); step ids: "
            "hive worksession list --session <id>\n") in out
    assert out.endswith("next page: hive worksession list --project test "
                        "--page 2 --page-size 10\n")


def test_page_two_prints_the_second_ten(capsys):
    order = _sessions()
    out = _list(capsys, "--page", "2", "--page-size", "10")
    assert _session_ids(out) == order[10:20]
    assert "page 2 of 3, 10 of 25 work session(s)" in out
    assert "next page: hive worksession list --project test --page 3 --page-size 10" in out


def test_the_last_page_is_short_and_offers_no_next_page(capsys):
    order = _sessions()
    out = _list(capsys, "--page", "3", "--page-size", "10")
    assert _session_ids(out) == order[20:]
    assert "page 3 of 3, 5 of 25 work session(s)" in out
    assert "next page" not in out and "past the end" not in out


def test_page_alone_takes_the_helpers_default_page_size(capsys):
    order = _sessions()
    out = _list(capsys, "--page", "2")
    assert _session_ids(out) == order[pgshape.DEFAULT_PAGE_SIZE:]
    assert f"page 2 of 2, {N - pgshape.DEFAULT_PAGE_SIZE} of {N} work session(s)" in out
    doc = json.loads(_list(capsys, "--page", "2", "--json"))
    assert (doc["page"], doc["page_size"]) == (2, pgshape.DEFAULT_PAGE_SIZE)


# --- 3. the JSON envelope ---------------------------------------------------------------

def test_json_carries_the_envelope_in_the_spec_key_order(capsys):
    order = _sessions()
    doc = json.loads(_list(capsys, "--json", "--page", "2", "--page-size", "10"))
    assert list(doc) == KEYS
    assert (doc["page"], doc["page_size"], doc["total_items"], doc["total_pages"],
            doc["has_next"], doc["has_prev"]) == (2, 10, N, 3, True, True)
    assert [r["session_id"] for r in doc["items"]] == order[10:20]
    assert doc == pgshape.paginate_records(_rows(), page=2, page_size=10)


# --- 4. clamping is the helper's, never a second copy -------------------------------------

@pytest.mark.parametrize("argv, page, size, want", [
    (["--page", "0"], 0, None, (1, pgshape.DEFAULT_PAGE_SIZE)),
    (["--page", "-5"], -5, None, (1, pgshape.DEFAULT_PAGE_SIZE)),
    (["--page-size", "0"], None, 0, (1, 1)),
    (["--page-size", "-1"], None, -1, (1, 1)),
    (["--page-size", "101"], None, 101, (1, pgshape.MAX_PAGE_SIZE_CEILING)),
    (["--page-size", "1000"], None, 1000, (1, pgshape.MAX_PAGE_SIZE_CEILING)),
    (["--page", "-1", "--page-size", "0"], -1, 0, (1, 1)),
])
def test_out_of_range_values_are_clamped_exactly_as_the_helper_clamps_them(
        capsys, argv, page, size, want):
    _sessions()
    doc = json.loads(_list(capsys, "--json", *argv))        # exit 0: never refused
    assert (doc["page"], doc["page_size"]) == want
    assert doc == pgshape.paginate_records(_rows(), page=page, page_size=size)
    out = _list(capsys, *argv)
    assert (f"page {want[0]} of {doc['total_pages']}, {len(doc['items'])} of {N} "
            "work session(s)") in out


def test_a_page_past_the_end_answers_no_rows_with_the_true_totals(capsys):
    _sessions()
    out = _list(capsys, "--page", "9", "--page-size", "10")
    assert _session_ids(out) == []
    assert "page 9 of 3, 0 of 25 work session(s); step ids:" in out
    assert out.endswith("past the end: the last page is hive worksession list "
                        "--project test --page 3 --page-size 10\n")
    doc = json.loads(_list(capsys, "--json", "--page", "9", "--page-size", "10"))
    assert (doc["items"], doc["total_items"], doc["total_pages"], doc["has_next"],
            doc["has_prev"]) == ([], N, 3, False, True)


# --- 5. the other two surfaces page the same way ----------------------------------------

def test_initiatives_page_the_same_way(capsys):
    _initiatives()
    want = pgshape.paginate_records(_all_initiatives(), page=1, page_size=10)
    out = _list(capsys, "--initiatives", "--page-size", "10")
    assert _reasons(out) == [r["reason"] for r in want["items"]] and len(_reasons(out)) == 10
    assert out.endswith("\n\npage 1 of 3, 10 of 25 initiative(s)\n"
                        "next page: hive worksession list --initiatives --project test "
                        "--page 2 --page-size 10\n")
    doc = json.loads(_list(capsys, "--initiatives", "--json", "--page", "3",
                           "--page-size", "10"))
    assert list(doc) == KEYS and len(doc["items"]) == 5 and doc["total_items"] == N
    assert doc == pgshape.paginate_records(_all_initiatives(), page=3, page_size=10)


def test_a_sessions_steps_page_with_the_marker_on_the_absolute_index(capsys):
    steps = _steps()
    ws.record_step_status("s1", "st-12", "active", project="test")
    out = _list(capsys, "--session", "s1", "--page", "2", "--page-size", "10")
    assert _step_ids(out) == steps[10:20]
    assert _marked(out) == ["st-12"]
    assert ("\n\npage 2 of 3, 10 of 25 step(s), session active; move one with: hive "
            "worksession status --session s1 --step-id <id> --status <status>\n") in out
    assert out.endswith("next page: hive worksession list --session s1 --project test "
                        "--page 3 --page-size 10\n")
    out = _list(capsys, "--session", "s1", "--page", "1", "--page-size", "10")
    assert _step_ids(out) == steps[:10] and _marked(out) == []
    doc = json.loads(_list(capsys, "--session", "s1", "--json", "--page", "2",
                           "--page-size", "10"))
    assert (doc["session_id"], doc["project"], doc["status"],
            doc["active_step_index"]) == ("s1", "test", "active", 12)
    assert list(doc["steps"]) == KEYS
    assert [s["step_id"] for s in doc["steps"]["items"]] == steps[10:20]
    assert all(list(s) == STEP_KEYS for s in doc["steps"]["items"])
    assert doc["steps"]["total_items"] == N and doc["steps"]["has_next"] is True


# --- 6. empty listings keep their hints; the parser; the hand-built Namespace ----------

def test_an_empty_listing_keeps_its_hint_and_answers_an_empty_envelope(capsys):
    out = _list(capsys, "--page", "1")
    assert "(no work sessions -- hive worksession record" in out and "page" not in out
    doc = json.loads(_list(capsys, "--page", "1", "--json"))
    assert doc == pgshape.paginate_records([], page=1, page_size=None)
    assert (doc["items"], doc["total_items"], doc["total_pages"]) == ([], 0, 0)
    out = _list(capsys, "--initiatives", "--page", "2")
    assert "(no initiatives -- hive worksession record --initiative" in out
    out = _list(capsys, "--session", "nobody", "--page-size", "5")
    assert "(no steps in work session 'nobody' -- hive worksession record" in out
    doc = json.loads(_list(capsys, "--session", "nobody", "--page-size", "5", "--json"))
    assert doc["steps"] == pgshape.paginate_records([], page=None, page_size=5)


def test_the_parser_takes_whole_numbers_and_defaults_to_none(capsys):
    from mindhive import __main__ as cli
    parser = cli._build_parser()
    ns = parser.parse_args(["worksession", "list", "--page", "2", "--page-size", "5"])
    assert (ns.page, ns.page_size) == (2, 5)
    ns = parser.parse_args(["worksession", "list"])
    assert (ns.page, ns.page_size) == (None, None)
    for bad in (["--page", "x"], ["--page", "1.5"], ["--page-size", ""],
                ["--page-size", "ten"]):
        with pytest.raises(SystemExit) as ei:
            parser.parse_args(["worksession", "list", *bad])
        assert ei.value.code == 2
    with pytest.raises(SystemExit) as ei:
        main(["worksession", "list", "--help"])
    assert ei.value.code == 0
    text = capsys.readouterr().out
    assert "--page" in text and "--page-size" in text
    assert str(pgshape.DEFAULT_PAGE_SIZE) in text and str(pgshape.MAX_PAGE_SIZE_CEILING) in text


def test_the_handler_tolerates_a_namespace_without_the_flags(capsys):
    """`tests/test_worksessions.py` builds its Namespace by hand, without the
    two keys: the handler reads them with a default, never with the attribute
    access a hand-built Namespace turns into AttributeError."""
    from mindhive.commands import orchestration
    _sessions(3)
    ns = argparse.Namespace(ws_cmd="list", session=None, project="test", json=True,
                            initiatives=False)
    assert orchestration.cmd_worksession(ns) == 0
    assert len(json.loads(capsys.readouterr().out)) == 3
