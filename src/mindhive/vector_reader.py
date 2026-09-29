# SPDX-License-Identifier: Apache-2.0
"""A chromadb read in a process of its own, so that what kills the reader
never kills the caller.

Measured on scratch copies of a chroma index (DATA_MODEL.md 4.19): a
persisted HNSW segment whose `link_lists.bin` was cut in half crashed the
process reading it with a segfault in 19 reads of 23 and hung it past 150 s
in 2. `vector_snapshot.check_segments` refuses that cut now, and every other
cut it can see; damage it cannot see -- bytes of the right length -- is met
by chromadb's native core, which can crash or hang on it, and a segfault in
the dashboard takes the server down. So `open_isolated` opens the copy in a
child process. `RemoteClient` has the shape a read path uses -- the client's
`list_collections` and `get_collection`, a collection's `count`, `query` and
`get` -- and every call is a message to the child and its answer, waited for
`READ_TIMEOUT_S`: a child that dies is `ReaderDied` in the caller, naming
the call, the exit status and the child's last words on stderr; one that
does not answer in time is killed and is `ReaderHung`; either is refused at
once after that. What the child raises is relayed by module, name and
message and rebuilt here (`_rebuild`) as that class when it is chromadb's,
the interpreter's own or this package's, else as a stand-in of the same name
and module -- a pyo3 `PanicException` included, which `vector_errors` knows
by name -- so the judge and the remedy read a relayed failure as they read
an in-process one. A failed read through an isolated client keeps nothing:
nothing in the caller's process touched the damage, so `vector_snapshot`
closes the client -- kills the child if it will not go -- and deletes the
copy.

**The child** is a plain `python -c` subprocess, never `multiprocessing`:
its spawn start method re-runs the parent's `__main__` in the child, which
failed from a `python -` stdin main and would re-import any script without a
main guard. The child reads the address of a named pipe (`AF_PIPE` on
Windows, `AF_UNIX` elsewhere), a 16-byte key and the parent's pid from its
stdin -- not the command line, which any process can list -- connects back
through `multiprocessing.connection`'s HMAC challenge, takes the parent's
`sys.path` so it imports the same `mindhive`, and serves (`_serve`). A
thread of it waits for the parent to go -- on the parent's process handle
on Windows, on the end of its stdin elsewhere, whose other end the parent
alone holds -- and ends the child, so a parent killed without its `atexit`
-- `taskkill`, a crashed dashboard -- leaves no reader behind. It does not
wait by reading its stdin: measured, with a read pending on that pipe from
another thread -- even a bare `ReadFile` -- `import numpy` in the child
never returned, and took 0.1 s with the thread waiting on an event or on the
parent's handle instead; a synchronous read holds the pipe's file object,
and a DLL start-up that asks after the standard handles waits behind it. It
ends with `os._exit`: tearing down a client whose read failed on a cut
header corrupts the heap (measured, exit 0xC0000374), and in the child that
costs nobody anything.

**Cost**, measured on a 1300-row scratch index: starting a child takes
1.2-1.9 s -- 0.9 s of it `import chromadb` -- once per copy, since
`vector_snapshot` keeps the copy and its client while the index is
unchanged; a read through it then costs about what one in-process does (1.2
ms median against 1.2 ms; the first read of a copy 22 ms against 63 ms), and
a close 0.2 s. Through `hive bench recall-semantic` on a copy of the real
index (9.76 MB, 654 events): warm query median 9.8 ms in-process, 10.9 ms
through the child -- two isolated runs 10.9 and 14.0 ms, so the difference
is inside the run-to-run spread -- and the child's start inside the 8-10 s
the bench times apart for the semantic open, most of it the model load. A
frozen build (`sys.frozen`) has no interpreter to start, and
`vector_snapshot.opener` reads in-process there.
"""
import importlib
import itertools
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from multiprocessing.connection import Listener

__all__ = ["ReaderDied", "ReaderHung", "RemoteClient", "RemoteCollection",
           "open_isolated", "READ_TIMEOUT_S", "START_TIMEOUT_S",
           "CLOSE_TIMEOUT_S", "LAST_WORDS", "REBUILT_FROM"]

#: How long one call may take before the child is killed: a query over 40k
#: rows answers in milliseconds, and the first read of a copy, which replays
#: the rows queued behind the persisted segment, in well under a second.
READ_TIMEOUT_S = 30.0
#: How long the child may take to connect, and then to open the copy: an
#: interpreter start, `import chromadb` and the open, 1.2-1.9 s measured,
#: on a machine that may be busy.
START_TIMEOUT_S = 60.0
#: How long a closing child may take to go on its own before it is killed.
CLOSE_TIMEOUT_S = 2.0
#: Stderr lines of the child kept for a `ReaderDied` message.
LAST_WORDS = 20
#: The packages an error class is imported from to rebuild a relayed error
#: as itself; the child names the module, and any other's is a stand-in.
REBUILT_FROM = ("builtins", "chromadb", "sqlite3", "mindhive")

_SERVE = "mindhive.vector_reader._serve"
_NAMES = itertools.count()


class ReaderDied(RuntimeError):
    """The reading process ended -- died, or was killed -- before it
    answered. `pid` is its pid, `code` its exit status."""

    def __init__(self, message, pid=None, code=None):
        super().__init__(message)
        self.pid, self.code = pid, code


class ReaderHung(RuntimeError):
    """The reading process did not answer, or did not connect, in time, and
    was killed. `pid` is its pid."""

    def __init__(self, message, pid=None):
        super().__init__(message)
        self.pid = pid


# What the child runs, stdlib only until the parent's sys.path arrives. The
# address, key and parent pid come on stdin; the watcher ends the child with
# the parent -- waiting on the parent's process handle on Windows (polling
# the stdin pipe when that handle cannot be opened), on stdin's end
# elsewhere, never by a read left pending on stdin, which stalls the load of
# numpy's extension module -- and `os._exit` skips a teardown chromadb may
# crash in.
_BOOTSTRAP = r'''
import os, sys, threading
from multiprocessing.connection import Client
line = sys.stdin.buffer.readline().decode("ascii").rstrip("\n")
address, key, parent = line.rsplit(" ", 2)
address = address.encode("ascii").decode("unicode_escape")
if sys.platform == "win32":
    import _winapi
    try:
        _parent = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, int(parent))
        def _gone():
            _winapi.WaitForSingleObject(_parent, _winapi.INFINITE)
    except OSError:
        import msvcrt, time
        _stdin = msvcrt.get_osfhandle(0)
        def _gone():
            while True:
                _winapi.PeekNamedPipe(_stdin)
                time.sleep(0.5)
else:
    import select
    def _gone():
        select.select([0], [], [])
def _orphaned():
    try:
        _gone()
    finally:
        os._exit(0)
threading.Thread(target=_orphaned, daemon=True).start()
conn = Client(address, authkey=bytes.fromhex(key))
path, target, copy = conn.recv()
sys.path[:] = path
import importlib
module, _, func = target.rpartition(".")
getattr(importlib.import_module(module), func)(conn, copy)
os._exit(0)
'''


def _relay(exc):
    """What the child sends of an exception: module, name and message."""
    return (type(exc).__module__, type(exc).__name__, str(exc) or repr(exc))


def _rebuild(module, name, text):
    """The exception the child relayed as *module*.*name* with *text*: the
    class itself, when its package is one of `REBUILT_FROM` and it takes a
    message; else a stand-in of the same name and module, which is what
    `vector_errors` reads (a chromadb class by its module, a pyo3 panic by
    its name and module)."""
    if module.split(".", 1)[0] in REBUILT_FROM:
        try:
            cls = getattr(importlib.import_module(module), name)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                return cls(text)
        except Exception:                          # noqa: BLE001 -- a stand-in then
            pass
    return type(name, (RuntimeError,), {"__module__": module})(text)


def _serve(conn, copy):
    """What the child runs: open *copy* with chromadb and answer `(target,
    method, kwargs)` requests until told None or cut off. *target* None is
    the client's own `list_collections` (names) or `get_collection`; else a
    collection's method. Each answer is `("ok", value)` or `("error",
    (module, name, message))`."""
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(copy))
    except BaseException as e:                     # noqa: BLE001 -- relayed
        conn.send(("error", _relay(e)))
        return
    conn.send(("ok", None))
    cols = {}
    while True:
        try:
            req = conn.recv()
        except (EOFError, OSError):
            break
        if req is None:
            break
        target, method, kwargs = req
        try:
            if target is None:
                if method == "list_collections":
                    out = [str(getattr(c, "name", c))
                           for c in client.list_collections()]
                else:
                    cols[kwargs["name"]] = client.get_collection(kwargs["name"])
                    out = kwargs["name"]
            else:
                col = cols.get(target)
                if col is None:
                    col = cols[target] = client.get_collection(target)
                out = getattr(col, method)(**kwargs)
            conn.send(("ok", out))
        except BaseException as e:                 # noqa: BLE001 -- relayed
            try:
                conn.send(("error", _relay(e)))
            except (OSError, ValueError):
                break
    try:
        conn.close()
    except (OSError, ValueError):
        pass


def _address():
    """A fresh pipe address for one child, and its family."""
    tag = f"mindhive-reader-{os.getpid()}-{next(_NAMES)}-{os.urandom(4).hex()}"
    if sys.platform == "win32":
        return rf"\\.\pipe\{tag}", "AF_PIPE"
    return os.path.join(tempfile.gettempdir(), tag + ".sock"), "AF_UNIX"


class RemoteCollection:
    """Collection *name* of a `RemoteClient`: the calls a read path makes,
    each answered by the child."""

    def __init__(self, client, name):
        self._client, self.name = client, name

    def count(self):
        return self._client._call(self.name, "count", {})

    def query(self, **kwargs):
        return self._client._call(self.name, "query", kwargs)

    def get(self, **kwargs):
        return self._client._call(self.name, "get", kwargs)


class RemoteClient:
    """A chromadb client on the copy at *copy*, served by a child process
    running *target* (`_serve`), each call waited for *timeout* seconds
    (`READ_TIMEOUT_S`). `isolated` tells `vector_snapshot` a failed read
    leaves nothing to keep; `pid` is the child's."""
    isolated = True

    def __init__(self, copy, target=_SERVE, timeout=None):
        self.copy = str(copy)
        self._timeout = READ_TIMEOUT_S if timeout is None else timeout
        self._lock = threading.Lock()
        self._dead = None                  # the exit status once it is gone
        self._words = deque(maxlen=LAST_WORDS)
        self._conn = None
        key = os.urandom(16)
        address, family = _address()
        listener = Listener(address, family=family, authkey=key)
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-c", _BOOTSTRAP], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            listener.close()
            raise
        self.pid = self._proc.pid
        self._stderr = threading.Thread(target=self._listen, daemon=True,
                                        name=f"reader-{self.pid}-stderr")
        self._stderr.start()
        try:
            self._conn = self._connect(listener, address, key)
            self._conn.send((list(sys.path), target, self.copy))
            self._answer("open", START_TIMEOUT_S)
        except BaseException:
            self._kill()
            self._close_pipes()
            raise
        finally:
            listener.close()

    def _connect(self, listener, address, key):
        """Hand the child its address, key and this pid and take its call --
        or say why it never came: died first (`ReaderDied`), or too slow
        (`ReaderHung`, killed)."""
        line = (f"{address.encode('unicode_escape').decode('ascii')} "
                f"{key.hex()} {os.getpid()}\n")
        try:
            self._proc.stdin.write(line.encode("ascii"))
            self._proc.stdin.flush()
        except OSError as e:
            raise self._died("start", e, connected=False)
        box = []

        def accept():
            try:
                box.append(listener.accept())
            except Exception as e:                 # noqa: BLE001 -- reported
                box.append(e)

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        deadline = time.monotonic() + START_TIMEOUT_S
        while not box and time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise self._died("start", connected=False)
            thread.join(0.05)
        if not box:
            self._kill()
            raise ReaderHung(f"the reading process {self.pid} did not connect "
                             f"within {START_TIMEOUT_S:g}s and was killed",
                             pid=self.pid)
        if isinstance(box[0], Exception):
            self._kill()
            raise ReaderDied(f"the reading process {self.pid} could not "
                             f"connect: {box[0]}", pid=self.pid,
                             code=self._dead) from box[0]
        return box[0]

    def _listen(self):
        """Keep the child's last stderr lines for a `ReaderDied` message."""
        stream = self._proc.stderr
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._words.append(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _kill(self):
        try:
            self._proc.kill()
        except OSError:
            pass
        try:
            self._dead = self._proc.wait(5)
        except subprocess.TimeoutExpired:
            self._dead = -1

    def _close_pipes(self):
        for pipe in (self._conn, self._proc.stdin):
            try:
                pipe.close()
            except (OSError, ValueError, AttributeError):
                pass

    def _died(self, what, cause=None, *, connected=True):
        """The `ReaderDied` for a child found gone during *what*, once it
        has been reaped and its last words read."""
        try:
            code = self._proc.wait(5)
        except subprocess.TimeoutExpired:
            self._kill()
            code = self._dead
        self._dead = code
        self._stderr.join(1.0)
        words = list(self._words)
        when = f"during the {what}" if connected else "before it connected"
        err = ReaderDied(
            f"the reading process {self.pid} died {when} "
            f"(exit {code & 0xFFFFFFFF:#x})"
            + (f"; its last words: {words[-1][:300]}" if words else ""),
            pid=self.pid, code=code)
        err.__cause__ = cause
        return err

    def _answer(self, what, timeout=None):
        wait = self._timeout if timeout is None else timeout
        try:
            ready = self._conn.poll(wait)
        except (EOFError, OSError) as e:
            raise self._died(what, e)
        if not ready:
            self._kill()
            raise ReaderHung(f"the reading process {self.pid} did not answer "
                             f"the {what} within {wait:g}s and was killed",
                             pid=self.pid)
        try:
            status, payload = self._conn.recv()
        except (EOFError, OSError) as e:
            raise self._died(what, e)
        if status == "error":
            raise _rebuild(*payload)
        return payload

    def _call(self, target, method, kwargs):
        with self._lock:
            if self._dead is not None:
                raise ReaderDied(
                    f"the reading process {self.pid} is gone (exit "
                    f"{self._dead & 0xFFFFFFFF:#x}); the {method} was not "
                    f"asked", pid=self.pid, code=self._dead)
            try:
                self._conn.send((target, method, kwargs))
            except (OSError, ValueError) as e:
                raise self._died(method, e)
            return self._answer(method)

    def list_collections(self):
        """The names of the collections the copy holds."""
        return self._call(None, "list_collections", {})

    def get_collection(self, name):
        """Collection *name*, which the child looks up -- and raises on, as
        chromadb does, when the copy does not hold it."""
        self._call(None, "get_collection", {"name": name})
        return RemoteCollection(self, name)

    def close(self):
        """Ask the child to go, kill it after `CLOSE_TIMEOUT_S`, close the
        pipes, and answer its exit status. A second close answers the same."""
        with self._lock:
            if self._dead is None:
                try:
                    self._conn.send(None)
                except (OSError, ValueError):
                    pass
                try:
                    self._dead = self._proc.wait(CLOSE_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    self._kill()
            self._close_pipes()
            return self._dead


def open_isolated(copy, target=_SERVE, timeout=None):
    """A `RemoteClient` on the copy at *copy*: the opener `vector_snapshot`
    hands `reading` when reads are isolated (`vector_snapshot.opener`)."""
    return RemoteClient(copy, target, timeout)
