# tests/test_orchestration_authorship.py
"""The orchestration-authorship chain: spawn identity -> attribution -> the
mandatory-SOP fence.

This is ONE exploit path, and the tests live in one file because fixing any
single link leaves it open. The path, end to end:

1. `orchestrator.lintester._run_child` spawned a child with no `env=`, so the
   child inherited the OPERATOR's `MINDHIVE_SESSION` and every ledger row it
   wrote was attributed to a human.
2. `mindnet.advice.is_orchestration_session` answers by session-id prefix and
   fails open, so an id in no known family reads as human-authored -- and
   `lintester`'s own prefix was outside the family.
3. `sopgate` composes MANDATORY SOP bodies into the operator's next prompt, on
   every prompt, inside the directive fence -- and had no authorship concept at
   all, because `sop._project_rows` dropped `session_id` on the way to the
   sidecar that is the only thing `sopgate` reads.

Net effect: a prompt-injected child runs `hive sop add --mandatory` (every child
has `hive` on PATH and is instructed to log), and its procedure arrives in front
of its own operator labelled as standing procedure.

Every negative here is paired with its positive half and a canary. A test that
asserts "the row does not appear" passes just as well against a fixture that
could never have produced the row -- that vacuity is recorded in this repo more
than once, so the partition is proved capable of admitting the very row it
withheld before its silence is trusted.
"""
import pytest

from mindhive import sop, sopgate
from mindhive.mindnet import advice as adv
from mindhive.orchestrator import lintester as lint


# --- fixture builders: valid-by-default, one thing wrong per test -------------

def _sop_row(sop_id="execution-discipline", session_id="", **kw):
    """A deliverable sidecar row. Valid by default; tests vary ONE field."""
    row = {"sop_id": sop_id, "title": "Execution discipline",
           "text": "Orient first, then prepare, then loop until satisfied.",
           "version": 1, "session_id": session_id}
    row.update(kw)
    return row


class _Lane:
    """The one attribute `_run_child` reads off a lane."""

    def __init__(self, worktree_path=""):
        self.worktree_path = worktree_path


class _Entry:
    def __init__(self, task):
        self.task = task


def _result():
    return type("R", (), {"result": "ok", "executed": True,
                          "reason": "", "stderr": ""})()


# --- link 1: identity at the spawn point --------------------------------------

def test_the_lintester_mission_prefix_is_covered():
    """The spawner's prefix must be in the family the consumer recognises.

    Asserted against BOTH imported constants -- a copied literal on either side
    would keep passing after the other moved, which is the failure mode
    `test_the_orchestrators_own_prefix_is_covered` was written against for the
    `mn9-` spawner."""
    assert f"{lint.MISSION_PREFIX}-" in adv.ORCHESTRATION_PREFIXES


def test_a_lintester_child_is_spawned_with_its_own_orchestration_session(
        monkeypatch):
    """`_run_child` must pass `env=` carrying a `MINDHIVE_SESSION`.

    Two independent things are asserted because two independent things were
    broken: that an overlay is passed at all (it was not -- the child inherited
    the operator's environment), and that the id it carries is one `advice`
    recognises as orchestration (`MISSION_PREFIX` was outside the family, so
    even an overlay would have been attributed to a human).

    The recognition half goes through the real predicate rather than a string
    comparison, so it fails if either side of the contract moves."""
    from mindhive.mindnet import agent as ag_mod

    seen = {}

    def _fake_run(run_key, prompt, **kw):
        seen["run_key"] = run_key
        seen.update(kw)
        return _result()

    monkeypatch.setattr(ag_mod, "run", _fake_run)
    monkeypatch.setattr(ag_mod, "ran", lambda _r: True)

    orch = lint.LintesterOrchestrator(root=".", project="test")
    task = lint.AgentTask(task_id="t-7", description="do the thing")
    out = orch._run_child(_Entry(task), _Lane())

    assert out.ok
    env = seen.get("env")
    assert env is not None, "the child was spawned with no environment overlay"
    assert adv.is_orchestration_session(env.get("MINDHIVE_SESSION")), (
        "child session %r is attributed to a human by the very predicate "
        "that gates composition" % (env.get("MINDHIVE_SESSION"),))


def test_the_child_session_names_the_task_and_not_the_operator(monkeypatch):
    """The canary for the test above: an overlay that merely EXISTS is not the
    control. Two children must not share a segment (concurrent appends to one
    `<session_id>.jsonl` are what tore the seven corrupt rows in the live
    ledger), so the id has to carry the task, and it must not be whatever the
    operator's own session happens to be."""
    from mindhive.mindnet import agent as ag_mod

    seen = []

    def _fake_run(run_key, prompt, **kw):
        seen.append((kw.get("env") or {}).get("MINDHIVE_SESSION"))
        return _result()

    monkeypatch.setattr(ag_mod, "run", _fake_run)
    monkeypatch.setattr(ag_mod, "ran", lambda _r: True)
    monkeypatch.setenv("MINDHIVE_SESSION", "hanif-laptop")

    orch = lint.LintesterOrchestrator(root=".", project="test")
    for tid in ("t-1", "t-2"):
        orch._run_child(_Entry(lint.AgentTask(task_id=tid)), _Lane())

    assert seen[0] != seen[1], "two children were given one ledger segment"
    assert "hanif-laptop" not in seen, "a child inherited the operator's session"
    assert all(tid in s for tid, s in zip(("t-1", "t-2"), seen))


# --- link 2: the sidecar has to carry the author at all -----------------------

def test_the_mandatory_sidecar_carries_the_writing_session():
    """`sopgate` reads the sidecar and nothing else, so an author dropped in this
    projection is an author `sopgate` can never consult. This is the data
    dependency the whole partition rests on."""
    sop.record("add", "agent-written", title="Agent written",
               text="A procedure an orchestration agent added, at length. " * 2,
               session_id="mn9-t42", project="test", mandatory=True)

    rows = sop.write_sidecar(project="test")["sops"]
    row = next(r for r in rows if r["sop_id"] == "agent-written")
    assert row["session_id"] == "mn9-t42"


def test_a_row_with_no_recorded_author_is_not_agent_authored():
    """Totality, and the fail-open case. Absent authorship is unknown
    authorship, not agent authorship -- dropping every SOP written before
    session stamping would delete the tier to close a second-order hole."""
    assert sopgate.is_agent_authored({"sop_id": "x", "text": "y"}) is False
    assert sopgate.is_agent_authored(_sop_row(session_id="")) is False


# --- link 3: the partition at the mandatory fence -----------------------------

def test_an_agent_written_mandatory_sop_never_enters_the_prompt_block():
    """The negative. Its positive half and its canary are the next two tests;
    read all three or this one proves nothing."""
    block = sopgate.render([_sop_row(sop_id="injected", session_id="mn9-t1")])
    assert block == ""


def test_a_human_written_mandatory_sop_still_enters_the_prompt_block():
    """The positive half. Identical row, human author -- so the test above is
    about the author and not about the row being undeliverable."""
    block = sopgate.render([_sop_row(sop_id="injected", session_id="hanif")])
    assert "[injected]" in block
    assert "Orient first" in block


def test_the_block_can_carry_the_withheld_row_with_the_partition_disabled(
        monkeypatch):
    """The canary. The SAME row the first test asserts is absent must render
    once the predicate is forced to answer False -- proving the block was
    capable of carrying it and the partition is what removed it."""
    row = _sop_row(sop_id="injected", session_id="mn9-t1")
    assert sopgate.render([row]) == ""

    monkeypatch.setattr(sopgate.advice_mod, "is_orchestration_session",
                        lambda _s: False)
    assert "[injected]" in sopgate.render([row])


def test_every_orchestration_prefix_is_partitioned():
    """One spawner outside the loop is the whole hole. Driven from the imported
    constant, so a family that grows is covered without editing this test."""
    for prefix in adv.ORCHESTRATION_PREFIXES:
        row = _sop_row(sop_id="injected", session_id=prefix + "t9")
        assert sopgate.render([row]) == "", prefix + " was composed"


def test_a_withheld_row_does_not_spend_the_prompt_budget(monkeypatch):
    """The drop happens before the budget is spent. Otherwise a withheld row
    could push a human-authored procedure over `PROMPT_BUDGET` and degrade it to
    a pointer -- silent damage from a row that was never delivered."""
    monkeypatch.setattr(sopgate, "PROMPT_BUDGET", 120)
    rows = [_sop_row(sop_id="injected", session_id="mn9-t1", text="A" * 100),
            _sop_row(sop_id="human", session_id="hanif", text="H" * 100)]

    plan = sopgate.plan(rows)
    assert [r["sop_id"] for r in plan] == ["human"]
    assert plan[0]["delivered"] == "body", "a withheld row consumed the budget"


def test_the_probe_counts_what_the_partition_withheld():
    """Exclusion is the control; the count is what keeps it from being silent --
    the same rule `gated` already follows in `brief.Stats`."""
    measured = sopgate.probe(rows=[
        _sop_row(sop_id="injected", session_id="mn9-t1"),
        _sop_row(sop_id="human", session_id="hanif"),
    ])
    assert measured["withheld"] == 1
    assert measured["mandatory"] == 1
    assert [r["sop_id"] for r in measured["sops"]] == ["human"]


def test_a_malformed_row_is_not_reported_as_withheld():
    """A row with no body is junk on either side of the partition. Counting it
    as withheld would report a malformed sidecar as an attempted injection."""
    measured = sopgate.probe(rows=[
        {"sop_id": "empty", "text": "", "session_id": "mn9-t1"},
        _sop_row(sop_id="human", session_id="hanif"),
    ])
    assert measured["withheld"] == 0
    assert measured["mandatory"] == 1


@pytest.mark.parametrize("row", [
    None, "", 0, [], {}, {"session_id": None}, {"session_id": 17},
    {"session_id": ["mn9-t1"]},
])
def test_the_partition_is_total_over_junk(row):
    """Never raises: this runs inside a UserPromptSubmit hook, where an
    exception is a user-visible outage on every prompt."""
    assert sopgate.is_agent_authored(row) in (True, False)


def test_the_partition_survives_a_raising_predicate(monkeypatch):
    """Fail-open on an error, not fail-shut: a broken predicate must not delete
    the operator's standing procedures."""
    def _boom(_s):
        raise RuntimeError("predicate down")

    monkeypatch.setattr(sopgate.advice_mod, "is_orchestration_session", _boom)
    assert "[injected]" in sopgate.render(
        [_sop_row(sop_id="injected", session_id="mn9-t1")])


# --- the chain, through the real write path -----------------------------------

def test_an_agent_written_sop_stays_out_of_the_block_end_to_end():
    """Link 2 and link 3 together through `sop.record` -> `write_sidecar` ->
    `sopgate.render`, with no hand-built row anywhere: the shape a spawned agent
    running `hive sop add --mandatory` actually produces.

    The `lintester` prefix specifically, so this fails if link 1's family entry
    is reverted."""
    body = "A procedure an orchestration agent added, at length. " * 2
    sop.record("add", "human-rule", title="Human rule", text=body,
               session_id="hanif-laptop", project="test", mandatory=True)
    sop.record("add", "agent-rule", title="Agent rule", text=body,
               session_id=lint.MISSION_PREFIX + "-t42", project="test",
               mandatory=True)

    rows = sop.write_sidecar(project="test")["sops"]
    assert {r["sop_id"] for r in rows} == {"human-rule", "agent-rule"}, (
        "the registry itself must keep the row -- exclusion is at composition")

    block = sopgate.render(rows)
    assert "[human-rule]" in block
    assert "[agent-rule]" not in block


# --- observability: an attempt must be visible after the fact -----------------

def _built(**stats):
    from mindhive.mindnet import brief as brief_mod
    return type("B", (), {"text": "", "stats": brief_mod.Stats(**stats)})()


def _brief_rows():
    from mindhive import events as ev_mod
    return [e for e in ev_mod.read_all()
            if getattr(e, "type", None) == "mindnet_brief"]


def test_the_brief_payload_splits_the_withheld_count_by_source():
    """`authored` alone says something was withheld, not which surface it was
    aimed at -- and the two are not the same size of hole. A withheld SOP would
    have been rendered body-verbatim into an agent's brief; an incident's
    attacker-controlled field is never rendered at all. An operator triaging a
    non-zero count needs the split."""
    from mindhive.mindnet import agent as ag_mod

    ag_mod._record_brief("t-1", _built(authored=3, authored_sops=2,
                                       authored_constraints=1))

    payload = _brief_rows()[-1].payload
    assert payload["authored"] == 3
    assert payload["authored_by"] == {"sops": 2, "constraints": 1}


def test_the_split_is_carried_from_the_resolver_not_recomputed(monkeypatch):
    """`brief.build` must FORWARD `advice`'s split rather than derive one of its
    own. Two derivations of one number disagree the first time a source changes
    -- the same reason `sopgate.plan` exists rather than `probe` counting for
    itself.

    The totals are deliberately INCONSISTENT with the split (5 against 4+1 is
    consistent, but the resolver is the only place either is computed): if
    `build` recomputed instead of forwarding, it would have to invent numbers
    and this would fail."""
    from mindhive.mindnet import advice as adv_mod
    from mindhive.mindnet import brief as brief_mod
    from mindhive.mindnet import tasks as t_mod

    resolved = adv_mod.Advice(sops=("a rule",), withheld=5, withheld_sops=4,
                              withheld_constraints=1)
    # `brief.build` imports advice INSIDE the function (`from . import advice as
    # adv_mod`), so the binding is looked up on the advice module itself at call
    # time -- patching a `brief.advice` global would silently patch nothing.
    monkeypatch.setattr(adv_mod, "resolve", lambda *a, **k: resolved)

    task = t_mod.Task(task_id="t1", title="serialise the worktree add",
                      description="concurrent git worktree add collides")
    stats = brief_mod.build(task, brief_mod.WaveContext()).stats

    assert (stats.authored, stats.authored_sops, stats.authored_constraints) \
        == (5, 4, 1)


def test_a_source_that_raises_reports_zero_withheld_for_itself(monkeypatch):
    """Each source's count is initialised before its own `try`. Otherwise a
    raising `resolve_constraints` would either leak the SOP count or raise a
    NameError inside the handler that exists to keep advice total."""
    from mindhive.mindnet import advice as adv_mod

    def _boom(*_a, **_k):
        raise RuntimeError("constraints down")

    monkeypatch.setattr(adv_mod, "resolve_constraints", _boom)
    monkeypatch.setattr(adv_mod, "resolve_sops", lambda *a, **k: (("s",), 2))
    monkeypatch.setattr(adv_mod, "resolve_skills", lambda *a, **k: ())

    got = adv_mod.resolve("widget handling failure", evs=[])
    assert got.withheld_sops == 2
    assert got.withheld_constraints == 0
    assert got.withheld == 2


def test_every_brief_payload_key_is_named_in_the_data_model():
    """The drift guard, and the reason the doc was fixed in the same change-set
    as the payload: `docs/DATA_MODEL.md` is the store contract, and a payload
    key it does not name is a key no reader of the contract knows exists. This
    reads the REAL emitted row, so a key added later without a doc line fails
    here rather than going stale silently."""
    import pathlib

    from mindhive.mindnet import agent as ag_mod

    ag_mod._record_brief("t-1", _built(authored=1, authored_sops=1))
    payload = _brief_rows()[-1].payload

    doc = pathlib.Path(__file__).resolve().parents[1] / "docs" / "DATA_MODEL.md"
    text = doc.read_text(encoding="utf-8")
    start = text.index("`mindnet_brief` (")
    section = text[start:start + 2000]
    missing = [k for k in payload if "`" not in k and k not in section]
    assert not missing, f"undocumented mindnet_brief payload keys: {missing}"
