# tests/test_mindnet_agent.py
"""MN-8 — the agent runner: what a spawned agent actually RUNS for a task.

This closes the gap that made MN-7's orchestrator decoration. `validation_script`
is a validator, so `executor.execute` could only ever RE-CHECK a task; nothing
could take a task and do its work.

## No test here spawns a real `claude`

The seam is `claude_cli.run_headless`, and it is patched. A real spawn would need
auth, cost money, take minutes, and — the point — its OUTCOME is what this module
translates, not its content. So the tests drive the translation directly by
feeding the exact `{"exit", "stdout"}` shapes `run_headless` documents.

One test does check the un-patched path, for the one case reachable without
credentials: a missing CLI answering 127.

## The mapping is where MN-7's P0 recurs, with a much likelier trigger

A usage-limit or auth failure returns **exit 0 with `is_error` set** — the CLI ran,
the model never worked. Calling that `fail` would mark the task `blocked`, and
`tasks.ready` then removes its whole subtree from every future frontier,
permanently, with no CLI able to undo it. Hitting a weekly limit mid-orchestration
would poison the graph. `test_a_usage_limit_does_not_block_the_task` is that
regression, and it matters more than the P0 it descends from because a usage limit
is a NORMAL event, not an edge case.
"""
import json

import pytest

from mindhive import claude_cli, runlog
from mindhive.mindnet import agent as ag
from mindhive.mindnet import orchestrator as orc
from mindhive.mindnet import tasks as t_mod


# --- fixture builders ---------------------------------------------------------

def _envelope(result="done", is_error=False, status=None):
    """The `--output-format json` envelope shape `claude_cli` parses."""
    obj = {"result": result, "is_error": is_error}
    if status is not None:
        obj["api_error_status"] = status
    return json.dumps(obj)


def _spawn(monkeypatch, exit_code=0, stdout=None):
    """Patch the ONE seam, recording what it was asked to run."""
    seen = {}

    def fake(prompt, model=None, timeout_s=180, permission_mode=None, cwd=None):
        seen.update(prompt=prompt, model=model, timeout_s=timeout_s,
                    permission_mode=permission_mode, cwd=cwd)
        return {"exit": exit_code,
                "stdout": _envelope() if stdout is None else stdout}

    monkeypatch.setattr(claude_cli, "run_headless", fake)
    return seen


def _task(tid="t1", **kw):
    return t_mod.Task(task_id=tid, title=kw.pop("title", "do the thing"), **kw)


# --- the prompt ---------------------------------------------------------------

def test_the_prompt_names_the_task_it_is_for():
    p = ag.prompt_for(_task("mn8-u1", title="wire the runner"))
    assert "mn8-u1" in p
    assert "wire the runner" in p


def test_the_prompt_carries_the_description_when_there_is_one():
    p = ag.prompt_for(_task(description="the long form of the ask"))
    assert "the long form of the ask" in p


def test_the_prompt_states_what_clears_the_current_phase():
    """Read from `phases.descriptor`, never restated — invariant 10. An agent told
    the wrong exit criterion produces evidence the machine then refuses."""
    from mindhive.mindnet import phases as ph_mod
    p = ag.prompt_for(_task(), phase="brainstorm")
    where = (ph_mod.descriptor("brainstorm") or {}).get("required_dir") or ()
    assert "brainstorm" in p
    for d in where:
        assert d in p


def test_the_prompt_restates_the_language_lock():
    """MN-4 will enforce `language_context` on the agent whether or not the agent
    was told about it, and a refusal it could have avoided is a wasted run."""
    p = ag.prompt_for(_task(language_context=("python",)))
    assert "python" in p


def test_the_prompt_carries_this_projects_two_standing_rules():
    """A change-log entry is mandatory here, and committing is the operator's
    call. An agent that does not know either produces work the repo rejects."""
    p = ag.prompt_for(_task())
    assert "hive log" in p
    assert "commit" in p.lower()


def test_the_prompt_survives_a_task_with_almost_nothing_on_it():
    """Totality: the graph's own defaults are mostly empty, so the minimum task
    must still produce a usable instruction rather than raising."""
    p = ag.prompt_for(t_mod.Task(task_id="bare", title=""))
    assert "bare" in p and len(p) > 50


def test_the_prompt_is_not_built_from_a_none_task():
    assert isinstance(ag.prompt_for(None), str)


# --- the outcome mapping -----------------------------------------------------

def test_a_clean_run_is_ok_and_counts_as_executed(monkeypatch):
    _spawn(monkeypatch, 0, _envelope("did the work"))
    run = ag.run("k1", "do it")
    assert run.result == "ok"
    assert run.executed is True
    assert orc.RESULT_STATUS[run.result] == t_mod.STATUS_COMPLETED


def test_a_usage_limit_does_not_block_the_task(monkeypatch):
    """THE regression for this module.

    Exit 0 + is_error is what a weekly-limit or auth failure looks like: the CLI
    ran, the model never did. Mapping it to `fail` would mark the task `blocked`,
    and `tasks.ready` then removes its whole subtree from every future frontier —
    permanently, with no CLI able to undo it. A usage limit is a normal event, so
    this would poison graphs routinely."""
    _spawn(monkeypatch, 0,
           _envelope("You've hit your weekly limit", is_error=True, status=429))
    run = ag.run("k2", "do it")

    assert run.executed is False, "a run that never happened must not be recorded"
    assert run.result in orc.NON_EXECUTING_RESULTS
    assert run.result not in orc.RESULT_STATUS
    assert "429" in run.reason and "weekly limit" in run.reason


# The exact envelope a real `claude` spawn produced on an unauthenticated
# machine, kept verbatim rather than paraphrased. Note the EXIT CODE that came
# with it: 1, not 0.
_REAL_NOT_LOGGED_IN = (
    '{"type":"result","subtype":"success","is_error":true,'
    '"api_error_status":null,"duration_ms":516,"num_turns":1,'
    '"result":"Not logged in \\u00b7 Please run /login",'
    '"stop_reason":"stop_sequence","session_id":"ef409db3","total_cost_usd":0}')


def test_a_real_auth_failure_at_exit_1_does_not_block_the_task(monkeypatch):
    """THE regression, found by a real spawn and missed by every unit test here.

    `is_error` was being checked AFTER the exit code, so this envelope — which a
    real unauthenticated spawn produced at **exit 1** — mapped to `fail` and
    blocked the task. On an unauthenticated or rate-limited machine, every task
    the orchestrator touched would have been permanently removed from the graph.

    The unit test that was supposed to cover this used exit 0, because
    `claude_cli`'s docstring says a 401 "sets is_error even on a zero exit".
    "Even on" is not "only on"."""
    _spawn(monkeypatch, 1, _REAL_NOT_LOGGED_IN)
    run = ag.run("real", "do it")

    assert run.executed is False, (
        "an auth failure was recorded as a task failure — this blocks the task "
        "and deletes its subtree from every future frontier")
    assert run.result in orc.NON_EXECUTING_RESULTS
    assert "Not logged in" in run.reason


@pytest.mark.parametrize("code", [0, 1, 2, 143])
def test_an_is_error_envelope_never_blocks_whatever_the_exit_code(monkeypatch,
                                                                 code):
    """Generalised from the finding above: the envelope decides, not the exit
    code. Parametrized because the real CLI's exit code for a model-level failure
    is not something this module gets to assume."""
    _spawn(monkeypatch, code,
           _envelope("You've hit your weekly limit", is_error=True, status=429))
    run = ag.run("k", "p")
    assert run.executed is False, f"is_error at exit {code} blocked the task"
    assert run.result in orc.NON_EXECUTING_RESULTS


def test_a_timeout_is_still_a_timeout_when_there_is_no_envelope(monkeypatch):
    """The canary for the reordering: moving the envelope check first must not
    swallow the timeout case, which has no envelope at all."""
    _spawn(monkeypatch, 124, "")
    assert ag.run("k", "p").result == "timeout"


def test_an_unreadable_envelope_is_treated_as_never_run(monkeypatch):
    """Exit 0 with no parseable envelope means we cannot establish that anything
    happened. Recording a status on that would be asserting evidence we do not
    have."""
    _spawn(monkeypatch, 0, "not json at all")
    run = ag.run("k3", "do it")
    assert run.executed is False
    assert run.result in orc.NON_EXECUTING_RESULTS


def test_a_missing_cli_is_never_run_not_a_failure(monkeypatch):
    _spawn(monkeypatch, 127, "")
    run = ag.run("k4", "do it")
    assert run.result == "spawn-failed"
    assert run.executed is False
    assert "not installed" in run.reason


def test_a_timeout_DID_run_and_blocks(monkeypatch):
    """The one non-zero case that must block: the agent was working and was
    killed, so the tree may hold partial work an operator has to look at."""
    _spawn(monkeypatch, 124, "")
    run = ag.run("k5", "do it", timeout_s=42)
    assert run.result == "timeout"
    assert run.executed is True
    assert orc.RESULT_STATUS[run.result] == t_mod.STATUS_BLOCKED
    assert "42" in run.reason


def test_a_nonzero_exit_WITH_a_clean_envelope_is_a_real_failure(monkeypatch):
    """`fail` is narrow on purpose: the agent reported a result, that result was
    not an error, and it still exited non-zero. That is a genuine task failure and
    the only shape that earns a `blocked` row."""
    _spawn(monkeypatch, 3, _envelope("I tried and the build broke"))
    run = ag.run("k6", "do it")
    assert run.result == "fail"
    assert run.executed is True
    assert orc.RESULT_STATUS[run.result] == t_mod.STATUS_BLOCKED


def test_a_nonzero_exit_with_NO_envelope_does_not_block(monkeypatch):
    """Nothing came back, so whether the agent ran cannot be established — and an
    unprovable run must not cost the task its whole subtree. The reason says so
    rather than guessing.

    This is the direction the P0 taught: when in doubt, leave the task
    recoverable and make the operator look."""
    _spawn(monkeypatch, 3, "")
    run = ag.run("k6b", "do it")
    assert run.executed is False
    assert run.result in orc.NON_EXECUTING_RESULTS
    assert "envelope" in run.reason


def test_a_raising_spawn_is_never_run_rather_than_a_crash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("something unforeseen")

    monkeypatch.setattr(claude_cli, "run_headless", boom)
    run = ag.run("k7", "do it")
    assert run.executed is False
    assert run.result in orc.NON_EXECUTING_RESULTS
    assert "unforeseen" in run.reason


def test_every_result_this_module_emits_is_classified_by_the_orchestrator(
        monkeypatch):
    """The two modules must agree, or a run comes back with a verdict the loop
    cannot act on."""
    known = set(orc.RESULT_STATUS) | set(orc.NON_EXECUTING_RESULTS)
    for code, out in ((0, _envelope()), (0, _envelope(is_error=True, status=429)),
                      (0, "junk"), (127, ""), (124, ""), (3, "")):
        _spawn(monkeypatch, code, out)
        assert ag.run("k", "p").result in known


# --- MN-0 durability ---------------------------------------------------------

def test_the_transcript_survives_a_failed_run(monkeypatch):
    """A failed agent's explanation of WHY is the only thing that makes the
    failure actionable, so it has to outlive the failure."""
    _spawn(monkeypatch, 3, "the agent said why it could not proceed")
    run = ag.run("k8", "do it")
    assert run.artifact, "no durable artifact for a failed agent run"
    body = runlog.read("k8")
    assert "the agent said why it could not proceed" in body
    assert "do it" in body, "the prompt is not recoverable from the artifact"


def test_the_transcript_records_the_prompt_even_when_the_spawn_raises(
        monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(claude_cli, "run_headless", boom)
    ag.run("k9", "the prompt that never ran")
    assert "the prompt that never ran" in (runlog.read("k9") or "")


def test_an_unwritable_artifact_does_not_take_the_run_down(monkeypatch):
    _spawn(monkeypatch, 0)

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(runlog, "write_once", boom)
    run = ag.run("k10", "do it")
    assert run.result == "ok", "a lost log must not change the verdict"
    assert run.artifact is None


# --- what reaches the spawn --------------------------------------------------

def test_the_prompt_and_cwd_reach_the_spawn(monkeypatch):
    seen = _spawn(monkeypatch)
    ag.run("k11", "the exact prompt", root="/some/root")
    assert seen["prompt"] == "the exact prompt"
    assert seen["cwd"] == "/some/root"


def test_the_timeout_default_is_longer_than_the_validators(monkeypatch):
    """A validation command's 600s cap is sized for a check. An agent doing real
    work needs materially longer, and a cap that fires mid-edit leaves a
    half-written tree."""
    from mindhive.mindnet import executor as x_mod
    assert ag.DEFAULT_TIMEOUT_S > x_mod.DEFAULT_TIMEOUT_S
    seen = _spawn(monkeypatch)
    ag.run("k12", "p")
    assert seen["timeout_s"] == ag.DEFAULT_TIMEOUT_S


def test_no_permission_mode_is_granted_by_default(monkeypatch):
    """Authority is the operator's to grant. Defaulting to a mode that lets an
    autonomous agent edit freely would hand out broad write access as a side
    effect of running one command."""
    seen = _spawn(monkeypatch)
    ag.run("k13", "p")
    assert seen["permission_mode"] is None


def test_the_recursion_guard_is_not_bypassed():
    """`run_headless` sets MINDHIVE_HEADLESS=1 so a spawned session cannot re-fire
    this project's hooks. This module must not build its own environment and defeat
    it — asserted structurally, because the failure mode is a spawn loop.

    Docstrings are stripped first: this module's own docstring EXPLAINS the guard,
    and a raw substring check would fail on its own rationale (the same trap the
    cat board's threshold test documents)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ag))
    for node in ast.walk(tree):                    # drop every docstring
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)

    assert "MINDHIVE_HEADLESS" not in code, (
        "this module is touching the recursion guard; leave it to claude_cli")
    assert "environ" not in code, (
        "building the child's environment here would defeat the guard")

    imported = set()
    for node in ast.walk(ast.parse(inspect.getsource(ag))):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.name for a in node.names}
            imported.add((node.module or "").split(".")[0])
    assert "subprocess" not in imported, (
        "a second spawn path here would miss the .CMD shim, the stdin prompt, "
        "and the recursion guard")


# --- the shared-tree cap ------------------------------------------------------

def test_agents_are_capped_to_one_shared_working_tree():
    """Every agent runs with cwd=root, i.e. the same checkout. Two editing one
    tree concurrently corrupt each other's edits and the git index."""
    assert ag.MAX_SAFE_WORKERS == 1


def test_the_real_spawn_path_reports_a_missing_cli(tmp_path, monkeypatch):
    """The one un-patched case reachable without credentials: point the resolver
    at a name that does not exist and confirm the 127 path is real rather than
    only asserted through the fake."""
    monkeypatch.setattr(claude_cli, "resolve_exe",
                        lambda cmd="claude": "definitely-not-a-real-binary-xyz")
    run = ag.run("k14", "p", root=tmp_path)
    assert run.result == "spawn-failed"
    assert run.executed is False
