# tests/test_changelog.py
"""Mandatory code change-log: recording, the pending gate, its STATE.md
notice, and the `hive log` CLI."""
import pytest
from mindhive import __main__ as cli_mod, changelog, events, paths, projector


# Default touch ts is in the PAST relative to the real clock: record() stamps
# events with real UTC now, and time-aware coverage requires entry >= touch.
def _touch(relpath, tmp_path, session="s1", ts="2026-07-14T10:00:00Z"):
    p = tmp_path / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x = 1\n", encoding="utf-8")
    events.append_event("file_touched", {"text": str(p)},
                        session_id=session, ts=ts)
    return str(p)


# --- scope ------------------------------------------------------------------

def test_in_scope_accepts_repo_relative_git_paths():
    # review.in_scope needs a leading slash; git emits repo-relative paths.
    assert changelog.in_scope("src/mindhive/graph.py")
    assert changelog.in_scope("tests/test_graph.py")
    assert changelog.in_scope("/repo/src/mindhive/graph.py")
    assert not changelog.in_scope("docs/plans/x.md")
    assert not changelog.in_scope("README.md")


# --- record -----------------------------------------------------------------

def test_record_appends_event_and_clears_marker(tmp_path):
    a = _touch("src/mindhive/a.py", tmp_path)
    changelog.queue(events.read_all(), session_id="s1")
    assert changelog.load_pending() is not None
    ev = changelog.record("update", "tightened the recall ranking logic",
                          session_id="s1")
    assert ev.type == "code_change"
    assert ev.payload["category"] == "update"
    assert ev.payload["files"] == [a]  # taken from the pending marker
    assert changelog.load_pending() is None


@pytest.mark.parametrize("category,text", [
    ("nonsense", "a perfectly fine explanation"),   # unknown category
    ("update", ""),                                  # empty
    ("update", "         "),                         # whitespace-only
    ("update", "123456789"),                         # 9 chars: below the floor
])
def test_record_rejects_invalid_input(category, text):
    with pytest.raises(ValueError):
        changelog.record(category, text, files=["src/mindhive/a.py"])
    assert events.read_all() == []  # nothing appended


def test_record_explicit_files_stored_verbatim():
    ev = changelog.record("deletion", "removed the dead legacy exporter",
                          files=["src/mindhive/old.py", "tests/test_old.py"])
    assert ev.payload["files"] == ["src/mindhive/old.py", "tests/test_old.py"]


def test_record_falls_back_to_session_touched_files(tmp_path):
    a = _touch("src/mindhive/b.py", tmp_path)
    # no marker queued — record must derive files from the session's events
    ev = changelog.record("minor_change", "renamed a confusing helper",
                          session_id="s1")
    assert ev.payload["files"] == [a]


def test_record_errors_when_nothing_detected(monkeypatch):
    monkeypatch.setattr(changelog, "_git_changed_files", lambda: [])
    with pytest.raises(ValueError, match="--files"):
        changelog.record("update", "an explanation with no files anywhere")


# --- queue / gate lifecycle ---------------------------------------------------

def test_queue_writes_marker_for_unlogged_files(tmp_path):
    a = _touch("src/mindhive/a.py", tmp_path)
    marker = changelog.queue(events.read_all(), session_id="s1")
    assert marker["files"] == [a]
    assert changelog.load_pending() == marker


def test_queue_clears_stale_marker_when_all_logged(tmp_path):
    _touch("src/mindhive/a.py", tmp_path)
    changelog.queue(events.read_all(), session_id="s1")
    changelog.record("update", "covered the change-set completely",
                     session_id="s1")
    # a re-queue after recording finds nothing unlogged and clears
    assert changelog.queue(events.read_all(), session_id="s1") is None
    assert changelog.load_pending() is None


def test_queue_excludes_files_covered_by_same_session_entry(tmp_path):
    a = _touch("src/mindhive/a.py", tmp_path)
    b = _touch("src/mindhive/b.py", tmp_path, ts="2026-07-15T10:01:00Z")
    events.append_event("code_change",
                        {"text": "logged a already", "category": "update",
                         "files": [a]},
                        session_id="s1", ts="2026-07-15T10:02:00Z")
    marker = changelog.queue(events.read_all(), session_id="s1")
    assert marker["files"] == [b]


def test_cross_session_manual_entry_covers_the_gate(tmp_path):
    # A `hive log` run in a plain terminal records under session "manual" —
    # the session_end queue for the coding session must still count it, or
    # the gate wrongly re-fires after a proper log.
    a = _touch("src/mindhive/a.py", tmp_path, session="s1")
    events.append_event("code_change",
                        {"text": "logged from a manual shell",
                         "category": "update", "files": [a]},
                        session_id="manual", ts="2026-07-15T10:05:00Z")
    assert changelog.queue(events.read_all(), session_id="s1") is None


def test_stale_entry_does_not_cover_new_work(tmp_path):
    # An entry recorded BEFORE this session first touched the file belongs to
    # an earlier change-set — it must not exempt the new work.
    p = tmp_path / "src/mindhive/a.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x = 1\n", encoding="utf-8")
    events.append_event("code_change",
                        {"text": "last week's change to the same file",
                         "category": "update", "files": [str(p)]},
                        session_id="manual", ts="2026-07-08T09:00:00Z")
    events.append_event("file_touched", {"text": str(p)},
                        session_id="s2", ts="2026-07-15T10:00:00Z")
    marker = changelog.queue(events.read_all(), session_id="s2")
    assert marker["files"] == [str(p)]


def test_record_without_session_does_not_sweep_history(tmp_path, monkeypatch):
    # No marker, no session id: record must NOT attribute every file ever
    # touched across all sessions to this entry — it falls to git detection.
    _touch("src/mindhive/old.py", tmp_path, session="s1")
    monkeypatch.setattr(changelog, "_git_changed_files",
                        lambda: ["src/mindhive/fresh.py"])
    ev = changelog.record("update", "recorded outside any known session",
                          session_id=None)
    assert ev.payload["files"] == ["src/mindhive/fresh.py"]


def test_gate_survives_unrelated_later_session(tmp_path):
    # Session s1 ends with unlogged debt; session s2 touches nothing. s2's
    # session_end queue must NOT evaporate s1's marker — the gate is
    # cumulative and only a covering entry clears it.
    a = _touch("src/mindhive/a.py", tmp_path, session="s1")
    changelog.queue(events.read_all(), session_id="s1")
    marker = changelog.queue(events.read_all(), session_id="s2")
    assert marker is not None and marker["files"] == [a]


def test_carried_over_debt_clears_once_covered(tmp_path):
    # A carried-forward file drops out when an entry recorded at-or-after the
    # marker's creation covers it — even if the covering entry was appended
    # without record() (which clears the marker itself). The covering ts is
    # derived from the marker, not a wall-clock literal: markers stamp real
    # now(), so a hardcoded date ages out and starts failing once the suite
    # runs past it.
    a = _touch("src/mindhive/a.py", tmp_path, session="s1")
    marker = changelog.queue(events.read_all(), session_id="s1")
    events.append_event("code_change",
                        {"text": "covered by a later direct entry",
                         "category": "update", "files": [a]},
                        session_id="manual", ts=marker["created_at"])
    assert changelog.queue(events.read_all(), session_id="s2") is None
    assert changelog.load_pending() is None


def test_out_of_scope_touches_do_not_trigger_gate():
    events.append_event("file_touched", {"text": "/repo/docs/notes.md"},
                        session_id="s1", ts="2026-07-15T10:00:00Z")
    assert changelog.queue(events.read_all(), session_id="s1") is None


# --- notice / projector -------------------------------------------------------

def test_notice_empty_without_marker():
    assert changelog.notice(None) == ""


def test_projector_renders_unlogged_notice_and_ledger_entry(tmp_path):
    a = _touch("src/mindhive/a.py", tmp_path)
    changelog.queue(events.read_all(), session_id="s1")
    projector.project()
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "Unlogged code changes" in state
    assert "hive log <category>" in state
    assert a in state
    # recording clears the notice on the next rebuild and lands in the LEDGER
    changelog.record("major_change", "added the mandatory change-log gate",
                     session_id="s1")
    projector.project()
    state2 = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "Unlogged code changes" not in state2
    ledger = list((paths.project_dir() / "LEDGER").glob("*code_change*.md"))
    assert len(ledger) == 1
    ledger_body = ledger[0].read_text(encoding="utf-8")
    assert "mandatory change-log gate" in ledger_body
    # audit metadata survives projection: category tag and affected files
    assert "[major_change]" in ledger_body
    assert a in ledger_body


def test_review_and_changelog_notices_coexist(tmp_path):
    from mindhive import review
    _touch("src/mindhive/a.py", tmp_path)
    review.queue(events.read_all(), session_id="s1")
    changelog.queue(events.read_all(), session_id="s1")
    projector.project()
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "Pending thermo-nuclear review" in state
    assert "Unlogged code changes" in state


def test_record_survives_locked_marker_file(monkeypatch):
    # Windows AV/indexer can hold pending_changelog.json; the entry is already
    # appended by then, so record must not raise (a failure exit would invite
    # a duplicate entry). The marker self-heals at the next queue().
    def _locked(project=None):
        raise OSError("file in use")
    monkeypatch.setattr(changelog, "clear", _locked)
    ev = changelog.record("update", "entry lands despite a locked marker",
                          files=["src/mindhive/a.py"])
    assert ev.type == "code_change"
    assert len(events.read_all()) == 1


def test_cli_append_enforces_code_change_contract(capsys):
    # `hive append code_change …` is the one user-facing path that accepts an
    # arbitrary payload — it must not smuggle an entry past the mandatory-log
    # contract. (events.append_event itself stays a type-agnostic writer.)
    assert cli_mod.main(["append", "code_change", "free text, no category"]) == 2
    assert "hive log" in capsys.readouterr().err
    bad = '{"text": "x", "category": "update", "files": ["src/mindhive/a.py"]}'
    assert cli_mod.main(["append", "code_change", bad, "--json"]) == 2
    good = ('{"text": "a real explanation of the change", '
            '"category": "update", "files": ["src/mindhive/a.py"]}')
    assert cli_mod.main(["append", "code_change", good, "--json"]) == 0
    evs = events.read_all()
    assert len(evs) == 1 and evs[0].type == "code_change"


def test_memory_view_never_collapses_code_change_entries():
    # Display-time dedupe must not merge similarly-worded audit entries —
    # they are distinct changes even when the prose is near-identical.
    from mindhive import dedupe
    items = [
        {"type": "code_change", "project": "p", "ts": "2026-07-15T10:01:00Z",
         "text": "updated the dashboard polling logic for the log view"},
        {"type": "code_change", "project": "p", "ts": "2026-07-15T10:00:00Z",
         "text": "updated the dashboard polling logic for the log view"},
    ]
    out = dedupe.collapse(items)
    assert len(out) == 2
    assert all(i["similar_count"] == 0 for i in out)


def test_code_change_exempt_from_consolidation():
    # The dreaming pass merges near-duplicate semantic memories; the change
    # log is an audit trail and must never have entries absorbed.
    from mindhive import consolidate
    assert "code_change" not in consolidate.CONSOLIDATE_TYPES
    assert "decision" in consolidate.CONSOLIDATE_TYPES  # sanity: set intact


# --- CLI ----------------------------------------------------------------------

def test_cli_log_records_entry():
    ret = cli_mod.main(["log", "update",
                        "sharpened the dashboard polling interval",
                        "--files", "src/mindhive/dashboard.py"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].type == "code_change"
    assert evs[0].payload["category"] == "update"


def test_cli_log_rejects_short_explanation(capsys):
    ret = cli_mod.main(["log", "update", "x",
                        "--files", "src/mindhive/dashboard.py"])
    assert ret == 2
    assert "explanation is mandatory" in capsys.readouterr().err
    assert events.read_all() == []


def test_cli_log_unknown_category_is_a_usage_error():
    with pytest.raises(SystemExit):
        cli_mod.main(["log", "banana", "some explanation here"])


def test_cli_log_queue_and_pending(tmp_path, capsys):
    a = _touch("src/mindhive/a.py", tmp_path)
    ret = cli_mod.main(["log", "queue", "--session", "s1"])
    assert ret == 0
    assert "change-log entry is required" in capsys.readouterr().out
    assert changelog.load_pending()["files"] == [a]


def test_cli_log_show_lists_entries_newest_first(capsys):
    cli_mod.main(["log", "update", "first entry, an older change",
                  "--files", "src/mindhive/a.py"])
    cli_mod.main(["log", "replacement", "second entry, swapped the parser",
                  "--files", "src/mindhive/b.py"])
    capsys.readouterr()
    ret = cli_mod.main(["log", "show"])
    assert ret == 0
    out = capsys.readouterr().out
    assert out.index("swapped the parser") < out.index("older change")


# --- the MindNetwork join key (KTD16) ----------------------------------------
#
# `execution` is the one phase whose evidence is an EVENT rather than a file: it
# looks for a code_change carrying its task_id. Before `--task` existed the
# contract validated a field no surface could produce, so the phase was
# unsatisfiable through the CLI that writes every change-log entry.

def _one_code_change():
    rows = [e for e in events.read_all() if e.type == "code_change"]
    assert len(rows) == 1, f"expected exactly one code_change, got {len(rows)}"
    return rows[0]


def test_record_stamps_the_task_join_key():
    ev = changelog.record("update", "taught the fold to refuse a cycling edge",
                          files=["src/mindhive/a.py"], task_id="mn1-u8")
    assert ev.payload["task_id"] == "mn1-u8"


def test_record_omits_the_key_entirely_when_no_task_is_given():
    """Not `None`, not `""` — ABSENT. An untracked entry has to stay
    byte-identical to every historical one, or the hash chain sees a schema
    change on rows nobody touched."""
    ev = changelog.record("update", "an ordinary entry, no task attached",
                          files=["src/mindhive/a.py"])
    assert "task_id" not in ev.payload


@pytest.mark.parametrize("bad", ["Not Kebab", "has space", "UPPER", "x" * 65])
def test_record_rejects_a_malformed_task_id(bad):
    with pytest.raises(ValueError):
        changelog.record("update", "a fine explanation of a real change",
                         files=["src/mindhive/a.py"], task_id=bad)


def test_cli_log_task_flag_stamps_the_key(capsys):
    ret = cli_mod.main(["log", "update", "wired the join key through the CLI",
                        "--files", "src/mindhive/a.py", "--task", "mn1-u8"])
    capsys.readouterr()
    assert ret is None or ret == 0
    assert _one_code_change().payload["task_id"] == "mn1-u8"


def test_cli_log_without_the_task_flag_omits_the_key(capsys):
    ret = cli_mod.main(["log", "update", "no task on this change-set at all",
                        "--files", "src/mindhive/a.py"])
    capsys.readouterr()
    assert ret is None or ret == 0
    assert "task_id" not in _one_code_change().payload


def test_cli_log_rejects_a_malformed_task_id(capsys):
    ret = cli_mod.main(["log", "update", "a fine explanation of a real change",
                        "--files", "src/mindhive/a.py", "--task", "Not Kebab"])
    assert ret == 2
    err = capsys.readouterr().err
    # Name the FLAG the operator typed, not the payload field it becomes — and
    # say why a near-miss matters, since an id that merely looks right joins to
    # nothing and leaves `execution` quietly unsatisfiable.
    assert "--task" in err and "near-miss" in err
    assert [e for e in events.read_all() if e.type == "code_change"] == []
