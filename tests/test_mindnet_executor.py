# tests/test_mindnet_executor.py
"""MN-1 U6 — the validation executor: the one place Python runs a string an
agent authored.

Written TEST-FIRST against
``docs/plans/2026-07-25-001-feat-mn1-phase-spine-plan.md`` KTD15 and the
committed contracts in `runlog.py`, `gateguard.py`, `phases.py` and
`controller.py`. No line of `executor.py` existed when these were written.

Every executing test drives a REAL subprocess against the committed
``scripts/mindnet/mock_validator.py``, per `tests/test_research.py`'s
precedent. The exit-code capture, the timeout kill, and the argv that actually
reaches the child ARE the behaviour under test; a mocked ``subprocess`` would
assert only that the module calls a function whose return value the test wrote,
and would have caught neither of the two defects this file exists to catch.

Three properties carry the file:

1. **The gate fails CLOSED.** Any reason string refuses, and so does any
   EXCEPTION raised inside the classifier. That inverts
   `enforce.evaluate_policies`, which wraps the same classifier in
   ``except Exception: return None`` — allow. Fail-open is right for an
   advisory hook where a crash must not block a real tool call, and
   fail-DANGEROUS here, where the same crash would read as "no reason found,
   therefore run it". `test_the_advisory_hook_fails_open_where_this_gate_fails_closed`
   asserts both halves side by side so the asymmetry is deliberate and visible.

2. **A refusal executes NOTHING.** Asserted by the absence of a marker file the
   mock writes as its very first act — not by the return value, which a broken
   gate could produce after already having spawned the child.

3. **``posix=False`` is load-bearing on this machine.** POSIX-mode ``shlex``
   treats backslashes as escapes, so ``pytest C:\\Users\\hanif\\mindhive\\tests``
   silently becomes ``pytest C:Usershanifmindhivetests``; the command then fails
   for a reason that has nothing to do with the code under test, and the
   executor records a FALSE ``result=fail`` as python-verified evidence.
   `test_a_windows_path_argument_survives_intact` is the regression guard, and
   it asserts the argv the child actually received rather than the command
   string, which looks correct either way.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import enforce, events, gateguard, paths, runlog
from mindhive.mindnet import controller as c_mod
from mindhive.mindnet import executor as x_mod
from mindhive.mindnet import phases as ph_mod
from mindhive.mindnet import tasks as t_mod

MOCK = Path(__file__).resolve().parents[1] / "scripts" / "mindnet" / "mock_validator.py"
TASK = "mn1-u6"

# A real, Windows-shaped absolute path. POSIX-mode shlex eats every backslash
# in this and leaves `C:Usershanifmindhivetests`.
WIN_PATH = r"C:\Users\hanif\mindhive\tests"


# --- helpers -------------------------------------------------------------------

def _script(*args):
    """A validation script string that runs the committed mock."""
    return " ".join([sys.executable, str(MOCK), *[str(a) for a in args]])


def _marker(tmp_path, name="ran.txt"):
    return tmp_path / name


def _ran(marker) -> int:
    """How many mock processes ran. 0 means the child was never spawned — the
    only honest way to assert a refusal refused."""
    try:
        return len([ln for ln in marker.read_text(encoding="utf-8").splitlines()
                    if ln.strip()])
    except OSError:
        return 0


def _declare(task_id=TASK, script="", **kw):
    """Declare on BOTH axes — `controller.fold` only materializes a PhaseRecord
    from a `mindnet_phase` declare, and a task holding only the graph half
    orphans every phase event aimed at it (see test_mindnet_controller.py).

    Every append here deliberately uses the DEFAULT session, the same one
    `executor.validate` writes under. `events.read_all` sorts by
    ``(ts, seq, segment, line)``. The append stamp ``seq`` puts same-second rows
    in append order while the high-water mark in `append.lock` rules every
    stamp, but not always across segments: two rows with no stamp on or above
    them in their segments, or two sharing a stamp, fall back to the segment
    name; an append on either of `events._append_lock`'s fail-open paths can
    replay ahead of a row written before it; and so can one that finds the mark
    lost or left stale by a failed write while the clock reads no later than a
    stamp already issued (`events._next_seq`). Any of these can replay setup
    events from another
    segment AFTER the satisfy they are a precondition for — the satisfy would
    orphan, the fold would show nothing, and the test would fail for a reason
    that has nothing to do with U6. That tie-break is H17 in the MN-1 plan
    (``docs/plans/2026-07-25-001-feat-mn1-phase-spine-plan.md``), which ``seq``
    settles only for rows whose stamps differ; it is not this unit's to fix, and
    a test that straddles two segments is testing the tie-break, not the
    executor."""
    payload = {"action": "declare", "task_id": task_id, "title": f"task {task_id}"}
    if script:
        payload["validation_script"] = script
    payload.update(kw)
    events.append_event("mindnet_task", payload, tags=["mindnet", "task"])
    events.append_event("mindnet_phase",
                        {"action": "declare", "task_id": task_id,
                         "verified_by": "python"},
                        tags=["mindnet", "phase"])


def _at_audit_bug(task_id=TASK, script=""):
    """Drive *task_id* legitimately to `audit-bug`, the command phase the
    executor satisfies. The triviality bypass makes the entry phase
    `execution`, so this is two honest moves rather than five."""
    _declare(task_id, script=script, triviality_bypass=True)
    events.append_event(
        "mindnet_phase",
        {"action": "satisfy", "task_id": task_id, "phase": "execution",
         "epoch": 0, "verified_by": "python",
         "evidence": {"kind": "event", "ref": "", "result": "ok"}},
        tags=["mindnet", "phase"])
    events.append_event(
        "mindnet_phase",
        {"action": "enter", "task_id": task_id, "phase": "audit-bug",
         "verified_by": "python"},
        tags=["mindnet", "phase"])
    evs = events.read_all()
    rec = c_mod.fold(evs, t_mod.fold(evs)).records[task_id]
    assert rec.phase == "audit-bug", "setup failed to reach the command phase"
    return task_id


def _records():
    evs = events.read_all()
    return c_mod.fold(evs, t_mod.fold(evs)).records


def _satisfies(task_id=TASK):
    """Raw `satisfy` rows on the ledger for *task_id* — the projection collapses
    contradicting records, and several assertions here are about the rows."""
    out = []
    for ev in events.read_all():
        if ev.type != "mindnet_phase":
            continue
        p = ev.payload
        if isinstance(p, dict) and p.get("action") == "satisfy" \
                and p.get("task_id") == task_id:
            out.append(p)
    return out


# --- the declared surface --------------------------------------------------------

# --- MN-7 U3: the child-environment seam ------------------------------------------
#
# The orchestrator needs a spawned child to inherit the parent's MN-6 arming
# decision, so `execute` grew an optional `env=`. Real subprocesses here, per the
# file's own precedent: what reaches the child IS the behaviour, and a mocked
# `subprocess` would assert only that the module passes along a dict the test
# wrote.

def _env_probe(tmp_path, *names):
    """A child script that writes the named variables to a marker file.

    A file rather than stdout because `execute` bounds and rewrites captured
    output, and a separate throwaway script rather than `python -c` because
    `gate` splits with ``shlex.split(posix=False)`` — quotes are retained in the
    token, so an inline `-c "code"` would reach the interpreter still quoted."""
    marker = tmp_path / "env.txt"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        f"names = {list(names)!r}\n"
        "vals = [name + '=' + os.environ.get(name, '<unset>') "
        "for name in names]\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8')"
        ".write('\\n'.join(vals))\n",
        encoding="utf-8")
    return marker, " ".join([sys.executable, str(probe)])


def _probe_read(marker) -> dict:
    text = marker.read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines() if line)


def test_no_env_argument_leaves_the_child_environment_untouched(tmp_path,
                                                               monkeypatch):
    """The regression guard for every existing caller. `env=None` must keep
    today's exact behaviour — inherit the parent — not an empty environment."""
    monkeypatch.setenv("MN7_PROBE", "inherited")
    marker, script = _env_probe(tmp_path, "MN7_PROBE")
    run = x_mod.execute("mn-t-env-1", script, root=tmp_path)
    assert run.result == "ok", run.reason
    assert _probe_read(marker)["MN7_PROBE"] == "inherited"


def test_an_injected_variable_reaches_the_child(tmp_path):
    marker, script = _env_probe(tmp_path, "MINDNETWORK_ENFORCE")
    run = x_mod.execute("mn-t-env-2", script, root=tmp_path,
                        env={"MINDNETWORK_ENFORCE": "1"})
    assert run.result == "ok", run.reason
    assert _probe_read(marker)["MINDNETWORK_ENFORCE"] == "1"


def test_an_injected_variable_overrides_the_inherited_one(tmp_path, monkeypatch):
    """Otherwise the orchestrator could not disarm a child in a shell that had
    already armed itself."""
    monkeypatch.setenv("MINDNETWORK_ENFORCE", "1")
    marker, script = _env_probe(tmp_path, "MINDNETWORK_ENFORCE")
    x_mod.execute("mn-t-env-3", script, root=tmp_path,
                  env={"MINDNETWORK_ENFORCE": "0"})
    assert _probe_read(marker)["MINDNETWORK_ENFORCE"] == "0"


def test_the_injected_env_is_merged_not_substituted(tmp_path, monkeypatch):
    """THE test. A bare replacement strips PATH and, on Windows, SYSTEMROOT —
    and a child with no SYSTEMROOT does not start at all, surfacing as an opaque
    `spawn-failed`. The child running successfully at all is half the proof; the
    inherited variable surviving alongside the injected one is the other half."""
    monkeypatch.setenv("MN7_INHERITED", "still-here")
    marker, script = _env_probe(tmp_path, "MN7_INHERITED", "MN7_INJECTED",
                                "PATH")
    run = x_mod.execute("mn-t-env-4", script, root=tmp_path,
                        env={"MN7_INJECTED": "new"})
    assert run.result == "ok", run.reason
    got = _probe_read(marker)
    assert got["MN7_INHERITED"] == "still-here"
    assert got["MN7_INJECTED"] == "new"
    assert got["PATH"] != "<unset>" and got["PATH"], (
        "PATH did not survive — the env was substituted, not merged")


def test_a_non_mapping_env_is_ignored_rather_than_raising(tmp_path):
    """Totality: this module maps every fault to a typed result, and a bad
    argument must not be the one thing that escapes as a crash."""
    marker, script = _env_probe(tmp_path, "MN7_PROBE")
    for junk in (["a", "b"], "MN7=1", 42, object()):
        run = x_mod.execute("mn-t-env-5", script, root=tmp_path, env=junk)
        assert run.result == "ok", f"env={junk!r} broke the run: {run.reason}"
    assert marker.exists()


def test_an_env_value_of_none_unsets_the_variable(tmp_path, monkeypatch):
    """The orchestrator needs to be able to REMOVE an inherited variable, not
    only overwrite it — otherwise there is no way to hand a child a clean slate
    for one setting."""
    monkeypatch.setenv("MN7_GONE", "present")
    marker, script = _env_probe(tmp_path, "MN7_GONE")
    x_mod.execute("mn-t-env-6", script, root=tmp_path, env={"MN7_GONE": None})
    assert _probe_read(marker)["MN7_GONE"] == "<unset>"


def test_the_probe_script_can_actually_observe_its_environment(tmp_path,
                                                              monkeypatch):
    """The canary for every assertion above: if the probe silently wrote nothing
    the tests would be reading a stale or empty marker."""
    monkeypatch.setenv("MN7_CANARY", "yes")
    marker, script = _env_probe(tmp_path, "MN7_CANARY")
    assert not marker.exists()
    x_mod.execute("mn-t-env-7", script, root=tmp_path)
    assert _probe_read(marker) == {"MN7_CANARY": "yes"}


def test_the_denylist_names_the_github_boundary_it_promises_to_keep():
    """KTD15: the research module's GitHub boundary is preserved. An explicit
    denylist is what makes the claim enforced rather than merely asserted, so
    the constants are pinned here — a tidy-up that empties them would leave
    every behavioural test below still passing for the wrong reason.

    This once carried a SECOND claim — that `pr-green` therefore stays
    agent-reported — and MN-8b severed it. The boundary below is unchanged and
    still the reason nothing here reads GitHub's CI matrix; what no longer
    follows is a conclusion about who may verify the terminal gate, whose
    evidence is the local suite's exit code. See
    `test_an_agent_cannot_attest_its_own_pr_is_green`."""
    assert "gh" in x_mod.DENIED_PROGRAMS
    assert "push" in x_mod.DENIED_GIT_SUBCOMMANDS
    assert "refused" in x_mod.RUN_RESULTS


def test_python_is_a_declared_verifier_and_the_strongest_one():
    """The executor stamps `verified_by="python"` (KTD2). If that string ever
    stops being a declared verifier the fold would record every machine
    verification as a violation, silently."""
    assert x_mod.VERIFIED_BY in ph_mod.VERIFIERS
    assert ph_mod.VERIFIERS.index(x_mod.VERIFIED_BY) == 0


def test_the_artifact_contract_is_MN0s_unchanged():
    """U6 writes into MN-0's store; it does not get its own cap, its own
    truncation marker, or its own excerpt length."""
    assert runlog.MAX_ARTIFACT_BYTES == 1024 * 1024
    assert runlog.EXCERPT_CHARS == 2000
    assert "{dropped}" in runlog.TRUNCATION_MARKER


# --- the gate: fail closed -------------------------------------------------------

def test_the_marker_mechanism_actually_records_a_run(tmp_path):
    """Positive control for every ``_ran(...) == 0`` assertion below.

    Without it those are vacuous negatives: a mock that quietly stopped writing
    markers would make every refusal test pass with the gate deleted, which is
    the exact shape of green suite this unit cannot afford."""
    marker = _marker(tmp_path)
    assert _ran(marker) == 0
    run = x_mod.execute("mn-t-1", _script("--marker", marker, "--mode", "ok"),
                        root=tmp_path, timeout_s=30)
    assert run.executed is True
    assert _ran(marker) == 1


def test_a_destructive_command_is_refused_pre_flight_and_nothing_executes(tmp_path):
    marker = _marker(tmp_path)
    script = _script("--marker", marker, "--mode", "ok") + " ; rm -rf " + str(tmp_path)
    run = x_mod.execute("mn-t-1", script, root=tmp_path)
    assert run.result == "refused"
    assert run.executed is False
    assert _ran(marker) == 0, "the gate returned a refusal AFTER running the command"
    assert run.artifact is None
    assert "rm" in run.reason


def test_a_raising_classifier_refuses_and_nothing_executes(tmp_path, monkeypatch):
    """The inversion KTD15 exists for. A crash inside the classifier means "no
    reason was found" — which an execution gate must never read as "therefore
    run it"."""
    def boom(cmd):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(gateguard, "is_destructive_command", boom)
    marker = _marker(tmp_path)
    run = x_mod.execute("mn-t-1", _script("--marker", marker, "--mode", "ok"),
                        root=tmp_path)
    assert run.result == "refused"
    assert run.executed is False
    assert _ran(marker) == 0, "a classifier crash let the command through"
    assert "classifier" in run.reason.lower()


def test_the_advisory_hook_fails_open_where_this_gate_fails_closed(monkeypatch):
    """Both call the same classifier and they disagree on purpose. Asserted
    together so a later reader cannot "simplify" the executor back to the
    advisory shape without deleting this test and reading why."""
    def boom(cmd):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(gateguard, "is_destructive_command", boom)
    # advisory hook: a crash allows the tool call
    assert enforce.evaluate_policies("Bash", {"command": "rm -rf /"}) is None
    # execution gate: the same crash refuses
    argv, reason = x_mod.gate("rm -rf /")
    assert argv is None and reason


def test_the_github_cli_is_refused_by_the_denylist_however_it_is_spelled(tmp_path):
    """A prefix check misses every one of these. The basename test catches the
    absolute path; dequoting the token catches the quoted form."""
    marker = _marker(tmp_path)
    for spelling in ("gh", "/usr/bin/gh", "gh.exe", '"gh"', "'gh'", "GH"):
        script = f"{spelling} pr checks --watch"
        argv, reason = x_mod.gate(script)
        assert argv is None, f"{spelling!r} slipped past the denylist"
        assert "gh" in reason
        run = x_mod.execute("mn-t-1", script, root=tmp_path)
        assert run.executed is False and run.result == "refused"
    assert _ran(marker) == 0


def test_the_git_publish_subcommand_is_refused_by_the_denylist():
    """Plain `git` + the publish subcommand is NOT destructive by gateguard's
    reckoning (only the --force spellings are), so this refusal is the
    denylist's alone — which is exactly why the denylist has to exist."""
    for script in ("git push origin main", "/usr/bin/git push",
                   "git -C /tmp push origin main", '"git" push'):
        argv, reason = x_mod.gate(script)
        assert argv is None, f"{script!r} slipped past the denylist"
        assert "push" in reason
    # and gateguard alone would have allowed the first one
    assert gateguard.is_destructive_command("git push origin main") is None


def test_the_denylist_is_targeted_not_a_blanket_ban_on_git():
    """A gate that refused every `git` invocation would be useless for real
    validation scripts, and the next person would delete it wholesale."""
    for script in ("git status --short", "git log --oneline -5",
                   "pytest -q", "npm test"):
        argv, reason = x_mod.gate(script)
        assert reason is None, f"{script!r} was refused: {reason}"
        assert argv and argv[0]


def test_an_empty_or_unparseable_script_is_refused_with_a_reason():
    for script in ("", "   ", None, 'pytest "unbalanced'):
        argv, reason = x_mod.gate(script)
        assert argv is None
        assert reason and reason.strip()


def test_a_run_outside_a_worktree_is_refused_rather_than_run_anywhere(
        tmp_path, monkeypatch):
    """`phases.resolve_ref` and `controller.restat` both refuse outside a
    worktree for the same reason: evidence resolved against whatever directory
    happened to be current is not this task's evidence.

    `worktree_root` is stubbed rather than the cwd moved — the suite itself runs
    inside a worktree, so `root=None` alone resolves to the real repo and this
    would assert nothing."""
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: None)
    marker = _marker(tmp_path)
    run = x_mod.execute("mn-t-1", _script("--marker", marker, "--mode", "ok"),
                        root=None)
    assert run.result == "refused"
    assert run.executed is False
    assert _ran(marker) == 0
    assert "worktree" in run.reason


# --- the subprocess contract -----------------------------------------------------

def test_the_subprocess_contract_is_argv_with_shell_false(tmp_path, monkeypatch):
    seen = {}
    real = subprocess.run

    def spy(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    run = x_mod.execute("mn-t-1", _script("--mode", "ok"), root=tmp_path,
                        timeout_s=30)
    assert run.result == "ok"
    assert isinstance(seen["argv"], list)
    kw = seen["kw"]
    assert kw["shell"] is False
    assert kw["capture_output"] is True
    assert kw["text"] is True
    assert kw["timeout"] == 30
    assert Path(kw["cwd"]) == tmp_path
    assert kw["creationflags"] == paths.NO_WINDOW


def test_shell_metacharacters_do_not_spawn_a_second_command(tmp_path):
    """`shell=False` proved behaviourally.

    ONE marker file, and its line count is the number of processes that ran:
    under a shell this script is two commands and appends two lines; as an argv
    vector it is one command whose trailing arguments happen to spell another,
    and it appends one. Two marker files would not work — argparse resolves a
    repeated ``--marker`` to the LAST one, so both spellings would write to the
    same place and the count is the honest measure anyway."""
    marker = _marker(tmp_path)
    one = _script("--marker", marker, "--mode", "args")
    run = x_mod.execute("mn-t-1", one + " ; " + one, root=tmp_path, timeout_s=30)
    assert run.executed is True
    assert _ran(marker) == 1, "shell=False was not honoured — a second command ran"
    assert ";" in run.stdout, "the separator was not passed through as an argument"
    assert run.stdout.count("--mode") == 2, "the tail did not arrive as arguments"


def test_a_windows_path_argument_survives_shlex_intact():
    """The `posix=False` regression guard at its narrowest, on the pure
    surface. POSIX-mode shlex reads every backslash as an escape and hands the
    child `C:Usershanifmindhivetests` — the command then fails for a reason
    that has nothing to do with the code under test, and the executor records a
    FALSE python-verified `result=fail`."""
    argv, reason = x_mod.gate(f"pytest {WIN_PATH}")
    assert reason is None
    assert argv == ("pytest", WIN_PATH), f"shlex mangled the path: {argv}"


def test_a_windows_path_argument_survives_intact(tmp_path):
    """The same guard end to end. Asserted on the argv the child actually
    received AND on what the child echoed back, because the command STRING
    looks correct under either mode."""
    run = x_mod.execute("mn-t-1", _script("--mode", "args", WIN_PATH),
                        root=tmp_path, timeout_s=30)
    assert run.executed is True and run.result == "ok"
    assert WIN_PATH in run.argv, f"shlex mangled the path: {run.argv}"
    assert WIN_PATH in run.stdout
    assert "C:Usershanifmindhivetests" not in run.stdout


def test_a_quoted_path_reaches_the_child_without_its_quotes(tmp_path):
    """`posix=False` keeps the quote characters INSIDE the token, so a naive
    hand-off passes `"C:\\a b\\x"` — quotes and all — to the child. Stripping
    the surrounding pair is what makes non-POSIX mode usable for real paths."""
    quoted = '"' + WIN_PATH + '"'
    run = x_mod.execute("mn-t-1", _script("--mode", "args", quoted),
                        root=tmp_path, timeout_s=30)
    assert WIN_PATH in run.argv
    assert quoted not in run.argv


# --- capture: the artifact is written on every path that ran ---------------------

def test_exit_zero_writes_an_artifact_and_returns_its_absolute_path(tmp_path):
    run = x_mod.execute("mn-t-1", _script("--mode", "ok"), root=tmp_path,
                        timeout_s=30)
    assert run.result == "ok"
    assert run.exit_code == 0
    assert run.artifact and Path(run.artifact).is_absolute()
    body = Path(run.artifact).read_text(encoding="utf-8")
    assert "mock_validator: stdout line" in body
    assert "mock_validator: stderr line" in body, "stderr was not captured"
    assert run.excerpt


def test_a_non_zero_exit_still_writes_the_artifact(tmp_path):
    """MN-0's whole point: the FAILED run is the one whose output you need."""
    run = x_mod.execute("mn-t-1", _script("--mode", "fail", "--exit-code", "7"),
                        root=tmp_path, timeout_s=30)
    assert run.result == "fail"
    assert run.exit_code == 7
    assert run.artifact and Path(run.artifact).exists()
    assert "mock_validator: stdout line" in Path(run.artifact).read_text(
        encoding="utf-8")


def test_a_timeout_is_killed_at_the_cap_and_partial_output_is_still_durable(tmp_path):
    import time
    start = time.monotonic()
    run = x_mod.execute("mn-t-1", _script("--mode", "hang"), root=tmp_path,
                        timeout_s=2)
    elapsed = time.monotonic() - start
    assert run.result == "timeout"
    assert elapsed < 30, "the child was not killed near the cap"
    assert run.artifact and Path(run.artifact).exists()
    body = Path(run.artifact).read_text(encoding="utf-8")
    assert "mock_validator: started" in body, "partial output was discarded"


def test_a_spawn_failure_is_a_typed_result_not_a_crash(tmp_path):
    """`research.run_experiment`'s shape: a spawn failure is a recorded outcome,
    never an untyped exception escaping into the caller's transition."""
    run = x_mod.execute("mn-t-1", str(MOCK) + "_does_not_exist.exe",
                        root=tmp_path, timeout_s=30)
    assert run.result == "spawn-failed"
    assert run.executed is False
    assert run.exit_code != 0
    assert run.reason


def test_output_over_the_cap_is_truncated_with_the_marker_inside_the_file(tmp_path):
    """MN-0 announces its bound in-file rather than stopping mid-sentence. The
    cap is injected so the test stays fast; the code path is the real one."""
    run = x_mod.execute("mn-t-1", _script("--mode", "spew", "--bytes", "20000"),
                        root=tmp_path, timeout_s=60, max_bytes=2000)
    assert run.artifact
    body = Path(run.artifact).read_text(encoding="utf-8")
    assert "truncated" in body and "bytes dropped" in body
    assert len(body.encode("utf-8")) < 20000


def test_a_lost_artifact_degrades_to_the_excerpt_and_never_raises(tmp_path,
                                                                  monkeypatch):
    """`runlog.finalise` answers None when the disk refuses. Evidence is lost;
    the run is not."""
    monkeypatch.setattr(runlog.RunLog, "finalise", lambda self: None)
    run = x_mod.execute("mn-t-1", _script("--mode", "ok"), root=tmp_path,
                        timeout_s=30)
    assert run.result == "ok"
    assert run.artifact is None
    assert "mock_validator: stdout line" in run.excerpt


# --- n is derived from the ledger ------------------------------------------------

def test_the_run_index_is_derived_from_the_ledger_and_starts_at_one():
    _at_audit_bug()
    assert x_mod.next_run_index(events.read_all(), TASK) == 1
    assert x_mod.run_key(TASK, 1) == f"mn-{TASK}-1"


def test_three_runs_for_one_task_write_three_distinct_artifacts(tmp_path):
    """The discriminating case. All three land at the SAME (phase, epoch), so
    `controller._resolve` collapses them into ONE folded Satisfaction — an index
    derived by counting folded satisfactions would hand runs 2 and 3 the same
    key and let run 3 overwrite run 2's evidence. Counting the raw ledger rows
    is monotonic by construction, because the ledger is append-only."""
    _at_audit_bug(script=_script("--mode", "ok"))
    keys, artifacts = [], []
    for _ in range(3):
        out = x_mod.validate(TASK, root=tmp_path, timeout_s=30)
        assert out.run.executed, out.reason
        keys.append(out.run.run_key)
        artifacts.append(out.run.artifact)
    assert len(set(keys)) == 3, f"run keys collided: {keys}"
    assert len(set(artifacts)) == 3, f"artifacts collided: {artifacts}"
    assert all(Path(a).exists() for a in artifacts)
    # and the fold really did collapse them, which is what makes this a trap
    rec = _records()[TASK]
    assert len([s for s in rec.satisfactions if s.phase == "audit-bug"]) == 1


# --- evidence emission -----------------------------------------------------------

def test_a_passing_validation_emits_a_python_verified_satisfy(tmp_path):
    _at_audit_bug(script=_script("--mode", "ok"))
    out = x_mod.validate(TASK, root=tmp_path, timeout_s=30)
    assert out.run.result == "ok"
    assert out.event is not None
    p = out.event.payload
    assert p["action"] == "satisfy"
    assert p["phase"] == "audit-bug" and p["epoch"] == 0
    assert p["verified_by"] == "python"
    assert p["evidence"]["kind"] == "command"
    assert p["evidence"]["result"] == "ok"
    assert p["evidence"]["ref"] == out.run.artifact
    # and the fold accepts it as a real satisfaction
    assert c_mod.satisfied(_records(), TASK, "audit-bug")


def test_a_failing_validation_emits_a_fail_and_still_points_at_the_artifact(tmp_path):
    _at_audit_bug(script=_script("--mode", "fail", "--exit-code", "2"))
    out = x_mod.validate(TASK, root=tmp_path, timeout_s=30)
    assert out.run.result == "fail"
    p = out.event.payload
    assert p["evidence"]["result"] == "fail"
    assert p["evidence"]["ref"] == out.run.artifact
    assert Path(p["evidence"]["ref"]).exists()
    assert not c_mod.satisfied(_records(), TASK, "audit-bug")


def test_a_timeout_is_recorded_as_a_fail_naming_the_cap(tmp_path):
    """`phases.RESULTS` has no `timeout`, and a run the machine killed is not a
    pass. It records `fail` and says why in the detail."""
    _at_audit_bug(script=_script("--mode", "hang"))
    out = x_mod.validate(TASK, root=tmp_path, timeout_s=2)
    assert out.run.result == "timeout"
    assert out.event.payload["evidence"]["result"] == "fail"
    assert "timeout" in out.event.payload["detail"].lower()


def test_verified_by_is_derived_by_this_code_path_and_never_a_parameter():
    """KTD2, structurally: no caller can hand `validate` an authority. The two
    surfaces in `phases.py` are worthless if the writer takes the field as an
    argument."""
    import inspect
    params = inspect.signature(x_mod.validate).parameters
    assert "verified_by" not in params
    assert "verified_by" not in inspect.signature(x_mod.execute).parameters


def test_the_emitted_payload_satisfies_the_writer_contract(tmp_path):
    """The CLI convention: `validate_stored` runs immediately before the
    append, so a payload the fold would only record as a violation never
    reaches the chain."""
    _at_audit_bug(script=_script("--mode", "ok"))
    out = x_mod.validate(TASK, root=tmp_path, timeout_s=30)
    ph_mod.validate_stored(out.event.payload)          # must not raise
    with pytest.raises(ValueError):
        ph_mod.validate_payload(out.event.payload)     # append surface refuses it


def test_a_refused_run_emits_no_event_at_all(tmp_path):
    """Nothing ran, so there is nothing Python verified. An event here would be
    exactly the self-attestation KTD2 exists to stop."""
    _at_audit_bug(script="rm -rf " + str(tmp_path))
    before = len(_satisfies())
    out = x_mod.validate(TASK, root=tmp_path, timeout_s=30)
    assert out.run.result == "refused"
    assert out.event is None
    assert out.reason
    assert len(_satisfies()) == before


def test_validation_is_refused_when_the_task_is_not_at_a_command_phase(tmp_path):
    """`brainstorm` takes file evidence. A command-kind satisfy there folds as a
    violation, so the executor refuses to write one — and refuses BEFORE
    spawning anything."""
    marker = _marker(tmp_path)
    _declare("doc-1", script=_script("--marker", marker, "--mode", "ok"))
    out = x_mod.validate("doc-1", root=tmp_path, timeout_s=30)
    assert out.event is None
    assert out.run.executed is False
    assert _ran(marker) == 0
    assert "command" in out.reason


def test_validation_of_an_undeclared_task_is_refused_not_orphaned(tmp_path):
    out = x_mod.validate("ghost-1", script=_script("--mode", "ok"),
                         root=tmp_path, timeout_s=30)
    assert out.event is None
    assert out.run.executed is False
    assert "ghost-1" in out.reason


def test_a_task_with_no_validation_script_is_refused(tmp_path):
    _at_audit_bug()
    out = x_mod.validate(TASK, root=tmp_path, timeout_s=30)
    assert out.event is None
    assert out.run.executed is False
    assert out.reason


# --- the refusal must not wedge the task -----------------------------------------

def test_a_refused_run_can_be_followed_by_an_agent_reported_satisfy(tmp_path):
    """KTD15's escape hatch. A classifier false positive must not deadlock a
    task, and the ledger must still show that Python did NOT verify it — so the
    refusal reason rides the satisfy VERBATIM."""
    _at_audit_bug(script="rm -rf " + str(tmp_path))
    out = x_mod.validate(TASK, root=tmp_path, timeout_s=30)
    assert out.run.result == "refused"
    reason = out.reason

    got = x_mod.agent_satisfy(TASK, reason)
    p = got.event.payload
    assert p["verified_by"] == "agent"
    assert p["detail"] == reason, "the refusal reason was not carried verbatim"
    assert p["evidence"]["detail"] == reason
    assert p["evidence"]["kind"] == "command"
    assert p["phase"] == "audit-bug" and p["epoch"] == 0
    ph_mod.validate_stored(p)                      # a legal stored event
    # it is on the ledger, and it is honestly labelled as agent-reported
    rows = [r for r in _satisfies() if r.get("verified_by") == "agent"]
    assert len(rows) == 1 and rows[0]["detail"] == reason


def test_agent_satisfy_reports_whether_the_fold_will_actually_clear_the_phase():
    """The gap this unit CANNOT close from here, surfaced rather than hidden.

    `phases.PHASE_EVIDENCE["audit-bug"]["verified_by"]` is `python`, and
    `controller._apply_satisfy` refuses any weaker claim ("a weaker claim cannot
    clear a criterion something stronger can check"). So the agent-reported
    satisfy KTD15 prescribes lands on the ledger as a VIOLATION and `audit-bug`
    stays unsatisfied — the task can still fall back, but it can never pass the
    gate. `clears` says so out loud so U7's CLI can warn instead of printing a
    success line over a no-op.

    Closing it needs a change in `phases.py` or `controller.py` — a fallback
    authority for a phase whose python verifier refused — which is outside this
    unit's file scope."""
    _at_audit_bug()
    got = x_mod.agent_satisfy(TASK, "gate refused: policy false positive")
    assert got.event is not None
    assert got.clears is False
    assert got.note
    rec = _records()[TASK]
    assert not c_mod.satisfied(_records(), TASK, "audit-bug")
    assert any("weaker claim" in v for v in rec.violations)


# --- MN-8b: the terminal gate is machine-verified ----------------------------------

def _at_pr_green(task_id=TASK, script=""):
    """Drive *task_id* to `pr-green`, the terminal gate.

    Longer than `_at_audit_bug` because the bypass only skips the METHOD
    phases — every audit between `execution` and the gate is a real move, and
    each is satisfied at the authority its own descriptor names so the walk
    never leans on the rule under test."""
    _declare(task_id, script=script, triviality_bypass=True)
    walk = ("execution", "audit-bug", "audit-code", "audit-system", "pr-green")
    for phase, nxt in zip(walk, walk[1:]):
        desc = ph_mod.PHASE_EVIDENCE[phase]
        events.append_event(
            "mindnet_phase",
            {"action": "satisfy", "task_id": task_id, "phase": phase,
             "epoch": 0, "verified_by": desc["verified_by"],
             "evidence": {"kind": desc["kind"],
                          "ref": ("" if desc["kind"] == "event"
                                  else f"runs/mn-{task_id}-setup/out.txt"),
                          "result": "ok"}},
            tags=["mindnet", "phase"])
        events.append_event(
            "mindnet_phase",
            {"action": "enter", "task_id": task_id, "phase": nxt,
             "verified_by": "python"},
            tags=["mindnet", "phase"])
    assert _records()[task_id].phase == "pr-green", \
        "setup failed to reach the terminal gate"
    return task_id


def test_an_agent_cannot_attest_its_own_pr_is_green(tmp_path):
    """MN-8b. `pr-green` names `python`, so the escape hatch no longer clears
    the terminal gate — and `clears` is what stops the CLI printing a success
    line over a no-op. `audit-bug` was the only phase in this set before; the
    module docstring says so and now names both."""
    _at_pr_green()
    got = x_mod.agent_satisfy(TASK, "gate refused: policy false positive")
    assert got.event is not None, "the attestation must still reach the ledger"
    assert got.clears is False
    assert got.note
    assert got.event.payload["verified_by"] == "agent"
    assert not c_mod.satisfied(_records(), TASK, "pr-green")
    assert any("weaker claim" in v for v in _records()[TASK].violations)


def test_a_real_run_clears_the_terminal_gate_and_completes_the_task(tmp_path):
    """THE anti-wedge proof, and the one test that makes MN-8b safe rather than
    merely strict.

    Refusing the agent is only defensible if something else can still clear the
    gate. A real subprocess exits 0, `validate` stamps `python` off that exit
    code, and the task completes — including the SECOND gate `can_complete`
    carries, the task-axis evidence tuple that only a validation run records.
    Without this, `pr-green` would be a terminal phase nothing on earth could
    satisfy, which is a bricked pipeline dressed up as a security fix."""
    _at_pr_green(script=_script("--mode", "ok"))
    out = x_mod.validate(TASK, root=tmp_path, timeout_s=30)
    assert out.run.result == "ok"
    p = out.event.payload
    assert p["phase"] == "pr-green"
    assert p["verified_by"] == "python"
    assert p["evidence"]["result"] == "ok"
    assert c_mod.satisfied(_records(), TASK, "pr-green")

    graph = t_mod.fold(events.read_all())
    assert graph[TASK].evidence, "the run recorded no task-axis evidence"
    assert t_mod.can_complete(
        graph, TASK, c_mod.satisfaction_predicate(_records())) is None


def test_a_failing_run_does_not_clear_the_terminal_gate(tmp_path):
    """A red suite reported honestly is not a green PR. Without this the test
    above would pass against a gate that accepts any `python`-stamped row
    regardless of what the run actually did."""
    _at_pr_green(script=_script("--mode", "fail", "--exit-code", "2"))
    out = x_mod.validate(TASK, root=tmp_path, timeout_s=30)
    assert out.run.result == "fail"
    assert out.event.payload["evidence"]["result"] == "fail"
    assert not c_mod.satisfied(_records(), TASK, "pr-green")


def test_a_refused_agent_attestation_still_leaves_fall_back_open(tmp_path):
    """The second live exit. KTD15 says a refusal must not wedge a task; MN-8b
    narrows the escape hatch at this one gate, so the remaining way back has to
    be shown working rather than assumed."""
    _at_pr_green()
    x_mod.agent_satisfy(TASK, "gate refused: policy false positive")
    events.append_event(
        "mindnet_phase",
        {"action": "fall_back", "task_id": TASK, "phase": "execution",
         "detail": "pr-green: suite red", "verified_by": "python"},
        tags=["mindnet", "phase"])
    rec = _records()[TASK]
    assert rec.phase == "execution", "the task could not fall back"
    assert rec.epoch == 1, "a fall_back must open a new epoch"


def test_agent_satisfy_requires_a_reason():
    _at_audit_bug()
    for empty in ("", "   ", None):
        with pytest.raises(ValueError):
            x_mod.agent_satisfy(TASK, empty)


def test_agent_satisfy_never_takes_the_python_authority():
    import inspect
    assert "verified_by" not in inspect.signature(x_mod.agent_satisfy).parameters


# --- source guards ---------------------------------------------------------------

def _source():
    return Path(x_mod.__file__.replace(".pyc", ".py")).read_text(encoding="utf-8")


def test_the_module_never_invokes_the_github_cli_or_publishes(tmp_path):
    """Mirrors `test_research.py`'s clause (d). The executor is the one place
    here that spawns a process, so the boundary the research module keeps by
    tool policy this module has to keep by construction."""
    src = _source()
    for forbidden in ("git push", "gh pr", "gh repo", "gh api", "gh auth"):
        assert forbidden not in src, f"forbidden invocation: {forbidden!r}"


def test_the_module_never_uses_a_shell():
    src = _source()
    assert "shell=True" not in src
    assert "os.system" not in src
    assert "shlex.split" in src and "posix=False" in src


def test_the_module_adds_no_dependency():
    """stdlib only — `pyproject.toml`'s `dependencies` stays `[]`."""
    src = _source()
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("import ") or s.startswith("from "):
            head = s.split()[1].split(".")[0]
            assert head in {"__future__", "os", "re", "shlex", "subprocess",
                            "time", "dataclasses", "pathlib", "typing"} \
                or s.startswith("from .") or s.startswith("from ..") \
                or head == "mindhive", s


# --- the Windows Store interpreter stub ---------------------------------------
# `paths.resolve_interpreter`: a bare `python` that PATH would hand to the
# Store's App Execution Alias never runs a line -- it prints an install prompt
# and exits 49, which is not ENOENT -- so the executor swaps it for the
# interpreter running this process and writes the swap into the artifact.

WINDOWS_APPS = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python.exe"


def test_a_bare_python_that_path_hands_to_the_store_alias_is_swapped():
    argv, note = paths.resolve_interpreter(("python", "x.py"),
                                           which=lambda n: WINDOWS_APPS,
                                           executable="/venv/bin/python")
    assert argv == ("/venv/bin/python", "x.py")
    assert "App Execution Alias" in note
    assert "/venv/bin/python" in note


def test_a_bare_python_that_resolves_to_nothing_is_swapped_too():
    argv, note = paths.resolve_interpreter(["python3", "-c", "pass"],
                                           which=lambda n: None,
                                           executable="/venv/bin/python")
    assert argv == ("/venv/bin/python", "-c", "pass")
    assert "nothing on PATH" in note


def test_a_real_interpreter_on_path_is_left_alone():
    argv, note = paths.resolve_interpreter(("python", "x.py"),
                                           which=lambda n: "/usr/bin/python",
                                           executable="/venv/bin/python")
    assert argv == ("python", "x.py")
    assert note == ""


@pytest.mark.parametrize("head", [r"C:\py\python.exe", "/usr/bin/python3",
                                  "pytest", "git"])
def test_a_path_or_another_program_is_never_touched(head):
    """A path is the author's explicit choice; a non-interpreter is not the
    alias problem at all, even when `which` finds nothing."""
    argv, note = paths.resolve_interpreter((head, "x"), which=lambda n: None,
                                           executable="/venv/bin/python")
    assert argv == (head, "x")
    assert note == ""


def test_resolution_is_total_on_an_empty_argv_and_a_raising_which():
    assert paths.resolve_interpreter(()) == ((), "")
    argv, note = paths.resolve_interpreter(
        ("python", "x"), which=lambda n: (_ for _ in ()).throw(OSError("x")),
        executable="/venv/bin/python")
    assert argv == ("/venv/bin/python", "x") and "nothing on PATH" in note


def test_the_executor_runs_this_interpreter_for_a_bare_python_and_records_the_swap(
        tmp_path, monkeypatch):
    import shutil

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: (WINDOWS_APPS
                                               if name in ("python", "python3")
                                               else real_which(name, *a, **k)))
    marker = _marker(tmp_path)
    script = " ".join(["python", str(MOCK), "--marker", str(marker),
                       "--mode", "ok"])
    run = x_mod.execute("mn-stub-1", script, root=tmp_path, timeout_s=30)
    assert run.result == "ok", run.reason
    assert run.executed is True
    assert _ran(marker) == 1, "the mock never ran -- the alias was not swapped"
    assert run.argv[0] == sys.executable
    assert run.artifact is not None
    text = Path(run.artifact).read_text(encoding="utf-8")
    assert "note:" in text and "App Execution Alias" in text
    assert f"argv:   {list(run.argv)}" in text, "the header shows the requested argv, not the one that ran"
