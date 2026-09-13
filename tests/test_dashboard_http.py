# tests/test_dashboard_http.py
"""The connection layer: keep-alive (T4) and the two obligations it creates.

The dashboard issues dozens of small JSON requests per poll cycle, and two
pollers run on timers, so HTTP/1.0 spent a TCP handshake per request. HTTP/1.1
removes that — and in exchange makes the connection STATEFUL: responses are now
framed one after another on the same socket, so anything that leaves unread bytes
behind (T2) or writes twice for one request (T3) no longer produces an error. It
produces answers attached to the wrong questions, which is worse than an error
because nothing reports it.

Every test here therefore speaks raw HTTP over a raw socket and asserts what came
back BYTE for byte on ONE connection. `urllib` cannot express any of these cases:
it opens a fresh connection per request, hides the status line, and would parse a
desynchronised stream into plausible-looking objects — exactly the failure these
tests exist to catch.

Store isolation comes from the autouse `isolate_store` fixture in
tests/conftest.py, like every other dashboard test.
"""
import collections
import json
import socket
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from mindhive import dashboard, events, paths

TOKEN = "test-token-123"


def _module_memos():
    """Every module-level memo, whatever they happen to be.

    The caches are module-level and so outlive a test, and `/api/projects` — the
    route these tests poll — reads through one of them. Enumerated rather than
    listed by name because this file's subject is the CONNECTION layer: it should
    not have to be edited when a cache is added, and naming one that does not
    exist would turn a behavioural failure into a setup error."""
    return [value for name, value in vars(dashboard).items()
            if name.endswith("_MEMO")
            and isinstance(value, collections.OrderedDict)]


@pytest.fixture(autouse=True)
def clean_module_state():
    for memo in _module_memos():
        memo.clear()
    yield
    for memo in _module_memos():
        memo.clear()


@pytest.fixture
def server():
    """A threaded dashboard on an ephemeral port, yielding the pieces these
    tests need to reach: address, token, and the handler CLASS.

    The class matters because `make_handler` mints a fresh one per call, so its
    route table and its byte limits can be adjusted for one test without leaking
    into the next — and because the limits under test are class attributes."""
    handler = dashboard.make_handler(TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address, TOKEN, handler
    finally:
        httpd.shutdown()


def _connect(address, timeout=10.0):
    sock = socket.create_connection(address, timeout=timeout)
    sock.settimeout(timeout)
    return sock


def _request_bytes(method, path, body=b"", content_length=None,
                   host="127.0.0.1", extra=()):
    """One raw request. `content_length` is separate from `len(body)` on purpose:
    claiming a length and then sending less (or nothing) is precisely how a
    client hangs up mid-body, and that case has its own branch in `_drain`.

    `host` and `extra` default to exactly what every earlier caller sent, so the
    connection tests below are byte-identical to before they existed. The header
    block is latin-1, not ascii: a Host or a token header is where a non-ASCII
    byte reaches the server, and encoding it as ascii here would make the TEST
    raise instead of the request being sent."""
    declared = len(body) if content_length is None else content_length
    head = f"{method} {path} HTTP/1.1\r\n"
    if host is not None:
        head += f"Host: {host}\r\n"
    for name, value in extra:
        head += f"{name}: {value}\r\n"
    head += (f"Content-Type: application/json\r\n"
             f"Content-Length: {declared}\r\n"
             f"\r\n")
    return head.encode("latin-1") + body


def _read_response(sock):
    """One response: (status line, headers dict, body). Framing is read the way a
    keep-alive client reads it — headers, then exactly Content-Length bytes — so a
    server that lies about the length shows up here as a hang or a short body
    rather than being silently tolerated.

    Returns None when the peer closed before a complete header block, which is a
    legitimate answer for the tests that assert a connection was dropped."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower().decode("latin-1")] = value.strip().decode("latin-1")
    length = int(headers.get("content-length", len(rest)))
    while len(rest) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        rest += chunk
    return lines[0], headers, rest[:length]


def _read_until_closed(sock):
    """(everything the server sent, whether it closed). The T3 assertion is about
    how many responses were FRAMED on the wire, so it has to see the whole stream
    rather than a parsed object — and the T2 assertions turn on whether the server
    hung up or is still waiting, which is the difference the flag carries.

    A reset counts as closed. On Windows, closing a socket that still has unread
    bytes inbound aborts it (WinError 10053) instead of delivering a clean FIN,
    and any bytes already sent may be discarded with it — so a test that demands a
    graceful EOF here would be asserting a platform detail, not our behaviour."""
    out = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, TimeoutError):
            return out, False
        except (ConnectionAbortedError, ConnectionResetError):
            return out, True
        if not chunk:
            return out, True
        out += chunk


def _send_or_already_gone(sock, data) -> bool:
    """Send *data*; True when it went out, False when the peer had already gone.

    The write side of the reset `_read_until_closed` documents, and the reason
    T2 was intermittent — it failed roughly one run in five, locally and on CI,
    for the behaviour it exists to prove. Refusing an oversized body without
    draining it leaves unread bytes in the server's receive buffer, so its close
    aborts the connection; a client still writing that megabyte, or writing
    anything after it, then dies with WinError 10053 here (EPIPE/ECONNRESET on
    POSIX). That is the server being CORRECT a few microseconds earlier than
    usual, not a failure, and every assertion downstream holds a fortiori: a
    request that could not even be sent was certainly not answered.

    Nothing weakens. `_read_until_closed` still sees the whole stream, and the
    caller still requires that at most one response was framed on it."""
    try:
        sock.sendall(data)
        return True
    except OSError:
        return False


def _seed():
    events.append_event("user_prompt", {"text": "do the thing"},
                        session_id="sess-1", project="test")


PROJECTS = f"/api/projects?project=test&token={TOKEN}"


# --- T4: HTTP/1.1, and one connection serving many requests -----------------

def test_the_server_speaks_http_1_1(server):
    address, _token, _handler = server
    _seed()
    sock = _connect(address)
    try:
        sock.sendall(_request_bytes("GET", PROJECTS))
        status, headers, body = _read_response(sock)
        assert status.startswith(b"HTTP/1.1 200"), status
        # An exact Content-Length is the PREREQUISITE for keep-alive, not a
        # nicety: without it the client cannot tell where this body ends and the
        # next response begins.
        assert "content-length" in headers
        assert len(body) == int(headers["content-length"])
        assert json.loads(body.decode("utf-8"))["projects"]
    finally:
        sock.close()


def test_one_connection_answers_two_requests_in_order(server):
    """The point of the change. Under HTTP/1.0 the second request on this socket
    reads EOF, because the server closed after the first response."""
    address, _token, _handler = server
    _seed()
    sock = _connect(address)
    try:
        for expected in (1, 2):
            sock.sendall(_request_bytes("GET", PROJECTS))
            answer = _read_response(sock)
            assert answer is not None, \
                f"the connection was closed before request {expected} was answered"
            status, _headers, body = answer
            assert status.startswith(b"HTTP/1.1 200")
            assert json.loads(body.decode("utf-8"))["current"] == "test"
    finally:
        sock.close()


def test_an_idle_connection_is_released_instead_of_pinning_a_thread(server):
    """DISCRIMINATING. The cost keep-alive adds: the base class's `timeout` is
    None, so `rfile.readline()` on a connection a browser is holding open blocks
    FOREVER and that worker thread never returns to the pool. Two dashboard tabs
    left open would each hold one for the life of the tab.

    Both halves are needed, and the first is the one that fails against a server
    that never set a timeout: assigning the attribute here would make the
    STDLIB's plumbing honour it whatever we ship, so the shipped value has to be
    asserted before it is lowered. Lowering it then proves the plumbing works
    end-to-end without the test waiting out the real 30 seconds."""
    address, _token, handler = server
    assert isinstance(handler.timeout, (int, float)) and handler.timeout > 0, (
        f"the handler ships timeout={handler.timeout!r}, so an idle keep-alive "
        f"connection blocks its worker thread forever")

    handler.timeout = 0.3
    sock = _connect(address, timeout=5.0)
    try:
        assert sock.recv(4096) == b"", \
            "an idle connection was still open past the handler's timeout"
    finally:
        sock.close()


def test_a_connection_is_not_dropped_while_the_timeout_has_not_passed(server):
    """CANARY for the test above, which passes trivially against a server that
    hangs up on everyone. Same idle socket, a timeout longer than the wait: it
    must still be open."""
    address, _token, handler = server
    handler.timeout = 30
    sock = _connect(address, timeout=1.0)
    try:
        with pytest.raises((socket.timeout, TimeoutError)):
            sock.recv(4096)
    finally:
        sock.close()


# --- T2: a body we refuse to read is a connection we stop reusing -----------

def test_a_refused_body_never_becomes_the_next_request(server):
    """DISCRIMINATING, and the reason refusal now closes.

    The refused megabyte is still sitting in the socket. Keeping the connection
    would have the server's next `readline()` read the middle of that junk as a
    request line, so the refusal stops being a refusal of ONE request: every
    request after it is answered off by one, or answered with a parse error for a
    call the client never made.

    The valid GET is queued BEHIND the refusal before anything is read, which is
    both what a real client does and the only ordering in which the junk is still
    buffered when the next request line would be parsed. Its correct fate is to
    be answered NOT AT ALL — the connection ends, and the client reconnects,
    which keep-alive already assumes can happen. What must never appear is a
    second framed response.

    The valid request FIRST is what makes this fail against a pre-keep-alive
    server: there, the connection is already gone before the refusal is sent, so
    there is no desync to have."""
    address, _token, handler = server
    _seed()
    sock = _connect(address, timeout=20.0)
    try:
        sock.sendall(_request_bytes("GET", PROJECTS))
        first = _read_response(sock)
        assert first is not None and first[0].startswith(b"HTTP/1.1 200"), \
            "the connection is not reusable at all, so T2 cannot be under test"

        # Neither write may assume the peer is still there: the refusal closes
        # without draining, and that close aborts a connection the client is
        # still writing to. See `_send_or_already_gone`.
        _send_or_already_gone(sock, _request_bytes(
            "POST", f"/api/append?token={TOKEN}",
            body=b"x" * (handler.MAX_BODY_BYTES + 1)))
        queued = _send_or_already_gone(sock, _request_bytes("GET", PROJECTS))
        stream, closed = _read_until_closed(sock)

        assert closed, "a connection with an unread body on it was kept for reuse"
        assert stream.count(b"HTTP/1.1 ") <= 1, (
            f"{stream.count(b'HTTP/1.1 ')} responses after the refusal — the "
            f"junk body was read as a request line: {stream[:300]!r} "
            f"(the GET behind the refusal was {'sent' if queued else 'never sent'})")
        if stream:
            # The abort discards whatever had not left the wire, and the
            # refusal's 48-byte body is small enough to be exactly what goes —
            # `_read_until_closed` says as much for the EOF and the same is
            # true of the payload. So the STATUS LINE is what must always
            # hold; the reason phrase is asserted whenever it survived. This
            # was the second half of T2's intermittency: a run where the
            # headers arrived and the body did not read as "the server gave
            # the wrong reason" when it had given the right one.
            head, _, body = stream.partition(b"\r\n\r\n")
            assert head.startswith(b"HTTP/1.1 500"), stream[:300]
            if body:
                assert b"request body too large" in body, stream[:300]
            assert b'"projects"' not in stream, \
                "the GET behind the refusal was answered from a desynced stream"
    finally:
        sock.close()


def test_a_declared_length_the_client_never_sends_does_not_pin_a_thread(server):
    """REGRESSION GUARD (passes against the baseline too, and says so).

    The first cut of T2 drained the refused body to keep the connection framed.
    That is an untrusted read: this request declares more than 4 MiB and sends 32
    bytes, so the drain waits for bytes that never arrive and holds a worker
    thread until the handler timeout — the exact cost MAX_BODY_BYTES exists to
    refuse, reintroduced by the fix for a different problem. No byte cap closes
    it, because the bytes never come.

    A server that refuses without reading answers and hangs up at once, which is
    what `closed` records here; the drained version shows up as a client timeout
    with the connection still open."""
    address, _token, handler = server
    sock = _connect(address, timeout=5.0)
    try:
        sock.sendall(_request_bytes(
            "POST", f"/api/append?token={TOKEN}", body=b"x" * 32,
            content_length=handler.MAX_BODY_BYTES * 4))
        _stream, closed = _read_until_closed(sock)
        assert closed, (
            "the server is still waiting for a body it had already decided to "
            "refuse — one local process can hold a worker thread on demand")
    finally:
        sock.close()


def test_an_unparseable_content_length_is_refused_by_name(server):
    """A length we cannot parse is a body whose END we cannot find, so the
    connection cannot be kept framed either. DISCRIMINATING on the message:
    `int("banana")` used to escape `_body` as a bare ValueError, so the client was
    told `invalid literal for int() with base 10: 'banana'` — our own internals
    quoted back as if they were the caller's problem."""
    address, _token, _handler = server
    sock = _connect(address, timeout=10.0)
    try:
        sock.sendall(b"POST /api/append?token=" + TOKEN.encode() +
                     b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Content-Length: banana\r\n\r\n")
        stream, closed = _read_until_closed(sock)
        assert closed, "a connection whose framing is unrecoverable stayed open"
        assert stream.count(b"HTTP/1.1 ") == 1, stream[:200]
        assert b"invalid Content-Length" in stream, stream[:300]
    finally:
        sock.close()


# --- T3: exactly one response per request -----------------------------------

def _writes_then_raises(handler, proj, am, qs):
    handler._json({"ok": True, "written": "the real answer"})
    raise RuntimeError("boom AFTER the headers went out")


def _raises_before_writing(handler, proj, am, qs):
    raise RuntimeError("boom BEFORE anything was written")


def test_a_route_that_raises_after_writing_frames_only_one_response(server):
    """DISCRIMINATING. A second full response for one request is not a harmless
    duplicate under keep-alive: it is FRAMED as the answer to the NEXT request,
    so from there on the client reads our error JSON as the body of something it
    asked for later. `_fail` therefore checks whether bytes are already on the
    wire and, if so, stops talking and lets the connection close.

    The route is injected rather than found: this is a property of the error path
    itself, and pinning it to whichever handler happens to write-then-fail today
    would test that handler, not the rule."""
    address, _token, handler = server
    handler._GET_ROUTES["/api/test_boom_late"] = _writes_then_raises
    sock = _connect(address, timeout=10.0)
    try:
        sock.sendall(_request_bytes("GET", f"/api/test_boom_late?token={TOKEN}"))
        stream, closed = _read_until_closed(sock)
        assert closed, \
            "a half-written response left the connection open and mis-framed"
        assert stream.count(b"HTTP/1.1 ") == 1, (
            f"{stream.count(b'HTTP/1.1 ')} responses framed for one request; the "
            f"second would be read as the answer to the next one: {stream[:300]!r}")
        assert b'"written": "the real answer"' in stream
        assert b"boom AFTER" not in stream, \
            "the error was appended behind a response that had already shipped"
    finally:
        sock.close()


def test_a_route_that_raises_before_writing_still_reports_500(server):
    """CANARY for the test above: a `_fail` that always went silent would satisfy
    "exactly one response" by never answering at all. A fault with nothing yet on
    the wire must still produce the 500 that tells monitoring a server fault
    happened — and, having framed exactly one complete response, must leave the
    connection usable."""
    address, _token, handler = server
    _seed()
    handler._GET_ROUTES["/api/test_boom_early"] = _raises_before_writing
    sock = _connect(address, timeout=10.0)
    try:
        sock.sendall(_request_bytes("GET", f"/api/test_boom_early?token={TOKEN}"))
        status, _headers, body = _read_response(sock)
        assert status.startswith(b"HTTP/1.1 500"), status
        assert json.loads(body.decode("utf-8")) == {
            "ok": False, "error": "boom BEFORE anything was written"}

        sock.sendall(_request_bytes("GET", PROJECTS))
        answer = _read_response(sock)
        assert answer is not None, \
            "a clean 500 dropped the connection, so keep-alive buys nothing here"
        assert answer[0].startswith(b"HTTP/1.1 200")
    finally:
        sock.close()


def test_the_responded_flag_is_per_request_not_per_connection(server):
    """The flag lives in `handle_one_request`, not `__init__`, because keep-alive
    reuses ONE handler instance for every request on the connection. Setting it
    once per connection would make it mean "this connection has ever answered" —
    and would silence every error response after the first, on a socket a browser
    holds open for the life of the tab."""
    address, _token, handler = server
    handler._GET_ROUTES["/api/test_boom_early"] = _raises_before_writing
    sock = _connect(address, timeout=10.0)
    try:
        for attempt in (1, 2, 3):
            sock.sendall(_request_bytes(
                "GET", f"/api/test_boom_early?token={TOKEN}"))
            answer = _read_response(sock)
            assert answer is not None, \
                f"the connection was dropped before fault {attempt} was reported"
            assert answer[0].startswith(b"HTTP/1.1 500"), (
                f"fault {attempt} on this connection was not reported: "
                f"{answer[0]!r} — the flag is per-connection, not per-request")
    finally:
        sock.close()


# --- the request boundary: what the server believes about a caller ----------
# Each of the four below was reproduced against a live server on an ephemeral
# port before it was written, and each fix was re-verified the same way. They
# live here rather than beside the route tests because all four are answered
# BEFORE dispatch — the subject is the boundary, not the route.


def _one_exchange(address, request, timeout=10.0):
    """Send one request on a fresh connection and read one response.

    Returns None when the server closed without framing a complete response —
    which is a real outcome here, not a test bug, and the one the non-ASCII
    token case is about.
    """
    sock = _connect(address, timeout=timeout)
    try:
        sock.sendall(request)
        return _read_response(sock)
    except (ConnectionAbortedError, ConnectionResetError):
        return None
    finally:
        sock.close()


@pytest.mark.parametrize("where", ["query", "header"])
def test_a_non_ascii_token_is_answered_403_not_dropped(server, where):
    """DISCRIMINATING on there being an ANSWER AT ALL, not on the status.

    `secrets.compare_digest` raises TypeError the moment either str operand
    carries a non-ASCII character, and the token check ran OUTSIDE do_GET's try,
    so `?token=caf%C3%A9` produced zero bytes on the wire and a traceback on the
    console. A test asserting only "not 200" would have passed against that
    server, because a dropped connection is also not 200 — so the assertion that
    carries the regression is `resp is not None`.

    Both doors are covered: the query parameter and the `X-Hive-Token` header
    reach the same comparison by different routes.
    """
    address, _token, _handler = server
    if where == "query":
        request = _request_bytes("GET", "/api/projects?token=caf%C3%A9")
    else:
        request = _request_bytes("GET", "/api/projects",
                                 extra=[("X-Hive-Token", "caf\u00c3\u00a9")])

    resp = _one_exchange(address, request)

    assert resp is not None, (
        "the server closed without answering: a token it cannot compare is "
        "still a token it must REFUSE")
    status, _headers, body = resp
    assert status.startswith(b"HTTP/1.1 403"), status
    assert b"forbidden" in body, body


def test_a_foreign_host_never_reaches_the_page_that_carries_the_token(server):
    """DNS rebinding, and why binding 127.0.0.1 is not the whole answer.

    A page served from a domain the attacker owns, resolving to 127.0.0.1, is a
    local client and is same-origin with us. `GET /` needs no token and the HTML
    it returns embeds one, so a single rebound `fetch` is full ledger access.

    CANARY, and it is the load-bearing half: the identical request with a
    loopback Host must still return the page WITH the token in it. Without that,
    this test would pass just as happily against a server that had stopped
    serving `/` at all.
    """
    address, token, _handler = server

    status, _headers, body = _one_exchange(
        address, _request_bytes("GET", "/", host="evil.example.com"))
    assert status.startswith(b"HTTP/1.1 403"), status
    assert token.encode() not in body, "the token was handed to a foreign origin"

    status, _headers, body = _one_exchange(
        address, _request_bytes("GET", "/", host="127.0.0.1"))
    assert status.startswith(b"HTTP/1.1 200"), status
    assert token.encode() in body, (
        "canary: the loopback request did not carry the token either, so the "
        "403 above proves nothing about the Host check")


def test_a_project_that_escapes_the_store_is_refused_by_name(server, tmp_path,
                                                             monkeypatch):
    """`project=` names a directory under the store, so `../x` is a traversal.

    Two layers stand in front of it, and the test holds both. The router
    refuses the name (`is_safe_project`); behind it `paths.project_dir`
    quarantines whatever still reaches a join, so with the router's check gone
    the read stays inside the store.

    CANARY by MUTATION, at both module seams: with the check forced open the
    planted file must still be unreachable, and with `paths.namespace_name`
    made the identity as well, the very same request must return it. A
    traversal test whose fixture does not actually traverse proves nothing and
    reads exactly like one that does.
    """
    address, token, _handler = server
    outside = tmp_path.parent / f"escaped-{tmp_path.name}"
    (outside / "ns").mkdir(parents=True, exist_ok=True)
    (outside / "ns" / "STATE.md").write_text("CANARY-OUTSIDE-THE-STORE",
                                             encoding="utf-8")
    # tmp_path IS the store home (see the autouse isolate_store fixture).
    escape = f"../{outside.name}/ns"
    path = f"/api/state?token={token}&project={quote(escape, safe='')}"

    status, _headers, body = _one_exchange(address, _request_bytes("GET", path))
    assert status.startswith(b"HTTP/1.1 400"), status
    assert b"invalid project" in body, body
    assert b"CANARY-OUTSIDE-THE-STORE" not in body

    monkeypatch.setattr(dashboard, "is_safe_project", lambda p: True)
    status, _headers, body = _one_exchange(address, _request_bytes("GET", path))
    assert b"CANARY-OUTSIDE-THE-STORE" not in body, (
        "the sink's quarantine did not hold with the router's check removed: "
        f"got {body[:200]!r}")

    for memo in _module_memos():
        memo.clear()
    monkeypatch.setattr(paths, "namespace_name",
                        lambda project, os_name=None: project)
    status, _headers, body = _one_exchange(address, _request_bytes("GET", path))
    assert b"CANARY-OUTSIDE-THE-STORE" in body, (
        "canary: with both layers removed the request read nothing outside the "
        f"store, so the refusals above are not evidence: got {body[:200]!r}")


def test_an_unparseable_limit_is_refused_by_name_not_by_traceback(server):
    """DISCRIMINATING on the MESSAGE and the CODE together.

    `int((qs.get("limit") or ["200"])[0])` let a caller's typo out as
    `500 {"ok": false, "error": "invalid literal for int() with base 10: 'abc'"}`
    — the interpreter's sentence, over a status that claims the SERVER faulted.
    Same defect the Content-Length path was fixed for, same answer.

    CANARY: a parseable limit on the same route must still answer 200, so this
    cannot pass against a server that refuses every `limit`.
    """
    address, token, _handler = server

    status, _headers, body = _one_exchange(
        address, _request_bytes("GET", f"/api/memory?token={token}&limit=abc"))
    assert status.startswith(b"HTTP/1.1 400"), status
    assert json.loads(body.decode("utf-8")) == {"ok": False,
                                                "error": "invalid limit"}
    assert b"literal" not in body, body

    status, _headers, _body = _one_exchange(
        address, _request_bytes("GET", f"/api/memory?token={token}&limit=5"))
    assert status.startswith(b"HTTP/1.1 200"), (
        "canary: a valid limit was refused too, so the 400 above says nothing "
        "about the unparseable one")


def test_a_non_numeric_scan_parameter_is_refused_by_name_not_by_traceback(server):
    """The POST twin of the case above, DISCRIMINATING on WHICH field is named.

    The GET side was fixed and the POST side kept the shape. Both answers below
    were read off a live server before this test existed:

        {"top": "abc"}            500 invalid literal for int() with base 10: 'abc'
        {"max_age_hours": "soon"} 500 could not convert string to float: 'soon'

    CANARY, and it is the point of the second case: it sends a VALID `top`
    beside the bad `max_age_hours`. An implementation that answered 400 to
    anything wrong on this route would satisfy a test that only read the status
    code; it cannot satisfy one that reads the field name back.
    """
    address, token, _handler = server

    for payload, named in (({"project": "test", "top": "abc"}, "top"),
                           ({"project": "test", "top": 5,
                             "max_age_hours": "soon"}, "max_age_hours")):
        body = json.dumps(payload).encode("utf-8")
        status, _headers, out = _one_exchange(
            address, _request_bytes("POST", f"/api/plugins/autoscan?token={token}",
                                    body))
        assert status.startswith(b"HTTP/1.1 400"), (payload, status)
        assert json.loads(out.decode("utf-8")) == {"ok": False,
                                                   "error": f"invalid {named}"}
        assert b"literal" not in out and b"convert" not in out, out


# --- a project name the sink would store under another name ------------------

@pytest.mark.parametrize("project", ["unsafe-0000000000000000", "LIVE_LOCK",
                                     "my project"])
def test_a_project_the_sink_would_rename_is_refused_not_answered(server, project):
    """DISCRIMINATING. Each of these passed the old shape check and reached
    `paths.namespace_name`, which stores it under a quarantine slot -- so the
    route answered 200 with ANOTHER directory's contents (usually none) under
    the name the caller asked for. Refused by name now, like a traversal.

    CANARY: the same route with a usable project still answers 200."""
    address, token, _handler = server
    path = (f"/api/memory?token={token}&project={quote(project, safe='')}"
            "&limit=5")
    status, _headers, body = _one_exchange(address, _request_bytes("GET", path))
    assert status.startswith(b"HTTP/1.1 400"), (project, status)
    assert json.loads(body.decode("utf-8")) == {"ok": False,
                                                "error": "invalid project"}

    status, _headers, _body = _one_exchange(address, _request_bytes(
        "GET", f"/api/memory?token={token}&project=test&limit=5"))
    assert status.startswith(b"HTTP/1.1 200"), status


@pytest.mark.parametrize("session_project", ["../../x",
                                             "unsafe-0000000000000000"])
def test_a_task_cards_session_project_is_refused_by_name(server,
                                                         session_project):
    """DISCRIMINATING. `session_project` picks the ledger `/api/task` folds, and
    it was the one project value on that route nothing was asked about: the
    sink quarantined it, and the route answered 200 from another directory.

    CANARY: a usable `session_project` still answers 200."""
    address, token, _handler = server
    path = (f"/api/task?token={token}&session=s1"
            f"&session_project={quote(session_project, safe='')}")
    status, _headers, body = _one_exchange(address, _request_bytes("GET", path))
    assert status.startswith(b"HTTP/1.1 400"), (session_project, status)
    assert json.loads(body.decode("utf-8")) == {
        "ok": False, "error": "invalid session_project"}

    status, _headers, _body = _one_exchange(address, _request_bytes(
        "GET", f"/api/task?token={token}&session=s1&session_project=test"))
    assert status.startswith(b"HTTP/1.1 200"), status
