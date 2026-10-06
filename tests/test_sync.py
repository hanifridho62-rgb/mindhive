# tests/test_sync.py
from pathlib import Path
from mindhive import events, paths
from mindhive.sync import encode_folder, native_memory_path, sync, USAGE_NOTE


def test_encode_folder_hanif():
    assert encode_folder(r"C:\Users\hanif") == "C--Users-hanif"


def test_encode_folder_fabio():
    assert encode_folder(r"C:\FABIO_HFT_V18") == "C--FABIO-HFT-V18"


def test_encode_folder_forward_slash():
    assert encode_folder("C:/Users/hanif") == "C--Users-hanif"


def test_encode_folder_hyphenates_every_character_claude_code_does():
    """Claude Code names a project directory by replacing every character
    outside ``[A-Za-z0-9]`` with a hyphen, a dot included: on this machine
    ``<repo>\\.claude\\worktrees\\x`` is ``<repo>--claude-worktrees-x``. The
    older rule kept the dot and wrote a ``<repo>-.claude-...`` folder Claude
    never reads (chip task_cb518538)."""
    wt = r"C:\Users\hanif\mindhive\.claude\worktrees\cli-login-status-detection-cfec62"
    assert encode_folder(wt) == ("C--Users-hanif-mindhive--claude-worktrees-"
                                 "cli-login-status-detection-cfec62")
    assert encode_folder(r"D:\my dir\x+y.z") == "D--my-dir-x-y-z"


def test_hive_sync_keys_memory_by_the_main_repo_from_a_linked_worktree(tmp_path, monkeypatch):
    """Claude reads MEMORY.md under the MAIN repository's folder even for a
    session in a linked worktree (`context._memory_folder`), so a cwd key
    taken in a worktree wrote the digest where no session looks. An explicit
    `--folder` still wins."""
    import subprocess
    import mindhive.sync as sync_mod
    from mindhive import __main__ as cli_mod
    repo = tmp_path / "repo"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
                   check=True)
    wt = repo / ".claude" / "worktrees" / "wt"
    subprocess.run([*git, "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
    seen = []
    written = tmp_path / "MEMORY.md"
    written.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(sync_mod, "sync", lambda folder, **kw: seen.append(folder) or written)
    monkeypatch.chdir(wt)

    assert cli_mod.main(["sync", "--no-briefing"]) == 0
    assert Path(seen[-1]).resolve() == repo.resolve()
    assert cli_mod.main(["sync", "--folder", r"C:\elsewhere"]) == 0
    assert seen[-1] == r"C:\elsewhere"


def test_the_dashboard_keys_an_unregistered_project_the_same_way(monkeypatch):
    """The panel's Sync button resolves a project's folder from the autosync
    registry and fell back to the server's cwd: launched in a linked worktree,
    the same wrong key. The fallback is the main repository now."""
    from mindhive import autosync, context, dashboard_data
    monkeypatch.setattr(autosync, "load_registry", lambda: [
        {"project": "other", "folder": r"C:\registered"}])
    monkeypatch.setattr(context, "_memory_folder", lambda *a, **k: r"C:\main-repo")
    assert dashboard_data._folder_for_project("other") == r"C:\registered"
    assert dashboard_data._folder_for_project("mine") == r"C:\main-repo"


def test_native_memory_path_structure(tmp_path):
    p = native_memory_path(r"C:\Users\hanif", home=tmp_path)
    assert p == tmp_path / ".claude" / "projects" / "C--Users-hanif" / "memory" / "MEMORY.md"


def test_sync_writes_file(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "Test task", "verified": "ok",
                         "next_step": "continue", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    assert p.exists()
    assert p.name == "MEMORY.md"


def test_sync_contains_state_task(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "My active task", "verified": "green",
                         "next_step": "ship it", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "My active task" in content


def test_sync_contains_usage_note(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "use python"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "START" in content
    assert "DURING" in content
    assert "END" in content


def test_sync_with_briefing(tmp_path):
    """include_briefing keeps BRIEFING.md fresh ON DISK for on-demand reads, but
    MEMORY.md is a compact digest — the full briefing is no longer embedded."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "briefing decision"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, include_briefing=True, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "PROJECT BRIEFING" not in content              # not embedded any more
    assert "briefing decision" in content                 # digest carries decisions
    bpath = paths.project_dir() / "BRIEFING.md"
    assert bpath.exists()                                 # still written to disk
    assert "PROJECT BRIEFING" in bpath.read_text(encoding="utf-8")


def test_sync_no_briefing(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "no briefing decision"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, include_briefing=False, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "PROJECT BRIEFING" not in content


def test_sync_does_not_duplicate_state_block(tmp_path):
    """The digest embeds the curated state block exactly once (no briefing
    embedding to duplicate it against)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "Unique-task-token", "verified": "ok",
                         "next_step": "go", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, include_briefing=True, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert content.count("Unique-task-token") == 1
    assert "## Current State" in content
    assert "PROJECT BRIEFING" not in content


def test_sync_reads_event_log_once(tmp_path, monkeypatch):
    """sync must read the event log a single time and reuse it for both the
    STATE and briefing projections (was 2+ full read_all calls)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "x"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    calls = {"n": 0}
    real = events.read_all

    def counting(project=None):
        calls["n"] += 1
        return real(project)

    monkeypatch.setattr(events, "read_all", counting)
    sync(r"C:\TEMP\demo", project=None, include_briefing=True, home=fake_home)
    assert calls["n"] == 1


def test_sync_runs_recommend_chain_exactly_once(tmp_path, monkeypatch):
    """sync composes the state twice (STATE.md + the embedded digest) but must
    compute the suggested-skills block — the recommend chain — exactly once."""
    from mindhive import plugins
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "x"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    calls = {"n": 0}
    real = plugins.recommend_for

    def counting(project=None, evs=None, top=None):
        calls["n"] += 1
        return real(project=project, evs=evs, top=top)

    monkeypatch.setattr(plugins, "recommend_for", counting)
    sync(r"C:\TEMP\demo", project=None, include_briefing=True, home=fake_home)
    assert calls["n"] == 1


def test_sync_path_matches_encode_folder(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "x"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    folder = r"C:\Users\hanif"
    p = sync(folder, project=None, home=fake_home)
    expected = fake_home / ".claude" / "projects" / "C--Users-hanif" / "memory" / "MEMORY.md"
    assert p == expected


def test_usage_note_is_constant():
    """USAGE_NOTE is a module-level constant (DRY)."""
    assert "START" in USAGE_NOTE
    assert "DURING" in USAGE_NOTE
    assert "END" in USAGE_NOTE


# --- compact digest (KTD8): rules block, pinned lessons, size budget ---------

from mindhive import incidents


def _seed_reflected_rules(n, project="test"):
    """n active incidents, each with a reflected prevention rule. Later indices
    have more recent failures, so they rank higher at equal occurrences."""
    for i in range(n):
        sig = f"pymodule:sig{i:02d}"
        for s, ts in (("s1", f"2026-07-01T10:{i:02d}:00Z"),
                      ("s2", f"2026-07-02T10:{i:02d}:00Z")):
            events.append_event("tool_failed",
                                {"text": "boom", "signature": sig, "action": "x"},
                                session_id=s, tags=["auto", "failure"],
                                ts=ts, project=project)
    incidents.project(project=project, threshold=2)
    d = paths.project_dir(project) / "rules" / "incidents"
    for i in range(n):
        path = d / f"{incidents._safe_name(f'pymodule:sig{i:02d}')}.md"
        path.write_text(incidents._replace_section(
            path.read_text(encoding="utf-8"), "Auto Evaluation",
            f"- Prevention rule: avoid failure {i:02d}"), encoding="utf-8")
    incidents.project(project=project, threshold=2)


def test_memory_rules_block_bounded_and_each_rule_once(tmp_path):
    """30 active reflected rules -> capped block (+N more trailer) in BOTH
    STATE.md and MEMORY.md; each shown rule appears exactly once in MEMORY.md
    (no state-body/rules-block duplication)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_reflected_rules(30)
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert content.count("# PREVENTION RULES") == 1
    assert content.count("- [pymodule:sig") == incidents.INJECT_CAP
    assert "+20 more: hive rules list" in content
    # most recent (highest index) shown exactly once; oldest cut entirely
    assert content.count("avoid failure 29") == 1
    assert "avoid failure 00" not in content
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert state.count("- [pymodule:sig") == incidents.INJECT_CAP
    assert "+20 more: hive rules list" in state


def test_pinned_caveat_survives_cap_unpinned_rotate(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("caveat", {"text": "NEVER disturb V17 --live"},
                        session_id="s", tags=["pinned"],
                        ts="2026-06-01T09:00:00Z")
    for i in range(15):
        events.append_event("caveat", {"text": f"ordinary caveat number {i:02d}"},
                            session_id="s", ts=f"2026-06-20T09:{i:02d}:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "NEVER disturb V17 --live" in content          # pinned, despite being oldest
    assert "ordinary caveat number 14" in content         # newest unpinned kept
    assert "ordinary caveat number 00" not in content     # oldest unpinned rotated out
    # pinned entry rides ahead of the recency-filled remainder
    lessons = content.split("## Lessons & caveats", 1)[1]
    assert lessons.index("NEVER disturb V17") < lessons.index("caveat number 14")


def test_long_history_memory_stays_under_budget(tmp_path):
    """MEMORY.md for a long history stays under the ~6 KB character budget
    (≈1,500 tokens × 4) and still carries state + prevention rules."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "Budget-task-token", "verified": "ok",
                         "next_step": "go", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-01T08:00:00Z")
    for i in range(100):
        events.append_event("caveat",
                            {"text": f"lesson {i:03d}: watch for edge case {i} "
                                     f"in module {i % 7} when the cache is cold"},
                            session_id="s", ts=f"2026-06-10T{i // 60:02d}:{i % 60:02d}:00Z")
        events.append_event("decision",
                            {"text": f"decision {i:03d}: keep approach {i % 5} "
                                     f"for subsystem {i % 3}"},
                            session_id="s", ts=f"2026-06-11T{i // 60:02d}:{i % 60:02d}:00Z")
    _seed_reflected_rules(30)
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert len(content) < 6000
    assert "Budget-task-token" in content
    assert "# PREVENTION RULES" in content
    assert "## Lessons & caveats" in content
    assert "## Key decisions" in content


# --- the SOP-injection notice on the digest that loads itself ----------------
# This is the half that matters. A renderer reached only by STATE.md passes every
# unit test for the renderer while the auto-loaded digest stays silent, and
# MEMORY.md is the file a session reads without being asked.

def _mandatory_sop(text):
    """A real mandatory SOP, materialised the way `record` does. Not a
    hand-written sidecar: the rebuild inside `sync` regenerates this project's
    slice from the events, so a hand-written row would be erased by the very
    pass under test."""
    from mindhive import sop
    events.append_event("sop", {"action": "add",
                                "sop_id": "execution-discipline",
                                "title": "Execution discipline",
                                "text": text, "mandatory": True},
                        session_id="s1", ts="2026-08-14T00:00:00Z", tags=["sop"])
    sop.write_sidecar()


def _degraded_global_sidecar():
    from mindhive import sopgate
    _mandatory_sop("y" * (sopgate.PROMPT_BUDGET + 1))


def _memory(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    events.append_event("state_set", {"task": "T", "verified": "v",
                                      "next_step": "n", "open_questions": [],
                                      "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    return sync(r"C:\TEMP\demo", project=None,
                home=fake_home).read_text(encoding="utf-8")


def test_memory_md_carries_the_injection_notice_when_degraded(tmp_path):
    _degraded_global_sidecar()
    content = _memory(tmp_path)
    assert "SOP injection: pointers_only" in content
    assert "hive sop show execution-discipline" in content
    assert "every session on this machine" in content


def test_memory_md_carries_nothing_when_the_injection_is_healthy(tmp_path):
    _mandatory_sop("read the exact file before editing it")
    assert "SOP injection" not in _memory(tmp_path)


def test_state_md_and_the_digest_agree_within_one_sync(tmp_path):
    """The rebuild refreshes the sidecar the verdict is measured from, so the two
    files must not report different verdicts for the same measurement."""
    _degraded_global_sidecar()
    content = _memory(tmp_path)
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "SOP injection: pointers_only" in state
    assert "SOP injection: pointers_only" in content



# --- the rollover notice and the adaptive tail (blueprint P1c) -----------------------------
# The item caps still decide what the default digest shows; what they leave out is
# now counted and pointed at the semantic index. The adaptive tail is opt-in: on,
# the digest is sized to the composer's safe budget and Tier 1 is never cut.

from mindhive import adaptivetail, context  # noqa: E402
from mindhive.sync import DECISIONS_CAP, LESSONS_CAP  # noqa: E402


def _history(n_caveats, n_decisions, pad=40):
    def ts(day, i):
        return f"2026-06-{day}T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z"
    for i in range(n_caveats):
        events.append_event("caveat", {"text": f"caveat {i:03d}: watch edge case {i} in "
                                               f"module {i * 3} " + "c" * pad},
                            session_id="s", ts=ts(10, i))
    for i in range(n_decisions):
        events.append_event("decision", {"text": f"decision {i:03d}: keep approach {i} "
                                                 f"for subsystem {i * 7} " + "d" * pad},
                            session_id="s", ts=ts(11, i))


def test_rollover_line_names_what_was_dropped(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _history(15, 7)
    content = sync(r"C:\TEMP\demo", project=None, home=fake_home).read_text(encoding="utf-8")
    found = adaptivetail.find_rollover(content)
    assert found is not None
    assert found["count"] == (15 - LESSONS_CAP) + (7 - DECISIONS_CAP) == 7
    # The notice closes the digest, after the sections whose items it counts.
    assert content.index("## Key decisions") < found["start"]
    assert content[found["end"]:].strip() == ""


def test_no_rollover_line_when_the_caps_leave_nothing_out(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _history(3, 2)
    content = sync(r"C:\TEMP\demo", project=None, home=fake_home).read_text(encoding="utf-8")
    assert adaptivetail.find_rollover(content) is None
    assert "keputusan dan pelajaran" not in content


def test_adaptive_fill_is_opt_in(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _history(40, 40)
    content = sync(r"C:\TEMP\demo", project=None, home=fake_home).read_text(encoding="utf-8")
    assert content.count("- caveat ") == LESSONS_CAP
    assert content.count("- decision ") == DECISIONS_CAP
    assert "## Active work session" not in content
    assert adaptivetail.find_rollover(content)["count"] == 80 - LESSONS_CAP - DECISIONS_CAP

    context.save_adaptive_tail(None, enabled=True, lessons=30, decisions=20)
    content = sync(r"C:\TEMP\demo", project=None, home=fake_home).read_text(encoding="utf-8")
    assert content.count("] caveat ") == 30 and content.count("] decision ") == 20
    assert adaptivetail.find_rollover(content)["count"] == 30
    assert "- [2026-06-10] caveat 039:" in content and "caveat 009:" not in content


def test_the_adaptive_digest_fits_the_safe_budget_and_keeps_tier_one(tmp_path):
    from datetime import datetime, timezone
    from mindhive import worksessions as ws
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("caveat", {"text": "NEVER disturb V17 --live"},
                        session_id="s", tags=["pinned"], ts="2026-01-01T00:00:00Z")
    _history(400, 400, pad=60)
    _mandatory_sop("Run the whole suite before calling anything done.")
    soul = incidents.soul_path()
    soul.parent.mkdir(parents=True, exist_ok=True)
    soul.write_text("- rule one: never push without green\n"
                    "- rule two: log every change\n", encoding="utf-8")
    step = ws.record_work_session_step("s-live", ws.create_work_session_step(
        "execution", "TAIL_TRIM", False,
        now_fn=lambda: datetime(2026, 9, 11, tzinfo=timezone.utc)))
    context.save_adaptive_tail(None, enabled=True, lessons=1000, decisions=1000)

    path = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    pressure = context.memory_pressure(path)
    assert pressure["bytes"] <= adaptivetail.MAX_LOADER_BYTES, pressure
    assert pressure["lines"] <= adaptivetail.MAX_LOADER_LINES, pressure
    assert not pressure["truncated"] and not pressure["over_safe"]
    assert pressure["managed"] and pressure["rollover_count"] > 0
    content = path.read_text(encoding="utf-8")
    assert "NEVER disturb V17 --live" in content                    # pinned
    assert "rule two: log every change" in content                  # SOUL
    assert "Execution discipline" in content                        # mandatory SOP
    assert "## Active work session" in content and step.technical_action in content
    shown = content.count("] caveat ") + content.count("] decision ")
    assert shown > 0 and shown + pressure["rollover_count"] == 800


def test_the_adaptive_digest_ends_before_a_lowered_loader_cut(tmp_path):
    """`memory_lines` / `memory_bytes` set BELOW the composer's safe budget are
    the cut `hive context --check` judges against, so the tail yields to them."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _history(400, 400, pad=60)
    context.save_adaptive_tail(None, enabled=True, lessons=1000, decisions=1000)
    for cuts in ({"memory_lines": 150}, {"memory_lines": 200, "memory_bytes": 20_000}):
        context.save_budget(1_000_000, **cuts)
        path = sync(r"C:\TEMP\demo", project=None, home=fake_home)
        pressure = context.memory_pressure(path)
        assert not pressure["truncated"] and pressure["managed"], (cuts, pressure)
        assert pressure["lines"] <= cuts["memory_lines"] - adaptivetail.LOADER_MARGIN_LINES
        assert pressure["bytes"] <= cuts.get("memory_bytes", 25_000) - \
            adaptivetail.LOADER_MARGIN_BYTES
        content = path.read_text(encoding="utf-8")
        shown = content.count("] caveat ") + content.count("] decision ")
        assert shown > 0 and shown + pressure["rollover_count"] == 800


def test_the_default_notice_never_gets_a_digest_cut(tmp_path, monkeypatch):
    """A digest the loader reads whole WITHOUT the notice and not with it goes out
    without it; any other digest that left items out carries it."""
    from mindhive import projector, vectors
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _history(LESSONS_CAP + 1, 2)                       # one caveat past the cap
    pad = {"n": 1000}
    monkeypatch.setattr(projector, "compose_state", lambda *a, **k: "x" * pad["n"] + "\n")
    monkeypatch.setattr(vectors, "has_vectors_support", lambda: True)

    def digest():
        return sync(r"C:\TEMP\demo", project=None, home=fake_home)

    with monkeypatch.context() as m:
        m.setattr(adaptivetail, "rollover_notice", lambda *a, **k: "")
        bare = context.memory_pressure(digest())["bytes"]
    pad["n"] += 24_950 - bare                          # 24 950 bytes without the notice
    path = digest()
    pressure = context.memory_pressure(path)
    assert pressure["bytes"] == 24_950 and not pressure["truncated"]
    assert adaptivetail.find_rollover(path.read_text(encoding="utf-8")) is None

    pad["n"] -= 400                                    # room for the notice: it is back
    pressure = context.memory_pressure(digest())
    assert not pressure["truncated"] and pressure["rollover_count"] == 1

    pad["n"] += 500                                    # cut with or without the notice
    pressure = context.memory_pressure(digest())
    assert pressure["truncated"] and pressure["rollover_count"] == 1


def test_the_adaptive_notice_never_gets_a_digest_cut(tmp_path, monkeypatch, capsys):
    """Review round 2. The default path withheld a notice that would push the
    digest past the loader cut; the adaptive path appended it regardless, so a
    fixed part within a notice's length of the cut produced an UNMANAGED
    truncation and `hive context --check` exit 1 -- from the notice alone. Same
    rule on both paths now, in bytes and in lines."""
    import mindhive.sync as sync_mod
    from mindhive import __main__ as cli_mod
    from mindhive import projector, vectors
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _history(3, 2)
    context.save_budget(1_000_000)
    context.save_adaptive_tail(None, enabled=True)
    # A fixed part this large leaves the tail no room in either dimension, so
    # all five items roll whatever the padding below is tuned to.
    pad = {"n": 23_500, "lines": 0}
    monkeypatch.setattr(projector, "compose_state",
                        lambda *a, **k: "x" * pad["n"] + "\n" + "l\n" * pad["lines"])
    monkeypatch.setattr(vectors, "has_vectors_support", lambda: True)

    def digest():
        return sync(r"C:\TEMP\demo", project=None, home=fake_home)

    def bare(key):
        with monkeypatch.context() as m:
            m.setattr(adaptivetail, "rollover_notice", lambda *a, **k: "")
            return context.memory_pressure(digest())[key]

    pad["n"] += 24_950 - bare("bytes")                 # 24 950 bytes without the notice
    path = digest()
    pressure = context.memory_pressure(path)
    assert pressure["bytes"] == 24_950 and not pressure["truncated"]
    assert adaptivetail.find_rollover(path.read_text(encoding="utf-8")) is None
    monkeypatch.setattr(sync_mod, "native_memory_path", lambda folder, home=None: path)
    assert cli_mod.main(["context", "--origin", "hive", "--check"]) == 0
    assert "TRUNCATED" not in capsys.readouterr().err

    pad["n"] -= 600                                    # room for the notice: it is back
    pressure = context.memory_pressure(digest())
    assert not pressure["truncated"] and pressure["rollover_count"] == 5

    pad["n"], pad["lines"] = 1000, 185                 # the same window, in LINES
    pad["lines"] += 199 - bare("lines")                # 199 lines without the notice
    pressure = context.memory_pressure(digest())
    assert pressure["lines"] == 199 and not pressure["truncated"]
    assert pressure["rollover_count"] == 0

    pad["lines"] -= 10                                 # room for its three lines: back
    pressure = context.memory_pressure(digest())
    assert not pressure["truncated"] and pressure["rollover_count"] == 5


def test_a_fixed_part_that_leaves_no_room_still_passes_the_check(tmp_path, monkeypatch, capsys):
    """Plan review KRITIK 3 end to end: fixed bytes that leave the tail 300 bytes
    give a digest of the fixed part and the notice alone, under the safe
    ceiling, and `hive context --check` passes it without a warning."""
    import mindhive.sync as sync_mod
    from mindhive import __main__ as cli_mod
    from mindhive import projector, vectors
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _history(20, 20)
    context.save_budget(1_000_000)
    context.save_adaptive_tail(None, enabled=True)
    monkeypatch.setattr(projector, "compose_state", lambda *a, **k: "state\n")
    monkeypatch.setattr(vectors, "has_vectors_support", lambda: False)   # the longer notice
    head = "\n".join([USAGE_NOTE, "## Current State\n", "```\nstate\n```\n"])
    want = adaptivetail.MAX_LOADER_BYTES - adaptivetail.SAFETY_MARGIN_BYTES - 300 \
        - adaptivetail.lf_bytes(head)
    rows, left = [], want                              # `want` bytes once "\n" closes it
    while left > 0:
        take = min(200, left)
        rows.append("s" * (take - 1))
        left -= take
    soul = "\n".join(rows)
    monkeypatch.setattr(incidents, "soul_block", lambda project=None: soul)
    assert adaptivetail.calculate_budget(head, soul + "\n", "") == 300

    path = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = path.read_text(encoding="utf-8")
    assert "] caveat " not in content and "] decision " not in content
    assert content.endswith(adaptivetail.rollover_notice(40, vectors_available=False))
    pressure = context.memory_pressure(path)
    assert pressure["bytes"] <= adaptivetail.MAX_LOADER_BYTES, pressure
    assert not pressure["truncated"] and not pressure["over_safe"]
    assert pressure["managed"] and pressure["rollover_count"] == 40

    monkeypatch.setattr(sync_mod, "native_memory_path", lambda folder, home=None: path)
    assert cli_mod.main(["context", "--origin", "hive", "--check"]) == 0
    err = capsys.readouterr().err
    assert "TRUNCATED" not in err and "WARNING" not in err


# --- review round 2: what the adaptive path charges, filters and guards -------------------------

def test_the_rules_block_and_the_active_session_are_charged_as_fixed_parts(
        tmp_path, monkeypatch, capsys):
    """Only the header and SOUL terms of the budget were pinned. Uncharged, thirty
    prevention rules write a 202-line digest whose notice falls past the line cut
    -- the unmanaged truncation the adaptive tail exists to prevent -- and an
    uncharged work session takes it past the safe budget."""
    from datetime import datetime, timezone
    import mindhive.sync as sync_mod
    from mindhive import __main__ as cli_mod
    from mindhive import projector, worksessions as ws
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_reflected_rules(30)
    _history(400, 400, pad=60)
    ws.record_work_session_step("s-live", ws.create_work_session_step(
        "execution", "x" * 120, True, initiative_reason="r" * 200,
        now_fn=lambda: datetime(2026, 9, 11, tzinfo=timezone.utc)))
    context.save_budget(1_000_000)
    context.save_adaptive_tail(None, enabled=True, lessons=1000, decisions=1000)
    monkeypatch.setattr(projector, "compose_state", lambda *a, **k: "state\n")
    asked = {}
    real = adaptivetail.compose_adaptive_parts

    def spy(decisions, lessons, available_bytes, **kw):
        asked.update(bytes=available_bytes, lines=kw["available_lines"])
        return real(decisions, lessons, available_bytes, **kw)
    monkeypatch.setattr(adaptivetail, "compose_adaptive_parts", spy)

    path = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    evs = events.read_all()
    rules = incidents.inject(None, evs=evs) + "\n"
    active = adaptivetail.active_work_session_block(evs, None)
    header = "\n".join([USAGE_NOTE, "## Current State\n", "```\nstate\n```\n"])
    assert "avoid failure" in rules and active.startswith("## Active work session")
    fixed = [header, rules, active]
    assert asked["bytes"] == (adaptivetail.MAX_LOADER_BYTES - adaptivetail.SAFETY_MARGIN_BYTES
                              - sum(adaptivetail.lf_bytes(part) for part in fixed))
    assert asked["lines"] == (adaptivetail.MAX_LOADER_LINES - adaptivetail.SAFETY_MARGIN_LINES
                              - sum(adaptivetail.line_count(part) for part in fixed))

    content = path.read_text(encoding="utf-8")
    assert rules in content and active in content
    pressure = context.memory_pressure(path)
    assert pressure["lines"] <= adaptivetail.MAX_LOADER_LINES, pressure
    assert pressure["bytes"] <= adaptivetail.MAX_LOADER_BYTES, pressure
    assert pressure["managed"] and not pressure["truncated"] and not pressure["over_safe"]
    monkeypatch.setattr(sync_mod, "native_memory_path", lambda folder, home=None: path)
    assert cli_mod.main(["context", "--origin", "hive", "--check"]) == 0
    assert "TRUNCATED" not in capsys.readouterr().err


def test_a_consolidated_memo_is_neither_shown_nor_counted_as_rolled_over(tmp_path):
    """The adaptive path filters absorbed events itself, and the default path's
    count depends on the same filter: a memo `hive consolidate` absorbed was not
    "rolled over", and must not come back into the digest."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    for i in range(LESSONS_CAP + 2):
        events.append_event("caveat", {"text": f"caveat {i:03d}: watch edge case {i}"},
                            session_id="s", ts=f"2026-06-10T00:00:{i:02d}Z")
    events.append_event("memory_consolidated",
                        {"absorbed_ts": ["2026-06-10T00:00:10Z", "2026-06-10T00:00:11Z"]},
                        session_id="s", ts="2026-06-12T00:00:00Z")

    content = sync(r"C:\TEMP\demo", project=None, home=fake_home).read_text(encoding="utf-8")
    assert content.count("- caveat ") == LESSONS_CAP
    assert "caveat 010" not in content and "caveat 011" not in content
    assert adaptivetail.find_rollover(content) is None      # ten visible, ten shown

    context.save_adaptive_tail(None, enabled=True, lessons=100, decisions=100)
    content = sync(r"C:\TEMP\demo", project=None, home=fake_home).read_text(encoding="utf-8")
    assert content.count("] caveat ") == LESSONS_CAP
    assert "caveat 010" not in content and "caveat 011" not in content
    assert adaptivetail.find_rollover(content) is None


def test_the_default_notice_guard_reads_the_line_cut_and_the_namespaces_own(
        tmp_path, monkeypatch):
    """The byte test above sits at the documented 25 000. The guard has a LINE
    clause too, and both clauses read this namespace's cut (`memory_lines`), not
    the constants: a digest of 197-200 lines goes out without the four-line
    notice, and under ``memory_lines=150`` that window is 147-150."""
    from mindhive import projector, vectors
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _history(LESSONS_CAP + 1, 2)                       # one caveat past the cap
    pad = {"n": 10}
    monkeypatch.setattr(projector, "compose_state", lambda *a, **k: "x\n" * pad["n"])
    monkeypatch.setattr(vectors, "has_vectors_support", lambda: True)

    def digest():
        return sync(r"C:\TEMP\demo", project=None, home=fake_home)

    with monkeypatch.context() as m:
        m.setattr(adaptivetail, "rollover_notice", lambda *a, **k: "")
        bare = context.memory_pressure(digest())["lines"] - pad["n"]

    def written(lines_without_the_notice):
        pad["n"] = lines_without_the_notice - bare
        path = digest()
        return (context.memory_pressure(path),
                adaptivetail.find_rollover(path.read_text(encoding="utf-8")))

    for cut in (200, 150):
        if cut != 200:
            context.save_budget(1_000_000, memory_lines=cut)
        pressure, notice = written(cut - 4)            # room for its four lines
        assert notice and pressure["lines"] == cut and not pressure["truncated"], cut
        for lines in (cut - 3, cut):                   # fits without it, not with it
            pressure, notice = written(lines)
            assert notice is None and pressure["lines"] == lines, (cut, lines)
            assert not pressure["truncated"], (cut, lines)
        pressure, notice = written(cut + 1)            # cut either way: still counted
        assert notice and pressure["truncated"] and not pressure["managed"], cut
