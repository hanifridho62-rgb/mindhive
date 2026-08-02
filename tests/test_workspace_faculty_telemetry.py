# tests/test_workspace_faculty_telemetry.py
"""The swarm lights MN-5's cat board — edge-triggered, once per episode.

Before this, `workspace.py` did not import `mindnet.telemetry` at all. Launching a
mission wrote `user_prompt` and `assistant_response` rows and nothing else, so the
faculty strip on the MindSwarm page read "no MindNetwork activity" forever — on the
one page an operator actually commands work from. A live scan of every namespace in
`~/.mindhive/*/events/` found 13 `mindnet_activity` rows in total, all written by
probe sessions; not one came from the workspace.

The shape that matters is the EDGE. `telemetry.fold` is last-write-wins per faculty,
so a per-agent `idle` would extinguish the cat the moment the first builder finished
while four siblings were still working — a false "nothing is running" on the board
whose entire purpose is to be trusted about that. So the Manager emits `active` once
when the swarm goes from nobody-running to somebody-running, and `idle` once when the
last agent settles. Everything in between is silence.

Held runs, not canned ones: `FakeProc` in test_workspace.py replays its lines and
exits immediately, which cannot express "three agents running at once". `HoldProc`
blocks its reader until the test releases it, so overlap is a thing the test can
actually construct rather than approximate.
"""
import json
import threading
import time

import pytest

from mindhive import workspace
from mindhive.mindnet import telemetry as tel


# --- fakes ------------------------------------------------------------------

class _FakeStdin:
    def __init__(self):
        self.data = ""
        self.closed = False

    def write(self, s):
        self.data += s

    def close(self):
        self.closed = True


class HoldProc:
    """A run that does not finish until the test releases it.

    The generator body does not execute until the reader thread pulls the first
    line, so the gate is waited on inside that thread — which is exactly where a
    real run's latency lives."""

    def __init__(self, lines, code=0):
        self._gate = threading.Event()
        self._lines = list(lines)
        self._code = code
        self._done = False
        self.stdin = _FakeStdin()
        self.killed = False
        self.stdout = self._gen()

    def _gen(self):
        self._gate.wait(5.0)
        for line in self._lines:
            yield line

    def release(self):
        self._gate.set()

    def poll(self):
        return self._code if self._done else None

    def wait(self):
        self._gate.set()
        self._done = True
        return self._code

    def kill(self):
        self.killed = True
        self.release()
        self._done = True

    def orphan(self):
        """Exit the process WITHOUT letting the reader finish — the stranded
        ``running`` state `_settle_orphan` exists to release."""
        self._done = True


def _stream_lines(session_id="sess-1", result="all done"):
    return [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": session_id, "model": "claude-x"}),
        json.dumps({"type": "result", "subtype": "success", "result": result,
                    "num_turns": 1, "total_cost_usd": 0.001,
                    "session_id": session_id}),
    ]


class Harness:
    """A Manager whose runs are held open and whose faculty emits are recorded."""

    def __init__(self, emit=None):
        self.procs = []
        self.emits = []          # one (faculty, status, kwargs) per emit

        def spawn(argv, env, cwd):
            proc = HoldProc(_stream_lines())
            self.procs.append(proc)
            return proc

        def append(etype, payload, **kw):
            pass                 # the ledger is not what this file is about

        def record(faculty, status, **kw):
            # Records FIRST, then defers to the injected hook for the return
            # value. `emit` is a hook over the recorder rather than a
            # replacement for it, so a test can make a write fail and still see
            # what was attempted — which is the whole subject of the lost-row
            # test below. Returning a truthy sentinel by default matters:
            # `telemetry.emit` answers `None` only when the row did NOT land,
            # and the producer keys its retry on exactly that.
            self.emits.append((faculty, status, kw))
            return emit(faculty, status, **kw) if emit is not None else object()

        self.mgr = workspace.Manager(spawn=spawn, append_event=append,
                                     emit_activity=record)

    def swarm_agent(self, project=None, prompt="go"):
        res = self.mgr.spawn(role="builder", swarm=True, project=project,
                             prompt=prompt)
        assert res["ok"], res
        return res["agent"]["id"]

    @property
    def statuses(self):
        return [s for _f, s, _kw in self.emits]


def _wait_settled(agent, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if agent.status != "running":
            return
        time.sleep(0.01)
    raise AssertionError(f"agent never settled (status={agent.status})")


def _wait_emits(h, n, timeout=5.0):
    """Block until at least *n* emits have landed. The idle edge is written from
    the reader thread, so asserting on it immediately after a release is a race
    the test would lose intermittently."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(h.emits) >= n:
            return
        time.sleep(0.01)
    raise AssertionError(f"expected {n} emits, saw {h.emits}")


# --- the active edge --------------------------------------------------------

def test_launching_a_mission_lights_the_faculty_exactly_once():
    """The reported bug, at its root: commanding the swarm emitted nothing at all.

    Once, not once per agent — a mission spawns a coordinator plus builders, scouts
    and reviewers, and four `active` rows for one episode is four rows saying the
    same thing."""
    h = Harness()
    res = h.mgr.start_mission("build the thing", builders=2, scouts=0, reviewers=0)
    assert res["ok"], res
    assert h.statuses == [tel.STATUS_ACTIVE], h.emits


def test_the_faculty_is_a_member_of_the_canonical_legend():
    """Asserted by membership, not against a hardcoded `"task-graph"`. A name
    outside `FACULTIES` is refused by `_payload` SILENTLY — the row never lands and
    the cat simply never appears, which R16 would read as a bug in the faculty
    rather than a typo in the caller."""
    h = Harness()
    h.swarm_agent()
    _wait_emits(h, 1)
    assert h.emits[0][0] in tel.FACULTIES, h.emits


def test_the_swarm_faculty_matches_the_orchestrators_wave_faculty():
    """One legend, one meaning. `orchestrator` already decided that an orchestrated
    wave of parallel execution lights `task-graph`; a mission is that same shape, and
    two different names for it would split the signal across two cats."""
    from mindhive.mindnet import orchestrator as orch
    assert workspace.SWARM_FACULTY == orch._WAVE_FACULTY


# --- the idle edge (the reason this is edge-triggered) ----------------------

def test_a_finishing_agent_does_not_extinguish_its_still_working_siblings():
    """The failure a naive per-agent emit produces, and the whole reason for the
    edge design. `fold` is last-write-wins per faculty, so one builder writing `idle`
    while four siblings work would put the cat out with the mission still running."""
    h = Harness()
    ids = [h.swarm_agent() for _ in range(3)]
    _wait_emits(h, 1)
    assert h.statuses == [tel.STATUS_ACTIVE]

    for proc, agent_id in zip(h.procs[:2], ids[:2]):
        proc.release()
        _wait_settled(h.mgr.get(agent_id))
        assert h.statuses == [tel.STATUS_ACTIVE], (
            f"a sibling settling put the cat out early: {h.emits}")

    h.procs[2].release()
    _wait_settled(h.mgr.get(ids[2]))
    _wait_emits(h, 2)
    assert h.statuses == [tel.STATUS_ACTIVE, tel.STATUS_IDLE], h.emits


def test_a_second_episode_lights_the_cat_again():
    """The flag resets rather than latching. A one-shot cat would make the board
    right exactly once per server lifetime."""
    h = Harness()
    first = h.swarm_agent()
    h.procs[0].release()
    _wait_settled(h.mgr.get(first))
    _wait_emits(h, 2)

    second = h.swarm_agent()
    _wait_emits(h, 3)
    assert h.statuses == [tel.STATUS_ACTIVE, tel.STATUS_IDLE, tel.STATUS_ACTIVE]
    h.procs[1].release()
    _wait_settled(h.mgr.get(second))
    _wait_emits(h, 4)
    assert h.statuses[-1] == tel.STATUS_IDLE


def _by_project(h):
    """`{project: [status, ...]}` — what each namespace's board actually saw."""
    out: dict = {}
    for _f, status, kw in h.emits:
        out.setdefault(kw.get("project"), []).append(status)
    return out


def test_every_project_gets_its_own_cat():
    """One Manager is process-global but the board is read per namespace, so the
    edge state has to be too. A single shared flag made the SECOND project's
    mission find the cat "already lit" and emit nothing at all into its own
    namespace — eight agents running under a board reading "no MindNetwork
    activity", which is the original bug, reintroduced one project over."""
    h = Harness()
    h.swarm_agent(project="alpha")
    h.swarm_agent(project="beta")
    _wait_emits(h, 2)
    assert _by_project(h) == {"alpha": [tel.STATUS_ACTIVE],
                              "beta": [tel.STATUS_ACTIVE]}


def test_one_projects_swarm_settling_leaves_anothers_cat_alone():
    """The compounding half: with a shared flag, alpha finishing while beta still
    worked produced no edge at all, so alpha's cat stayed lit over an empty fleet
    and beta's eventual idle landed in alpha's namespace."""
    h = Harness()
    alpha = h.swarm_agent(project="alpha")
    beta = h.swarm_agent(project="beta")
    _wait_emits(h, 2)

    h.procs[0].release()
    _wait_settled(h.mgr.get(alpha))
    _wait_emits(h, 3)
    assert _by_project(h)["alpha"] == [tel.STATUS_ACTIVE, tel.STATUS_IDLE]
    assert _by_project(h)["beta"] == [tel.STATUS_ACTIVE], (
        "beta's cat moved when alpha settled")

    h.procs[1].release()
    _wait_settled(h.mgr.get(beta))
    _wait_emits(h, 4)
    assert _by_project(h) == {
        "alpha": [tel.STATUS_ACTIVE, tel.STATUS_IDLE],
        "beta": [tel.STATUS_ACTIVE, tel.STATUS_IDLE]}


def test_a_cat_is_put_out_in_the_namespace_it_was_lit_in():
    """`ui_cats` is explicit that a stuck cat is worse than a missing one — it
    claims work is running that is not — and a cat lit in one namespace and
    extinguished in another is stuck until the 6-hour hide rule clears it."""
    h = Harness()
    agent_id = h.swarm_agent(project="alpha")
    _wait_emits(h, 1)
    h.procs[0].release()
    _wait_settled(h.mgr.get(agent_id))
    _wait_emits(h, 2)
    assert [kw.get("project") for _f, _s, kw in h.emits] == ["alpha", "alpha"]


def test_the_session_id_rides_along_so_the_row_lands_in_the_run_s_segment():
    """`_log` stamps its events with the same `session_id or ws-<id>` fallback so
    a run's rows share one segment. The `active` edge fires before the reader has
    parsed a session id, so the FALLBACK is what executes on every real launch —
    and it was previously asserted nowhere."""
    h = Harness()
    agent_id = h.swarm_agent()
    _wait_emits(h, 1)
    assert h.emits[0][2]["session_id"] == f"ws-{agent_id}"


# --- what must NOT light it -------------------------------------------------

def test_a_mindparallel_pane_never_lights_the_faculty():
    """`task-graph` means orchestrated graph-walking. A MindParallel pane is one
    ad-hoc agent with no coordinator and no decomposition; lighting the same cat for
    it would make the signal mean less, so the narrowing is deliberate."""
    h = Harness()
    res = h.mgr.spawn(role="agent", swarm=False, prompt="go")
    assert res["ok"], res
    h.procs[0].release()
    _wait_settled(h.mgr.get(res["agent"]["id"]))
    assert h.emits == [], f"a non-swarm pane lit the swarm faculty: {h.emits}"


def test_an_agent_that_never_ran_puts_nothing_out():
    """`stop()` on a settled agent is a no-op on the board. Emitting `idle` for a
    cat that was never lit would write rows nobody asked for on every UI click."""
    h = Harness()
    res = h.mgr.spawn(role="builder", swarm=True)      # spawned, never sent
    assert res["ok"], res
    h.mgr.stop(res["agent"]["id"])
    assert h.emits == [], h.emits


# --- the other settle paths -------------------------------------------------

def test_stopping_the_last_running_agent_puts_the_cat_out():
    """A user hitting Stop is a settle like any other. Without this the cat would
    outlive the mission it was reporting on."""
    h = Harness()
    agent_id = h.swarm_agent()
    _wait_emits(h, 1)
    h.mgr.stop(agent_id)
    _wait_emits(h, 2)
    assert h.statuses == [tel.STATUS_ACTIVE, tel.STATUS_IDLE], h.emits


def test_stopping_the_whole_mission_puts_the_cat_out_once():
    h = Harness()
    for _ in range(3):
        h.swarm_agent()
    _wait_emits(h, 1)
    h.mgr.stop_mission()
    _wait_emits(h, 2)
    assert h.statuses == [tel.STATUS_ACTIVE, tel.STATUS_IDLE], h.emits


def test_settling_an_orphaned_run_puts_the_cat_out():
    """The orphan cascade: a process that exited while its worker thread was
    abandoned strands `status == "running"`. `_settle_orphan` releases it, and the
    board has to follow — a cat left lit by an orphan is a permanent false positive."""
    h = Harness()
    agent_id = h.swarm_agent()
    _wait_emits(h, 1)
    h.procs[0].orphan()                       # exited, reader still parked
    assert h.mgr.settle(agent_id)["settled"] is True
    _wait_emits(h, 2)
    assert h.statuses == [tel.STATUS_ACTIVE, tel.STATUS_IDLE], h.emits


# --- the heartbeat: a long mission must not read as a hang ------------------

def test_the_beat_interval_stays_under_the_dim_threshold():
    """The heartbeat exists to keep a working mission out of the dim band, so an
    interval at or above that band would be decoration. Pinned as a RELATIONSHIP
    rather than restating 180 here — invariant 10 — so moving either number keeps
    the guarantee or fails loudly."""
    from mindhive.mindnet import ui_cats as ui
    assert workspace.FACULTY_BEAT_SECS < ui.DIM_AFTER_SECS


def test_a_mission_outliving_the_dim_threshold_would_read_as_a_hang():
    """The defect the heartbeat repairs, stated against the real thresholds
    rather than described. `RUN_TIMEOUT_S` is 1800 s and the board greys a cat at
    180 s, so a lone `active` row at launch leaves a HEALTHY mission looking
    stalled — and past 900 s its panel reads "possible silent hang" — for the
    overwhelming majority of every real mission."""
    from mindhive.mindnet import ui_cats as ui
    assert ui.classify(ui.DIM_AFTER_SECS + 1, tel.STATUS_ACTIVE) == ui.DISPLAY_DIM
    assert workspace.RUN_TIMEOUT_S > ui.DIM_AFTER_SECS * 5


def test_a_producing_swarm_re_asserts_its_cat(monkeypatch):
    """The repair. `telemetry.emit`'s own docstring blesses the repeat: "the fold
    is idempotent under repeats, so a duplicate `active` is harmless"."""
    monkeypatch.setattr(workspace, "FACULTY_BEAT_SECS", 0)
    h = Harness()
    agent_id = h.swarm_agent()
    _wait_emits(h, 1)
    h.procs[0].release()                      # output flows -> beats
    _wait_settled(h.mgr.get(agent_id))
    _wait_emits(h, 2)
    assert h.statuses.count(tel.STATUS_ACTIVE) > 1, (
        f"the cat was lit once and never re-asserted: {h.statuses}")
    assert h.statuses[-1] == tel.STATUS_IDLE
    assert h.statuses[0] == tel.STATUS_ACTIVE


def test_the_heartbeat_is_throttled_by_its_interval():
    """Unthrottled, this would append a ledger row per stream object — the board's
    own read path is a full replay, so a chatty agent would make the cat expensive
    to look at. The default interval is 60 s, so a whole fast run beats zero
    times."""
    h = Harness()
    agent_id = h.swarm_agent()
    _wait_emits(h, 1)
    h.procs[0].release()
    _wait_settled(h.mgr.get(agent_id))
    _wait_emits(h, 2)
    assert h.statuses == [tel.STATUS_ACTIVE, tel.STATUS_IDLE], h.emits


def test_a_silent_swarm_stops_beating_so_the_dim_band_still_means_something(
        monkeypatch):
    """The design's load-bearing choice: output drives the beat, not a clock. A
    wedged mission produces nothing, so it stops beating and dims on schedule —
    and the dim band goes on meaning "said active, then went silent". A
    wall-clock heartbeat would have kept a hung swarm green forever and deleted
    the one signal this board exists to carry."""
    monkeypatch.setattr(workspace, "FACULTY_BEAT_SECS", 0)
    h = Harness()
    h.swarm_agent()                            # held: running, but mute
    _wait_emits(h, 1)
    time.sleep(0.2)
    assert h.statuses == [tel.STATUS_ACTIVE], (
        f"a silent swarm kept its cat awake: {h.statuses}")


# --- the observer never disturbs the observed -------------------------------

def test_a_failing_emitter_cannot_break_a_run():
    """`_log`'s contract, extended to the board: "the ledger must never crash a
    pane". A run that succeeded must not be reported as failed because a cat could
    not be logged."""
    def boom(*_a, **_kw):
        raise RuntimeError("ledger is down")

    h = Harness(emit=boom)
    res = h.mgr.spawn(role="builder", swarm=True, prompt="go")
    assert res["ok"] is True, res
    agent = h.mgr.get(res["agent"]["id"])
    assert agent.status == "running"
    h.procs[0].release()
    _wait_settled(agent)
    assert agent.status == "done"


def test_concurrent_sends_and_settles_neither_deadlock_nor_corrupt_the_edge():
    """`_sync_faculty` reads the fleet under the Manager's lock while `spawn` and
    `stop` hold it on other threads. Acquiring it re-entrantly would deadlock the
    whole fleet, and the failure mode is a hung dashboard rather than a test error —
    so it is worth constructing on purpose."""
    h = Harness()
    ids = []
    lock = threading.Lock()

    def worker():
        agent_id = h.swarm_agent()
        with lock:
            ids.append(agent_id)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "a send deadlocked"

    for proc in h.procs:
        proc.release()
    for agent_id in ids:
        _wait_settled(h.mgr.get(agent_id))
    _wait_emits(h, 2)
    assert h.statuses == [tel.STATUS_ACTIVE, tel.STATUS_IDLE], h.emits


def test_a_row_that_never_landed_is_retried_rather_than_assumed(monkeypatch):
    """`telemetry.emit` degrades to ``None`` on any fault — an unwritable segment,
    a Windows sharing violation, a backend that is down. Committing the edge flag
    ahead of that answer left a cat lit in this process's memory and dark in the
    ledger, and because the producer only speaks on EDGES nothing would ever
    retry it: the mission ran to completion with no cat and an unpaired `idle`.

    `emit`'s docstring justifies being unconditional on the grounds that repeats
    are harmless — the edge design removed the repeats that made a lossy emitter
    safe, so the loss has to be noticed here instead."""
    monkeypatch.setattr(workspace, "FACULTY_BEAT_SECS", 0)
    seen = []

    def flaky(faculty, status, **kw):
        seen.append(status)
        return None if len(seen) == 1 else object()    # the first write is lost

    h = Harness(emit=flaky)
    agent_id = h.swarm_agent()
    _wait_emits(h, 1)
    assert h.statuses == [tel.STATUS_ACTIVE]
    h.procs[0].release()                       # output -> another attempt
    _wait_settled(h.mgr.get(agent_id))
    _wait_emits(h, 2)
    assert h.statuses[1] == tel.STATUS_ACTIVE, (
        f"the lost row was assumed to have landed, not retried: {h.statuses}")
    assert h.statuses[-1] == tel.STATUS_IDLE


def test_a_fresh_manager_puts_out_a_cat_the_ledger_still_says_is_lit():
    """The restart case. The edge state dies with the process while the ledger
    does not, so a dashboard restarted mid-mission came back believing nothing was
    lit — and since the producer only speaks on edges, the stale `active` row sat
    there for the full six hours with nothing able to correct it.

    This is the coupling `telemetry.py`'s docstring warns about ("MindSwarm's
    mission state is a plain instance attribute that dies with the server"),
    reintroduced one layer up. The new Manager adopts the row so it can close
    it."""
    tel.emit(workspace.SWARM_FACULTY, tel.STATUS_ACTIVE)   # the previous process
    assert tel.current().get(workspace.SWARM_FACULTY).status == tel.STATUS_ACTIVE

    h = Harness()                              # a restarted dashboard
    agent_id = h.swarm_agent()
    # No `active` edge here, and that is the point: the ledger already says the
    # cat is lit, so re-announcing it would be noise. What the fresh Manager
    # gains is the ability to CLOSE it.
    h.procs[0].release()
    _wait_settled(h.mgr.get(agent_id))
    _wait_emits(h, 1)
    assert h.statuses[-1] == tel.STATUS_IDLE, (
        f"the stale row was never closed: {h.statuses}")


# --- the reader's own hazards, which this producer amplifies ----------------

def test_a_ghost_reader_cannot_report_the_run_that_replaced_it():
    """`_settle_orphan` fires only when the process has exited while its reader is
    STILL PARKED — so on that path a live competing reader is guaranteed, not
    merely possible. The next `send` installs a fresh proc, timer and reader; when
    the ghost finally wakes it used to cancel the LIVE run's timeout backstop,
    overwrite its status with `error`, and hand the board an `idle` for a mission
    that was still working. The edge rule then refused to re-light it.

    The file already guards its runlog this way (`if a._runlog is rl`); the
    status, timer and cat needed the same identity check."""
    h = Harness()
    agent_id = h.swarm_agent()
    _wait_emits(h, 1)
    agent = h.mgr.get(agent_id)

    h.procs[0].orphan()                        # exited; its reader still parked
    assert h.mgr.send(agent_id, "again")["ok"] is True
    assert agent.status == "running"
    live_timer = agent._timer

    h.procs[0].release()                       # the ghost wakes up
    time.sleep(0.3)
    assert agent.status == "running", (
        "a ghost reader reported the run that replaced it as finished")
    assert agent._timer is live_timer, "the live run's timeout was cancelled"
    assert h.statuses[-1] == tel.STATUS_ACTIVE, (
        f"a ghost reader put the cat out mid-run: {h.statuses}")

    h.procs[1].release()
    _wait_settled(agent)
    assert agent.status == "done"
    _wait_emits(h, len(h.emits) + 1 if h.statuses[-1] != tel.STATUS_IDLE else 1)
    assert h.statuses[-1] == tel.STATUS_IDLE


def test_a_non_dict_stream_line_does_not_kill_the_reader():
    """`_default_spawn` merges stderr into stdout, so anything the CLI writes
    there is fed to `json.loads`. A bare scalar or array parses fine and then dies
    on `.get` with an AttributeError the loop did not catch — killing the only
    automatic settle path. The agent then sits at `running` forever, which under
    this change means the faculty cat stays lit AND the chip cat keeps bobbing:
    both new signals lying at once."""
    procs = []

    def spawn(argv, env, cwd):
        proc = HoldProc(["[1]", "null", '"a bare warning"', "3"]
                        + _stream_lines())
        procs.append(proc)
        return proc

    emits = []
    mgr = workspace.Manager(spawn=spawn, append_event=lambda *a, **k: None,
                            emit_activity=lambda f, s, **kw: (emits.append(s), object())[1])
    res = mgr.spawn(role="builder", swarm=True, prompt="go")
    assert res["ok"], res
    procs[0].release()
    agent = mgr.get(res["agent"]["id"])
    _wait_settled(agent)
    assert agent.status == "done", (
        f"the reader died on a non-dict line (status={agent.status})")
    assert emits[-1] == tel.STATUS_IDLE


def test_a_failed_run_still_puts_the_cat_out():
    """The `error` settle branch. Every other test drives a clean exit, so the
    failing path was structurally present and never executed."""
    procs = []

    def spawn(argv, env, cwd):
        proc = HoldProc([], code=1)            # no result event, non-zero exit
        procs.append(proc)
        return proc

    emits = []
    mgr = workspace.Manager(spawn=spawn, append_event=lambda *a, **k: None,
                            emit_activity=lambda f, s, **kw: (emits.append(s), object())[1])
    res = mgr.spawn(role="builder", swarm=True, prompt="go")
    procs[0].release()
    agent = mgr.get(res["agent"]["id"])
    _wait_settled(agent)
    assert agent.status == "error"
    assert emits == [tel.STATUS_ACTIVE, tel.STATUS_IDLE], emits


# --- the canary: the real wiring, not just the fake -------------------------

def test_the_default_emitter_writes_a_real_activity_row():
    """Every test above injects a recorder, so all of them would still pass if the
    Manager's default emitter pointed at nothing real. This one takes the default
    path end to end and reads the row back out of the ledger — it is the assertion
    that the other fifteen are about something."""
    procs = []

    def spawn(argv, env, cwd):
        proc = HoldProc(_stream_lines())
        procs.append(proc)
        return proc

    mgr = workspace.Manager(spawn=spawn)      # no emit_activity: the real one
    res = mgr.spawn(role="builder", swarm=True, prompt="go")
    assert res["ok"], res

    state = tel.current()
    act = state.get(workspace.SWARM_FACULTY)
    assert act is not None, f"no real row reached the ledger: {state}"
    assert act.status == tel.STATUS_ACTIVE

    procs[0].release()
    _wait_settled(mgr.get(res["agent"]["id"]))
    deadline = time.time() + 5.0
    while time.time() < deadline:
        act = tel.current().get(workspace.SWARM_FACULTY)
        if act is not None and act.status == tel.STATUS_IDLE:
            return
        time.sleep(0.01)
    pytest.fail(f"the real idle row never landed: {act}")
