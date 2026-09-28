# tests/test_bench_scenarios.py
"""`hive bench cli-startup` and `hive bench recall-semantic` -- blueprint P8
(MEB K15 "measure, don't claim"; spec section 4.7 "Pencarian Vektor vs
Pemindaian Penuh").

Two numbers were written down before anything measured them: the MEB's "42 ms"
CLI startup and the spec's "14.7x faster" vector search. `bench_scenarios` is
the harness that turns each into a measurement, and this file drives it the way
`tests/test_bench.py` drives `bench`: through seams (`spawn`, `clock`,
`lexical`, `semantic_open`), so the arithmetic and the classification are
proven on a machine that spawns no child and has no vector extra installed.

The rule the harness inherits from `bench.py` is what every test here pins from
one side or another: **a failure is never timed as a success.** A crashed child
exits in a millisecond and would be the fastest sample of the run; a search that
raised returns in microseconds. Both are counted, named and excluded from every
median. The two published figures ride along as CLAIMS, printed beside the
measurement -- neither is a gate, because an antivirus scanner and a ledger of
twelve files are both real conditions, not failures.

The one real-process run of `cli-startup` lives in `tests/test_cli_startup.py`
(MEB K15). The real lexical path is exercised here against a fixture ledger;
the real semantic open is exercised as far as its refusals, because the harness
must never create the index it would then measure, and its copy isolation
through fake `vectors` seams that write on open the way chromadb does.
"""
import json
import os
import platform
import sys
import time

import pytest

from mindhive import __main__ as cli_mod
from mindhive import bench_scenarios as bs
from mindhive import paths

TS = "2026-09-28T00:00:00+00:00"

# --- fixture builders ---------------------------------------------------------

OK_CHILD = (0, False, "")
CRASHED = (1, False, "Traceback (most recent call last): ... "
                     "ModuleNotFoundError: No module named 'mindhive'")
HUNG = (None, True, "timed out after 60s")
UNSPAWNABLE = (None, False,
               "[WinError 2] The system cannot find the file specified")


def _spawn(answers, log=None, delay=0.0):
    """A fake child launcher: *answers* are `(returncode, timed_out, error)`
    triples consumed in order, the last one repeating. Records what it was
    handed, so a test can read the argv, the timeout and the environment."""
    queue = list(answers)

    def launch(argv, timeout_s, env):
        if log is not None:
            log.append({"argv": list(argv), "timeout_s": timeout_s,
                        "env": dict(env)})
        if delay:
            time.sleep(delay)
        return queue.pop(0) if len(queue) > 1 else queue[0]
    return launch


def _clock(step=0.01):
    """A fake `perf_counter` advancing a fixed step per read: every timed
    interval measures exactly *step* seconds."""
    state = {"t": 0.0}

    def read():
        state["t"] += step
        return state["t"]
    return read


def _scripted_clock(elapsed):
    """Successive read PAIRS span the given seconds, one entry per timed
    interval, in order. Valid only for strictly sequential timing, which is
    all this module does: lexical samples, then the semantic open, then the
    semantic samples."""
    queue = list(elapsed)
    state = {"t": 0.0, "open": False}

    def read():
        if not state["open"]:
            state["open"] = True
            return state["t"]
        state["open"] = False
        state["t"] += (queue.pop(0) if queue else 0.001)
        return state["t"]
    return read


def _startup(**kw):
    kw.setdefault("spawn", _spawn([OK_CHILD]))
    kw.setdefault("clock", _clock())
    kw.setdefault("timestamp", TS)
    return bs.run_cli_startup(**kw)


def _row(report, name):
    return next(r for r in report["rows"] if r["name"] == name)


HITS = [{"file": "2026-09-01-decision.md", "snippet": "we decided", "score": 2.5}]


def _lexical(log=None, hits=HITS):
    def search(query, project, limit):
        if log is not None:
            log.append((query, project, limit))
        return list(hits)
    return search


def _open(search=None, facts=None, raising=None):
    """A fake `semantic_open`: raises *raising* (the path is unavailable) or
    answers `(facts, search)` the way the default does."""
    def open_index(project):
        if raising is not None:
            raise raising
        found = dict(facts or {"path": "events", "indexed_events": 3,
                               "model": "fake"})
        return found, (search or (lambda query, limit: list(HITS)))
    return open_index


def _recall(**kw):
    kw.setdefault("lexical", _lexical())
    kw.setdefault("semantic_open", _open())
    kw.setdefault("clock", _clock())
    kw.setdefault("timestamp", TS)
    kw.setdefault("project", "test")
    return bs.run_recall_semantic(**kw)


def _ledger(n=3):
    """*n* projected ledger entries in the isolated store, in the shape
    `projector.build_ledger_entries` writes them: frontmatter with `type:` and
    `tags:`, then the memo. Each memo names all three default query terms, so
    every default query has something to hit."""
    ldir = paths.project_dir("test") / "LEDGER"
    ldir.mkdir(parents=True)
    for i in range(n):
        (ldir / f"2026-09-{i + 1:02d}T00-00-00Z-decision-{i}.md").write_text(
            "---\ntype: decision\nts: 2026-09-%02dT00:00:00Z\nsession: manual\n"
            "tags: bench\n---\nDecision %d: keep the caveat and the next_step "
            "list beside the decision itself.\n" % (i + 1, i),
            encoding="utf-8")
    return ldir


# --- cli-startup: the two MEB children, reps times each -----------------------

def test_the_two_scenarios_are_the_meb_children_each_run_reps_times():
    log = []
    report = _startup(reps=3, spawn=_spawn([OK_CHILD], log), interpreter="py.exe")
    assert [r["name"] for r in report["rows"]] == ["import", "help"]
    assert _row(report, "import")["argv"] == ["py.exe", "-c",
                                              "import mindhive.__main__"]
    assert _row(report, "help")["argv"] == ["py.exe", "-m", "mindhive", "--help"]
    assert len(log) == 6
    assert all((r["ok"], r["total"]) == (3, 3) for r in report["rows"])
    assert report["failed"] == 0 and report["measured"] == 2


def test_the_children_inherit_pythonpath_and_are_told_not_to_write_bytecode(
        monkeypatch):
    """A checkout measures ITSELF only if the child sees the parent's
    PYTHONPATH (the editable install points at the main checkout otherwise),
    and MEB K14 wants no `__pycache__` written by a benchmark child."""
    monkeypatch.setenv("PYTHONPATH", "X:/some/checkout/src")
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    log = []
    report = _startup(reps=1, spawn=_spawn([OK_CHILD], log))
    assert len(log) == 2
    for entry in log:
        assert entry["env"]["PYTHONPATH"] == "X:/some/checkout/src"
        assert entry["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        assert entry["timeout_s"] == bs.DEFAULT_CHILD_TIMEOUT_S
    assert report["conditions"]["pythonpath"] == "X:/some/checkout/src"
    assert report["conditions"]["dont_write_bytecode"] == "1"


def test_the_operators_bytecode_choice_wins_and_is_reported(monkeypatch):
    """`paths.no_bytecode_env` is a default, never a switch: the value the
    operator exported reaches the child and the report says which it was."""
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    log = []
    report = _startup(reps=1, spawn=_spawn([OK_CHILD], log))
    assert log[0]["env"]["PYTHONDONTWRITEBYTECODE"] == "0"
    assert report["conditions"]["dont_write_bytecode"] == "0"


def test_the_median_is_the_true_median_on_an_even_count():
    # Four samples of 10, 20, 30, 40 ms: the median is 25. An upper-median
    # formula answers 30 and a lower-median one 20; only 25 is right, and an
    # odd count would let both wrong formulas pass.
    report = _startup(reps=4,
                      clock=_scripted_clock([0.010, 0.020, 0.030, 0.040] * 2))
    row = _row(report, "import")
    assert row["median_ms"] == 25.0
    assert (row["min_ms"], row["max_ms"]) == (10.0, 40.0)
    assert _row(report, "help")["median_ms"] == 25.0


def test_a_failed_child_is_counted_named_and_never_timed():
    # The second child of each scenario crashes in 1 ms; the others take 50 ms.
    # A crash is the fastest sample of the run, so a harness that timed it
    # would report a faster CLI the more often it broke.
    report = _startup(reps=4,
                      spawn=_spawn([OK_CHILD, CRASHED, OK_CHILD, OK_CHILD] * 2),
                      clock=_scripted_clock([0.050, 0.001, 0.050, 0.050] * 2))
    row = _row(report, "import")
    assert row["state"] == bs.NONZERO
    assert (row["ok"], row["failed"], row["total"]) == (3, 1, 4)
    assert (row["median_ms"], row["min_ms"], row["max_ms"]) == (50.0, 50.0, 50.0)
    assert "ModuleNotFoundError" in row["error"]
    assert report["failed"] == 2
    text = bs.render_cli_startup(report)
    assert "ModuleNotFoundError" in text and "3/4" in text


def test_a_hung_child_is_a_timeout_and_the_run_still_completes():
    log = []
    report = _startup(reps=2, spawn=_spawn([HUNG], log), timeout_s=7.5)
    assert {entry["timeout_s"] for entry in log} == {7.5}
    assert [r["state"] for r in report["rows"]] == [bs.TIMEOUT, bs.TIMEOUT]
    assert all(r["median_ms"] is None for r in report["rows"])
    assert report["failed"] == 4 and report["measured"] == 0
    text = bs.render_cli_startup(report)
    assert "timed out" in text and "NOTHING WAS MEASURED" in text


def test_an_interpreter_that_cannot_be_spawned_is_reported_not_raised():
    report = _startup(reps=1, spawn=_spawn([UNSPAWNABLE]))
    assert all(r["state"] == bs.SPAWN_ERROR for r in report["rows"])
    assert "WinError 2" in _row(report, "help")["error"]
    assert report["failed"] == 2


def test_reps_below_one_is_refused_by_the_harness():
    with pytest.raises(ValueError):
        _startup(reps=0)


def test_the_claim_is_printed_beside_the_median_and_is_not_a_gate():
    # 500 ms per child: eleven times the MEB's figure, and every child ran, so
    # nothing failed. The claim is a number to compare against, not a bar.
    report = _startup(reps=2, clock=_clock(0.5))
    assert report["failed"] == 0
    assert report["conditions"]["claim_ms"] == 45.0
    assert "MEB" in report["conditions"]["claim_source"]
    text = bs.render_cli_startup(report)
    assert "500.0ms" in text and "45.0ms" in text and "not a gate" in text


# --- cli-startup: the command -------------------------------------------------

def test_the_command_exits_zero_when_every_child_ran_however_slow(monkeypatch,
                                                                  capsys):
    # Each fake child sleeps 60 ms: over the 45 ms claim on every sample, and
    # the exit code does not care. Slow is a measurement, not a failure.
    monkeypatch.setattr(bs, "_default_spawn", _spawn([OK_CHILD], delay=0.06))
    assert cli_mod.main(["bench", "cli-startup", "--reps", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert all(r["median_ms"] >= 60.0 for r in report["rows"])
    assert report["conditions"]["claim_ms"] == 45.0


def test_the_command_exits_one_when_any_child_failed(monkeypatch, capsys):
    monkeypatch.setattr(bs, "_default_spawn",
                        _spawn([OK_CHILD, CRASHED, OK_CHILD, OK_CHILD]))
    assert cli_mod.main(["bench", "cli-startup", "--reps", "2"]) == 1
    out = capsys.readouterr().out
    assert "ModuleNotFoundError" in out and "1 of 4" in out


def test_reps_zero_is_refused_at_the_command_line(capsys):
    assert cli_mod.main(["bench", "cli-startup", "--reps", "0"]) == 2
    assert "--reps" in capsys.readouterr().out


def test_json_output_carries_the_conditions_and_every_row(monkeypatch, capsys):
    monkeypatch.setattr(bs, "_default_spawn", _spawn([OK_CHILD]))
    assert cli_mod.main(["bench", "cli-startup", "--reps", "2", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    c = report["conditions"]
    for field in ("interpreter", "python_version", "reps", "timeout_s",
                  "pythonpath", "dont_write_bytecode", "method", "timestamp",
                  "claim_ms", "claim_source"):
        assert field in c, field
    assert c["interpreter"] == sys.executable
    assert c["python_version"] == platform.python_version()
    assert c["reps"] == 2 and c["timeout_s"] == bs.DEFAULT_CHILD_TIMEOUT_S
    assert [r["name"] for r in report["rows"]] == ["import", "help"]
    for row in report["rows"]:
        for field in ("argv", "samples", "ok", "failed", "total", "median_ms",
                      "min_ms", "max_ms", "state", "error"):
            assert field in row, field
        assert row["total"] == len(row["samples"]) == 2
        assert row["argv"][0] == sys.executable
    assert report["failed"] == 0


# --- recall-semantic: both paths, same queries, timed apart from the open -----

def test_both_paths_are_timed_over_the_same_queries_the_same_number_of_times():
    lex_log, sem_log = [], []

    def semantic(query, limit):
        sem_log.append((query, limit))
        return list(HITS)
    report = _recall(queries=["alpha", "beta"], reps=3, limit=7,
                     lexical=_lexical(lex_log), semantic_open=_open(search=semantic))
    assert [q for q, _, _ in lex_log] == ["alpha", "beta"] * 3
    assert [q for q, _ in sem_log] == ["alpha", "beta"] * 3
    assert {p for _, p, _ in lex_log} == {"test"}
    assert {n for _, _, n in lex_log} == {7} and {n for _, n in sem_log} == {7}
    assert report["lexical"]["total"] == report["semantic"]["total"] == 6
    assert report["conditions"]["samples_per_path"] == 6


def test_the_semantic_open_runs_once_before_the_timed_queries():
    opened, calls = [], []

    def open_index(project):
        opened.append(project)
        return {"path": "events"}, lambda q, limit: calls.append(q) or list(HITS)
    report = _recall(queries=["a"], reps=3, semantic_open=open_index)
    assert opened == ["test"] and calls == ["a", "a", "a"]
    assert report["semantic"]["open"] == {"path": "events"}


def test_the_open_cost_is_reported_apart_from_the_query_time():
    # Intervals, in order: one lexical sample per rep, then the open, then one
    # semantic sample per rep. The open pays 2 s (deps import, model load,
    # index open) and the queries 20 and 30 ms; only the queries are the
    # query time.
    clock = _scripted_clock([0.001, 0.001, 2.0, 0.020, 0.030])
    report = _recall(queries=["q"], reps=2, clock=clock)
    sem = report["semantic"]
    assert sem["open_ms"] == 2000.0
    assert sem["cold_ms"] == 20.0
    assert sem["median_ms"] == 30.0        # warm only: the cold sample is not averaged in
    assert report["lexical"]["cold_ms"] == 1.0
    assert report["lexical"]["median_ms"] == 1.0
    text = bs.render_recall_semantic(report)
    assert "2000.0ms" in text and "30.0ms" in text


def test_the_warm_median_is_a_true_median_and_excludes_the_cold_sample():
    # Lexical: cold 100 ms then warm 10, 20, 30, 40. The warm median is 25;
    # counting the cold sample in gives 30, and so does an upper-median
    # formula over the warm four -- one number rules out both.
    clock = _scripted_clock([0.100, 0.010, 0.020, 0.030, 0.040, 0.5] + [0.001] * 5)
    report = _recall(queries=["q"], reps=5, clock=clock)
    lex = report["lexical"]
    assert lex["cold_ms"] == 100.0 and lex["median_ms"] == 25.0
    assert (lex["min_ms"], lex["max_ms"]) == (10.0, 40.0)
    assert (lex["ok"], lex["total"]) == (5, 5)


def test_the_ratio_is_the_lexical_warm_median_over_the_semantic_warm_median():
    # Lexical warm 1000 ms, semantic warm 50 ms: 20.0x, read the way the spec
    # reads its own figure (1090 / 74 = 14.7x, "lebih cepat").
    clock = _scripted_clock([0.9, 1.0, 1.0, 0.1, 0.005, 0.05, 0.05])
    report = _recall(queries=["q"], reps=3, clock=clock)
    assert report["ratio"] == 20.0
    text = bs.render_recall_semantic(report)
    assert "20.0x" in text and "14.7" in text     # measured, beside the claim


def test_a_sub_millisecond_scan_keeps_its_digits_and_its_ratio():
    # A ledger of a dozen files scans in tens of microseconds. Rounded to
    # 0.1 ms it read "0.0ms", and its ratio against a 10 ms semantic query
    # read "0.0x": a measured time printed as nothing (seen on the real
    # scratch store). Intervals: lexical cold 0.2 ms then warm 0.04, 0.05,
    # 0.06 ms; the open; four semantic queries of 10 ms.
    clock = _scripted_clock([0.0002, 0.00004, 0.00005, 0.00006, 1.0] + [0.010] * 4)
    report = _recall(queries=["q"], reps=4, clock=clock)
    lex = report["lexical"]
    assert lex["median_ms"] == 0.05
    assert (lex["min_ms"], lex["max_ms"]) == (0.04, 0.06)
    assert report["ratio"] == 0.005
    text = bs.render_recall_semantic(report)
    lex_line = next(ln for ln in text.splitlines() if ln.strip().startswith("lexical"))
    ratio_line = next(ln for ln in text.splitlines() if ln.strip().startswith("ratio"))
    assert "0.050ms" in lex_line and "0.0ms" not in lex_line
    assert "0.005x" in ratio_line and "0.0x" not in ratio_line


def test_the_absent_extra_is_unavailable_and_the_lexical_row_still_measures():
    reason = ("the `vectors` extra is not installed (chromadb / "
              "sentence-transformers not importable)")
    report = _recall(reps=2, semantic_open=_open(raising=RuntimeError(reason)))
    sem = report["semantic"]
    assert sem["state"] == bs.UNAVAILABLE and reason in sem["reason"]
    assert sem["open_ms"] is None and sem["median_ms"] is None
    assert (sem["ok"], sem["total"]) == (0, 0)
    assert report["ratio"] is None
    assert report["lexical"]["state"] == bs.OK
    assert report["lexical"]["median_ms"] is not None
    assert "unavailable: " + reason in bs.render_recall_semantic(report)


def test_a_search_that_raises_is_a_failed_sample_never_timed_as_a_success():
    calls = {"n": 0}

    def flaky(query, limit):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("chroma: collection vanished")
        return list(HITS)
    # 4 lexical samples, the open, then 4 semantic: the raise is the fastest
    # interval of the run (0.1 ms) and must not become the minimum.
    clock = _scripted_clock([0.001] * 4 + [0.1] + [0.05, 0.0001, 0.05, 0.05])
    report = _recall(queries=["a", "b"], reps=2, semantic_open=_open(search=flaky),
                     clock=clock)
    sem = report["semantic"]
    assert sem["state"] == bs.FAILED
    assert (sem["ok"], sem["total"]) == (3, 4)
    assert (sem["median_ms"], sem["min_ms"]) == (50.0, 50.0)
    assert "collection vanished" in sem["error"]
    per = {q["query"]: q for q in sem["per_query"]}
    assert (per["b"]["ok"], per["b"]["total"]) == (1, 2)
    assert "collection vanished" in bs.render_recall_semantic(report)


def test_the_default_queries_are_used_and_named_when_none_are_given():
    log = []
    report = _recall(lexical=_lexical(log), reps=1)
    assert len(bs.DEFAULT_QUERIES) >= 2
    assert [q for q, _, _ in log] == list(bs.DEFAULT_QUERIES)
    assert report["conditions"]["queries"] == list(bs.DEFAULT_QUERIES)
    assert "default" in report["conditions"]["query_set"]


def test_supplied_queries_replace_the_defaults():
    report = _recall(queries=["only this"], reps=1)
    assert report["conditions"]["queries"] == ["only this"]
    assert "caller-supplied" in report["conditions"]["query_set"]


def test_reps_below_one_is_refused_by_the_recall_harness():
    with pytest.raises(ValueError):
        _recall(reps=0)


def test_the_spec_figures_ride_along_as_a_claim_not_a_gate():
    report = _recall(reps=1)
    claim = report["conditions"]["claim"]
    assert (claim["full_scan_ms"], claim["vector_ms"], claim["ratio"]) == \
        (1090.0, 74.0, 14.7)
    assert "4.7" in claim["source"]
    text = bs.render_recall_semantic(report)
    assert "1090" in text and "not a gate" in text


# --- recall-semantic: the command ---------------------------------------------

def test_the_command_exits_zero_when_the_extra_is_absent(monkeypatch, capsys):
    monkeypatch.setattr(bs, "default_semantic_open",
                        _open(raising=RuntimeError("no extra here")))
    monkeypatch.setattr(bs, "default_lexical", _lexical())
    assert cli_mod.main(["bench", "recall-semantic", "--reps", "1",
                         "--query", "x"]) == 0
    assert "unavailable: no extra here" in capsys.readouterr().out


def test_the_command_exits_one_when_a_search_raised(monkeypatch, capsys):
    def boom(query, limit):
        raise RuntimeError("index corrupt")
    monkeypatch.setattr(bs, "default_semantic_open", _open(search=boom))
    monkeypatch.setattr(bs, "default_lexical", _lexical())
    assert cli_mod.main(["bench", "recall-semantic", "--reps", "1",
                         "--query", "x"]) == 1
    assert "index corrupt" in capsys.readouterr().out


def test_json_output_has_the_documented_shape(monkeypatch, capsys):
    monkeypatch.setattr(bs, "default_semantic_open", _open())
    monkeypatch.setattr(bs, "default_lexical", _lexical())
    assert cli_mod.main(["bench", "recall-semantic", "--reps", "2", "--query", "a",
                         "--query", "b", "--limit", "3", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    c = report["conditions"]
    for field in ("project", "queries", "query_set", "reps", "limit",
                  "samples_per_path", "ledger_files", "ledger_bytes", "method",
                  "timestamp", "claim"):
        assert field in c, field
    assert c["queries"] == ["a", "b"] and c["reps"] == 2 and c["limit"] == 3
    assert c["samples_per_path"] == 4 and c["project"] == "test"
    for path in ("lexical", "semantic"):
        row = report[path]
        for field in ("state", "cold_ms", "median_ms", "min_ms", "max_ms", "ok",
                      "total", "per_query", "error", "samples"):
            assert field in row, (path, field)
    for field in ("reason", "open_ms", "open"):
        assert field in report["semantic"], field
    assert report["ratio"] is None or isinstance(report["ratio"], float)
    assert [q["query"] for q in report["semantic"]["per_query"]] == ["a", "b"]
    assert all(q["hits"] == 1 for q in report["semantic"]["per_query"])
    assert report["measured"] == 2


def test_the_project_flag_is_the_shared_validator(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["bench", "recall-semantic", "--project", "../../x"])
    assert exc.value.code == 2
    assert "invalid project" in capsys.readouterr().err


def test_reps_and_limit_below_one_are_refused_at_the_command_line(monkeypatch,
                                                                  capsys):
    searched = []
    monkeypatch.setattr(bs, "default_lexical", _lexical(searched))
    assert cli_mod.main(["bench", "recall-semantic", "--reps", "0"]) == 2
    assert cli_mod.main(["bench", "recall-semantic", "--limit", "0"]) == 2
    out = capsys.readouterr().out
    assert "--reps" in out and "--limit" in out
    assert searched == []                       # refused before any search ran


# --- recall-semantic: the real paths ------------------------------------------

def test_the_real_lexical_path_is_measured_against_the_ledger_and_writes_nothing(
        monkeypatch, capsys):
    """The default `lexical` is `recall.recall(semantic=False)`, the function
    behind `hive recall`, read against a fixture ledger; every default query
    hits, the file count and bytes are reported, and the store is byte for
    byte what it was."""
    ldir = _ledger(3)
    monkeypatch.setattr(bs, "default_semantic_open",
                        _open(raising=RuntimeError("no extra here")))
    home = paths.store_home()
    before = sorted(str(p) for p in home.rglob("*"))
    assert cli_mod.main(["bench", "recall-semantic", "--reps", "2",
                         "--project", "test", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["conditions"]["ledger_files"] == 3
    assert report["conditions"]["ledger_bytes"] == sum(
        p.stat().st_size for p in ldir.glob("*.md"))
    lex = report["lexical"]
    assert lex["state"] == bs.OK and (lex["ok"], lex["total"]) == (6, 6)
    assert [q["hits"] for q in lex["per_query"]] == [3, 3, 3]
    assert sorted(str(p) for p in home.rglob("*")) == before


def test_the_default_semantic_open_refuses_to_create_an_index():
    """`hive recall --semantic` creates the chroma directory and its collections
    on demand. The bench never writes, so on a namespace that was never
    embedded it reports unavailable and leaves no `chroma/` behind. Without
    the extra installed the reason is the install hint instead; either way
    the store is untouched."""
    from mindhive import vectors
    home = paths.store_home()
    before = sorted(str(p) for p in home.rglob("*"))
    report = bs.run_recall_semantic(queries=["q"], reps=1, project="test",
                                    lexical=_lexical(), timestamp=TS)
    sem = report["semantic"]
    assert sem["state"] == bs.UNAVAILABLE, sem
    if vectors.has_vectors_support():
        assert "hive embed" in sem["reason"]
    else:
        assert "vectors" in sem["reason"] and "pip install" in sem["reason"]
    assert not (paths.project_dir("test") / "chroma").exists()
    assert sorted(str(p) for p in home.rglob("*")) == before


def _byte_snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _fake_chroma(monkeypatch, *, encoder=lambda: None):
    """The `vectors` seams the default semantic open goes through, behaving the
    way chromadb does: every client open WRITES into the directory it was
    pointed at, resolved from the store at call time exactly as
    `vectors._client` resolves it. Returns the list of directories opened."""
    from mindhive import vectors
    opened = []

    class Collection:
        name = vectors.EVENT_COLLECTION

        def count(self):
            return 4

    class Client:
        def __init__(self, path):
            opened.append(path)
            (path / "chroma.sqlite3").write_bytes(b"re-saved by chroma")

        def list_collections(self):
            return [Collection()]

        def get_collection(self, name):
            return Collection()

    def client(project):
        return Client(paths.project_dir(project) / "chroma")

    def recall_events(query, project=None, limit=10, force=False):
        client(project)                    # the real one opens the store again
        return list(HITS)

    monkeypatch.setattr(vectors, "has_vectors_support", lambda: True)
    monkeypatch.setattr(vectors, "_require_deps", lambda: None)
    monkeypatch.setattr(vectors, "_client", client)
    monkeypatch.setattr(vectors, "_encoder", encoder)
    monkeypatch.setattr(vectors, "semantic_recall_events", recall_events)
    return opened


def _embedded_namespace():
    real = paths.project_dir("test") / "chroma"
    real.mkdir(parents=True)
    (real / "chroma.sqlite3").write_bytes(b"index bytes")
    return real


def test_the_semantic_path_queries_a_copy_and_never_rewrites_the_index(monkeypatch):
    """chromadb re-saves its segment files on every open -- measured on the
    scratch store: one `hive recall --semantic` changed the hashes of
    `chroma.sqlite3` and of the HNSW segment of an index nothing was adding
    to. So the bench opens and queries a temporary COPY of the namespace's
    `chroma/`, puts the store path back, and removes the copy."""
    _embedded_namespace()
    opened = _fake_chroma(monkeypatch)
    home = paths.store_home()
    home_env = os.environ.get("MINDHIVE_HOME")
    before = _byte_snapshot(home)
    report = bs.run_recall_semantic(queries=["q"], reps=2, project="test",
                                    lexical=_lexical(), timestamp=TS)
    sem = report["semantic"]
    assert sem["state"] == bs.OK and (sem["ok"], sem["total"]) == (2, 2), sem
    assert (sem["open"]["path"], sem["open"]["indexed_events"]) == ("events", 4)
    assert sem["open"]["copy_bytes"] == len(b"index bytes")   # a copy OF the index
    assert sem["open"]["copy_removed"] is True
    assert len(opened) >= 3                       # the open, then one per query
    assert all(home not in p.parents for p in opened)   # never the store's index
    assert _byte_snapshot(home) == before                # byte for byte
    assert os.environ.get("MINDHIVE_HOME") == home_env   # the redirect is undone
    assert not any(p.exists() for p in opened)           # the copy is gone
    copy_line = next(ln for ln in bs.render_recall_semantic(report).splitlines()
                     if ln.strip().startswith("index copy"))
    assert "11 bytes" in copy_line and "removed" in copy_line
    assert "NOT removed" not in copy_line


def test_a_semantic_open_that_fails_after_the_copy_still_removes_it(monkeypatch):
    def no_model():
        raise RuntimeError("model all-MiniLM-L6-v2 not in the cache")
    _embedded_namespace()
    opened = _fake_chroma(monkeypatch, encoder=no_model)
    home = paths.store_home()
    home_env = os.environ.get("MINDHIVE_HOME")
    before = _byte_snapshot(home)
    report = bs.run_recall_semantic(queries=["q"], reps=1, project="test",
                                    lexical=_lexical(), timestamp=TS)
    assert report["semantic"]["state"] == bs.UNAVAILABLE
    assert "not in the cache" in report["semantic"]["reason"]
    assert opened and not any(p.exists() for p in opened)
    assert _byte_snapshot(home) == before
    assert os.environ.get("MINDHIVE_HOME") == home_env


def test_the_live_lock_makes_the_semantic_row_unavailable_not_the_run():
    """The semantic path is a burst-CPU op behind `guard.ensure_safe`; the
    lexical scan is not. The bench has no `--force`: under LIVE_LOCK the
    semantic row says why it is missing and the lexical row is still
    measured, exit 0."""
    paths.store_file("LIVE_LOCK").write_text("v17", encoding="utf-8")
    report = bs.run_recall_semantic(queries=["q"], reps=1, project="test",
                                    lexical=_lexical(), timestamp=TS)
    assert report["semantic"]["state"] == bs.UNAVAILABLE
    assert "LIVE_LOCK" in report["semantic"]["reason"]
    assert (report["lexical"]["ok"], report["lexical"]["total"]) == (1, 1)


# --- wiring -------------------------------------------------------------------

def test_both_leaves_are_reachable_with_their_flags_and_the_defaults_named(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["bench", "cli-startup", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--reps", "--timeout", "--json"):
        assert flag in out, flag
    assert "45" in out                                   # the claim, named as one

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["bench", "recall-semantic", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--query", "--reps", "--project", "--limit", "--json"):
        assert flag in out, flag
    for query in bs.DEFAULT_QUERIES:
        assert query in out, query                       # the defaults are stated
