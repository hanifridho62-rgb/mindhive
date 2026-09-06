# tests/test_events_append_race.py
"""The append race that tore the live ledger's seven `command_run` rows.

`hive verify` reports seven unparseable rows between 2026-07-19 and 2026-08-12,
all `command_run`, and their reasons pair up: `Extra data` on one line and
`Expecting value` on another, with previews that are the TAIL of a row
(`":\\"command_run\\"}"`, `"mand_run\\"}"`). That is not a truncated write. It is
two writers splicing bytes inside one line -- one row split across two.

The writer is `events.append_event`, and the mechanism is that `open(seg, "a")`
is NOT an atomic append on Windows: the CRT seeks to end and then writes, two
syscalls, so two writers can take the SAME offset and the second lands on top of
the first. `events._append_lock` (an OS byte-range lock, `msvcrt` on Windows and
`fcntl` elsewhere) is the fix that shipped in `bdf0e3e`; nothing in the suite
exercised it, so this file does.

**These tests reproduce the defect before they assert it is fixed.** The
reproduction is DETERMINISTIC rather than a timing race: `_RacyOpen` emulates the
CRT's two syscalls with a rendezvous between them, so both writers provably take
the same offset. A fixture that merely races usually wins, and a regression test
whose fixture does not reproduce the defect proves nothing while reading exactly
like one that does -- this repo has that recorded as a caveat.

The ledger itself is append-only and hash-chained, so the seven live rows are
NOT repaired here. See `events.corrupt_rows` on why they stay reported.
"""
import json
import os
import subprocess
import sys
import threading

import pytest

from mindhive import events, paths


# --- fixture builders ---------------------------------------------------------

class _RacyOpen:
    """`open(seg, "a")` as the Windows CRT really implements it, made certain.

    Two syscalls with a rendezvous between them: take the end offset, wait for
    the other writer to take the SAME offset, then write there. Under the append
    lock the second writer never arrives while the first holds it, so the wait
    times out, the barrier breaks, and both writes land in order -- which is
    exactly the behaviour under test.

    `timeout` is what makes this usable on BOTH sides of the fix. Without it the
    locked run would deadlock and read as a hang rather than as a pass."""

    def __init__(self, barrier, timeout=0.5):
        self.barrier = barrier
        self.timeout = timeout
        self.path = None

    def __call__(self, path, mode="r", *a, **kw):
        self.path = path
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, text):
        data = text.encode("utf-8")
        # "r+b", NEVER an "a" mode: append mode carries O_APPEND, which makes
        # the kernel move every write to the current end and would silently
        # repair the very race this class exists to stage.
        if not os.path.exists(self.path):
            open(self.path, "wb").close()
        with open(self.path, "r+b") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            try:
                self.barrier.wait(timeout=self.timeout)
            except threading.BrokenBarrierError:
                pass                      # serialised: the other writer is gone
            f.seek(pos)
            f.write(data)


def _append_from_threads(monkeypatch, *, racy):
    """Two threads appending one row each to one segment. Returns the segment's
    physical lines.

    *racy* chooses whether the write goes through `_RacyOpen`. Everything else --
    `append_event`, the lock, the segment path -- is the real thing."""
    barrier = threading.Barrier(2)
    if racy:
        monkeypatch.setattr(events, "open", _RacyOpen(barrier), raising=False)

    def _write(i):
        events.append_event("command_run", {"cmd": f"probe-{i}", "n": i},
                            session_id="race", project="test")

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    seg = paths.events_dir("test") / "race.jsonl"
    return [ln for ln in seg.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --- the reproduction ---------------------------------------------------------

def test_two_writers_taking_one_offset_lose_a_row(monkeypatch):
    """THE DEFECT, reproduced. Both writers take the same end offset, so the
    second overwrites the first and two appends leave one row.

    This is the canary for the test below: it proves the fixture is capable of
    producing the damage, so the fixed run's cleanliness means the lock did it
    and not the fixture."""
    monkeypatch.setattr(events, "_append_lock",
                        lambda project: __import__("contextlib").nullcontext())

    lines = _append_from_threads(monkeypatch, racy=True)

    assert len(lines) < 2, (
        "the racy writer did not race -- with both writers at one offset a row "
        "must be lost, and a fixture that cannot produce the defect proves "
        "nothing about the fix")


def test_both_live_corrupt_row_reasons_are_one_defect_an_orphan_tail():
    """The DIAGNOSIS, tested rather than argued.

    `hive verify` reports the seven live rows under two different reasons, and
    reading them as two defects is the trap. They are one: an ORPHAN TAIL, the
    remainder of a row whose head was overwritten at the same offset by another
    writer. Which reason `json` gives depends only on where the cut fell.

    - Cut on a token boundary and the fragment's first token parses -- the live
      `Extra data` preview is `"],"ts":"...","type":"command_run"}`, whose
      leading `"],"` IS a complete JSON string, so everything after it is
      "extra".
    - Cut mid-token and there is no first token at all -- the live
      `Expecting value` previews are `mand_run"}` and `":"command_run"}`.

    Both previews are the END of a row, which is why neither is a truncated
    write: a truncated write loses the tail, not the head."""
    row = events.canonical_json(
        {"ts": "2026-08-07T23:28:39Z", "session_id": "s", "type": "command_run",
         "payload": {"cmd": "python -m pytest"}, "tags": ["auto"]})

    on_boundary = row.index(',"ts":') + 1          # fragment starts at `"ts":`
    mid_token = row.index("command_run") + 4       # fragment starts at `and_run"}`
    seg = paths.events_dir("test") / "spliced.jsonl"
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_text(row[on_boundary:] + "\n" + row[mid_token:] + "\n",
                   encoding="utf-8")

    rows = events.corrupt_rows(project="test")
    reasons = [r["reason"] for r in rows]
    assert any("Extra data" in r for r in reasons), reasons
    assert any("Expecting value" in r for r in reasons), reasons

    # The shape that identifies it as a tail rather than a truncation: every
    # fragment is a SUFFIX of the row, exactly as all seven live previews are.
    # Asserted against the segment's lines, not `preview`, which is capped at
    # 80 chars and so cannot show the end of a long fragment.
    lines = seg.read_text(encoding="utf-8").splitlines()
    assert all(row.endswith(ln) for ln in lines), lines
    # `mand_run"}` is one of the live previews verbatim: the cut can land inside
    # the type name itself, so the shared suffix is the row's closing brace, not
    # a whole field.
    assert all(ln.endswith('run"}') for ln in lines), lines


# --- the fix ------------------------------------------------------------------

def test_the_append_lock_keeps_both_rows_at_one_offset(monkeypatch):
    """The regression guard. The SAME racy writer, the SAME rendezvous -- the
    only difference from the reproduction above is that `_append_lock` is left
    in place. Both rows must survive, intact and parseable."""
    lines = _append_from_threads(monkeypatch, racy=True)

    assert len(lines) == 2, f"a row was lost under the lock: {lines}"
    payloads = sorted(json.loads(ln)["payload"]["cmd"] for ln in lines)
    assert payloads == ["probe-0", "probe-1"]
    assert events.corrupt_rows(project="test") == []


def test_the_append_lock_excludes_a_second_holder():
    """The lock is a real OS lock, not a no-op that the tests happen to outrun.

    Asserted from a SECOND descriptor on the same file: Windows byte-range locks
    are per HANDLE and `fcntl.flock` is per open-file-description, so on both
    platforms a second open must be refused while the first is held. This is
    what makes the two tests above about serialisation rather than about
    scheduling luck."""
    with events._append_lock("test"):
        lock = paths.project_dir("test") / "append.lock"
        assert lock.exists(), "no lock file was created at all"
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            with pytest.raises(OSError):
                if events.msvcrt is not None:
                    events.msvcrt.locking(fd, events.msvcrt.LK_NBLCK, 1)
                else:
                    events._flock.flock(
                        fd, events._flock.LOCK_EX | events._flock.LOCK_NB)
        finally:
            os.close(fd)


# --- the real thing: separate processes ---------------------------------------

_CHILD = """
import os, sys
from mindhive import events
for i in range(int(sys.argv[2])):
    events.append_event("command_run", {"cmd": "c", "who": sys.argv[1], "i": i},
                        session_id="shared", project="test")
"""


def test_concurrent_processes_appending_one_segment_lose_no_rows(tmp_path):
    """Processes, not threads: the shape the live corruption actually had, where
    several `hive` invocations shared one `MINDHIVE_SESSION` and therefore one
    segment.

    `PYTHONPATH` is set from the imported module's own location rather than from
    the cwd. The venv installs `mindhive` editable against the MAIN checkout, so
    a child launched from a worktree imports master unless told otherwise -- and
    a child running master's code would be testing the wrong tree while looking
    identical."""
    src = str(__import__("pathlib").Path(events.__file__).resolve().parents[2])
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")

    env = dict(os.environ, PYTHONPATH=src,
               MINDHIVE_HOME=os.environ["MINDHIVE_HOME"],
               MINDHIVE_PROJECT="test", MINDHIVE_AUTORESUME_TIMER="off")
    procs = [subprocess.Popen([sys.executable, str(script), f"p{i}", "40"],
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
             for i in range(4)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err.decode("utf-8", "replace")[-2000:]

    seg = paths.events_dir("test") / "shared.jsonl"
    lines = [ln for ln in seg.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    assert len(lines) == 160, f"{160 - len(lines)} of 160 rows were lost"
    assert events.corrupt_rows(project="test") == []
