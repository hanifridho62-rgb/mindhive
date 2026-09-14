# src/mindhive/dashboard_query.py
"""What the dashboard is allowed to believe about an incoming request.

Every value here arrives from a caller — a query string, a JSON body, a request
header — and each function answers exactly one question about one of them. It is
a leaf module on purpose: `research` and `workspace` dispatch their own dashboard
routes and need the same coercions, and neither may import the dashboard package
back. Nothing in here touches the filesystem or the store.

The three checks were written after each was reproduced against a live server on
an ephemeral port; the docstrings say what was observed rather than what was
feared.

Its one import from this package is `paths`, itself dependency-free, so the leaf
property holds: `is_safe_project` asks `paths.namespace_name`, a pure function of
the string, rather than keeping a second copy of its rule.
"""
from __future__ import annotations

import re

from . import paths

__all__ = ["BadRequest", "qs_int", "body_num", "is_safe_project",
           "host_is_local"]


class BadRequest(ValueError):
    """A request the CALLER framed wrongly — answered 400, by name.

    Subclasses ValueError so a handler that already guards a conversion keeps
    working, but it is caught before the routing catch-all: a 500 asserts the
    server faulted, and the generic handler quotes the exception text back, which
    for a bare `int()` failure means handing the caller a fragment of our
    internals as if it were their diagnosis.
    """


def qs_int(qs, name, default):
    """One integer query parameter, the *default* when absent, else BadRequest.

    `int((qs.get("limit") or ["200"])[0])` was the shape at every call site, and
    `?limit=abc` therefore produced, verbatim on the wire:

        500 {"ok": false, "error": "invalid literal for int() with base 10: 'abc'"}

    Same class of defect the Content-Length path was already fixed for, and the
    same answer: refuse it by the name the caller used.
    """
    return _coerced((qs.get(name) or [None])[0], default, name, int)


def body_num(body, name, default, kind=int):
    """One number off a JSON body, the *default* when absent, else BadRequest.

    The POST side kept the shape the GET side had already been fixed for. Both
    of these were answered by a live server, verbatim on the wire:

        POST /api/plugins/autoscan {"top": "abc"}
          500 {"ok": false, "error": "invalid literal for int() with base 10: 'abc'"}
        POST /api/plugins/autoscan {"max_age_hours": "soon"}
          500 {"ok": false, "error": "could not convert string to float: 'soon'"}

    `kind` is a parameter rather than there being a second near-identical
    function, because one of those two fields is a float and the pair would
    drift.
    """
    return _coerced(body.get(name), default, name, kind)


def _coerced(raw, default, name, kind):
    """The one conversion both readers share: absent or empty is the caller
    declining to say, which is what the default is for; anything that will not
    convert is refused by the name the caller used, never by the interpreter's
    sentence about it."""
    if raw is None or raw == "":
        return default
    try:
        return kind(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"invalid {name}") from None


def is_safe_project(project) -> bool:
    """True when *project* names, verbatim, the directory a read of it lands on:
    exactly the names `paths.namespace_name` keeps.

    `paths.project_dir` was `store_home() / (project or detect_project())` with no
    sanitisation, so the string arriving on the wire WAS a path component. A
    request carrying a relative path escaped the store completely: against a live
    server, `GET /api/state?project=<relpath-to-a-temp-dir>` answered 200 with the
    contents of a file created outside the store home. `paths.namespace_name` now
    quarantines such a name inside the store, which closes the escape but would
    answer a hostile request with an empty namespace and a 200; this guard stays
    so the HTTP surface refuses it out loud, as `cliparse.project_arg` does for
    the CLI.

    It ASKS THE SINK, for `cliparse.project_arg`'s reason. It used to be a shape
    test of its own -- no separator, no drive colon, no wildcard, no control
    character -- and the sink outgrew it: `проект`, `my project` and
    `unsafe-<16 hex>` passed here, as did `abc.` and `CON` on Windows, while
    `namespace_name` sent each to a quarantine slot, so each was answered 200
    with a DIFFERENT directory's contents (usually an empty one) under the name
    the caller asked for. A store-root file name (`LIVE_LOCK`, `autosync.json`)
    is refused the same way. The HIVE MIND tokens are recognised by `is_all`
    before this is asked, so a project reaching a path join is never one of
    them. A check that asks the function deciding where the read lands cannot
    fall behind it.
    """
    return isinstance(project, str) and paths.namespace_name(project) == project


# Loopback by NAME. An attacker's domain can resolve to 127.0.0.1; it can never
# BE one of these strings, which is the whole of why an allow-list works here.
_LOCAL_HOSTS = {"localhost", "::1", "[::1]"}
_LOOPBACK_IP = re.compile(r"^127(?:\.\d{1,3}){3}$")


def host_is_local(host_header) -> bool:
    """True when the Host header names this machine's loopback interface.

    The server binds 127.0.0.1, so only a local client can connect — but a
    browser on this machine will happily connect to a name the ATTACKER controls
    that resolves to 127.0.0.1 (DNS rebinding), and the attacker's page is then
    same-origin with the dashboard. `GET /` is unauthenticated by design and the
    page it serves carries the API token in its source, so that single fetch
    hands an arbitrary web page full read/write access to the ledger. Observed on
    a live server: `Host: evil.example.com` was answered 200, token included.

    An ABSENT or empty Host is allowed. HTTP/1.0 clients do not send one, several
    of this repo's own raw-socket tests predate the header, and a request with no
    Host is not a request a rebound browser can make — `fetch` always sends it.
    """
    if host_header is None:
        return True
    host = host_header.strip()
    if not host:
        return True
    if host.startswith("["):                       # [::1]:54461 — bracketed v6
        host = host.split("]", 1)[0] + "]"
    elif host.count(":") == 1:                     # 127.0.0.1:54461
        host = host.rsplit(":", 1)[0]
    # More than one colon and no brackets is a bare IPv6 literal, which cannot
    # carry a port — splitting on the last colon would turn `::1` into `::`.
    host = host.lower()
    return host in _LOCAL_HOSTS or bool(_LOOPBACK_IP.match(host))
