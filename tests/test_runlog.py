# tests/test_runlog.py
"""Run-artifact store: the durable home for full agent output.

Encodes MN-0 §3.1 of
docs/superpowers/specs/2026-07-25-mn0-parallel-output-durability-design.md —
one text artifact per run under ``<project>/runs/``, written atomically, capped
at MAX_ARTIFACT_BYTES with the loss recorded as an in-file marker, and readable
after the writing process is gone. The ledger keeps a bounded excerpt plus the
path; the artifact keeps everything.

Every read is total: a missing, unreadable, or unwritable artifact degrades to
None, never a raise — losing evidence must not break a consumer.
"""
import threading

from mindhive import paths, runlog


# --- fixture builders --------------------------------------------------------

def _log(key="ws-abc-1", **kw):
    return runlog.RunLog(key, **kw)


def _body(key="ws-abc-1"):
    """The artifact's bytes on disk, read back the way a consumer would."""
    return runlog.artifact_path(key).read_text(encoding="utf-8")


# --- the caps are contract, not taste ----------------------------------------

def test_module_caps_match_the_spec():
    """Pin the real constants: the excerpt cap must stay at the 2000 chars the
    pre-MN-0 assistant_response already used (so no consumer changes shape),
    and the per-artifact cap is the spec's explicit 1 MiB."""
    assert runlog.EXCERPT_CHARS == 2000
    assert runlog.MAX_ARTIFACT_BYTES == 1024 * 1024


# --- keys and locations ------------------------------------------------------

def test_runs_dir_lives_under_the_project_namespace():
    assert runlog.runs_dir() == paths.project_dir() / "runs"
    assert runlog.runs_dir("other") == paths.project_dir("other") / "runs"


def test_keys_are_deterministic_and_greppable():
    assert runlog.workspace_key("ab12cd34", 3) == "ws-ab12cd34-3"
    assert runlog.loop_key("deadbeef1234", 7) == "loop-deadbeef1234-7"


def test_keys_cannot_escape_the_runs_directory():
    # a key becomes a FILE NAME. Ids are secrets.token_hex today, but the key
    # builder is the boundary and enforces it rather than trusting its caller.
    p = runlog.artifact_path(runlog.workspace_key("../../etc/passwd", 1))
    assert p.parent == runlog.runs_dir()
    assert ".." not in p.name and "/" not in p.name and "\\" not in p.name


# --- write / read roundtrip --------------------------------------------------

def test_write_once_roundtrips_the_whole_text():
    path = runlog.write_once("ws-one-1", "line a\nline b\n")
    assert path == str(runlog.artifact_path("ws-one-1"))
    assert _body("ws-one-1") == "line a\nline b\n"
    assert runlog.read("ws-one-1") == "line a\nline b\n"


def test_write_once_with_nothing_to_say_writes_no_file():
    # an artifact is evidence; an empty one is noise on the operator's disk
    assert runlog.write_once("ws-empty-1", "") is None
    assert runlog.write_once("ws-blank-1", "   \n\n") is None
    assert not runlog.artifact_path("ws-empty-1").exists()


def test_read_missing_artifact_is_none_not_a_raise():
    assert runlog.read("ws-never-written-9") is None


def test_appended_chunks_all_survive_finalise():
    rl = _log("ws-chunks-1")
    for i in range(50):
        rl.append(f"chunk {i}\n")
    assert rl.finalise() == str(runlog.artifact_path("ws-chunks-1"))
    body = _body("ws-chunks-1")
    assert body.startswith("chunk 0\n")
    assert body.endswith("chunk 49\n")
    assert body.count("\n") == 50


def test_finalise_is_idempotent_and_later_appends_are_refused():
    rl = _log("ws-idem-1")
    rl.append("kept\n")
    first = rl.finalise()
    rl.append("after the fact\n")
    assert rl.finalise() == first
    assert _body("ws-idem-1") == "kept\n"


def test_excerpt_is_bounded_while_the_artifact_stays_complete():
    rl = _log("ws-excerpt-1")
    rl.append("x" * 5000)
    assert len(rl.excerpt()) == runlog.EXCERPT_CHARS
    assert len(rl.excerpt(limit=10)) == 10
    rl.finalise()
    assert len(_body("ws-excerpt-1")) == 5000       # the FULL text is on disk


# --- the byte cap announces its own bound ------------------------------------

def test_byte_cap_keeps_the_head_and_records_the_dropped_bytes(monkeypatch):
    monkeypatch.setattr(runlog, "MAX_ARTIFACT_BYTES", 100)
    rl = _log("ws-cap-1")
    rl.append("A" * 80)
    rl.append("B" * 50)          # 20 bytes fit, 30 are refused
    rl.append("C" * 10)          # nothing fits
    rl.finalise()
    body = _body("ws-cap-1")
    assert body.startswith("A" * 80 + "B" * 20)
    assert "C" not in body
    # a bounded view must SAY it is bounded — the loss is a literal marker line
    # inside the file, not a silent stop
    assert "truncated: 40 bytes dropped" in body
    assert rl.dropped_bytes == 40


def test_an_uncapped_run_carries_no_truncation_marker(monkeypatch):
    # canary for the assertion above: with room to spare the marker MUST be
    # absent, proving the marker tracks real loss rather than always printing
    monkeypatch.setattr(runlog, "MAX_ARTIFACT_BYTES", 100)
    rl = _log("ws-nocap-1")
    rl.append("A" * 40)
    rl.finalise()
    assert "truncated" not in _body("ws-nocap-1")
    assert rl.dropped_bytes == 0


# --- durability while the run is still in flight ------------------------------

def test_output_is_readable_before_finalise(monkeypatch):
    """The point of MN-0: text produced before a death is already on disk. A
    long run flushes to a sidecar as it streams, and read() falls back to it,
    so a killed run's work is retrievable even though finalise never ran."""
    monkeypatch.setattr(runlog, "FLUSH_BYTES", 64)
    rl = _log("ws-inflight-1")
    rl.append("x" * 200)                      # crosses the flush threshold
    assert runlog.read("ws-inflight-1") == "x" * 200
    assert not runlog.artifact_path("ws-inflight-1").exists()   # not final yet
    rl.finalise()
    assert runlog.artifact_path("ws-inflight-1").exists()
    assert runlog.read("ws-inflight-1") == "x" * 200


def test_finalise_clears_the_in_flight_sidecar(monkeypatch):
    monkeypatch.setattr(runlog, "FLUSH_BYTES", 64)
    rl = _log("ws-sidecar-1")
    rl.append("y" * 200)
    part = rl.part_path
    assert part.exists()
    rl.finalise()
    assert not part.exists()                  # one artifact per run, not two


def test_artifact_outlives_the_writer():
    """Restart durability — the property the in-memory scrollback ring cannot
    provide: the object that wrote it is gone, the bytes are not."""
    rl = _log("ws-outlive-1")
    rl.append("survives\n")
    rl.finalise()
    del rl
    assert runlog.read("ws-outlive-1") == "survives\n"


# --- concurrency: 16 agents can finish at once --------------------------------

_RACE_TEXT = "z" * 200_000
# A hang guard, not a pacing knob: the wait it bounds returns the moment the
# reader has looked, so a passing run never spends it. It only caps how long a
# harness whose reader has died can hang the suite.
_SWEEP_TIMEOUT = 60


class _Sweeper:
    """The race's one reader: polls all MAX_AGENTS keys over and over, and
    counts the sweeps it completes so a writer can block until a whole sweep has
    looked at its file.

    That count is what makes the canary deterministic. A torn writer that only
    sleeps between its halves bets that the reader is scheduled inside the
    window; on a saturated machine the single GIL-bound reader lost that bet for
    all 16 windows at once, and the canary reported a blind harness that was not
    blind. A writer that waits for a sweep makes no bet."""

    def __init__(self, prefix):
        self.keys = [f"{prefix}-{i}" for i in range(16)]
        self.torn = []                    # (key, chars) of every partial read
        self.stop = threading.Event()
        self._sweeps = 0
        self._swept = threading.Condition()

    def run(self):
        while not self.stop.is_set():
            for key in self.keys:
                got = runlog.read(key)
                if got is not None and got != _RACE_TEXT:
                    self.torn.append((key, len(got)))
            with self._swept:
                self._sweeps += 1
                self._swept.notify_all()

    def wait_for_a_full_sweep(self, timeout=_SWEEP_TIMEOUT):
        """Block until a sweep has run start to finish AFTER this call, so every
        key was read while the caller's file stood as it stands now. The sweep
        in progress at the call may already be past the caller's key — hence
        two. False on timeout."""
        with self._swept:
            target = self._sweeps + 2
            return self._swept.wait_for(lambda: self._sweeps >= target, timeout)


def _finalise_race(monkeypatch, sweeper):
    """MAX_AGENTS artifacts finalised at once while ``sweeper`` polls all of
    them. Returns the reads that saw something other than the complete text.

    FLUSH_BYTES is raised out of reach so no in-flight sidecar exists: a partial
    read here can only come from finalise itself, which is the invariant under
    test. (Mid-run sidecar reads are legitimately partial — that is what
    test_output_is_readable_before_finalise covers.)"""
    monkeypatch.setattr(runlog, "FLUSH_BYTES", 10 ** 9)

    def writer(key):
        rl = _log(key)
        rl.append(_RACE_TEXT)
        rl.finalise()

    r = threading.Thread(target=sweeper.run, daemon=True)
    r.start()
    threads = [threading.Thread(target=writer, args=(k,)) for k in sweeper.keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sweeper.stop.set()
    r.join(timeout=5)
    return sweeper.torn


def test_concurrent_finalises_never_yield_a_partial_read(monkeypatch):
    """Sixteen agents finishing together must not let a reader catch a
    half-written artifact — the invariant paths.write_text_atomic exists for."""
    sweeper = _Sweeper("ws-race")
    torn = _finalise_race(monkeypatch, sweeper)
    assert torn == []
    assert all(runlog.read(k) == _RACE_TEXT for k in sweeper.keys)


def test_the_race_harness_can_actually_catch_a_torn_write(monkeypatch):
    """Canary for the test above: a negative assertion is only meaningful from a
    harness that can speak. Swap in a deliberately non-atomic writer and the
    SAME reader loop must observe tearing — proving the clean run's silence is
    evidence of atomicity, not of a reader that never looks.

    The torn writer holds its half-written state until the reader has swept past
    it instead of sleeping for 10 ms and hoping: the sleep made this canary fail
    whenever a loaded machine scheduled the reader past all 16 windows."""
    sweeper = _Sweeper("ws-torn")
    unswept = []

    def torn_write(path, text):
        p = runlog.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        half = len(text) // 2
        with open(p, "w", encoding="utf-8") as f:
            f.write(text[:half])
            f.flush()
            if not sweeper.wait_for_a_full_sweep():
                unswept.append(p.stem)
            f.write(text[half:])

    monkeypatch.setattr(runlog.paths, "write_text_atomic", torn_write)
    torn = _finalise_race(monkeypatch, sweeper)
    assert unswept == [], (
        f"the reader completed no sweep within {_SWEEP_TIMEOUT}s for {unswept}")
    # Every writer, not merely one, and at exactly its half: a reader that
    # skipped keys, or only ever saw the truncate-to-empty instant, fails here.
    half = len(_RACE_TEXT) // 2
    assert {key for key, chars in torn if chars == half} == set(sweeper.keys)


def test_concurrent_appends_to_one_log_lose_nothing():
    rl = _log("ws-append-race-1")

    def spam(n):
        for i in range(100):
            rl.append(f"{n}:{i}\n")

    threads = [threading.Thread(target=spam, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rl.finalise()
    body = _body("ws-append-race-1")
    assert len(body.splitlines()) == 800
    for n in range(8):
        assert f"{n}:99\n" in body


# --- enumeration for `hive gc` -------------------------------------------------

def test_list_artifacts_enumerates_finalised_runs_with_sizes():
    runlog.write_once("ws-gc-1", "aaa")
    runlog.write_once("loop-gc-2", "bbbb")
    rows = runlog.list_artifacts()
    by_key = {r["run_key"]: r for r in rows}
    assert set(by_key) == {"ws-gc-1", "loop-gc-2"}
    assert by_key["ws-gc-1"]["bytes"] == 3
    assert by_key["loop-gc-2"]["bytes"] == 4
    assert runlog.total_bytes() == 7


def test_list_artifacts_ignores_in_flight_sidecars(monkeypatch):
    monkeypatch.setattr(runlog, "FLUSH_BYTES", 8)
    rl = _log("ws-gc-partial-1")
    rl.append("still running\n")
    assert runlog.list_artifacts() == []      # nothing finalised yet
    rl.finalise()
    assert [r["run_key"] for r in runlog.list_artifacts()] == ["ws-gc-partial-1"]


def test_list_artifacts_on_a_store_with_no_runs_is_empty():
    assert runlog.list_artifacts() == []
    assert runlog.total_bytes() == 0


# --- totality: losing an artifact breaks nothing --------------------------------

def test_finalise_survives_an_unwritable_destination(monkeypatch):
    def boom(path, text):
        raise OSError("disk full")
    monkeypatch.setattr(runlog.paths, "write_text_atomic", boom)
    rl = _log("ws-unwritable-1")
    rl.append("work that cannot be saved\n")
    assert rl.finalise() is None              # a Finding-shaped None, not a raise
    # the excerpt still carries the evidence forward for the ledger event
    assert "work that cannot be saved" in rl.excerpt()


def test_append_survives_an_unflushable_sidecar(monkeypatch):
    monkeypatch.setattr(runlog, "FLUSH_BYTES", 8)
    rl = _log("ws-unflushable-1")

    def boom(*a, **kw):
        raise OSError("no space left")
    monkeypatch.setattr(rl, "_write_part", boom)
    rl.append("x" * 100)                      # flush fails, buffer keeps the text
    assert rl.finalise()
    assert _body("ws-unflushable-1") == "x" * 100


def test_read_of_an_unreadable_artifact_is_none(monkeypatch):
    runlog.write_once("ws-unreadable-1", "hi")

    def boom(*a, **kw):
        raise OSError("permission denied")
    monkeypatch.setattr(runlog.Path, "read_text", boom)
    assert runlog.read("ws-unreadable-1") is None
