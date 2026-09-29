# SPDX-License-Identifier: Apache-2.0
"""`vector_reader`: a chromadb read in a process of its own, so that what
kills the reader -- a segfault or a hang in chromadb's native core on damage
the segment checks cannot see -- is an error in the caller, never the
caller's death. Each test here starts a real child on a tiny real index.
"""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from mindhive import vector_errors, vector_reader, vector_snapshot


@pytest.fixture
def index(tmp_path):
    """A real chroma index of one collection `rows`, three of them of width
    4, released by this process once written so the child may open it."""
    chromadb = pytest.importorskip("chromadb")
    root = tmp_path / "chroma"
    client = chromadb.PersistentClient(path=str(root))
    col = client.get_or_create_collection("rows",
                                          metadata={"hnsw:space": "cosine"})
    col.add(ids=["a", "b", "c"], documents=["alpha", "beta", "gamma"],
            embeddings=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
                        [0.0, 0.0, 1.0, 0.0]])
    from chromadb.api.shared_system_client import SharedSystemClient as S
    S._identifier_to_refcount.pop(str(root), None)
    S._identifier_to_system.pop(str(root)).stop()
    return root


def _helpers(tmp_path, monkeypatch):
    """A module the child can import -- it takes this process's `sys.path`
    -- whose serve functions die, sleep or panic on the first call."""
    (tmp_path / "reader_helpers.py").write_text(textwrap.dedent('''
        import os
        import sys
        import time

        def _opened(conn):
            conn.send(("ok", None))
            return conn.recv()

        def dying(conn, copy):
            _opened(conn)
            print("goodbye from the reader", file=sys.stderr, flush=True)
            os._exit(3)

        def sleepy(conn, copy):
            _opened(conn)
            time.sleep(60)

        def busy(conn, copy):
            _opened(conn)
            conn.send(("ok", "busy"))
            time.sleep(60)

        def panicking(conn, copy):
            _opened(conn)
            from mindhive import vector_reader
            panic = type("PanicException", (BaseException,),
                         {"__module__": "pyo3_runtime"})
            conn.send(("error", vector_reader._relay(
                panic("called `Option::unwrap()` on a `None` value"))))
            conn.recv()
    '''), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return "reader_helpers"


def _gone(pid, within=15.0) -> bool:
    deadline = time.monotonic() + within
    while vector_snapshot._pid_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def test_the_reader_answers_from_a_process_of_its_own_and_closes(index):
    """The client a read path uses, served by a child: the collections, a
    collection's count, query and get, and a close that ends the child."""
    client = vector_reader.open_isolated(index)
    try:
        assert client.isolated is True
        assert client.pid != os.getpid() and vector_snapshot._pid_alive(client.pid)
        assert client.list_collections() == ["rows"]
        col = client.get_collection("rows")
        assert col.name == "rows"
        assert col.count() == 3
        res = col.query(query_embeddings=[[1.0, 0.0, 0.0, 0.0]], n_results=2,
                        include=["documents", "distances"])
        assert res["ids"][0][0] == "a" and res["documents"][0][0] == "alpha"
        got = col.get(ids=["b"], include=["documents"])
        assert got["ids"] == ["b"] and got["documents"] == ["beta"]
    finally:
        code = client.close()
    assert code == 0, code
    assert _gone(client.pid, within=5), "the closed reader lives on"
    assert client.close() == 0, "a second close is not an error"


def test_an_error_in_the_reader_keeps_its_class(index):
    """Relayed by module, name and message, and rebuilt here as the class
    itself when it is importable: `refused_arguments` and `_from_chromadb`
    judge a relayed failure as they judge one raised in this process."""
    chromadb = pytest.importorskip("chromadb")
    client = vector_reader.open_isolated(index)
    try:
        col = client.get_collection("rows")
        with pytest.raises(TypeError):
            col.query(query_embeddings=[[1.0, 0.0, 0.0, 0.0]], n_results=0)
        with pytest.raises(chromadb.errors.InvalidArgumentError) as err:
            col.query(query_embeddings=[[1.0, 0.0]], n_results=1)
        assert vector_errors._from_chromadb(err.value)
        assert not vector_errors.refused_arguments(err.value, "query")
        with pytest.raises(Exception) as missing:
            client.get_collection("nope")
        assert vector_errors._from_chromadb(missing.value), type(missing.value)
        assert col.count() == 3, "the reader did not survive its own errors"
    finally:
        client.close()


def test_a_relayed_error_is_rebuilt_as_its_class_or_a_stand_in():
    """Only chromadb's, the interpreter's own and this package's classes are
    imported to rebuild an error -- the child names the module -- and a
    class that will not take a message, or any other module's, becomes a
    stand-in of the same name and module, which is what `vector_errors`
    reads (a pyo3 panic is known by name and module alone)."""
    chromadb = pytest.importorskip("chromadb")
    real = vector_reader._rebuild("chromadb.errors", "NotFoundError", "no rows")
    assert isinstance(real, chromadb.errors.NotFoundError)
    assert "no rows" in str(real)
    assert isinstance(vector_reader._rebuild("builtins", "TypeError", "n"),
                      TypeError)
    odd = vector_reader._rebuild("builtins", "UnicodeDecodeError", "bad bytes")
    assert type(odd).__name__ == "UnicodeDecodeError"
    assert isinstance(odd, RuntimeError) and str(odd) == "bad bytes"
    other = vector_reader._rebuild("subprocess", "CalledProcessError", "rc 1")
    assert isinstance(other, RuntimeError)
    assert not isinstance(other, subprocess.CalledProcessError)
    assert type(other).__module__ == "subprocess" and str(other) == "rc 1"
    panic = vector_reader._rebuild("pyo3_runtime", "PanicException", "unwrap")
    assert vector_errors._panicked(panic) and vector_errors.is_failure(panic)


def test_a_reader_that_dies_is_an_error_here_never_a_death(index, tmp_path,
                                                            monkeypatch):
    """The containment itself. Measured, a `link_lists.bin` cut in half
    segfaulted the process reading it (exit 0xC0000005): in a child, that
    is `ReaderDied` in the caller, naming the call, the exit code and the
    child's last words, and every later call is refused at once."""
    helpers = _helpers(tmp_path, monkeypatch)
    client = vector_reader.open_isolated(index, target=f"{helpers}.dying")
    col = vector_reader.RemoteCollection(client, "rows")
    with pytest.raises(vector_reader.ReaderDied) as err:
        col.count()
    assert "count" in str(err.value) and "0x3" in str(err.value), str(err.value)
    assert "goodbye from the reader" in str(err.value), str(err.value)
    assert err.value.pid == client.pid and err.value.code == 3
    with pytest.raises(vector_reader.ReaderDied):
        col.query(query_embeddings=[[1.0, 0.0, 0.0, 0.0]], n_results=1)
    assert client.close() == 3


def test_a_reader_that_hangs_is_killed_and_reported(index, tmp_path,
                                                     monkeypatch):
    """Measured, the same damage hung the reading process past 150 s. A
    child that does not answer within the timeout is killed and the call is
    `ReaderHung`; the client is then gone, and says so at once."""
    helpers = _helpers(tmp_path, monkeypatch)
    client = vector_reader.open_isolated(index, target=f"{helpers}.sleepy",
                                         timeout=0.5)
    col = vector_reader.RemoteCollection(client, "rows")
    began = time.monotonic()
    with pytest.raises(vector_reader.ReaderHung) as err:
        col.count()
    assert time.monotonic() - began < 10
    assert "count" in str(err.value) and "0.5s" in str(err.value), str(err.value)
    assert err.value.pid == client.pid
    assert _gone(client.pid, within=5), "the hung reader was not killed"
    with pytest.raises(vector_reader.ReaderDied):
        col.count()
    assert client.close() is not None


def test_a_reader_that_never_connects_is_killed(index, monkeypatch):
    """A child that never calls back -- an interpreter that will not start,
    a machine too busy for `START_TIMEOUT_S` -- is killed, and the open is
    `ReaderHung`; one that dies before calling back is `ReaderDied`."""
    monkeypatch.setattr(vector_reader, "START_TIMEOUT_S", 1.0)
    monkeypatch.setattr(vector_reader, "_BOOTSTRAP", "import time; time.sleep(60)")
    with pytest.raises(vector_reader.ReaderHung, match="did not connect") as err:
        vector_reader.open_isolated(index)
    assert _gone(err.value.pid, within=5), "the reader that never connected lives on"
    monkeypatch.setattr(vector_reader, "_BOOTSTRAP",
                        "import sys; print('no chromadb here', file=sys.stderr); "
                        "sys.exit(7)")
    with pytest.raises(vector_reader.ReaderDied, match="before it connected") as err:
        vector_reader.open_isolated(index)
    assert err.value.code == 7 and "no chromadb here" in str(err.value)


def test_a_panic_relayed_from_the_reader_is_a_failure_here(index, tmp_path,
                                                           monkeypatch):
    """pyo3 raises a panic in chromadb's Rust core as a BaseException of a
    class every extension builds for itself; relayed, it is a stand-in of
    the same name and module, which `vector_errors` knows as a failure."""
    helpers = _helpers(tmp_path, monkeypatch)
    client = vector_reader.open_isolated(index, target=f"{helpers}.panicking")
    with pytest.raises(BaseException) as err:
        vector_reader.RemoteCollection(client, "rows").count()
    assert vector_errors._panicked(err.value), type(err.value)
    assert vector_errors.is_failure(err.value)
    assert "Option::unwrap()" in str(err.value)
    client.close()


def _orphan(script, *args):
    """Run *script* as a parent process that starts a reader, prints the
    reader's pid and dies with `os._exit`; the reader's pid."""
    src = Path(vector_reader.__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-c", script, *args],
                          capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, PYTHONPATH=str(src)))
    assert done.returncode == 0, done.stderr[-2000:]
    return int(done.stdout.strip().splitlines()[-1])


def test_the_parents_death_ends_an_idle_reader(index):
    """A parent killed without its `atexit` -- `taskkill`, a crashed
    dashboard -- would leave one python process per copy, for good. A child
    waiting for its next request sees the parent go as the end of the pipe
    it serves, and exits."""
    pytest.importorskip("chromadb")
    pid = _orphan("import os, sys\nfrom mindhive import vector_reader\n"
                  "c = vector_reader.open_isolated(sys.argv[1])\n"
                  "print(c.pid, flush=True)\nos._exit(0)\n", str(index))
    assert _gone(pid), "the idle reader outlived the parent that started it"


@pytest.mark.parametrize("watch", ["wait", "poll"])
def test_the_parents_death_ends_a_busy_reader(tmp_path, monkeypatch, watch):
    """A child busy in a call that hangs -- the case the child exists for --
    sees nothing on the pipe it serves. A thread of it waits on the parent's
    process handle (Windows) or on the end of its stdin, whose other end
    only the parent holds, and ends the child when the parent is gone; one
    that cannot open the parent's handle polls its stdin pipe instead
    (`poll`, forced here with a parent pid no one can open). The child here
    sleeps 60 s in its call when the parent dies, and is gone within 15."""
    if watch == "poll" and sys.platform != "win32":
        pytest.skip("the process-handle wait, and its fallback, are Windows only")
    helpers = _helpers(tmp_path, monkeypatch)
    patch = ("vector_reader._BOOTSTRAP = vector_reader._BOOTSTRAP.replace("
             "'int(parent)', '0')\n" if watch == "poll" else "")
    pid = _orphan("import os, sys\nsys.path.insert(0, sys.argv[2])\n"
                  "from mindhive import vector_reader\n" + patch +
                  "c = vector_reader.open_isolated(sys.argv[1], "
                  f"target='{helpers}.busy')\n"
                  "c._conn.send(('rows', 'count', {}))\n"
                  "assert c._conn.recv() == ('ok', 'busy')\n"
                  "print(c.pid, flush=True)\nos._exit(0)\n",
                  str(tmp_path), str(tmp_path))
    assert _gone(pid), "the busy reader outlived the parent that started it"
