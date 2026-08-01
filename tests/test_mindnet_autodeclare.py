# tests/test_mindnet_autodeclare.py
"""MN-18 — auto-declare, and the two gates that kept the spine silent.

The phase spine, the SOP section and the Improvements section were built, tested, and
never reached. `advice.resolve_sops` and `advice.resolve_constraints` worked; nothing
injected them, because `inject.block` returns "" when the sidecar holds no phase records
— and the live store held none.

Auto-declaring a task is the obvious fix and it is not sufficient. Measurement found a
second, independent gate: **one appended event of any type marks the sidecar stale**, the
hook itself appends on every prompt, and advice rode only the fresh branch. So a declared
task would still have got the generic block forever.

These tests cover both gates. The end-to-end canary at the bottom is the one that would
have caught the original dormancy — every unit can pass while the user still sees nothing.
"""
import json

import pytest

from mindhive import events, promptgate
from mindhive.mindnet import autodeclare as ad
from mindhive.mindnet import sidecar as sc_mod
from mindhive.mindnet import tasks as t_mod


# --- fixtures -------------------------------------------------------------------

def _sidecar(records=None, advice=None, fresh=True):
    """A stand-in Sidecar carrying only what `decide` reads.

    A namespace rather than a real `Sidecar`: `decide` must depend on the two attributes
    it names and nothing else, and a hand-built object is what proves that. The
    end-to-end tests below use the REAL sidecar, so the two shapes cannot drift
    unnoticed."""
    import types
    return types.SimpleNamespace(
        state=types.SimpleNamespace(records=records if records is not None else {}),
        advice=advice or {}, fresh=fresh)


CODE_PROMPT = "buatkan fitur export CSV baru di dashboard"


# --- U1: the decision, pure ------------------------------------------------------

def test_a_code_prompt_with_an_empty_sidecar_decides_to_declare():
    got = ad.decide(CODE_PROMPT, _sidecar())
    assert got.declare is True
    assert got.task_id and got.title
    assert got.reason == ""


def test_the_same_prompt_twice_yields_the_same_id():
    """The flood guard that holds even if the active-task check is bypassed: a repeat
    prompt re-declares the SAME id, and the graph fold upserts a re-declare rather than
    creating a second task. The ledger is append-only — a duplicate cannot be cleaned up
    later, so determinism here is a design requirement, not a nicety."""
    a = ad.decide(CODE_PROMPT, _sidecar())
    b = ad.decide(CODE_PROMPT, _sidecar())
    assert a.task_id == b.task_id


def test_a_prompt_with_no_inferable_technique_refuses():
    """Silence beats a task parked at a gate nobody chose. `playbook.infer` returned None
    for 33 of 191 distinct real code prompts, so this is the common path, not an edge."""
    got = ad.decide("src/mindhive/enforce.py", _sidecar())
    assert got.declare is False
    assert "technique" in got.reason.lower()


def test_a_non_code_prompt_refuses():
    got = ad.decide("terima kasih atas bantuannya", _sidecar())
    assert got.declare is False
    assert got.reason


@pytest.mark.parametrize("prompt", ["", "   ", None, "<task-notification>x</task-notification>",
                                    "/ce-plan do the thing"])
def test_empty_and_machine_injected_prompts_refuse(prompt):
    got = ad.decide(prompt, _sidecar())
    assert got.declare is False


def test_an_active_record_refuses_and_names_reuse():
    """At most one auto-declared root is live at a time. That is what bounds writes to
    one-per-work-session instead of one-per-prompt."""
    got = ad.decide(CODE_PROMPT, _sidecar(records={"existing-task": {"phase": "plan"}}))
    assert got.declare is False
    assert "reuse" in got.reason.lower() or "active" in got.reason.lower()


@pytest.mark.parametrize("prompt", [
    "!!! ??? ...",                                  # punctuation only
    "perbaiki",                                     # one Indonesian word
    "x",                                            # single character
    "buatkan fitur " + "sangat " * 200 + "panjang",  # far over the id budget
    "BUATKAN FITUR BARU",                           # uppercase
    "fitur/dengan\\slash: dan titik.dua",           # separators that ID_RE forbids
])
def test_every_derived_id_satisfies_the_id_contract(prompt):
    """`tasks.ID_RE` is the append surface's contract. An id that violates it would be
    refused after the event was already built, which is a failure with no good recovery
    on an append-only store."""
    got = ad.decide(prompt, _sidecar())
    if got.declare:
        assert t_mod.ID_RE.match(got.task_id), f"{got.task_id!r} violates ID_RE"


def test_a_colliding_id_is_disambiguated_deterministically():
    first = ad.decide(CODE_PROMPT, _sidecar())
    taken = {first.task_id: {"phase": "plan"}}
    # The active-record check refuses on a populated sidecar, so collision handling is
    # exercised through the id helper directly — the seam that owns it.
    again = ad.derive_id(CODE_PROMPT, taken=set(taken))
    assert again and again != first.task_id
    assert again == ad.derive_id(CODE_PROMPT, taken=set(taken))
    assert t_mod.ID_RE.match(again)


def test_the_decision_never_carries_a_phase():
    """KTD2, asserted structurally. `cmd_mindnet_declare` is explicit that the phase-axis
    declare names no phase — "a caller-named entry phase is the teleport MN-2 stopped
    accepting" — so an edit that adds one must fail here rather than in a task silently
    parked past its gates."""
    got = ad.decide(CODE_PROMPT, _sidecar())
    for field in vars(got):
        assert "phase" not in field, f"the decision carries a phase field: {field}"


def test_the_decision_is_deterministic():
    a, b = ad.decide(CODE_PROMPT, _sidecar()), ad.decide(CODE_PROMPT, _sidecar())
    assert vars(a) == vars(b)


@pytest.mark.parametrize("bad", [
    None,
    "not a sidecar",
    object(),
])
def test_a_malformed_sidecar_refuses_rather_than_raising(bad):
    """It runs on the hook path, where a raise breaks the user's prompt."""
    got = ad.decide(CODE_PROMPT, bad)
    assert got.declare is False


def test_records_of_the_wrong_shape_refuse():
    import types
    for records in ("not a dict", 7, [1, 2, 3]):
        sc = types.SimpleNamespace(state=types.SimpleNamespace(records=records),
                                  advice={}, fresh=True)
        assert ad.decide(CODE_PROMPT, sc).declare is False


def test_the_title_is_a_readable_headline_not_the_whole_prompt():
    long_prompt = ("buatkan fitur export CSV di dashboard. " + "detail tambahan " * 60)
    got = ad.decide(long_prompt, _sidecar())
    assert got.declare
    assert len(got.title) <= ad.MAX_TITLE_CHARS
    assert got.title.strip() == got.title
    assert "\n" not in got.title


def test_deciding_touches_no_disk(monkeypatch):
    """Pure by contract: the sidecar is passed in, so nothing here may read the ledger or
    the projection. The hook path forbids the first and cannot afford the second."""
    def explode(*a, **kw):
        raise AssertionError("decide() read the ledger")

    monkeypatch.setattr(events, "read_all", explode)
    monkeypatch.setattr(sc_mod, "read",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("decide() read the sidecar")))
    assert ad.decide(CODE_PROMPT, _sidecar()).declare is True


# --- U2: wired into the hook, behind the opt-in ----------------------------------

def _hook(prompt, session_id="s-auto", project=None, monkeypatch=None):
    """Run the real `hive hook user_prompt_submit` in-process.

    The real entry point, not `decide` — the whole point of U2 is the wiring, and a test
    that called the decision directly would pass while the hook ignored it."""
    import io
    import sys
    from mindhive import __main__ as cli
    raw = json.dumps({"prompt": prompt, "session_id": session_id})
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    return cli.main(["hook", "user_prompt_submit"])


def _declares(project=None):
    """Every task/phase declare in the store, as `(type, task_id)` pairs."""
    out = []
    for e in events.read_all(project):
        if e.type not in ("mindnet_task", "mindnet_phase"):
            continue
        p = e.payload if isinstance(e.payload, dict) else {}
        if p.get("action") == "declare":
            out.append((e.type, p.get("task_id")))
    return out


def _arm(monkeypatch):
    monkeypatch.setenv("MINDNETWORK_ENFORCE", "1")


def test_an_unarmed_project_declares_nothing(monkeypatch, capsys):
    """Off until armed. This writes permanently to an append-only ledger on the user's
    behalf, so the default has to be silence."""
    monkeypatch.delenv("MINDNETWORK_ENFORCE", raising=False)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    capsys.readouterr()
    assert _declares() == []


def test_an_armed_project_declares_on_both_axes(monkeypatch, capsys):
    """Both events or neither: a task on one axis is the half-declared state
    `_mn_no_record` exists to complain about."""
    _arm(monkeypatch)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    capsys.readouterr()
    got = _declares()
    assert len(got) == 2, got
    kinds = {t for t, _tid in got}
    assert kinds == {"mindnet_task", "mindnet_phase"}
    ids = {tid for _t, tid in got}
    assert len(ids) == 1, f"the two axes named different tasks: {got}"


def test_the_declared_entry_phase_is_derived_not_taken_from_the_prompt(monkeypatch, capsys):
    """R6. The phase payload names no phase, so `tasks.fold` derives the entry point —
    the gate. A task that entered at the phase its prompt happened to mention would have
    teleported past every gate before it."""
    _arm(monkeypatch)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    capsys.readouterr()
    graph = t_mod.fold(events.read_all())
    assert graph, "no task was folded"
    task = next(iter(graph.values()))
    assert task.phase == t_mod.PHASES[0], (
        f"entry phase {task.phase!r} is not the gate {t_mod.PHASES[0]!r}")


def test_a_second_prompt_with_an_active_task_declares_nothing(monkeypatch, capsys):
    """One auto-declared root at a time — the bound that makes writes
    one-per-work-session instead of one-per-prompt."""
    _arm(monkeypatch)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    sc_mod.rebuild()
    _hook("perbaiki bug pada exporter", session_id="s-auto-2", monkeypatch=monkeypatch)
    capsys.readouterr()
    assert len({tid for _t, tid in _declares()}) == 1


def test_the_same_prompt_twice_declares_one_task(monkeypatch, capsys):
    """End-to-end form of the deterministic-id guard. Two declares of the SAME id is an
    upsert in the fold; two different ids would be an un-deletable duplicate."""
    _arm(monkeypatch)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    _hook(CODE_PROMPT, session_id="s-auto-2", monkeypatch=monkeypatch)
    capsys.readouterr()
    assert len({tid for _t, tid in _declares()}) == 1
    assert len(t_mod.fold(events.read_all())) == 1


def test_a_conversational_prompt_declares_nothing_even_when_armed(monkeypatch, capsys):
    _arm(monkeypatch)
    _hook("terima kasih atas bantuannya", monkeypatch=monkeypatch)
    capsys.readouterr()
    assert _declares() == []


def _seed_manifest(project=None):
    """A trained promptgate manifest, so the hook has something to print.

    Without it an unseeded project prints nothing at all — and a test asserting "the
    context survived" against empty output would pass while proving nothing."""
    skills = [{"name": "planner", "plugin": "p",
               "description": "plan break down structured multi-step task goal "
                              "requirements"}]
    stats = {"planner": {"calls": 5, "ok": 5, "fail": 0, "completion_rate": 1.0}}
    promptgate._write_manifest(
        project, promptgate.train(skills, stats, now="2026-07-14T00:00:00Z"))


def test_an_append_failure_leaves_the_printed_context_intact(monkeypatch, capsys):
    """Same contract as every other write on this path: the context is the product, and a
    bookkeeping failure must not touch it."""
    _arm(monkeypatch)
    _seed_manifest()
    from mindhive.mindnet import autodeclare as ad_mod

    def explode(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ad_mod, "declare", explode)
    # Exit code 0, not None: `cmd_hook` returns None and `cli.main` turns that into a
    # successful exit. The property that matters is that the failure did not surface.
    assert _hook(CODE_PROMPT, monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert out.strip(), "the hook printed nothing after a declare failure"
    json.loads(out)                      # still valid JSON on stdout
    assert _declares() == [], "a half-declared task survived the failure"


def test_a_corrupt_sidecar_declares_nothing(monkeypatch, capsys):
    _arm(monkeypatch)
    monkeypatch.setattr(sc_mod, "read",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("corrupt")))
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    capsys.readouterr()
    assert _declares() == []


def test_the_hook_path_never_reads_the_whole_ledger(monkeypatch, capsys):
    """`events.read_all` measures ~1.09 s on the live store and this runs on every
    prompt. The same guard the playbook path carries."""
    _arm(monkeypatch)

    def explode(*a, **kw):
        raise AssertionError("the hook read the whole ledger")

    monkeypatch.setattr(events, "read_all", explode)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    capsys.readouterr()


def test_the_hook_does_not_rebuild_the_projection(monkeypatch, capsys):
    """KTD6. `sidecar.rebuild` costs 0.32 s against a 0.38-0.46 s budget, and the next
    prompt's own write would invalidate the result anyway."""
    _arm(monkeypatch)

    def explode(*a, **kw):
        raise AssertionError("the hook rebuilt the projection")

    monkeypatch.setattr(sc_mod, "rebuild", explode)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    capsys.readouterr()


# --- U4: the projection is refreshed off the prompt path -------------------------
#
# `sidecar.rebuild` costs 0.32 s against the hook's ~0.4 s budget, so it cannot run per
# prompt — but without it the projection never returns to fresh and per-task phase direction
# never appears. The host has to be somewhere a fold is already tolerated and that runs once
# per session.
#
# `hive state` is that place: the session-start hook already runs it, it is the only command
# in that hook belonging to this repo, and 0.32 s is invisible at session start while it is
# decisive on the prompt path. Stale-gated, so a session opening against an already-fresh
# projection pays nothing.

def test_the_session_refresh_rebuilds_a_stale_projection(monkeypatch, capsys):
    _arm(monkeypatch)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)          # declares, leaving it stale
    capsys.readouterr()
    assert sc_mod.read().fresh is False, "fixture did not leave the projection stale"
    ad.refresh_projection()
    assert sc_mod.read().fresh is True


def test_the_session_refresh_leaves_a_fresh_projection_alone(monkeypatch):
    """Stale-gated: a session that opens against a fresh projection must not pay the
    fold."""
    sc_mod.rebuild()
    calls = {"n": 0}
    real = sc_mod.rebuild

    def counted(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(sc_mod, "rebuild", counted)
    ad.refresh_projection()
    assert calls["n"] == 0


def test_a_refresh_failure_is_silent(monkeypatch):
    """It runs inside a display command at session start. A projection that cannot be
    rebuilt must not take that command down."""
    monkeypatch.setattr(sc_mod, "rebuild",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    assert ad.refresh_projection() is False


def test_hive_state_refreshes_the_projection(monkeypatch, capsys):
    """The wiring, not just the helper. A test that only called `refresh_projection`
    would pass while nothing invoked it — which is how the spine went dormant in the
    first place."""
    from mindhive import __main__ as cli
    _arm(monkeypatch)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    capsys.readouterr()
    assert sc_mod.read().fresh is False
    cli.main(["state"])
    capsys.readouterr()
    assert sc_mod.read().fresh is True


def test_the_prompt_path_never_refreshes(monkeypatch, capsys):
    """The whole reason the refresh lives elsewhere."""
    _arm(monkeypatch)

    def explode(*a, **kw):
        raise AssertionError("the prompt path refreshed the projection")

    monkeypatch.setattr(ad, "refresh_projection", explode)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    capsys.readouterr()


def test_after_a_refresh_the_declared_task_steers_the_next_prompt(monkeypatch, capsys):
    """The point of the whole unit: once fresh, per-task phase direction appears — so the
    fresh branch is reachable in practice and not only in principle."""
    from mindhive.mindnet import inject as mn_inject
    _arm(monkeypatch)
    _hook(CODE_PROMPT, monkeypatch=monkeypatch)
    capsys.readouterr()
    ad.refresh_projection()
    out = mn_inject.block(sc_mod.read())
    assert "active task" in out, out[:200]


# --- U5: the canary that would have caught the dormancy --------------------------
#
# Every unit above can pass while the user still sees nothing. That is not hypothetical —
# it is exactly what happened: `advice.resolve_sops` and `advice.resolve_constraints` were
# implemented and tested, and reached no prompt for the whole life of the feature, because
# two gates upstream of them were shut. Unit tests on the resolvers were green throughout.
#
# So this walks the entire path in one test and asserts on the text the user actually
# receives. It is the same shape as MN-17's emitter guard: that one fails when a declared
# faculty has no emitter, this one fails when a resolved section has no route to the prompt.

def _seed_sop_and_incident(project=None):
    """One SOP and one prior-failure record, both matching the task's own words.

    Seeded through the real surfaces — a `sop` event and an incident markdown file — so
    what the resolvers see here is what they see in production."""
    from mindhive import paths
    events.append_event(
        "sop", {"action": "add", "sop_id": "sop-csv-export",
                "title": "csv export dashboard procedure",
                "text": "always stream the csv export dashboard endpoint, never buffer "
                        "the whole file in memory"},
        session_id="s-seed", tags=["sop"], project=project)
    d = paths.project_dir(project) / "rules" / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "csv-export-buffer.md").write_text(
        "---\nsignature: csv:export:buffer\nstatus: active\noccurrences: 2\n"
        "rule_source: auto\nflag: \nsessions: 1\n---\n\n"
        "# Incident: csv:export:buffer\n\n"
        "## Raw Signal\n- Error excerpt: `buffering the whole csv export dashboard "
        "endpoint exhausted memory`\n\n"
        "## Auto Evaluation\n- Prevention rule: stream the csv export dashboard endpoint "
        "instead of buffering it\n\n"
        "## Human Evaluation\n<!-- -->\n\n"
        "## Active Prevention Rule\n> stream the csv export dashboard endpoint instead "
        "of buffering it\n", encoding="utf-8")


def _injected(prompt, session_id, monkeypatch, capsys) -> str:
    _hook(prompt, session_id=session_id, monkeypatch=monkeypatch)
    out = capsys.readouterr().out.strip()
    if not out:
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_the_sop_and_improvements_sections_reach_the_prompt(monkeypatch, capsys):
    """The end-to-end claim of MN-18, on the text the user receives.

    arm -> prompt (declares) -> refresh -> prompt (advice is injected). Both gates open, or
    this fails."""
    _arm(monkeypatch)
    _seed_manifest()
    _seed_sop_and_incident()
    # First prompt brings the task into existence.
    _injected(CODE_PROMPT, "s-e2e-1", monkeypatch, capsys)
    assert _declares(), "no task was declared, so the spine has nothing to speak about"
    ad.refresh_projection()
    # Second prompt receives the spine's advice for that task.
    ctx = _injected("lanjutkan pekerjaan csv export dashboard endpoint",
                    "s-e2e-2", monkeypatch, capsys)
    assert "stream the csv export dashboard endpoint" in ctx, ctx[:400]
    assert "buffering the whole csv export" in ctx or "instead of buffering" in ctx, ctx[:400]


def test_an_unarmed_project_stays_silent_end_to_end(monkeypatch, capsys):
    """The opt-in is real, not decorative: with MindNetwork unarmed nothing is declared and
    the spine says nothing, however much SOP and incident material exists."""
    monkeypatch.delenv("MINDNETWORK_ENFORCE", raising=False)
    _seed_manifest()
    _seed_sop_and_incident()
    _injected(CODE_PROMPT, "s-e2e-off", monkeypatch, capsys)
    assert _declares() == []
    ad.refresh_projection()
    ctx = _injected("lanjutkan pekerjaan csv export dashboard endpoint",
                    "s-e2e-off-2", monkeypatch, capsys)
    assert "stream the csv export" not in ctx


def test_no_seeded_material_invents_no_sections(monkeypatch, capsys):
    """A declared task with nothing to advise about must not manufacture advice."""
    _arm(monkeypatch)
    _seed_manifest()
    _injected(CODE_PROMPT, "s-e2e-bare", monkeypatch, capsys)
    ad.refresh_projection()
    ctx = _injected("lanjutkan pekerjaan export", "s-e2e-bare-2", monkeypatch, capsys)
    for label in ("Standing procedures", "prior failure", "avoid:"):
        assert label not in ctx, f"invented an advice section: {ctx[:300]}"


def test_the_canary_fails_when_the_advice_call_is_removed(monkeypatch, capsys):
    """The canary's own canary. If `inject.block` stopped carrying advice, the test above
    must fail rather than pass by finding nothing — that failure mode has bitten this repo
    twice, so it is asserted rather than assumed."""
    from mindhive.mindnet import inject as mn_inject
    _arm(monkeypatch)
    _seed_manifest()
    _seed_sop_and_incident()
    _injected(CODE_PROMPT, "s-e2e-break", monkeypatch, capsys)
    ad.refresh_projection()
    monkeypatch.setattr(mn_inject, "advice_block", lambda *a, **kw: "")
    ctx = _injected("lanjutkan pekerjaan csv export dashboard endpoint",
                    "s-e2e-break-2", monkeypatch, capsys)
    assert "stream the csv export dashboard endpoint" not in ctx, (
        "advice still appeared with advice_block disabled — the canary above proves "
        "nothing")
