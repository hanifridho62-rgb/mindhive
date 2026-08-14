# tests/test_replenish_liveness.py
"""Every registered lens must be able to LAND, not merely to produce candidates.

This file exists because a green suite has never once caught the failure it
guards. Five times now a lens or a condition has shipped whose best possible
score sat below every threshold it ran under, so it produced candidates the value
gate was guaranteed to refuse — dead config that costs a fold every round and can
never produce work. Each time, every unit test for that lens passed: they all
assert that a candidate comes OUT of the lens, and producing a candidate that will
always be refused is indistinguishable from working when that is all you check.

The five:

  * tech-debt `lines` — 10 vs 25, then diagnosed, then "fixed" by riding the
    magnitude on `occurrences`, which left it dead on the real tree anyway
  * innovation proposals — 10 vs 15
  * the shipped evidence-gap lens after the float-to-int migration — 1.0 vs 25
  * the caveat lens — 10 vs 25/15, until `severity` was read off the pinned tag
  * the coverage lens — 10 vs 25/15, until recurrence was read off the churn

So the assertion here is not "the lens returns something". It is "something the
lens returns clears the most permissive threshold of a position that actually
runs it". A lens that cannot manage that under a fixture built to suit it cannot
manage it against the real ledger either.

`test_every_registered_lens_has_a_liveness_row` is the part that makes this
durable: adding a lens to `LENSES` without adding it here fails, so the next lens
cannot ship dead the way the last five did.
"""
import pytest

from mindhive import events, replenish

# One fixed instant for every fixture, so the decay term is exercised against
# known ages rather than against whenever the suite happened to run.
NOW = "2026-08-06T12:00:00Z"
TODAY = "2026-08-06T09:00:00Z"
YESTERDAY = "2026-08-05T09:00:00Z"


def _trees(tmp_path, modules=(), test_files=()):
    """A throwaway checkout. `modules` is (name, line_count)."""
    src_root = tmp_path / "src" / "mindhive"
    tests_root = tmp_path / "tests"
    src_root.mkdir(parents=True)
    tests_root.mkdir(parents=True)
    for name, lines in modules:
        (src_root / name).write_text("# line\n" * lines, encoding="utf-8")
    for name, text in test_files:
        (tests_root / name).write_text(text, encoding="utf-8")
    return dict(src_root=str(src_root), tests_root=str(tests_root),
                repo_root=str(tmp_path))


def _ctx(evs=(), **kwargs):
    return replenish.LensContext(evs=tuple(evs), now_ts=NOW, **kwargs)


# --- one fixture per lens -----------------------------------------------------
# Each builder returns the LensContext that lens needs to find its own condition,
# built to be REALISTIC rather than maximal: the numbers are ones this project
# actually produces in a week, because a fixture tuned until the lens passes
# proves only that some input exists somewhere that would clear the bar.

def _live_evidence_gap(tmp_path, monkeypatch):
    events.append_event("verification",
                        {"text": "pytest", "argv": ["pytest"], "exit_code": 0,
                         "counts": {"passed": 1}, "excerpt": "",
                         "verified_by": "python"},
                        ts="2026-08-04T09:00:00Z")
    events.append_event("code_change",
                        {"category": "update", "text": "changed the injector",
                         "files": ["src/mindhive/replenish.py"]}, ts=TODAY)
    return _ctx(events.read_all())


def _live_repair(tmp_path, monkeypatch):
    # Four failures of one signature with no prevention rule — the shape SOP
    # tool-failure-prevention describes as the common case in this ledger.
    for hour in (6, 7, 8, 9):
        events.append_event(
            "tool_failed",
            {"text": "Edit failed: string not found", "signature": "edit:not-read"},
            ts=f"2026-08-06T0{hour}:00:00Z")
    return _ctx(events.read_all())


def _live_tech_debt(tmp_path, monkeypatch):
    # 1848 lines: what `dashboard_page.py` measured when this fixture was
    # written, not a number chosen to pass. The real file is 209 lines now — it
    # was split — and the figure stays because what this exercises is the lens
    # firing on a module well past the threshold, which needs a module well past
    # the threshold to exist. `workspace.py` (1462) is the nearest real one.
    return _ctx(**_trees(tmp_path, modules=[("dashboard_page.py", 1848)]))


def _live_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(replenish, "git_modified_paths",
                        lambda _root: ["src/mindhive/replenish.py"])
    return _ctx(**_trees(tmp_path))


def _live_caveat(tmp_path, monkeypatch):
    events.append_event(
        "caveat",
        {"text": "paths.write_text_atomic is flaky on Windows CI: os.replace "
                 "raises PermissionError on src/mindhive/paths.py"},
        tags=["pinned"], ts=YESTERDAY)
    return _ctx(events.read_all())


def _live_coverage(tmp_path, monkeypatch):
    for hour in (6, 7, 8, 9):
        events.append_event("code_change",
                            {"category": "update", "text": "touched it again",
                             "files": ["src/mindhive/uncovered.py"]},
                            ts=f"2026-08-06T0{hour}:00:00Z")
    trees = _trees(tmp_path, modules=[("uncovered.py", 40)],
                   test_files=[("test_other.py", "from mindhive import events\n")])
    return _ctx(events.read_all(), **trees)


def _live_innovation(tmp_path, monkeypatch):
    def runner(_prompt):
        return {"exit": 0, "stdout": '{"subtasks": [{"title": "Bound the '
                                     'evidence list", "description": "cap it"}]}'}

    # Two changed files: the least grounding the lens will now spend a model call
    # on, so this pins the worst case rather than a comfortable one. At ONE file
    # the lens declines to call at all — see `INNOVATION_MIN_GROUNDING`.
    return _ctx(changed_paths=("src/mindhive/replenish.py",
                               "tests/test_replenish.py"), runner=runner)


# The table. A lens named in `replenish.LENSES` and missing here is a test
# failure, which is the entire point — the previous dead lenses each shipped with
# their own passing unit tests and nothing that asked this question.
LIVENESS = {
    "evidence-gap": _live_evidence_gap,
    "repair": _live_repair,
    "tech-debt": _live_tech_debt,
    "audit": _live_audit,
    "caveat": _live_caveat,
    "coverage": _live_coverage,
    "innovation": _live_innovation,
}


def _positions_running(lens_name) -> list:
    """Every dial position that actually runs this lens, easiest bar first."""
    running = sorted((p for p in replenish.PROFILES.values()
                      if lens_name in p.lens_names),
                     key=lambda p: p.min_score)
    assert running, f"{lens_name} is registered but no dial position runs it"
    return running


def _lowest_threshold_running(lens_name) -> int:
    """The most permissive dial position that actually runs this lens."""
    return _positions_running(lens_name)[0].min_score


def _hardest_threshold_running(lens_name) -> int:
    """The strictest bar this lens must still be able to clear.

    Strictest means the highest position that runs it PLUS the furthest the
    auto-tuner may raise that position. `TUNE_MAX_DRIFT` is not hypothetical:
    `TUNE_RAISE_DELTA` fires after three injections each followed by a failure,
    which is an ordinary bad week rather than a pathological ledger."""
    return _positions_running(lens_name)[-1].min_score + replenish.TUNE_MAX_DRIFT


def test_the_innovation_grounding_floor_matches_the_dial():
    """`INNOVATION_MIN_GROUNDING` and the threshold it was derived from, checked
    against each other rather than against a comment.

    The standing caveat this whole file answers says it plainly: when a lens or a
    threshold moves, assert the lens still clears its own dial position. This is
    that assertion for the one lens whose floor is a hand-derived number — it
    fails both if the floor drops below what `ambitious` needs and if it drifts
    above, which would silence proposals that could perfectly well have landed."""
    threshold = _lowest_threshold_running("innovation")
    floor = replenish.INNOVATION_MIN_GROUNDING

    def score_at(grounding):
        return replenish.BASE_SCORE + replenish.RECURRENCE_STEP * (grounding - 1)

    assert score_at(floor) >= threshold, (
        "the grounding floor admits proposals that cannot clear ambitious")
    assert score_at(floor - 1) < threshold, (
        "the grounding floor is higher than it needs to be — proposals that "
        "would have landed are being suppressed before the model is even asked")


def test_the_innovation_lens_does_not_call_the_model_below_the_floor():
    calls = []

    def runner(prompt):
        calls.append(prompt)
        return {"exit": 0, "stdout": '{"subtasks": [{"title": "x"}]}'}

    ctx = _ctx(changed_paths=("src/mindhive/replenish.py",), runner=runner)
    assert replenish.innovation(ctx) == []
    assert calls == [], (
        "the lens spent a model call it could not use: one grounding fact scores "
        "the bare base, which ambitious refuses")


def test_every_registered_lens_has_a_liveness_row():
    assert set(LIVENESS) == set(replenish.LENSES), (
        "a lens was registered without a liveness fixture. Add one: a lens whose "
        "best score cannot clear its own dial position is dead config, and no "
        "other test in this suite asks that question.")


# The lenses whose `occurrences` can actually reach the recurrence ceiling, and
# whether each carries `severity` and an `observed_ts` (so decay applies).
# `innovation` is deliberately absent: its grounding count is bounded well below
# the ceiling, which `test_the_ceiling_is_a_no_op_for_the_agentic_lens` asserts
# rather than assumes.
CEILING_REACHING = {
    "repair": {"severity": False},
    "evidence-gap": {"severity": True},
    "coverage": {"severity": False},
}


def _at_ceiling(lens_name, severity):
    """A candidate of this lens pinned to the worst case the clamp allows.

    Worst case is both extremes at once: recurrence pinned AT the ceiling (so the
    clamp is what decides the term) and evidence old enough that decay takes the
    full `DECAY_FLOOR` bite out of it. Testing either alone passes while the
    combination fails, which is exactly how a ceiling of 12 would have shipped."""
    return replenish.Candidate(
        lens=lens_name, title=f"{lens_name} at the ceiling",
        fingerprint=f"fp-{lens_name}", evidence=("a real pointer",),
        occurrences=replenish.RECURRENCE_MAX_STEPS + 1, severity=severity,
        observed_ts="2020-01-01T00:00:00Z")


def test_the_recurrence_ceiling_matches_the_dial():
    """`RECURRENCE_MAX_STEPS` is derived, and this is the derivation.

    The ceiling must be the SMALLEST value at which the least-supported lens
    (`repair`: no severity, always dated) still clears the strictest bar it runs
    under while fully decayed. Both directions matter. Too low and the lens goes
    dead at `conservative` — the failure class this file exists for, and the
    reason 12 was rejected despite looking tidier. Too high and the term is
    unbounded again in every case that matters."""
    hardest = _hardest_threshold_running("repair")

    def aged_total_at(ceiling):
        term = replenish.RECURRENCE_STEP * ceiling
        return replenish.BASE_SCORE + term - round(term * replenish.DECAY_FLOOR)

    assert aged_total_at(replenish.RECURRENCE_MAX_STEPS) >= hardest, (
        f"a fully decayed repair candidate at the ceiling scores "
        f"{aged_total_at(replenish.RECURRENCE_MAX_STEPS)} and cannot clear "
        f"{hardest} — the lens is dead at its strictest position")
    assert aged_total_at(replenish.RECURRENCE_MAX_STEPS - 1) < hardest, (
        "the ceiling is higher than the derivation requires; lower it to the "
        "smallest value that still clears the strictest bar")


@pytest.mark.parametrize("lens_name", sorted(CEILING_REACHING))
def test_a_clamped_aged_candidate_still_clears_its_hardest_dial(lens_name):
    """R-B, per lens, at the worst case the clamp can produce.

    The table-driven liveness check below cannot answer this: it scores each lens
    against `_lowest_threshold_running`, which is 15 for `repair` and
    `evidence-gap`, so it would pass unchanged for a ceiling of 2. Capping raises
    the opposite question — can the lens still clear its HARDEST dial — and this
    is where that is asked."""
    breakdown = replenish.score_candidate(
        _at_ceiling(lens_name, CEILING_REACHING[lens_name]["severity"]),
        None, now_ts=NOW)
    hardest = _hardest_threshold_running(lens_name)
    assert breakdown.total >= hardest, (
        f"{lens_name} clamped and fully decayed scores {breakdown.total} "
        f"({breakdown.as_terms()}), below the {hardest} its strictest position "
        f"can demand — the clamp made it dead there")


def test_the_ceiling_is_a_no_op_for_the_agentic_lens():
    """`innovation` counts grounding facts, which are bounded far below the
    ceiling, so the clamp cannot reach it.

    Asserted rather than assumed because the bound is the sum of two independent
    caps in the lens, and either one growing would silently put `innovation` in
    the clamp's reach without anyone revisiting `CEILING_REACHING`."""
    max_grounding = replenish._EVIDENCE_FILES + 2
    assert max_grounding - 1 < replenish.RECURRENCE_MAX_STEPS, (
        "innovation's grounding can now reach the recurrence ceiling — add it to "
        "CEILING_REACHING and give it a worst-case liveness row")


@pytest.mark.parametrize("lens_name", sorted(LIVENESS))
def test_the_lens_clears_every_position_that_runs_it(lens_name, tmp_path,
                                                     monkeypatch):
    """A lens has to be able to land at each dial its profile set includes.

    Driven from `PROFILES` rather than a hand-written list, so adding a position —
    or adding a lens to one — cannot escape the check by nobody remembering to
    update this file."""
    ctx = LIVENESS[lens_name](tmp_path, monkeypatch)
    candidates = replenish.LENSES[lens_name](ctx)
    assert candidates, f"{lens_name} produced no candidate from its own fixture"
    best = max(replenish.score_candidate(c, None, healthy=False,
                                         now_ts=NOW).total
               for c in candidates)

    reachable = [p.name for p in _positions_running(lens_name)
                 if best >= p.min_score]
    assert reachable, (
        f"{lens_name} is DEAD at every position that runs it: its best fixture "
        f"candidate scores {best}, and the positions running it need "
        + ", ".join(f"{p.name}>={p.min_score}"
                    for p in _positions_running(lens_name)))


@pytest.mark.parametrize("lens_name", sorted(LIVENESS))
def test_the_lens_can_clear_a_threshold_it_runs_under(lens_name, tmp_path,
                                                      monkeypatch):
    ctx = LIVENESS[lens_name](tmp_path, monkeypatch)
    candidates = replenish.LENSES[lens_name](ctx)
    assert candidates, f"{lens_name} produced no candidate from its own fixture"

    threshold = _lowest_threshold_running(lens_name)
    scored = [(c, replenish.score_candidate(c, None, healthy=False, now_ts=NOW))
              for c in candidates]
    best = max(b.total for _c, b in scored)
    assert best >= threshold, (
        f"{lens_name} is DEAD: its best candidate scores {best}, and the most "
        f"permissive position running it needs {threshold}. Scores: "
        + ", ".join(f"{c.title[:60]} -> {b.total} ({b.as_terms()})"
                    for c, b in scored))


@pytest.mark.parametrize("lens_name", sorted(LIVENESS))
def test_the_lens_survives_the_round_it_would_run_in(lens_name, tmp_path,
                                                     monkeypatch):
    """Landing is not the same as clearing the bar, so assert it end to end.

    A candidate can clear its threshold and still never become work: no evidence,
    no fingerprint, a shape `normalise` mangles. Running the real gate against the
    real profile is the only check that covers the whole path from lens to sink."""
    ctx = LIVENESS[lens_name](tmp_path, monkeypatch)
    profile = min((p for p in replenish.PROFILES.values()
                   if lens_name in p.lens_names), key=lambda p: p.min_score)

    placed = []

    def sink(kept):
        placed.extend(kept)
        return [replenish.Placement(c.fingerprint, "plan", i)
                for i, c in enumerate(kept)], 0

    result = replenish.run_round(
        ctx.evs, surface="probe", sink=sink,
        cfg=replenish.ReplenishConfig.for_profile(profile),
        lenses={lens_name: replenish.LENSES[lens_name]},
        ctx=ctx, append=False, now_ts=NOW)
    assert result.landed, (
        f"{lens_name} produced candidates but none survived a {profile.name} "
        f"round. Refusals: "
        + "; ".join(f"{c.title[:60]} -> {reason}" for c, reason in result.refused))
