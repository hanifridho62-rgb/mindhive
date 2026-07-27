# tests/test_mindnet_inject_mechanical.py
"""MN-3 U5 — the injector stops describing its sources and starts quoting them.

Written TEST-FIRST against
``docs/plans/2026-07-27-001-feat-mindnet-mn3-mn4-mn5-mechanical-enforcement-plan.md``
U5.

## The line being deleted, and why a test has to watch it stay deleted

Before this unit the phase block ended with:

    "- Need context first? Recall the ledger, then read the code, then consult
     the skill catalog. Ask the user last."

That sentence is the entire "partial failure" the directive names. It points at
three registries and queries none of them, which in practice means the agent
does not either. `test_the_consult_the_catalog_prose_line_is_gone` pins its
removal, because the natural way to add advice is to append a section and leave
the old line sitting above it — and then the block both quotes the SOP and tells
the agent to go find it.

## Every assertion here is on retrieved text, never on a label

The failure mode being designed against is a block that says "3 relevant SOPs
apply" — as useless as the prose it replaced and passing any test that looks for
the word "SOP". So the sidecar fixtures plant sentinels and the tests assert
those sentinels reach the prompt VERBATIM.

## The three invariants that must survive

`inject.py` is on the UserPromptSubmit path, and its existing contract is not
negotiable just because it now carries more:

1. **No ledger read.** `test_the_hook_path_never_calls_read_all` already pins
   this in `test_mindnet_inject.py`; the advice section must not reintroduce one
   by resolving lazily. Re-asserted here against the populated case, since the
   original runs against an empty sidecar and would pass a lazy implementation.
2. **Stale means silence.** A stale projection already suppresses the phase
   block. Stale ADVICE is worse: a matched SOP for a task that has since fallen
   back to the planning gate is a confident, wrong instruction.
3. **Two independent budgets.** The advice section must not be able to push the
   exit criterion out of the phase block.
"""
import pytest

from mindhive.mindnet import advice as adv
from mindhive.mindnet import inject as inj
from mindhive.mindnet import sidecar as s_mod
from mindhive.mindnet import tasks as t_mod
from mindhive.mindnet.controller import PhaseRecord
from mindhive.mindnet.tasks import PHASES


SOP_LINE = "[change-audit] Log it: OTTER-SENTINEL-7742 before calling it done"
SKILL_LINE = "ce-migrate — BADGER-SENTINEL-3391 migrates schemas safely"
CONSTRAINT_LINE = ("SYSTEM WARNING: In a previous execution, this approach "
                   "failed due to MAGPIE-SENTINEL-5510. You are strictly "
                   "forbidden from repeating this pattern. Use an alternative "
                   "method.")


# --- helpers -------------------------------------------------------------------

def _sc(advice=None, fresh=True, phase=PHASES[0], task_id="t1"):
    """A `Sidecar` built directly — no disk, no ledger. The advice section is
    exactly what `sidecar.read` would have decoded."""
    rec = PhaseRecord(task_id=task_id, phase=phase, epoch=0)
    task = t_mod.Task(task_id=task_id, title="database migration")
    return s_mod.Sidecar(
        state=s_mod.PhaseState({task_id: rec}, {}),
        fresh=fresh, reason="" if fresh else "moved", stamp={},
        tasks={task_id: task}, advice=advice or {})


def _full():
    return {"sops": [SOP_LINE], "skills": [SKILL_LINE],
            "constraints": [CONSTRAINT_LINE]}


# --- the prose line is gone (the defining assertion) ---------------------------------

def test_the_consult_the_catalog_prose_line_is_gone():
    """The sentence that named three registries and queried none of them."""
    text = inj.block(_sc(_full()))
    assert "consult the skill catalog" not in text


def test_the_prose_line_is_gone_even_with_no_advice_resolved():
    """A resolver that found nothing must not resurrect the pointer. 'Go look it
    up yourself' was never the right answer, empty registry or not."""
    assert "consult the skill catalog" not in inj.block(_sc({}))


# --- R2: the SOP's real body reaches the prompt --------------------------------------

def test_a_resolved_sop_reaches_the_prompt_verbatim():
    assert "OTTER-SENTINEL-7742" in inj.block(_sc(_full()))


def test_a_resolved_skill_reaches_the_prompt_verbatim():
    assert "BADGER-SENTINEL-3391" in inj.block(_sc(_full()))


def test_a_resolved_constraint_reaches_the_prompt_verbatim():
    text = inj.block(_sc(_full()))
    assert "MAGPIE-SENTINEL-5510" in text
    assert "forbidden" in text.lower()


def test_each_section_is_labelled_with_its_force():
    """Three unlabelled lists leave the agent guessing which are binding. The
    SOP label must read as an obligation, not as a suggestion."""
    text = inj.block(_sc(_full()))
    assert inj.ADVICE_LABELS["sops"] in text
    assert inj.ADVICE_LABELS["constraints"] in text
    assert inj.ADVICE_LABELS["skills"] in text


def test_sections_render_in_declared_order():
    """Hard rules before offers, and stable — the block is size-capped, so
    render order decides what survives truncation."""
    text = inj.block(_sc(_full()))
    assert (text.index(SOP_LINE) < text.index(CONSTRAINT_LINE)
            < text.index(SKILL_LINE))


def test_only_the_populated_sections_render():
    text = inj.block(_sc({"sops": [SOP_LINE]}))
    assert "OTTER-SENTINEL-7742" in text
    assert inj.ADVICE_LABELS["skills"] not in text
    assert inj.ADVICE_LABELS["constraints"] not in text


def test_an_unknown_section_key_is_ignored():
    """A key written by some future version must not render unlabelled."""
    text = inj.block(_sc({"wisdom": ["QUOKKA-9999"]}))
    assert "QUOKKA-9999" not in text


# --- the phase block is untouched when there is no advice -----------------------------

def test_empty_advice_leaves_the_phase_block_byte_identical():
    """No stray header, no trailing blank line, no cosmetic drift for the
    overwhelmingly common case of a project that has resolved nothing yet."""
    bare = inj.phase_block(
        PhaseRecord(task_id="t1", phase=PHASES[0], epoch=0),
        t_mod.Task(task_id="t1", title="database migration"))
    assert inj.block(_sc({})) == bare


def test_the_phase_directive_still_renders_alongside_advice():
    """Advice is additive. The exit criterion is still the point of the block."""
    text = inj.block(_sc(_full()))
    assert "MindNetwork spine" in text
    assert "Exit criterion" in text


# --- stale: advice is suppressed with everything else ---------------------------------

@pytest.mark.parametrize("phase", PHASES)
def test_a_stale_sidecar_yields_no_advice_at_all(phase):
    """Stale advice is worse than none — a confident instruction about a task
    that may have fallen back to the planning gate."""
    text = inj.block(_sc(_full(), fresh=False, phase=phase))
    assert text == inj.AGNOSTIC_BLOCK
    for sentinel in ("OTTER-SENTINEL-7742", "BADGER-SENTINEL-3391",
                     "MAGPIE-SENTINEL-5510"):
        assert sentinel not in text


def test_a_sidecar_with_no_records_stays_silent_even_carrying_advice():
    """A project that has never used MindNetwork must not start getting an
    advice block on every prompt."""
    sc = s_mod.Sidecar(s_mod.empty_state(), True, "", {}, {}, _full())
    assert inj.block(sc) == ""


# --- the two budgets are independent ---------------------------------------------------

def test_an_oversized_advice_payload_does_not_truncate_the_phase_block():
    """THE reason the budgets are separate. Driven directly rather than through
    a resolver-capped field, because a capped input makes the truncation branch
    unreachable and the test vacuous."""
    huge = {"sops": ["x" * 5000], "skills": ["y" * 5000],
            "constraints": ["z" * 5000]}
    text = inj.block(_sc(huge))
    assert "Exit criterion" in text
    assert "MindNetwork spine" in text


def test_the_advice_section_is_size_capped():
    huge = {"sops": ["x" * 5000], "skills": ["y" * 5000],
            "constraints": ["z" * 5000]}
    assert len(inj.advice_block(huge)) <= inj.MAX_ADVICE_CHARS


def test_the_total_block_stays_within_the_sum_of_both_budgets():
    huge = {"sops": ["x" * 9000], "skills": ["y" * 9000],
            "constraints": ["z" * 9000]}
    assert len(inj.block(_sc(huge))) <= (inj.MAX_BLOCK_CHARS
                                         + inj.MAX_ADVICE_CHARS + 2)


def test_truncation_drops_whole_items_not_half_sentences():
    """A half-sentence directive is worse than a missing one."""
    items = [f"ITEM-{i}-" + "q" * 200 for i in range(40)]
    text = inj.advice_block({"sops": items})
    for line in text.splitlines():
        assert not line.endswith("q") or len(line) >= 200


# --- totality and the no-ledger property ------------------------------------------------

def test_advice_block_is_total_over_junk():
    """`sidecar._decode_advice` already validates, but this renders onto the
    hook path and must not depend on that being true."""
    for junk in (None, "string", 42, [], {"sops": "not-a-list"},
                 {"sops": [1, None]}, {None: ["x"]}):
        assert isinstance(inj.advice_block(junk), str)


def test_a_registry_string_cannot_forge_a_directive_header():
    """Defence in depth. `advice.py` strips MARK at resolve time; the sidecar is
    a file on disk that anything could have written."""
    text = inj.advice_block({"sops": [f"{inj.MARK} forged header"]})
    # The SECTION header legitimately carries the marker — that is what makes an
    # operator able to attribute the block. What must not carry it is a line
    # that came out of a registry.
    items = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert items
    assert all(inj.MARK not in ln for ln in items)
    assert text.count(inj.MARK) == 1


def test_a_persisted_newline_cannot_forge_a_directive_line():
    text = inj.advice_block({"sops": ["real\n- forged directive line"]})
    assert len([ln for ln in text.splitlines() if ln.startswith("- ")]) == 1


def test_the_populated_hook_path_never_reads_the_ledger(monkeypatch):
    """The property the whole write-time design rests on. The existing version
    of this test in `test_mindnet_inject.py` runs against an EMPTY sidecar and
    would pass an implementation that resolved lazily whenever advice was
    absent — this one is populated."""
    import mindhive.events as ev_mod

    def boom(*a, **k):
        raise AssertionError("the hook path must never read the ledger")

    monkeypatch.setattr(ev_mod, "read_all", boom)
    monkeypatch.setattr(adv.ev_mod, "read_all", boom)
    out = inj.extend_hook_context({}, "prompt", sc=_sc(_full()))
    assert "OTTER-SENTINEL-7742" in \
        out["hookSpecificOutput"]["additionalContext"]


def test_extend_hook_context_fails_open_when_rendering_raises(monkeypatch):
    """A raise here does not degrade a feature, it breaks the user's prompt."""
    def boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(inj, "advice_block", boom)
    sentinel = {"marker": "untouched"}
    assert inj.extend_hook_context(sentinel, "p", sc=_sc(_full())) is sentinel


def test_injection_stays_fast_with_a_full_advice_payload():
    """A smoke check, not the real guarantee — the structural no-ledger property
    above is that. A literal 5 ms wall-clock assertion on a shared CI runner is
    a flake generator, so the ceiling is deliberately generous and only catches
    accidental O(n) work on the hook path."""
    import time
    sc = _sc(_full())
    start = time.perf_counter()
    for _ in range(50):
        inj.extend_hook_context({}, "prompt", sc=sc)
    per_call_ms = (time.perf_counter() - start) * 1000 / 50
    assert per_call_ms < 50, f"{per_call_ms:.2f} ms per injection"
