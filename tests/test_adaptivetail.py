# tests/test_adaptivetail.py — the Adaptive Tail Extension (blueprint P1c;
# BAB 6 and section 8.5; MEB CTX-01/CTX-02).
"""`adaptivetail.py`: the composer's safe budget under the loader's ceiling,
the byte and line budget left once the fixed part is measured (no floor --
plan review KRITIK 3), a tail filled newest first inside that budget with
pinned lessons never cut, near-duplicates dropped by the Dreaming Pass, and one
rollover notice that counts what was left out and says how to reach it."""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mindhive import adaptivetail as at
from mindhive import context, events, paths, projector, vectors
from mindhive import sync as sync_mod
from mindhive import worksessions as ws
from mindhive.core import dreaming


def _ts(i: int) -> str:
    return f"2026-09-10T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z"


def _d(i: int) -> dict:
    return {"ts": _ts(i), "text": f"decision {i}: keep approach {i} for subsystem {i * 7}"}


def _l(i: int) -> dict:
    return {"ts": _ts(i), "text": f"lesson {i}: watch edge case {i} in module {i * 3}"}


def _items(md: str) -> list:
    return [ln for ln in md.splitlines() if ln.startswith("- ")]


# --- 1. the constants (MEB CTX-01 verbatim) ---------------------------------------------

def test_the_composer_budget_sits_under_the_loaders_ceiling():
    assert at.MAX_LOADER_BYTES == 24500 and at.MAX_LOADER_LINES == 195
    assert at.MIN_TAIL_BYTES == 1500 and at.SAFETY_MARGIN_BYTES == 800
    assert at.DREAMING_THRESHOLD == 0.88 == dreaming.DREAMING_THRESHOLD
    assert at.dreaming_pass is dreaming.dreaming_pass
    # The loader's numbers stay `context`'s: the composer budget is below them.
    assert at.MAX_LOADER_BYTES < context.MEMORY_BYTE_BUDGET
    assert at.MAX_LOADER_LINES < context.MEMORY_LINE_BUDGET


def test_the_default_caps_are_the_digests_caps():
    assert (at.DEFAULT_LESSONS_CAP, at.DEFAULT_DECISIONS_CAP) == \
        (sync_mod.LESSONS_CAP, sync_mod.DECISIONS_CAP)


def test_the_rollover_template_is_the_meb_literal():
    assert at.ROLLOVER_TEMPLATE.format(n=7) == (
        "\n... 7 keputusan dan pelajaran dialihkan ke basis data vektor sekunder "
        "(ChromaDB).\nPencarian semantik lengkap: `hive recall --semantic \"\"`\n")


# --- 2. the budget ------------------------------------------------------------------------

def test_calculate_budget_is_the_meb_formula_without_a_floor():
    assert at.calculate_budget(650, 1800, 2200) == 24500 - 4650 - 800 == 19050
    assert at.calculate_budget(0, 0, 0) == 23700
    assert at.calculate_budget(23_400, 0, 0) == 300
    # Section 8.5's max(1500, ...) would hand 1 500 bytes to a tail with no room.
    assert at.calculate_budget(24_200, 0, 0) == 0
    assert at.calculate_budget(99_999, 1, 1) == 0


def test_calculate_budget_measures_text_lf_normalised_in_utf8():
    assert at.lf_bytes("a\r\nb") == 3
    assert at.calculate_budget("a\r\nb", "é", None) == 24500 - 3 - 2 - 800


@pytest.mark.parametrize("bad, exc", [(-1, ValueError), (True, TypeError),
                                      (1.5, TypeError), (b"x", TypeError)])
def test_calculate_budget_refuses_what_is_not_a_size(bad, exc):
    with pytest.raises(exc):
        at.calculate_budget(bad, 0, 0)


def test_calculate_line_budget_mirrors_it_for_lines():
    assert at.calculate_line_budget(0, 0, 0) == 195 - 8
    assert at.calculate_line_budget("a\r\nb\n", "c", 10) == 195 - 2 - 1 - 10 - 8
    assert at.calculate_line_budget(400, 0, 0) == 0


def test_projector_keeps_bab_6_2s_statement_of_the_budget():
    assert projector.MIN_ADAPTIVE_TAIL_BYTES == 2000
    assert projector.calculate_adaptive_tail_budget() == 24500 - 850
    assert projector.calculate_adaptive_tail_budget(24500, 650 + 1800 + 2200) == 19000
    assert projector.calculate_adaptive_tail_budget(24500, 24_000) == 0
    for fixed in range(0, 30_000, 777):
        assert projector.calculate_adaptive_tail_budget(fixed_bytes=fixed) == \
            max(0, at.calculate_budget(fixed, 0, 0) - 50)


# --- 3. the notice ------------------------------------------------------------------------

def test_the_notice_is_empty_for_nothing_and_the_template_with_vectors():
    assert at.rollover_notice(0, vectors_available=True) == ""
    assert at.rollover_notice(-3, vectors_available=False) == ""
    assert at.rollover_notice(3, vectors_available=True) == at.ROLLOVER_TEMPLATE.format(n=3)


def test_without_vectors_the_pointer_says_what_to_install(monkeypatch):
    monkeypatch.delenv("MINDHIVE_EVENT_BACKEND", raising=False)
    first, second = at.rollover_notice(2, vectors_available=False).strip("\n").split("\n")
    assert first == ("... 2 keputusan dan pelajaran dialihkan ke basis data vektor "
                     "sekunder (ChromaDB).")
    assert second == ("Pencarian semantik lengkap: `hive recall --semantic \"\"` "
                      "(memerlukan instalasi extras: pip install -e .[vectors])")


@pytest.mark.parametrize("backend", ["dual", "pg", " PG "])
def test_a_postgres_backend_also_names_the_pg_tool(monkeypatch, backend):
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", backend)
    assert at.rollover_notice(1, vectors_available=False).endswith(
        "pip install -e .[vectors]) atau: hive pg tool search_events_by_type\n")
    assert at.rollover_notice(1, vectors_available=True) == at.ROLLOVER_TEMPLATE.format(n=1)


@pytest.mark.parametrize("backend", ["file", "", "pgx"])
def test_a_file_backend_names_no_pg_tool(monkeypatch, backend):
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", backend)
    assert "hive pg tool" not in at.rollover_notice(1, vectors_available=False)


def test_an_explicit_backend_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "file")
    assert at.PG_SUFFIX in at.rollover_notice(1, vectors_available=False, backend="dual")


def test_vectors_available_none_asks_has_vectors_support(monkeypatch):
    calls = []
    monkeypatch.setattr(vectors, "has_vectors_support", lambda: calls.append(1) or False)
    assert at.EXTRAS_SUFFIX in at.rollover_notice(1)
    assert calls == [1]


def test_even_the_longest_notice_fits_inside_the_safety_margins():
    worst = at.rollover_notice(10 ** 9, vectors_available=False, backend="pg")
    assert at.lf_bytes(worst) + 1 < at.SAFETY_MARGIN_BYTES
    assert at.line_count(worst) + 1 <= at.SAFETY_MARGIN_LINES


def test_find_rollover_locates_the_notice_and_ignores_a_quoted_one():
    text = "## Key decisions\n\n- x\n\n" + at.rollover_notice(4, vectors_available=True)
    found = at.find_rollover(text)
    assert found["count"] == 4
    assert text[found["start"]:found["line_end"]].startswith("... 4 keputusan")
    assert text[found["line_end"]:found["end"]].startswith("\nPencarian semantik lengkap:")
    assert found["bytes"] == len(text[found["start"]:found["end"]].encode("utf-8"))
    quoted = "- ... 4 keputusan dan pelajaran dialihkan ke basis data vektor sekunder (ChromaDB)."
    assert at.find_rollover(quoted) is None
    assert at.find_rollover("") is None and at.find_rollover(None) is None


def test_find_rollover_passes_over_a_notice_quoted_inside_a_fence():
    """The state block is fenced and carries SOP bodies raw: a notice line quoted
    there is not the composer's, and must not make a truncation read as managed."""
    quoted = at.rollover_notice(3, vectors_available=True).lstrip("\n")
    assert at.find_rollover("```\n" + quoted + "```\n") is None
    own = "```\n" + quoted + "```\n\n## Key decisions\n\n- x\n" + \
        at.rollover_notice(9, vectors_available=True)
    assert at.find_rollover(own)["count"] == 9
    # A balanced inner fence (an SOP body with a code block) does not flip it.
    nested = "```\nbody\n```py\ncode\n```\n```\n" + at.rollover_notice(2, vectors_available=True)
    assert at.find_rollover(nested)["count"] == 2


def test_find_rollover_survives_an_unbalanced_fence_in_a_quoted_body():
    """An SOP body rides the state block raw. One ``` line it never closes made
    the fence parity odd for the rest of the file, so the composer's OWN closing
    notice was passed over and a managed truncation read as unmanaged. The
    quoted span is first fence line to last, whatever is unbalanced inside."""
    own = ("```\nstate\n```py\nan SOP body that never closes its fence\n```\n\n"
           "## Key decisions\n\n- x\n" + at.rollover_notice(7, vectors_available=True))
    assert at.find_rollover(own)["count"] == 7
    # Inside the span it is a quote, balanced or not...
    quoted = at.rollover_notice(3, vectors_available=True).lstrip("\n")
    assert at.find_rollover("```\n```py\n" + quoted + "```\n\n## Key decisions\n\n- x\n") is None
    # ...and outside it the composer's own, content after it or not: a MANAGED
    # truncation is exactly a notice the loader's cut falls after.
    assert at.find_rollover(quoted + "\n## Key decisions\n\n- x\n")["count"] == 3
    assert at.find_rollover(quoted + "\n- x\n" + at.rollover_notice(5, vectors_available=True))["count"] == 5
    # One fence line closes nothing: what follows it is treated as quoted.
    assert at.find_rollover("```\nstate never closed\n" + quoted) is None
    assert at.find_rollover(quoted + "\n```\nstate never closed\n")["count"] == 3


def test_compose_adaptive_parts_is_the_tail_in_two_pieces():
    """`sync` needs the notice apart from the body, to withhold it from a digest
    it would push past the loader cut. Joined the way `sync` joins sections, the
    two pieces are the tail, byte for byte."""
    ds = [at.TailItem("decision", "2026-01-%02dT00:00:00Z" % (k + 1), "keputusan nomor %d " % k + "x" * 60)
          for k in range(12)]
    ls = [at.TailItem("lesson", "2026-02-%02dT00:00:00Z" % (k + 1), "pelajaran nomor %d " % k + "y" * 60)
          for k in range(12)]
    for budget, caps in ((100_000, {}), (1_700, {}), (100_000, {"lessons_cap": 2, "decisions_cap": 1}), (0, {})):
        body, notice, rolled = at.compose_adaptive_parts(ds, ls, budget, vectors_available=True, **caps)
        md, n = at.compose_adaptive_tail(ds, ls, budget, vectors_available=True, **caps)
        assert "\n".join(p for p in (body, notice) if p) == md and n == rolled
        assert at.find_rollover(body) is None
        assert notice == at.rollover_notice(rolled, vectors_available=True)
    assert at.compose_adaptive_parts([], [], 100_000) == ("", "", 0)


def test_find_rollover_never_raises_on_an_absurd_count():
    rest = " keputusan dan pelajaran dialihkan ke basis data vektor sekunder (ChromaDB).\n"
    assert at.find_rollover("... " + "9" * 5000 + rest) is None      # past int()'s limit
    assert at.find_rollover("... " + "9" * 18 + rest)["count"] == 10 ** 18 - 1


def test_safe_budgets_keep_the_same_margin_under_a_lowered_cut():
    assert at.safe_budgets() == (24_500, 195) == at.safe_budgets(25_000, 200)
    assert at.safe_budgets(1_000_000, 5_000) == (24_500, 195)       # never looser
    assert at.safe_budgets(20_000, 150) == (19_500, 145)
    assert at.safe_budgets(100, 3) == (0, 0)
    assert context.MEMORY_BYTE_BUDGET - at.LOADER_MARGIN_BYTES == at.MAX_LOADER_BYTES
    assert context.MEMORY_LINE_BUDGET - at.LOADER_MARGIN_LINES == at.MAX_LOADER_LINES


# --- 4. vectors.has_vectors_support --------------------------------------------------------

def test_has_vectors_support_asks_find_spec_and_imports_nothing(monkeypatch):
    asked = []
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: asked.append(name) or object())
    before = set(sys.modules)
    assert vectors.has_vectors_support() is True
    assert asked == ["chromadb", "sentence_transformers"]
    assert not {"chromadb", "sentence_transformers"} & (set(sys.modules) - before)


def test_has_vectors_support_is_false_when_either_package_is_missing(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "sentence_transformers" else object())
    assert vectors.has_vectors_support() is False
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    assert vectors.has_vectors_support() is False


def test_has_vectors_support_is_false_when_find_spec_raises(monkeypatch):
    def broken(name, *a):
        raise ValueError(f"{name}.__spec__ is None")
    monkeypatch.setattr(importlib.util, "find_spec", broken)
    assert vectors.has_vectors_support() is False


# --- 5. composing the tail ------------------------------------------------------------------

def test_a_tail_that_fits_has_both_sections_newest_first_and_no_notice():
    md, n = at.compose_adaptive_tail([_d(0), _d(1)], [_l(2)], 20_000,
                                     vectors_available=True)
    assert n == 0 and at.find_rollover(md) is None
    assert md == ("## Lessons & caveats\n\n- [2026-09-10] " + _l(2)["text"] + "\n\n"
                  "## Key decisions\n\n- [2026-09-10] " + _d(1)["text"] + "\n"
                  "- [2026-09-10] " + _d(0)["text"] + "\n")


def test_the_tail_never_exceeds_the_bytes_it_was_given():
    ds, ls = [_d(i) for i in range(400)], [_l(i + 400) for i in range(400)]
    for budget in (1500, 2000, 5_000, 12_345, 19_000):
        md, n = at.compose_adaptive_tail(ds, ls, budget, vectors_available=False)
        assert at.lf_bytes(md) <= budget, budget
        assert n > 0 and at.find_rollover(md)["count"] == n
        assert len(_items(md)) + n == 800


def test_the_line_budget_is_honoured_as_well():
    ds, ls = [_d(i) for i in range(200)], [_l(i) for i in range(200)]
    md, n = at.compose_adaptive_tail(ds, ls, 60_000, available_lines=40,
                                     vectors_available=True)
    assert at.line_count(md) <= 40 and n == 400 - len(_items(md)) > 0


def test_the_newest_items_across_both_kinds_are_the_ones_shown():
    ds = [_d(i) for i in range(0, 100, 2)]           # even seconds
    ls = [_l(i) for i in range(1, 100, 2)]           # odd seconds
    md, n = at.compose_adaptive_tail(ds, ls, 1800, vectors_available=True)
    shown = sorted(int(x.split()[3].rstrip(":")) for x in _items(md))
    assert 10 < len(shown) < 100 and n == 100 - len(shown)
    assert shown == list(range(100 - len(shown), 100))


def test_below_the_threshold_only_the_notice_is_emitted():
    md, n = at.compose_adaptive_tail([_d(0)], [_l(1)], at.MIN_TAIL_BYTES - 1,
                                     vectors_available=True)
    assert (md, n) == (at.rollover_notice(2, vectors_available=True), 2)
    assert at.compose_adaptive_tail([], [], 0) == ("", 0)


def test_a_300_byte_budget_keeps_the_whole_digest_under_the_safe_ceiling(monkeypatch):
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "pg")     # the longest notice
    header = "h" * 23_399 + "\n"                             # 23 400 fixed bytes
    budget = at.calculate_budget(header, "", "")
    assert budget == 300
    md, n = at.compose_adaptive_tail([_d(i) for i in range(50)],
                                     [_l(i) for i in range(50)], budget,
                                     vectors_available=False)
    assert (md, n) == (at.rollover_notice(100, vectors_available=False), 100)
    assert at.lf_bytes(header + "\n" + md) <= at.MAX_LOADER_BYTES


def test_the_fill_stops_at_the_first_item_that_does_not_fit():
    big = {"ts": _ts(9), "text": "x" * 1500 + " big"}          # newest, too large
    # 1590: the two together need 1597 bytes even with no notice, and the newer
    # one alone does not fit beside the notice that rolling the other requires.
    md, n = at.compose_adaptive_tail([_d(0), big], [], 1590, vectors_available=True)
    # The older, smaller memo does not jump the queue past a newer one.
    assert (md, n) == (at.rollover_notice(2, vectors_available=True), 2)


def test_no_notice_is_charged_when_every_item_fits_without_one():
    """The notice is reserved only when something must roll: a tail that fits
    whole is shown whole, however close to the budget it sits."""
    newest = {"ts": _ts(2), "text": "a" * 1400 + " alpha"}
    older = {"ts": _ts(1), "text": "short beta memo"}
    md, n = at.compose_adaptive_tail([older, newest], [], 1500, vectors_available=True)
    assert n == 0 and at.find_rollover(md) is None
    assert newest["text"] in md and older["text"] in md and at.lf_bytes(md) <= 1500

    ds = [{"ts": _ts(i), "text": " ".join(f"w{i}x{j}" for j in range(20))} for i in range(12)]
    whole, n = at.compose_adaptive_tail(ds, [], 60_000, vectors_available=False)
    assert n == 0 and at.lf_bytes(whole) >= at.MIN_TAIL_BYTES
    # Exactly its own size, with the LONGER notice that would have been reserved.
    assert at.compose_adaptive_tail(ds, [], at.lf_bytes(whole),
                                    vectors_available=False) == (whole, 0)
    # One byte less and something must roll, so the notice is charged again.
    md, n = at.compose_adaptive_tail(ds, [], at.lf_bytes(whole) - 1, vectors_available=False)
    assert n > 0 and at.find_rollover(md)["count"] == n and at.lf_bytes(md) < at.lf_bytes(whole)


def test_pinned_lessons_are_tier_one_whatever_the_budget():
    pinned = {"ts": "2026-01-01T00:00:00Z", "text": "NEVER disturb V17 --live",
              "tags": ["pinned"]}
    md, n = at.compose_adaptive_tail([_d(i) for i in range(30)],
                                     [pinned] + [_l(i) for i in range(30)], 0,
                                     vectors_available=True)
    assert "NEVER disturb V17 --live" in md and n == 60
    assert md.startswith("## Lessons & caveats\n\n- [2026-01-01] NEVER disturb")
    md, _ = at.compose_adaptive_tail([], [_l(i) for i in range(30)] + [pinned], 3000,
                                     vectors_available=True)
    lessons = md.split("## Lessons & caveats", 1)[1]
    assert lessons.index("NEVER disturb") < lessons.index("lesson 29")


def test_pinned_lessons_count_toward_the_cap_but_the_cap_never_cuts_them():
    pins = [{"text": f"pin {w} stays", "pinned": True} for w in ("alpha", "beta", "gamma")]
    md, n = at.compose_adaptive_tail([], pins + [_l(i) for i in range(5)], 20_000,
                                     lessons_cap=2, vectors_available=True)
    assert [x for x in _items(md)] == ["- pin alpha stays", "- pin beta stays",
                                       "- pin gamma stays"]
    assert n == 5


def test_caps_bound_each_kind_and_what_they_leave_out_is_counted():
    md, n = at.compose_adaptive_tail([_d(i) for i in range(8)],
                                     [_l(i + 8) for i in range(12)], 20_000,
                                     lessons_cap=10, decisions_cap=5,
                                     vectors_available=True)
    assert md.count("] decision ") == 5 and md.count("] lesson ") == 10 and n == 5
    assert "decision 7:" in md and "decision 2:" not in md
    assert "lesson 19:" in md and "lesson 9:" not in md


def test_the_dreaming_pass_keeps_the_newer_of_two_near_duplicates():
    older = {"ts": _ts(1), "text": "keep the ledger append only and hash chained"}
    newer = {"ts": _ts(2), "text": "Keep the ledger append-only and hash chained!"}
    md, n = at.compose_adaptive_tail([older, newer], [], 20_000, vectors_available=True)
    assert n == 1 and _items(md) == ["- [2026-09-10] Keep the ledger append-only and "
                                     "hash chained!"]
    # A shown pinned lesson absorbs a later duplicate decision too.
    pin = {"ts": _ts(0), "text": "keep the ledger append only and hash chained",
           "pinned": True}
    md, n = at.compose_adaptive_tail([newer], [pin], 20_000, vectors_available=True)
    assert n == 1 and "Key decisions" not in md


def test_an_injected_similarity_replaces_the_jaccard_default():
    seen = []

    def sim(candidate, shown):
        seen.append((candidate.kind, shown.kind))
        return 0.9
    md, n = at.compose_adaptive_tail([_d(0), _d(1)], [], 20_000, similarity=sim,
                                     vectors_available=True)
    assert n == 1 and seen == [("decision", "decision")]
    md, n = at.compose_adaptive_tail([_d(0), _d(1)], [], 20_000, similarity=lambda a, b: 0.5,
                                     vectors_available=True)
    assert n == 0
    with pytest.raises(ValueError):
        at.compose_adaptive_tail([], [], 100, threshold=1.5)


def test_the_reexported_dreaming_pass_drops_jaccard_088_and_keeps_05():
    kept, dropped = at.dreaming_pass(["alpha beta gamma delta", "delta gamma beta alpha"])
    assert (kept, dropped) == (["alpha beta gamma delta"], ["delta gamma beta alpha"])
    kept, dropped = at.dreaming_pass(["a b c", "a b d"])            # Jaccard 2/4
    assert (kept, dropped) == (["a b c", "a b d"], [])
    kept, _ = at.dreaming_pass(["a", "b"], similarity=lambda x, y: 1.0)
    assert kept == ["a"]


def test_tail_item_reads_events_dicts_and_strings():
    ev = SimpleNamespace(ts="2026-09-01T00:00:00Z", payload={"text": "  two\nlines "},
                         tags=["pinned"])
    assert at.tail_item(ev, "lesson") == at.TailItem("lesson", "2026-09-01T00:00:00Z",
                                                     "two lines", True)
    legacy = {"last_seen": "2026-08-01", "active_rule": "a rule"}
    assert at.tail_item(legacy, "lesson") == at.TailItem("lesson", "2026-08-01", "a rule")
    assert at.tail_item("plain", "decision") == at.TailItem("decision", "", "plain")
    assert at.tail_item({"text": " \n "}, "decision") is None
    assert at.tail_item(SimpleNamespace(ts="", payload="raw", tags=None), "decision").text == "raw"
    # A null text is no text on either shape, never the memo "None".
    assert at.tail_item(SimpleNamespace(ts="", payload={"text": None}, tags=[]), "decision") is None
    assert at.tail_item({"text": None}, "decision") is None


# --- 6. Tier 2: the active work session --------------------------------------------------------

def _clock(iso):
    return lambda: datetime.strptime(iso, ws.TS_FORMAT).replace(tzinfo=timezone.utc)


def test_the_active_work_session_block_carries_the_newest_active_step_and_its_tags():
    ws.record_work_session_step("s-old", ws.create_work_session_step(
        "plan", "TDD_RED", False, now_fn=_clock("2026-09-10T10:00:00Z")), project="test")
    step = ws.record_work_session_step("s-new", ws.create_work_session_step(
        "execution", "TAIL_TRIM", True, initiative_reason="budget audit",
        tokens_used=9000, token_budget=12000, now_fn=_clock("2026-09-11T10:00:00Z")),
        project="test")
    block = at.active_work_session_block(events.read_all("test"), "test")
    assert block.splitlines() == [
        "## Active work session", "",
        f"- session s-new · phase execution · step 1/1 {step.step_id} (pending)",
        f"- {step.technical_action}", f"- {step.initiative_tag}", f"- {step.usage_tag}"]


def test_no_block_without_an_active_session():
    assert at.active_work_session_block([], "test") == ""
    step = ws.record_work_session_step("s-done", ws.create_work_session_step(
        "plan", "TDD_RED", False, now_fn=_clock("2026-09-10T10:00:00Z")), project="test")
    ws.record_step_status("s-done", step.step_id, "completed", project="test")
    assert at.active_work_session_block(events.read_all("test"), "test") == ""


# --- 7. the opt-in ---------------------------------------------------------------------------

def test_the_adaptive_tail_is_off_by_default_and_round_trips():
    assert context.load_adaptive_tail is at.load_adaptive_tail
    assert context.load_adaptive_tail("test") == {"enabled": False, "lessons": 10,
                                                  "decisions": 5}
    assert context.save_adaptive_tail("test", enabled=True, lessons=20) == \
        {"enabled": True, "lessons": 20, "decisions": 5}
    assert context.load_adaptive_tail("test") == {"enabled": True, "lessons": 20,
                                                  "decisions": 5}
    assert context.save_adaptive_tail("test", decisions=0)["decisions"] == 0


def test_the_opt_in_and_the_budgets_share_one_sidecar_without_erasing_each_other():
    context.save_budget(30000, project="test", memory_bytes=24000)
    context.save_adaptive_tail("test", enabled=True, decisions=8)
    context.save_budget(31000, project="test")
    assert context.load_adaptive_tail("test") == {"enabled": True, "lessons": 10,
                                                  "decisions": 8}
    assert context.load_budget("test") == 31000
    assert context.load_memory_byte_budget("test") == 24000


@pytest.mark.parametrize("raw", [
    '{"adaptive_tail": {"enabled": "yes", "lessons": -3, "decisions": true}}',
    '{"adaptive_tail": [1, 2]}', '[]', 'not json'])
def test_a_corrupt_opt_in_reads_as_the_default(raw):
    sidecar = paths.project_dir("test") / context.BUDGET_FILE
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(raw, encoding="utf-8")
    assert context.load_adaptive_tail("test") == {"enabled": False, "lessons": 10,
                                                  "decisions": 5}


@pytest.mark.parametrize("kw", [{"lessons": -1}, {"decisions": 1.5}, {"lessons": True},
                                {"enabled": "1"}])
def test_save_refuses_a_value_the_loader_would_ignore(kw):
    with pytest.raises(ValueError):
        context.save_adaptive_tail("test", **kw)


def test_parse_adaptive_tail_spec():
    assert at.parse_adaptive_tail_spec("lessons=20,decisions=8,enabled=1") == \
        {"lessons": 20, "decisions": 8, "enabled": True}
    assert at.parse_adaptive_tail_spec(" enabled = off ") == {"enabled": False}
    for bad in ("", ",", "lessons", "size=3", "lessons=-1", "lessons=x", "enabled=2",
                "lessons=1,lessons=2"):
        with pytest.raises(ValueError):
            at.parse_adaptive_tail_spec(bad)
    # The ceiling is named as a ceiling, not as "must be zero or more".
    assert at.parse_adaptive_tail_spec("lessons=999999") == {"lessons": 999999}
    with pytest.raises(ValueError, match="lessons must be at most 999999"):
        at.parse_adaptive_tail_spec("lessons=1000000")


def test_cli_set_adaptive_tail_persists_and_keeps_the_token_budget(capsys):
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["context", "--set-budget", "40000"]) == 0
    assert cli_mod.main(["context", "--set-adaptive-tail",
                         "lessons=20,decisions=8,enabled=1"]) == 0
    assert "adaptive tail enabled: lessons cap 20, decisions cap 8" in capsys.readouterr().out
    assert context.load_adaptive_tail(None) == {"enabled": True, "lessons": 20,
                                                "decisions": 8}
    assert context.load_budget(None) == 40000


def test_cli_set_adaptive_tail_with_a_budget_writes_both(capsys):
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["context", "--set-adaptive-tail", "enabled=1",
                         "--set-budget", "41000"]) == 0
    assert context.load_adaptive_tail(None)["enabled"] is True
    assert context.load_budget(None) == 41000
    assert json.loads((paths.project_dir(None) / context.BUDGET_FILE)
                      .read_text(encoding="utf-8"))["adaptive_tail"]["enabled"] is True


def test_cli_set_adaptive_tail_beside_both_budgets_stores_all_three(capsys):
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["context", "--set-adaptive-tail", "enabled=1,lessons=12",
                         "--set-budget", "42000", "--set-memory-lines", "180"]) == 0
    out = capsys.readouterr().out
    assert "adaptive tail enabled: lessons cap 12" in out and "line budget 180" in out
    assert context.load_adaptive_tail(None) == {"enabled": True, "lessons": 12,
                                                "decisions": 5}
    assert context.load_budget(None) == 42000
    assert context.load_memory_line_budget(None) == 180


def test_cli_stores_no_adaptive_tail_when_the_memory_budget_is_refused(capsys):
    """Exit 2 means nothing was written: the opt-in is saved only once the
    token-budget precondition of the flag beside it has passed."""
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["context", "--set-adaptive-tail", "enabled=1",
                         "--set-memory-lines", "150"]) == 2
    got = capsys.readouterr()
    assert "--set-memory-lines needs a token budget too" in got.err
    assert "adaptive tail" not in got.out
    assert context.load_adaptive_tail(None)["enabled"] is False
    assert not (paths.project_dir(None) / context.BUDGET_FILE).exists()


def test_cli_refuses_a_malformed_adaptive_tail_spec(capsys):
    from mindhive import __main__ as cli_mod
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["context", "--set-adaptive-tail", "lessons=-1"])
    assert exc.value.code == 2
    assert "lessons must be zero or more" in capsys.readouterr().err
    assert context.load_adaptive_tail(None)["enabled"] is False


# --- 8. review round 2: the bounds nobody had pinned ------------------------------------------

def _pin(k: int, width: int) -> dict:
    return {"ts": _ts(k), "text": "pinned %d " % k + "z" * width, "pinned": True}


def test_pinned_lessons_and_the_notice_are_the_one_overrun_and_it_is_one_notice():
    """Pinned lessons are never cut and the count of what rolled is never dropped
    here, so a tail that shows NO Tier-3 item is the pinned lessons and the
    notice as they are: over the budget by at most that one notice. The body
    alone keeps the bound, and `sync` decides about the notice."""
    pinned = [_pin(k, 280) for k in range(5)]
    pool = [{"ts": _ts(9), "text": "d" * 100}]
    body, notice, rolled = at.compose_adaptive_parts(pool, pinned, 1600,
                                                     vectors_available=False)
    md, _ = at.compose_adaptive_tail(pool, pinned, 1600, vectors_available=False)
    assert rolled == 1 and len(_items(body)) == 5 and "Key decisions" not in body
    assert at.lf_bytes(notice) < at.MIN_TAIL_BYTES <= at.lf_bytes(body) <= 1600
    assert at.lf_bytes(md) == at.lf_bytes(body) + 1 + at.lf_bytes(notice) > 1600
    # The line side: three lines of pinned lesson, a budget of four, seven written.
    body, notice, rolled = at.compose_adaptive_parts(pool, pinned[:1], 100_000,
                                                     available_lines=4,
                                                     vectors_available=False)
    md, _ = at.compose_adaptive_tail(pool, pinned[:1], 100_000, available_lines=4,
                                     vectors_available=False)
    assert rolled == 1 and at.line_count(body) == 3 <= 4 < at.line_count(md) == 7
    assert at.line_count(md) - at.line_count(body) <= at.SAFETY_MARGIN_LINES


def test_a_tail_that_shows_a_tier_three_item_keeps_the_bound_with_its_notice():
    """The other half of the contract: once one Tier-3 item is shown, the fill
    charged the notice for it, so body and notice together fit -- with pinned
    lessons in the tail, which the unpinned sweep above never had."""
    pinned = [_pin(k, 270) for k in range(5)]
    ds, ls = [_d(i) for i in range(60)], [_l(i + 60) for i in range(60)]
    alone, _, _ = at.compose_adaptive_parts([], pinned, 0)
    assert at.lf_bytes(alone) <= 1500
    seen = set()
    for budget in range(1500, 4000, 50):
        body, notice, rolled = at.compose_adaptive_parts(ds, pinned + ls, budget,
                                                         vectors_available=False)
        md, _ = at.compose_adaptive_tail(ds, pinned + ls, budget, vectors_available=False)
        shown = len(_items(body)) - len(pinned)
        assert rolled == 120 - shown > 0 and at.lf_bytes(body) <= budget, budget
        if shown:
            assert at.lf_bytes(md) <= budget, budget
        else:
            assert body == alone
            assert at.lf_bytes(md) == at.lf_bytes(alone) + 1 + at.lf_bytes(notice)
        seen.add(shown > 0)
    assert seen == {True, False}
    for lines in range(8, 30):
        body, notice, rolled = at.compose_adaptive_parts(ds, pinned + ls, 100_000,
                                                         available_lines=lines,
                                                         vectors_available=False)
        md, _ = at.compose_adaptive_tail(ds, pinned + ls, 100_000, available_lines=lines,
                                         vectors_available=False)
        shown = len(_items(body)) - len(pinned)
        assert at.line_count(body) <= lines and rolled == 120 - shown > 0, lines
        assert at.line_count(md) <= lines if shown else body == alone, lines


def test_the_composer_folds_a_pair_at_exactly_the_threshold():
    """CTX-02 is ``>= 0.88``. The composer has its own comparison (it does not
    call `dreaming_pass`), and every other fixture here scores 1.0, 0.9 or 0.5."""
    shared = [f"tok{i}" for i in range(22)]
    older = {"ts": _ts(1), "text": " ".join(shared + ["onlya", "onlyb"])}
    newer = {"ts": _ts(2), "text": " ".join(shared + ["onlyc"])}
    score = dreaming.jaccard_similarity(dreaming.tokens(older["text"]),
                                        dreaming.tokens(newer["text"]))
    assert score == at.DREAMING_THRESHOLD == 0.88          # 22 shared of a 25-token union
    md, n = at.compose_adaptive_tail([older, newer], [], 20_000, vectors_available=True)
    assert n == 1 and _items(md) == ["- [2026-09-10] " + newer["text"]]
    assert at.dreaming_pass([older["text"], newer["text"]]) == ([older["text"]],
                                                                [newer["text"]])
    # One shared token fewer is under it, and both are shown.
    under = {"ts": _ts(1), "text": " ".join(shared[:21] + ["onlya", "onlyb", "onlyd"])}
    md, n = at.compose_adaptive_tail([under, newer], [], 20_000, vectors_available=True)
    assert n == 0 and len(_items(md)) == 2
    # The same edge through an injected similarity.
    for value, rolled in ((0.88, 1), (0.8799, 0)):
        _, n = at.compose_adaptive_tail([_d(0), _d(1)], [], 20_000, vectors_available=True,
                                        similarity=lambda a, b, value=value: value)
        assert n == rolled, value


def test_the_block_shows_the_active_step_not_the_last_one():
    """"Current step" is `active_step_index`. With one step per session -- all the
    fixture above has -- the last step and the active one are the same step."""
    first = ws.record_work_session_step("s-multi", ws.create_work_session_step(
        "plan", "TDD_RED", False, now_fn=_clock("2026-09-10T10:00:00Z")), project="test")
    ws.record_step_status("s-multi", first.step_id, "active", project="test")
    second = ws.record_work_session_step("s-multi", ws.create_work_session_step(
        "execution", "TAIL_TRIM", False, now_fn=_clock("2026-09-10T11:00:00Z")),
        project="test")
    block = at.active_work_session_block(events.read_all("test"), "test")
    assert block.splitlines()[2:4] == [
        f"- session s-multi · phase plan · step 1/2 {first.step_id} (active)",
        f"- {first.technical_action}"]
    assert second.step_id not in block and second.technical_action not in block
