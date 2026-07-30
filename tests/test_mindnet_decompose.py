# tests/test_mindnet_decompose.py
"""MN-11 — a task proposes its own subtasks.

The founding prompt asked for a graph where "subtask atau task dapat bertambah
sesuai kebutuhan", with the shape spelled out: a `pembuatan graph` task breaking
into `visualisasi dari informasi`, `generate setiap kapan`, and so on. MN-2 built
the hierarchy; `declare --parent` writes one edge; nothing decided WHAT the
subtasks were.

## Every test here treats model output as hostile

Not because a model is malicious, but because `tasks.fold` DROPS an unsound edge
silently — so an unvalidated proposal does not error, it half-lands, and the
operator reads "declared 6 subtasks" with four missing. Worse, `declare` on an
existing id is an UPSERT, not an error: a hallucinated id matching a real task
would rewrite that task's title. The validation tests below are the substance of
this unit.
"""
import json

import pytest

from mindhive import events
from mindhive.mindnet import decompose as dc
from mindhive.mindnet import executor as x_mod
from mindhive.mindnet import tasks as t_mod


def _task(tid, **kw):
    return t_mod.Task(task_id=tid, title=kw.pop("title", f"task {tid}"), **kw)


def _graph(*tasks):
    return {t.task_id: t for t in tasks}


def _reply(*pairs):
    """A model reply in the documented shape, wrapped in prose like a real one."""
    body = json.dumps({"subtasks": [{"title": t, "description": d}
                                    for t, d in pairs]})
    return "Here is the breakdown.\n\n```json\n" + body + "\n```\nDone."


def _runner(text, executed=True, result="ran-unverified"):
    def fake(run_key, prompt, **kw):
        fake.prompt = prompt
        return x_mod.RunResult(result=result, executed=executed, exit_code=0,
                               excerpt=text, reason="" if executed else "no run")
    return fake


# --- the prompt ---------------------------------------------------------------

def test_the_prompt_names_the_parent_and_its_description():
    t = _task("graph", title="build the graph view",
              description="render the knowledge graph")
    p = dc.prompt_for(t)
    assert "graph" in p and "build the graph view" in p
    assert "render the knowledge graph" in p


def test_the_prompt_allows_an_empty_answer():
    """An atomic task must have a legal way to say so. Without it the model is
    pushed into inventing subtasks to look useful."""
    assert "empty list" in dc.prompt_for(_task("a"))


def test_the_prompt_does_not_ask_the_model_for_ids():
    """Ids are this repo's contract (`ID_RE`), not something to guess — a violation
    would be refused at the append surface AFTER the operator approved it."""
    assert "No ids" in dc.prompt_for(_task("a"))


# --- parsing ------------------------------------------------------------------

def test_parse_tolerates_prose_and_fences():
    pairs, why = dc.parse(_reply(("first", "d1"), ("second", "")))
    assert why == ""
    assert pairs == [("first", "d1"), ("second", "")]


def test_parse_accepts_a_deliberately_empty_list():
    pairs, why = dc.parse('{"subtasks": []}')
    assert pairs == [] and why == ""


@pytest.mark.parametrize("bad,expect", [
    ("", "no text"),
    ("no json here at all", "no json"),
    ("{not json}", "did not parse"),
    ('{"other": 1}', "subtasks"),
    ('{"subtasks": "nope"}', "not a list"),
])
def test_an_unusable_reply_says_WHY_rather_than_reading_as_atomic(bad, expect):
    """THE distinction. An unparseable reply and "this task is already atomic" are
    completely different claims, and collapsing them would silently report the
    second when the first happened."""
    pairs, why = dc.parse(bad)
    assert pairs == []
    assert expect in why


def test_parse_skips_entries_with_no_title_rather_than_failing():
    pairs, _why = dc.parse(
        '{"subtasks": [{"title": "keep"}, {"description": "x"}]}')
    assert pairs == [("keep", "")]


# --- ids ----------------------------------------------------------------------

def test_a_derived_id_is_legal_and_prefixed_by_its_parent():
    tid = dc.slugify("Visualisasi dari informasi", "graph")
    assert t_mod.ID_RE.match(tid), tid
    assert tid.startswith("graph-")


@pytest.mark.parametrize("title", ["ALL CAPS", "with  spaces", "punctuation!!!",
                                   "sim/bols and:more", "emoji here", "123"])
def test_every_derived_id_satisfies_the_repos_own_pattern(title):
    tid = dc.slugify(title, "p")
    assert t_mod.ID_RE.match(tid), (title, tid)


def test_a_colliding_id_gets_a_suffix_rather_than_upserting():
    """`declare` on an existing id is an UPSERT, not an error — a collision would
    silently rewrite a real task's title."""
    first = dc.slugify("same name", "p")
    second = dc.slugify("same name", "p", taken={first})
    assert second != first
    assert t_mod.ID_RE.match(second)


def test_a_very_long_title_still_yields_a_legal_id():
    tid = dc.slugify("x" * 300, "parent")
    assert t_mod.ID_RE.match(tid), tid
    assert len(tid) <= 64


# --- validation: the substance ------------------------------------------------

def test_a_clean_proposal_is_usable():
    props = dc.validate(_graph(_task("p")), "p",
                        [("first thing", ""), ("second thing", "")])
    assert [p.ok for p in props] == [True, True]
    assert len({p.task_id for p in props}) == 2


def test_a_proposal_colliding_with_an_existing_task_does_not_reuse_its_id():
    """The upsert trap, at the validation layer."""
    existing = dc.slugify("already here", "p")
    g = _graph(_task("p"), _task(existing, title="the REAL title"))
    props = dc.validate(g, "p", [("already here", "")])
    assert props[0].task_id != existing
    assert props[0].ok


def test_an_over_deep_parent_refuses_rather_than_landing_silently():
    """`tasks.fold` drops an over-deep edge without a word, so the refusal has to
    happen here or the operator sees a success with gaps."""
    chain, prev = {}, None
    for i in range(t_mod.MAX_DEPTH + 1):
        tid = f"d{i}"
        chain[tid] = _task(tid, parent_id=prev)
        prev = tid
    props = dc.validate(chain, prev, [("one more level", "")])
    assert props[0].ok is False
    assert props[0].reason


def test_a_missing_parent_refuses_every_proposal():
    props = dc.validate(_graph(_task("a")), "ghost", [("x", ""), ("y", "")])
    assert props and all(not p.ok for p in props)
    assert "no such parent" in props[0].reason


def test_the_proposal_count_is_capped():
    props = dc.validate(_graph(_task("p")), "p",
                        [(f"thing {i}", "") for i in range(40)])
    assert len(props) == dc.MAX_PROPOSED


def test_an_over_long_title_is_truncated_not_refused():
    props = dc.validate(_graph(_task("p")), "p", [("x" * 500, "")])
    assert props[0].ok
    assert len(props[0].title) <= t_mod.MAX_TITLE_CHARS


def test_validation_appends_nothing():
    """It is a pure decision. The ledger is append-only; a refused proposal must
    leave no trace to supersede later."""
    before = len(events.read_all())
    dc.validate(_graph(_task("p")), "p", [("a", ""), ("b", "")])
    assert len(events.read_all()) == before


# --- run ----------------------------------------------------------------------

def test_run_returns_validated_proposals_without_writing():
    before = len(events.read_all())
    out = dc.run("p", _graph(_task("p")),
                 runner=_runner(_reply(("alpha", ""), ("beta", ""))))
    assert [p.title for p in out.usable] == ["alpha", "beta"]
    assert out.declared == []
    assert len(events.read_all()) == before


def test_run_on_an_unknown_task_says_so():
    out = dc.run("ghost", _graph(_task("p")), runner=_runner(_reply()))
    assert out.reason and "ghost" in out.reason


def test_a_run_that_never_started_carries_the_agents_reason():
    """An auth failure or usage limit must not read as "already atomic" — the same
    distinction MN-8's outcome mapping exists for."""
    out = dc.run("p", _graph(_task("p")),
                 runner=_runner("", executed=False, result="refused"))
    assert out.proposals == []
    assert out.reason


def test_a_raising_runner_is_reported_not_propagated():
    def boom(*_a, **_k):
        raise OSError("spawn died")

    out = dc.run("p", _graph(_task("p")), runner=boom)
    assert "OSError" in out.reason


# --- declare ------------------------------------------------------------------

def test_declare_writes_both_axes_per_subtask():
    """A task declared on the graph axis alone ORPHANS every phase event it ever
    receives — `controller.fold` is create-only. `test_cli_mindnet` pins the same
    trap for the manual declare."""
    out = dc.declare(dc.run("p", _graph(_task("p")),
                            runner=_runner(_reply(("alpha", "why")))))
    assert out.declared

    kinds = {(e.type, e.payload.get("action")) for e in events.read_all()
             if e.payload.get("task_id") in out.declared}
    assert ("mindnet_task", "declare") in kinds
    assert ("mindnet_phase", "declare") in kinds


def test_declared_subtasks_fold_into_the_graph_under_their_parent():
    events.append_event("mindnet_task",
                        {"action": "declare", "task_id": "p", "title": "parent"},
                        tags=["mindnet"])
    out = dc.declare(dc.run("p", _graph(_task("p")),
                            runner=_runner(_reply(("alpha", "")))))
    folded = t_mod.fold(events.read_all())
    tid = out.declared[0]
    assert folded[tid].parent_id == "p"
    assert folded[tid].title == "alpha"


def test_declare_writes_nothing_for_a_refused_proposal():
    out = dc.Decomposition(task_id="p", proposals=[
        dc.Proposal(task_id="x", title="nope", ok=False, reason="refused")])
    before = len(events.read_all())
    got = dc.declare(out)
    assert got.declared == []
    assert len(events.read_all()) == before


def test_declared_subtasks_make_the_parent_a_container():
    """The consequence that ties this to MN-7: a parent with open children is
    withheld from the frontier, because completing it would contradict
    `can_complete`."""
    from mindhive.mindnet import orchestrator as orc
    assert orc.frontier(_graph(_task("p"))) == ["p"], (
        "canary: a lone parent starts runnable")

    out = dc.declare(dc.run("p", _graph(_task("p")),
                            runner=_runner(_reply(("alpha", "")))))
    child = out.declared[0]
    folded = _graph(_task("p"), _task(child, parent_id="p"))
    assert orc.frontier(folded) == [child]
