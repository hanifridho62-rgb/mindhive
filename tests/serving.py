# tests/serving.py
"""Serve a `socketserver` server on a daemon thread for the length of a test,
and stop it in BOUNDED time.

Every HTTP-level test in this suite used to hand-roll the same four lines:

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield ...
    finally:
        srv.shutdown()

`BaseServer.shutdown()` sets a flag and then waits, with no timeout, on an
Event that only `serve_forever` sets -- in its `finally`, on the way out. The
stdlib's own docstring says so: "This must be called while serve_forever() is
running in another thread, or it will deadlock." A `shutdown()` on a server
whose `serve_forever` is not running (the thread never reached it, or the loop
is wedged) is therefore an unbounded wait inside a fixture teardown: the whole
pytest process stops, no test is reported failed, and nothing names the
fixture. A full default run of this suite hung exactly there once
(`test_mindnet_cat_board.py`'s `server` fixture, the main thread parked in
`shutdown()` for more than seven minutes), and only a py-spy dump said where.

`stop_serving` keeps `shutdown()`'s contract and bounds it: the real
`shutdown()` runs on a helper thread that is joined for `timeout_s`, the
listening socket is closed on both paths, and a stop that did not complete
raises `ServerStopTimeout` naming the server, whether the serving thread is
alive, and the frame it is parked in -- the diagnosis a hang withholds. A
teardown that fails loudly is one the suite can move past.

`serve_in_thread` polls at 0.05 s rather than the stdlib's 0.5 s. `shutdown()`
returns only when the serve loop next wakes from its poll, so every
server-backed test used to pay up to half a second in teardown; across the
hundreds of them that was minutes of a default run spent waiting for a poll to
cycle.
"""
import contextlib
import sys
import threading
import traceback

#: How long `stop_serving` waits for `shutdown()` before declaring the server
#: wedged. The normal cost of a stop is one poll interval; ten seconds is two
#: orders of magnitude of slack for a loaded CI box.
STOP_TIMEOUT_S = 10.0

#: `serve_forever`'s poll interval: the latency of a stop, paid once per
#: server-backed test.
POLL_INTERVAL_S = 0.05


class ServerStopTimeout(RuntimeError):
    """`stop_serving` gave up waiting for the server to stop."""


def start_serving(server, *, poll_interval_s=POLL_INTERVAL_S):
    """Run `server.serve_forever` on a daemon thread and return the thread.

    The thread is named after the address it serves, so a thread dump (py-spy,
    faulthandler) says WHICH server a `serve_forever` frame belongs to; the
    default `Thread-N (serve_forever)` does not."""
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": poll_interval_s},
                              name=f"serve_forever {_address(server)}",
                              daemon=True)
    thread.start()
    return thread


def stop_serving(server, thread=None, *, timeout_s=STOP_TIMEOUT_S):
    """`server.shutdown()` then `server.server_close()`, in at most *timeout_s*.

    *thread* is the serving thread `start_serving` returned, when the caller
    has it: it is joined once the stop completes, and named in the report when
    it should not be alive. Raises `ServerStopTimeout` when the stop did not
    complete -- the guard firing. It is an error on purpose: the fixture's
    contract (a server that serves, then stops) was broken, and the test that
    broke it is the one that should be reported."""
    stopper = threading.Thread(target=_shutdown_and_close, args=(server,),
                               name=f"stop {_address(server)}", daemon=True)
    stopper.start()
    stopper.join(timeout_s)
    if not stopper.is_alive():
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                raise ServerStopTimeout(_report(
                    server, thread, "shutdown() returned but the serving "
                    "thread is still alive"))
        return
    # The guard fires. Close the listener here -- the stopper never got that
    # far -- and leave the stopper parked: a daemon thread waiting on an Event
    # nobody will set costs nothing and cannot hold the process open.
    try:
        server.socket.close()
    except (AttributeError, OSError):
        pass
    raise ServerStopTimeout(_report(
        server, thread, f"shutdown() did not return within {timeout_s:g}s"))


@contextlib.contextmanager
def serve_in_thread(server, *, poll_interval_s=POLL_INTERVAL_S,
                    stop_timeout_s=STOP_TIMEOUT_S):
    """Serve *server* for the block, then stop it -- the fixture form:

        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        with serve_in_thread(srv):
            yield f"http://127.0.0.1:{srv.server_address[1]}"
    """
    thread = start_serving(server, poll_interval_s=poll_interval_s)
    try:
        yield server
    finally:
        stop_serving(server, thread, timeout_s=stop_timeout_s)


def _shutdown_and_close(server):
    try:
        server.shutdown()
    finally:
        server.server_close()


def _address(server) -> str:
    try:
        host, port = server.server_address[:2]
    except (AttributeError, TypeError, ValueError):
        return "?"
    return f"{host}:{port}"


def _report(server, thread, why) -> str:
    """The message the guard raises with: the two facts a hang withholds --
    is the serving thread alive, and where is it parked -- plus any other
    server thread still running, which is how a leaked server gets a name."""
    lines = [f"{type(server).__name__} on {_address(server)}: {why}."]
    if thread is None:
        lines.append("serving thread: none was handed to stop_serving")
    else:
        lines.append(f"serving thread: {thread.name!r} alive={thread.is_alive()}")
        frame = sys._current_frames().get(thread.ident)
        if frame is not None:
            lines.append("parked at:")
            lines.extend(entry.rstrip("\n")
                         for entry in traceback.format_stack(frame, limit=3))
    others = sorted(t.name for t in threading.enumerate()
                    if t is not thread and "serve_forever" in t.name)
    if others:
        lines.append(f"other serve_forever threads alive: {others}")
    return "\n".join(lines)
