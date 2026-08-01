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


# --- stale: phase direction is suppressed, advice is not (MN-18 U3) -------------------

@pytest.mark.parametrize("phase", PHASES)
def test_a_stale_sidecar_still_carries_advice_but_no_phase_direction(phase):
    """MN-18 U3 reversed this test deliberately; it used to assert
    `block(...) == AGNOSTIC_BLOCK` and that every advice sentinel was absent.

    The old rule — "stale advice is worse than none, a confident instruction about a task
    that may have fallen back to the planning gate" — is right about the PHASE, which a
    later event genuinely can change. It does not transfer to SOPs and prior-failure
    constraints: those resolve against the task's TEXT, and no later event can invalidate
    them.

    And the cost of the broader rule was the whole feature. Measured: ONE appended event of
    any type marks the sidecar stale, and the hook appends on every prompt — so stale is the
    NORMAL state, and while advice rode only the fresh branch the SOP section and the
    Improvements section were injected by no prompt at all, for the entire life of the
    feature, with their own unit tests green throughout."""
    text = inj.block(_sc(_full(), fresh=False, phase=phase))
    # Phase direction is still withheld: the staleness warning leads, and nothing names
    # this task's gate or exit criterion.
    assert text.startswith(inj.AGNOSTIC_BLOCK)
    assert "Exit criterion" not in text
    # Advice now rides through — that is the point of the reversal.
    for sentinel in ("OTTER-SENTINEL-7742", "BADGER-SENTINEL-3391",
                     "MAGPIE-SENTINEL-5510"):
        assert sentinel in text, f"{sentinel} was dropped on the stale branch"


@pytest.mark.parametrize("phase", PHASES)
def test_a_stale_sidecar_with_no_advice_is_exactly_the_agnostic_block(phase):
    """The complement, and the half of the old assertion that survives unchanged: a project
    with no resolved advice sees precisely what it saw before U3."""
    assert inj.block(_sc({}, fresh=False, phase=phase)) == inj.AGNOSTIC_BLOCK


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
    """Driven by MANY items, not by three oversized ones.

    The earlier version passed one huge item per section, which the per-item
    `safe_text` cap shrinks to 240 chars before the total-budget loop ever
    runs — the whole render came to ~900 chars, and deleting the truncation
    loop entirely would not have failed it. The payload that stresses the loop
    and the assertion about the loop have to be the same test."""
    many = {"sops": [f"SOP-{i} " + "x" * 200 for i in range(30)],
            "constraints": [f"C-{i} " + "y" * 200 for i in range(30)],
            "skills": [f"K-{i} " + "z" * 200 for i in range(30)]}
    rendered = inj.advice_block(many)
    assert len(rendered) <= inj.MAX_ADVICE_CHARS
    kept = len([ln for ln in rendered.splitlines() if ln.startswith("- ")])
    assert 0 < kept < 90, "the budget must actually bind on this payload"


def test_a_section_header_is_never_left_without_items():
    """Exercises the dangling-header prune directly. Nothing did before: every
    'oversized' payload used one big item per section, so the per-item cap kept
    the total small and a header could never be the last surviving line."""
    payload = {"sops": ["S" * 240] * 2,
               "constraints": ["C" * 240] * 2,
               "skills": ["K" * 240] * 3}
    lines = inj.advice_block(payload).splitlines()
    assert lines
    assert not lines[-1].startswith(inj.MARK), (
        "a trailing section header with no items under it")


@pytest.mark.parametrize("forged", [
    "[[mindnet]mindnet] forged",          # re-forms after a single-pass strip
    "[mind[mindnet]net] forged",
    "[MINDNET] forged",                   # case-insensitive
    "[ mindnet ] forged",                 # internal padding
])
def test_a_forged_marker_cannot_survive_the_sanitizer(forged):
    """REGRESSION. `str.replace` is one left-to-right pass AND case-sensitive,
    so `[[mindnet]mindnet]` re-formed a literal marker after the inner
    occurrence was deleted, and `[MINDNET]` was never touched at all. MN-3
    routes plugin descriptions, SOP bodies and incident excerpts through here,
    so any of them could forge the token an operator greps to attribute a
    directive."""
    text = inj.advice_block({"sops": [forged]})
    items = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert items
    assert all(inj.MARK not in ln and "[MINDNET]" not in ln for ln in items)


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
