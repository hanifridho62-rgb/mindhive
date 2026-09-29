# SPDX-License-Identifier: Apache-2.0
"""The public names over the dashboard's stat-only gate (blueprint P5, Pillar 1).

Spec §10.5.1 and runbook step 1 (§10.11) name three things: `BoundedMemo`,
`compute_project_fingerprint(project_dir)` and
`gated_fold(memo, cache_key, project_dir, fold_func)`. The binding plan
decision (Pilar 1 row) makes them THIN WRAPPERS over the gate that already
exists in `dashboard_memo`: `BoundedMemo` IS `_BoundedMemo`, and the new
fingerprint obeys `_ledger_fingerprint`'s trust rules -- including where that
means NOT following the spec's code, which answers `(0, 0)` for a missing
ledger (a constant that compares equal to itself, i.e. a cache frozen on a
transient error) and a stable stamp under the `pg` backend.

Each test names the wrong implementation it kills:

* identity -- a second LRU class is a second bound free to drift;
* LRU order -- FIFO eviction, or a `put` that does not refresh recency;
* stat-only -- a fingerprint that opens a segment to learn what changed;
* the chain term -- a wrapper that returns `_ledger_fingerprint` alone;
* the trust rules -- `(0, 0)` for no ledger, a live stamp under `pg`;
* the gate -- a fold re-run on an unchanged ledger, a fold NOT re-run after an
  append, and a stamp taken AFTER the fold, which puts a write that landed
  inside the fold window in the key while it is absent from the value.

The new names are reached as `dashboard_memo.<name>` inside each test rather
than imported at the top, so a missing name fails the tests that need it one
by one instead of turning the whole file into one collection error.

Store isolation is the autouse `isolate_store` fixture in tests/conftest.py.
"""
import builtins
import io
import os

import pytest

from mindhive import chain, dashboard_memo, events, paths

# --- fixture builders -------------------------------------------------------

PROJECT = "test"            # the namespace `isolate_store` points the store at
FP = ("stamp", 1)           # an opaque trusted stamp for the pure-memo tests
OTHER_FP = ("stamp", 2)


def _append(text, session_id="sess-memo"):
    """One semantic event, through the real writer, into the isolated store."""
    return events.append_event("decision", {"text": text},
                               session_id=session_id, project=PROJECT)


def _project_dir():
    return paths.project_dir(PROJECT)


def _fingerprint(project_dir=None):
    return dashboard_memo.compute_project_fingerprint(
        project_dir if project_dir is not None else _project_dir())


def _memo():
    return dashboard_memo.BoundedMemo(max_entries=512)


class _ContentRead(Exception):
    """Raised by the patched openers in the stat-only test. Not an OSError on
    purpose: nothing in a fingerprint may swallow it as a filesystem fault."""


# --- BoundedMemo ------------------------------------------------------------

def test_bounded_memo_is_the_existing_bounded_memo_class_not_a_second_one():
    """The plan's identity rule. A copy would carry its own bound and its own
    eviction, and the dashboard's memos (all `_BoundedMemo`) would stop being
    instances of the public name."""
    assert dashboard_memo.BoundedMemo is dashboard_memo._BoundedMemo
    assert isinstance(dashboard_memo._MEMORY_MEMO, dashboard_memo.BoundedMemo)


def test_bounded_memo_serves_a_value_only_under_the_fingerprint_it_was_put_with():
    memo = _memo()
    value = {"rows": [1, 2, 3]}
    memo.put("k", FP, value)

    assert memo.get("k", FP) is value            # the stored object, not a copy
    assert memo.get("k", OTHER_FP) is None       # the ledger moved: a miss
    assert memo.get("absent", FP) is None

    memo.put("k", OTHER_FP, "refolded")          # a re-put replaces, never adds
    assert len(memo) == 1
    assert memo.get("k", OTHER_FP) == "refolded"
    assert memo.get("k", FP) is None

    memo.clear()
    assert len(memo) == 0
    assert memo.get("k", OTHER_FP) is None


def test_bounded_memo_at_512_evicts_the_least_recently_used_not_the_oldest():
    """DISCRIMINATING on the ORDER. `k0` is the oldest insertion but was read
    last, and `k1` was re-put: an insertion-order (FIFO) memo evicts `k0`, a
    `put` that forgets to refresh recency evicts `k1`. Only an LRU evicts `k2`."""
    memo = _memo()
    for i in range(512):
        memo.put(f"k{i}", FP, f"v{i}")
    assert len(memo) == 512

    assert memo.get("k0", FP) == "v0"            # a read is use
    memo.put("k1", FP, "v1-new")                 # so is a re-put
    memo.put("k512", FP, "v512")                 # one past the bound

    assert len(memo) == 512
    assert memo.get("k2", FP) is None, "the least recently used entry survived"
    # CANARY for the miss above: the entries FIFO or a stale re-put would have
    # evicted are still there, so the one that went is gone by recency alone.
    assert memo.get("k0", FP) == "v0"
    assert memo.get("k1", FP) == "v1-new"
    assert memo.get("k3", FP) == "v3"
    assert memo.get("k512", FP) == "v512"


def test_bounded_memo_keeps_the_bound_required_as_the_existing_class_documents():
    """Pins the EXISTING design through the new name. §10.5.1 gives
    `max_entries` a default of 512; `_BoundedMemo` deliberately has none,
    because `OrderedDict.copy` calls `self.__class__(self)` with no bound and a
    default would hand back a copy silently carrying the wrong one. The name is
    an identity, so it cannot take the spec's default without changing the
    class every dashboard memo is built from."""
    with pytest.raises(TypeError):
        dashboard_memo.BoundedMemo()


# --- compute_project_fingerprint -------------------------------------------

def test_fingerprint_is_the_spec_tuple_over_the_segment_stats_the_ledger_stamp_reads():
    """The §10.5.1 shape -- `(segment count, ((name, mtime_ns, size), ...)
    sorted by name, chain.json mtime_ns)` -- over EXACTLY the per-segment stat
    `_ledger_fingerprint` already reads, so the two gates cannot disagree about
    which segments exist or what they weigh."""
    _append("one")
    _append("two", session_id="sess-other")
    ledger = dashboard_memo._ledger_fingerprint(PROJECT)
    assert ledger is not None and len(ledger) == 2, \
        "the fixture did not produce two segments, so this compares nothing"

    count, segments, chain_mtime = _fingerprint()

    assert count == 2
    assert list(segments) == sorted(segments)
    assert {name: [size, mtime_ns] for name, mtime_ns, size in segments} == ledger
    assert chain_mtime == 0            # no checkpoint has been written yet


def test_fingerprint_holds_still_until_an_append_moves_it():
    _append("one")
    first = _fingerprint()

    assert _fingerprint() == first, "an unchanged ledger stamped differently"
    _append("two")                     # same session: the same segment grows
    assert _fingerprint() != first, "an append did not move the fingerprint"


def test_fingerprint_watches_chain_json_which_the_ledger_stamp_cannot_see():
    """DISCRIMINATING against a wrapper that returns `_ledger_fingerprint` and
    stops: the checkpoint sidecar moves with no event appended, and §10.5.1
    names its mtime as the fingerprint's third term."""
    _append("one")
    chain.write_chain(PROJECT)
    chain_json = _project_dir() / "chain.json"
    ledger_before = dashboard_memo._ledger_fingerprint(PROJECT)
    before = _fingerprint()
    assert before[2] == chain_json.stat().st_mtime_ns != 0

    st = chain_json.stat()
    os.utime(chain_json, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    after = _fingerprint()

    # CANARY: the segment stamp really is blind to this change, so the move
    # asserted below can only have come from the chain term.
    assert dashboard_memo._ledger_fingerprint(PROJECT) == ledger_before
    assert after != before
    assert after[2] == chain_json.stat().st_mtime_ns


def test_fingerprint_opens_no_file_it_only_stats(monkeypatch):
    """§10.5.1: 'Dilarang keras membuka (open()) atau membaca berkas JSONL.'

    Every opener a read could go through -- `builtins.open`, `io.open` (what
    `pathlib` calls) and `os.open` -- is made to refuse, and the fingerprint
    must still come back EQUAL to the unpatched one. Equality, not merely "no
    exception": an implementation that read the segments and swallowed the
    failure would answer None here, and None is not the stamp."""
    _append("one")
    _append("two", session_id="sess-other")
    chain.write_chain(PROJECT)
    pdir = _project_dir()               # resolved unpatched, like any caller's
    expected = _fingerprint(pdir)       # warm: any lazy import happens unpatched
    assert expected is not None
    segment = sorted((pdir / "events").glob("*.jsonl"))[0]
    opened = []

    def refuse(*args, **kwargs):
        opened.append(args[0] if args else kwargs.get("file"))
        raise _ContentRead(f"opened {opened[-1]!r}")

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", refuse)
        m.setattr(io, "open", refuse)
        m.setattr(os, "open", refuse)
        # CANARY: the patch stands between a caller and the bytes.
        with pytest.raises(_ContentRead):
            segment.read_text(encoding="utf-8")
        got = _fingerprint(pdir)

    assert got == expected
    assert opened == [segment], f"the fingerprint opened files: {opened[1:]}"


@pytest.mark.parametrize("backend,trusted", [
    ("file", True), ("dual", True), ("pg", False), ("PG", False)])
def test_fingerprint_trusts_exactly_the_backends_the_ledger_stamp_trusts(
        monkeypatch, backend, trusted):
    """Under `pg` the reader answers from Postgres while the local segments
    never change again, so a stamp over them is stable forever: every poll a
    hit, the view frozen. `dual` still reads the segments and must stay
    cacheable -- treating it as `pg` was a measured 1.6 ms -> 751 ms defect.
    The `file`/`dual` rows are the canary for the `pg` rows' None."""
    _append("one")
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", backend)

    assert (_fingerprint() is not None) is trusted
    assert (dashboard_memo._ledger_fingerprint(PROJECT) is not None) is trusted


@pytest.mark.parametrize("layout", [
    "no project directory", "no events directory", "an empty events directory",
    "an events directory holding no segment"])
def test_fingerprint_of_a_ledger_with_no_segments_is_untrusted_not_a_constant(
        layout):
    """§10.5.1 answers `(0, 0)` here, and `(0, 0) == (0, 0)`: the first
    request would cache its answer against a key every later request matches,
    and a directory error that looks the same would pin it for the life of the
    process. `_ledger_fingerprint` answers None for the same states, and so
    must this."""
    pdir = _project_dir()
    if layout != "no project directory":
        pdir.mkdir(parents=True)
    if layout in ("an empty events directory",
                  "an events directory holding no segment"):
        (pdir / "events").mkdir()
    if layout == "an events directory holding no segment":
        (pdir / "events" / "notes.txt").write_text("not a segment",
                                                   encoding="utf-8")

    assert _fingerprint(pdir) is None
    assert dashboard_memo._ledger_fingerprint(PROJECT) is None
    # CANARY: the same directory is stamped as soon as it holds a segment, so
    # the None above is the empty ledger's and not a wrong path's.
    _append("one")
    assert _fingerprint(pdir) is not None


# --- gated_fold -------------------------------------------------------------

def _counting_fold(calls):
    def fold(project_dir):
        calls.append(project_dir)
        return {"folds": len(calls)}
    return fold


def test_gated_fold_folds_once_while_the_ledger_is_unchanged():
    _append("one")
    memo, calls = _memo(), []
    fold = _counting_fold(calls)

    first = dashboard_memo.gated_fold(memo, "memory:test", _project_dir(), fold)
    second = dashboard_memo.gated_fold(memo, "memory:test", _project_dir(), fold)

    assert calls == [_project_dir()], "folded again with nothing appended"
    assert first == {"folds": 1}
    assert second is first             # served from the memo, the same object


def test_gated_fold_folds_again_after_an_append_and_then_settles():
    _append("one")
    memo, calls = _memo(), []
    fold = _counting_fold(calls)
    dashboard_memo.gated_fold(memo, "memory:test", _project_dir(), fold)
    dashboard_memo.gated_fold(memo, "memory:test", _project_dir(), fold)
    assert len(calls) == 1, \
        "the gate did not hold while nothing moved, so the case below cannot " \
        "tell an invalidation from a cache that never worked"

    _append("two")
    third = dashboard_memo.gated_fold(memo, "memory:test", _project_dir(), fold)
    fourth = dashboard_memo.gated_fold(memo, "memory:test", _project_dir(), fold)

    assert len(calls) == 2
    assert third == {"folds": 2}
    assert fourth is third


def test_gated_fold_keeps_one_answer_per_cache_key_over_the_same_project():
    """Two endpoints sharing one memo and one project must not be served each
    other's fold -- the key is the cache key, not the directory."""
    _append("one")
    memo = _memo()

    a = dashboard_memo.gated_fold(memo, "stats", _project_dir(), lambda d: "S")
    b = dashboard_memo.gated_fold(memo, "folds", _project_dir(), lambda d: "F")

    assert (a, b) == ("S", "F")
    stamp = _fingerprint()
    assert memo.get("stats", stamp) == "S" and memo.get("folds", stamp) == "F"


def test_gated_fold_with_an_untrusted_fingerprint_folds_every_time_and_stores_nothing():
    memo, calls = _memo(), []
    fold = _counting_fold(calls)

    dashboard_memo.gated_fold(memo, "k", _project_dir(), fold)   # no ledger yet
    dashboard_memo.gated_fold(memo, "k", _project_dir(), fold)

    assert len(calls) == 2
    assert len(memo) == 0, "an untrusted stamp was stored as a key"
    # CANARY: the same memo and fold over a real ledger do cache.
    _append("one")
    dashboard_memo.gated_fold(memo, "k", _project_dir(), fold)
    dashboard_memo.gated_fold(memo, "k", _project_dir(), fold)
    assert len(calls) == 3 and len(memo) == 1


def test_gated_fold_stamps_before_folding_so_a_write_inside_the_fold_is_not_cached_over():
    """DISCRIMINATING on the ORDER of stamp and fold (`_oversee_cached`'s
    lesson). The fold reads, and then a writer lands while it is still
    running. Stamped AFTER the fold, the key would describe the two-event
    ledger while the value counted one -- and every later call would match the
    key and serve the stale count until some unrelated append moved it."""
    _append("one")
    memo, seen = _memo(), []

    def fold(project_dir):
        count = len(events.read_all(PROJECT))        # the fold reads...
        seen.append(count)
        if len(seen) == 1:
            _append("landed while the first fold was running")   # ...a write lands
        return count

    assert dashboard_memo.gated_fold(memo, "k", _project_dir(), fold) == 1
    assert dashboard_memo.gated_fold(memo, "k", _project_dir(), fold) == 2, \
        "the write that landed inside the first fold was cached over"
    assert dashboard_memo.gated_fold(memo, "k", _project_dir(), fold) == 2
    assert seen == [1, 2], "the gate did not settle once nothing moved"
