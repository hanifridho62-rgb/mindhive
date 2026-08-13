"""The shared outcome vocabulary and the surface registry (T15).

The distinction under test throughout is the one the module exists for: a
surface that did nothing and a surface that did something must not be able to
report the same thing.
"""
import pytest

from mindhive import outcomes
# Imported for its import side effect: registering the "dashboard-jobs" surface,
# so `test_every_registered_probe_*` below is parametrized over a real entry
# rather than silently over an empty list.
from mindhive import jobs                                    # noqa: F401


@pytest.fixture
def clean_registry():
    """Restore the process-wide registry. Without this, a test that registers a
    surface changes what the parametrized probe tests see."""
    saved = dict(outcomes._SURFACES)
    yield
    outcomes._SURFACES.clear()
    outcomes._SURFACES.update(saved)


# --- the vocabulary ----------------------------------------------------------

def test_did_work_and_nothing_to_do_are_different_answers():
    """The whole reason the module exists. Both are successes; only one of them
    changed anything, and `worked` must not be the only thing a caller can ask."""
    did = outcomes.Report(outcomes.DID_WORK, "harvested 40 sessions")
    none = outcomes.Report(outcomes.NOTHING_TO_DO, "no transcripts newer than "
                           "the last harvest")
    assert did.worked and none.worked
    assert did.outcome != none.outcome


def test_degraded_is_neither_success_nor_failure():
    """Reporting a half-working run as success hides the broken half; as failure
    hides the working one."""
    report = outcomes.Report(outcomes.DEGRADED, "embedded 12; chromadb absent "
                             "so 0 were indexed")
    assert not report.worked
    assert report.outcome != outcomes.FAILED


def test_an_unknown_outcome_is_refused():
    with pytest.raises(ValueError, match="unknown outcome"):
        outcomes.Report("fine", "everything is fine")


def test_a_report_cannot_be_a_bare_outcome_with_no_reason():
    """'nothing_to_do' with no summary is the same dead end as `ok: True`."""
    for empty in ("", "   ", None):
        with pytest.raises(ValueError, match="requires a summary"):
            outcomes.Report(outcomes.NOTHING_TO_DO, empty)


def test_a_long_summary_is_capped_not_rejected():
    report = outcomes.Report(outcomes.DID_WORK, "x" * 5000)
    assert len(report.summary) == outcomes.MAX_SUMMARY_CHARS


def test_abandoned_is_not_something_a_run_may_claim_about_itself():
    """A run cannot witness its own death — only the boot seal may say this."""
    assert outcomes.ABANDONED in outcomes.OUTCOMES
    assert outcomes.ABANDONED not in outcomes.SELF_REPORTABLE


# --- the registry ------------------------------------------------------------

def test_a_surface_needs_a_name_a_loop_and_a_probe(clean_registry):
    ok = lambda project=None: outcomes.Report(outcomes.NOTHING_TO_DO, "fine")
    with pytest.raises(ValueError, match="needs a name"):
        outcomes.register("", "some loop", ok)
    with pytest.raises(ValueError, match="needs a `what`"):
        outcomes.register("s", "", ok)
    with pytest.raises(ValueError, match="needs a callable probe"):
        outcomes.register("s", "some loop", "not callable")


def test_two_modules_cannot_each_own_the_same_loop(clean_registry):
    """A silent overwrite is how two surfaces would both think they reported on
    the same thing, with only one of them actually running."""
    first = lambda project=None: outcomes.Report(outcomes.DID_WORK, "a")
    second = lambda project=None: outcomes.Report(outcomes.DID_WORK, "b")
    outcomes.register("shared", "a loop", first)
    with pytest.raises(ValueError, match="already registered"):
        outcomes.register("shared", "a loop", second)
    # Re-registering the SAME probe is fine: module re-import must not explode.
    outcomes.register("shared", "a loop", first)
    assert outcomes.get("shared").probe is first


def test_run_probe_contains_a_probe_that_raises(clean_registry):
    """A probe reports on a surface that may be broken, so the probe raising is a
    state this has to survive."""
    def exploding(project=None):
        raise RuntimeError("the surface is on fire")
    outcomes.register("boom", "a loop", exploding)
    report = outcomes.run_probe("boom")
    assert report.outcome == outcomes.FAILED
    assert "RuntimeError" in report.summary and "on fire" in report.summary


def test_run_probe_refuses_a_probe_that_returns_the_wrong_thing(clean_registry):
    outcomes.register("wrong", "a loop", lambda project=None: {"ok": True})
    report = outcomes.run_probe("wrong")
    assert report.outcome == outcomes.FAILED
    assert "not a Report" in report.summary


def test_an_unregistered_name_is_a_caller_bug_not_a_surface_fault():
    with pytest.raises(KeyError):
        outcomes.run_probe("nothing-registered-under-this-name")


# --- over whatever is registered today ---------------------------------------
# Parametrized rather than a single big-bang assertion: this grows a case as each
# of the plan's five surfaces lands, instead of staying red until the last one
# does. The PR that lands the fifth surface adds the test that all five are here.

@pytest.mark.parametrize("surface", outcomes.registered(),
                         ids=lambda s: s.name)
def test_every_registered_probe_reports_without_raising(surface):
    """Calls the probe DIRECTLY, not through `run_probe`. Going through the
    containment would pass for a probe that raises on every call, which is the
    defect this is looking for."""
    report = surface.probe(None)
    assert isinstance(report, outcomes.Report)
    assert report.outcome in outcomes.SELF_REPORTABLE
    assert report.summary.strip()


@pytest.mark.parametrize("surface", outcomes.registered(),
                         ids=lambda s: s.name)
def test_every_registered_probe_is_cheap_enough_to_run_in_a_probe(surface):
    """A probe that started real work would make this suite spawn pytest inside
    pytest. Two calls in a row must stay side-effect free."""
    first = surface.probe(None)
    second = surface.probe(None)
    assert first.outcome == second.outcome


def test_the_registry_is_not_silently_empty():
    """The parametrized tests above are vacuous if nothing is registered, and an
    empty registry is indistinguishable from a passing run."""
    assert outcomes.registered(), "no surface registered — the tests above are vacuous"
