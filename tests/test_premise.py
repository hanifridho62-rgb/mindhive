# tests/test_premise.py
"""The premise gate: mechanical checking of the checkable claims a prompt makes.

The fixture below is a faithful excerpt of the real handoff directive that
motivated this module. Every factual claim in it was false, and none of them
were checked, because at ~20KB the prompt sailed past
`promptgate.PROMPT_SCAN_CAP` without any assessment at all.
"""
import time

from mindhive import premise


# A faithful excerpt of the directive. Kept verbatim in shape (the placeholder
# braces, the phrasing) because the extractor has to cope with the real text,
# not a tidied paraphrase of it.
REAL_DIRECTIVE = """\
### PART II: INITIALIZATION AND ROADMAP RESUMPTION PROTOCOL

**Step 1: The Resumption Command**

You are strictly required to execute the following Mind Hive command
immediately to re-anchor your session state to the overarching plan:

`hive roadmap resume {roadmap_id_or_error}`

*(Note: Replace `{roadmap_id_or_error}` with the specific ID passed to you by
the system or identified as the most recently interrupted plan in your ledger).*

## 5. ARCHITECTURAL BLUEPRINT: PHASE 1 - AUDIT RECOVERY AND VULNERABILITY PATCHING

### 5.1. Patching the Stopword Footgun (`history.py`)
*   You must modify the signature loader and matcher in `src/mindhive/history.py`.
*   **The Stopword Filter:** Implement an immutable, hardcoded tuple of English
    and programming-specific stopwords.

## 3. CONTEXT HYDRATION: THE EXACT VERIFIED REPOSITORY BASELINE
*   **Global Test Suite:** Currently sitting at 3518 passed, 0 failed.
*   You are protecting a 3518-test baseline.
"""


# --- extraction: pure, bounded, total ----------------------------------------

def test_extract_finds_the_real_incidents_three_claim_classes():
    """The regression test for the actual failure. One input, three classes."""
    got = premise.extract(REAL_DIRECTIVE)
    kinds = {c["kind"] for c in got["claims"]}
    assert premise.KIND_PATH in kinds
    assert premise.KIND_ROADMAP_INTERRUPTED in kinds
    assert premise.KIND_TEST_COUNT in kinds

    paths = [c["value"] for c in got["claims"] if c["kind"] == premise.KIND_PATH]
    assert "src/mindhive/history.py" in paths

    counts = [c["value"] for c in got["claims"]
              if c["kind"] == premise.KIND_TEST_COUNT]
    assert 3518 in counts


def test_extract_ignores_prose_abbreviations():
    """`e.g.` and friends look exactly like dotted paths. promptgate already
    learned this; the extractor must not relearn it the hard way."""
    got = premise.extract("Use a helper, e.g. the one in utils, i.e. etc. vs. that.")
    assert [c for c in got["claims"] if c["kind"] == premise.KIND_PATH] == []


def test_extract_ignores_paths_inside_urls():
    """`https://example.com/setup.py` is a link, not a claim about this repo.
    Resolving it against the repo root would manufacture a false contradiction."""
    got = premise.extract("See https://example.com/pkg/setup.py for the recipe.")
    assert [c for c in got["claims"] if c["kind"] == premise.KIND_PATH] == []


def test_extract_ignores_numbers_far_from_test_vocabulary():
    got = premise.extract("The cache holds 4096 entries and the port is 54461.")
    assert [c for c in got["claims"]
            if c["kind"] == premise.KIND_TEST_COUNT] == []


def test_extract_reads_both_test_count_phrasings():
    a = premise.extract("the suite is at 3401 passed")
    b = premise.extract("protecting a 3518-test baseline")
    assert 3401 in [c["value"] for c in a["claims"]
                    if c["kind"] == premise.KIND_TEST_COUNT]
    assert 3518 in [c["value"] for c in b["claims"]
                    if c["kind"] == premise.KIND_TEST_COUNT]


def test_extract_finds_a_concrete_roadmap_id_in_command_position():
    got = premise.extract("run `hive roadmap resume auth-refactor` first")
    ids = [c["value"] for c in got["claims"]
           if c["kind"] == premise.KIND_ROADMAP_ID]
    assert ids == ["auth-refactor"]


def test_extract_does_not_read_a_placeholder_as_a_roadmap_id():
    """`{roadmap_id_or_error}` is a template slot. Treating it as an asserted
    id would make the gate accuse the prompt of naming a plan it never named."""
    got = premise.extract("`hive roadmap resume {roadmap_id_or_error}`")
    assert [c for c in got["claims"]
            if c["kind"] == premise.KIND_ROADMAP_ID] == []


def test_extract_does_not_read_a_mentioned_command_as_an_existence_claim():
    """Documentation that MENTIONS `hive roadmap resume` is not asserting that
    an interrupted roadmap exists. This repo's own DATA_MODEL.md and plan files
    mention it, and a gate that contradicts its own docs is a gate the operator
    learns to ignore."""
    doc = "The `hive roadmap resume <id>` command re-enters an existing plan."
    got = premise.extract(doc)
    assert [c for c in got["claims"]
            if c["kind"] == premise.KIND_ROADMAP_INTERRUPTED] == []


def test_extract_does_not_read_a_runtime_as_a_test_count():
    """"the suite finished in 228 seconds" is not a claim of 228 tests. A
    contradiction manufactured out of a duration is precisely the false
    positive that destroys trust in the gate."""
    for benign in ("the suite finished in 228 seconds",
                   "baseline run took 262 s",
                   "the suite has a 30 second timeout"):
        got = premise.extract(benign)
        assert [c for c in got["claims"]
                if c["kind"] == premise.KIND_TEST_COUNT] == [], benign


def test_extract_is_total_on_junk_input():
    for junk in ("", "   \n\t ", None, 42, {"a": 1}, [], b"bytes"):
        got = premise.extract(junk)
        assert got["claims"] == []


def test_extract_respects_its_own_scan_cap():
    """A cap on WORK, reported rather than silent. Distinct from
    promptgate.PROMPT_SCAN_CAP — the whole point of this module is that it
    runs on prompts that one skips."""
    assert premise.PREMISE_SCAN_CAP > 2000
    under = "x " * 10 + "src/mindhive/history.py"
    assert premise.extract(under)["scanned"] is True

    over = ("y " * premise.PREMISE_SCAN_CAP) + " src/mindhive/history.py"
    got = premise.extract(over)
    assert got["scanned"] is False
    assert got["claims"] == []


def test_extract_caps_claims_per_class_and_says_so():
    """A truncated view must announce its bound — runlog's rule, applied here."""
    many = " ".join(f"src/mod_{i}.py" for i in range(premise.MAX_CLAIMS_PER_CLASS * 3))
    got = premise.extract(many)
    paths = [c for c in got["claims"] if c["kind"] == premise.KIND_PATH]
    assert len(paths) == premise.MAX_CLAIMS_PER_CLASS
    assert got["truncated"].get(premise.KIND_PATH)


def test_extract_survives_pathological_input_quickly():
    """CANARY. promptgate caps its scan at 2000 chars because its regexes
    backtrack catastrophically (a 100KB run of dots measured ~100s). This
    module runs on far larger inputs, so its patterns carry the bound
    structurally. Replace any bounded quantifier here with a greedy one and
    this test hangs."""
    for hostile in ("." * 20000, "/" * 20000, "a." * 10000, "../" * 6000):
        start = time.monotonic()
        premise.extract(hostile)
        assert time.monotonic() - start < 2.0, "extract() is backtracking"


def test_extract_captures_absolute_and_traversal_paths_rather_than_dropping_them():
    """The extractor reports what the text said; refusing to FOLLOW an unsafe
    path is the checker's job. Dropping them here would hide the claim."""
    got = premise.extract(r"see C:\Users\x\thing.py and ../../etc/shadow.py")
    values = [c["value"] for c in got["claims"] if c["kind"] == premise.KIND_PATH]
    assert any("thing.py" in v for v in values)
    assert any("shadow.py" in v for v in values)


def test_extract_is_pure():
    """No I/O, and repeated calls agree."""
    a = premise.extract(REAL_DIRECTIVE)
    b = premise.extract(REAL_DIRECTIVE)
    assert a == b


# --- checking: three-way verdicts against real evidence ----------------------

def _repo(tmp_path):
    """A throwaway checkout root: a `.git` marker plus whatever files a test
    wants to exist."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "mindhive").mkdir(parents=True)
    (tmp_path / "src" / "mindhive" / "advice.py").write_text("x", encoding="utf-8")
    return tmp_path


def _outcome(verdicts, kind):
    return [v["outcome"] for v in verdicts if v["claim"]["kind"] == kind]


def test_path_claim_for_an_existing_file_is_confirmed(tmp_path):
    root = _repo(tmp_path)
    claims = premise.extract("see src/mindhive/advice.py")["claims"]
    got = premise.check(claims, root=root, evs=[])
    assert _outcome(got, premise.KIND_PATH) == [premise.CONFIRMED]


def test_path_claim_for_a_missing_file_is_contradicted(tmp_path):
    """The real incident: the directive asserted `src/mindhive/history.py`,
    which exists on no branch."""
    root = _repo(tmp_path)
    claims = premise.extract("modify `src/mindhive/history.py`")["claims"]
    got = premise.check(claims, root=root, evs=[])
    assert _outcome(got, premise.KIND_PATH) == [premise.CONTRADICTED]


def test_unsafe_paths_are_refused_without_touching_the_filesystem(monkeypatch,
                                                                  tmp_path):
    """CANARY. Absolute paths and traversal are refused as UNKNOWN, and the
    refusal happens before any stat — a gate that follows `../../etc/shadow`
    to answer a question about it has already lost."""
    root = _repo(tmp_path)
    called = []
    monkeypatch.setattr(premise, "_path_exists",
                        lambda p: called.append(p) or True)
    claims = premise.extract(
        r"see C:\Users\x\thing.py and ../../../../etc/shadow.py")["claims"]
    got = premise.check(claims, root=root, evs=[])
    assert set(_outcome(got, premise.KIND_PATH)) == {premise.UNKNOWN}
    assert called == [], "an unsafe path was stat'd"


def test_path_claims_are_unknown_outside_a_repo(tmp_path, monkeypatch):
    """No root resolved means no evidence, not a contradiction."""
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    monkeypatch.chdir(bare)
    claims = premise.extract("see src/mindhive/history.py")["claims"]
    got = premise.check(claims, root=None, evs=[])
    assert _outcome(got, premise.KIND_PATH) == [premise.UNKNOWN]


def test_interrupted_roadmap_claim_is_contradicted_on_an_empty_store(tmp_path):
    """The exact false premise that started this. `hive roadmap list` was
    empty; the directive insisted a plan was waiting to be resumed."""
    root = _repo(tmp_path)
    claims = premise.extract(REAL_DIRECTIVE)["claims"]
    got = premise.check(claims, root=root, evs=[])
    assert _outcome(got, premise.KIND_ROADMAP_INTERRUPTED) == [
        premise.CONTRADICTED]


def test_interrupted_roadmap_claim_is_confirmed_when_one_exists(tmp_path):
    from mindhive import events as ev_mod
    from mindhive import roadmap as rm
    rm.record("set", "auth-refactor", prompt="do the thing", steps=["one"])
    rm.record("interrupt", "auth-refactor", cause="context_limit")
    evs = ev_mod.read_all()

    claims = premise.extract("resume the interrupted plan")["claims"]
    got = premise.check(claims, root=_repo(tmp_path), evs=evs)
    assert _outcome(got, premise.KIND_ROADMAP_INTERRUPTED) == [premise.CONFIRMED]


def test_roadmap_id_claim_resolves_against_the_fold(tmp_path):
    from mindhive import events as ev_mod
    from mindhive import roadmap as rm
    rm.record("set", "auth-refactor", prompt="do the thing", steps=["one"])
    evs = ev_mod.read_all()
    root = _repo(tmp_path)

    hit = premise.check(
        premise.extract("`hive roadmap resume auth-refactor`")["claims"],
        root=root, evs=evs)
    assert premise.CONFIRMED in _outcome(hit, premise.KIND_ROADMAP_ID)

    miss = premise.check(
        premise.extract("`hive roadmap resume no-such-plan`")["claims"],
        root=root, evs=evs)
    assert _outcome(miss, premise.KIND_ROADMAP_ID) == [premise.CONTRADICTED]


def test_test_count_with_no_recorded_run_is_unknown_not_contradicted(tmp_path):
    """CANARY, and the most important one in this file. Collapsing the
    three-way verdict into a boolean re-creates the failure being fixed:
    either silence reads as assent, or a true statement gets called a lie the
    first time the store is empty. Assert the exact outcome."""
    claims = premise.extract("protecting a 3518-test baseline")["claims"]
    got = premise.check(claims, root=_repo(tmp_path), evs=[])
    assert _outcome(got, premise.KIND_TEST_COUNT) == [premise.UNKNOWN]


def test_test_count_matching_the_last_run_is_confirmed(tmp_path):
    import sys
    from mindhive import events as ev_mod
    from mindhive import verify
    verify.run_and_record(
        [sys.executable, "-c", "print('3580 passed in 1.0s')"], echo=False)

    claims = premise.extract("the suite is at 3580 passed")["claims"]
    got = premise.check(claims, root=_repo(tmp_path), evs=ev_mod.read_all())
    assert _outcome(got, premise.KIND_TEST_COUNT) == [premise.CONFIRMED]


def test_test_count_differing_from_the_last_run_is_contradicted(tmp_path):
    import sys
    from mindhive import events as ev_mod
    from mindhive import verify
    verify.run_and_record(
        [sys.executable, "-c", "print('3580 passed in 1.0s')"], echo=False)

    claims = premise.extract("protecting a 3518-test baseline")["claims"]
    got = premise.check(claims, root=_repo(tmp_path), evs=ev_mod.read_all())
    verdicts = [v for v in got if v["claim"]["kind"] == premise.KIND_TEST_COUNT]
    assert verdicts[0]["outcome"] == premise.CONTRADICTED
    assert "3580" in verdicts[0]["ground"], "the ground must name both numbers"
    assert "3518" in verdicts[0]["ground"]


def test_a_failing_recorded_run_contradicts_any_green_count(tmp_path):
    """A run that exited non-zero is not a green suite, whatever it counted."""
    import sys
    from mindhive import events as ev_mod
    from mindhive import verify
    verify.run_and_record(
        [sys.executable, "-c",
         "import sys; print('3580 passed in 1.0s'); sys.exit(1)"], echo=False)

    claims = premise.extract("the suite is at 3580 passed")["claims"]
    got = premise.check(claims, root=_repo(tmp_path), evs=ev_mod.read_all())
    assert _outcome(got, premise.KIND_TEST_COUNT) == [premise.CONTRADICTED]


def test_check_is_total_when_the_evidence_source_raises(monkeypatch, tmp_path):
    """A broken ledger degrades every affected verdict to unknown. It must not
    raise, and it must not invent a contradiction."""
    def boom(*a, **k):
        raise RuntimeError("ledger is down")

    monkeypatch.setattr(premise.roadmap, "interrupted", boom)
    claims = premise.extract("resume the interrupted plan")["claims"]
    got = premise.check(claims, root=_repo(tmp_path), evs=[])
    assert _outcome(got, premise.KIND_ROADMAP_INTERRUPTED) == [premise.UNKNOWN]


def test_check_never_spawns_a_subprocess(monkeypatch, tmp_path):
    """CANARY. `paths.worktree_root` shells out to `git rev-parse`; using it
    here would put a process spawn on the UserPromptSubmit critical path,
    which today spawns nothing. Root resolution walks up for `.git` instead."""
    import subprocess

    def boom(*a, **k):
        raise AssertionError("premise.check spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    claims = premise.extract(REAL_DIRECTIVE)["claims"]
    premise.check(claims, root=_repo(tmp_path), evs=[])


def test_check_returns_a_verdict_per_claim(tmp_path):
    claims = premise.extract(REAL_DIRECTIVE)["claims"]
    got = premise.check(claims, root=_repo(tmp_path), evs=[])
    assert len(got) == len(claims)
    for v in got:
        assert v["outcome"] in (premise.CONFIRMED, premise.CONTRADICTED,
                                premise.UNKNOWN)
        assert v["ground"]
