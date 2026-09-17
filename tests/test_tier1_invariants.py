# tests/test_tier1_invariants.py
"""tests/test_tier1_invariants.py — Deterministic Verification for Tier 1
Foundation Bugs.

## What a Tier-1 invariant is

A Tier-1 invariant is a property of the FOUNDATION -- the ledger, its
namespace on disk, the gate in front of a merge -- that every later phase
builds on and that no later phase can repair if it is wrong. Each one is
pinned here as a deterministic, isolated test: no network, no clock
dependence, a throwaway store per test (`tests/conftest.py`'s autouse
`isolate_store` fixture sets `MINDHIVE_HOME` and `MINDHIVE_PROJECT=test`).

## This file lands in two phases

**P0 (this section): the `session_id` contract of Bug 2.** A `session_id` is
DATA in the row and a FILENAME on disk, and the second use is what
`--session ../../x` exploited. The repo already answered the hook path with a
silent quarantine (`events.segment_name`, pinned by
`tests/test_events_segment_path.py`); Bug 2 adds the LOUD half:

* `events.validate_session_id` raises `ValueError` whose message starts
  `Format session_id '<value>' tidak valid: ` for anything that is not its own
  segment name on EVERY platform -- traversal, separators, Windows-illegal
  characters, control characters, the reserved `unsafe-` prefix in any case,
  and Windows device names, refused on POSIX too because a store moves between
  machines. A valid id comes back VERBATIM: no strip, no `"unknown"`
  substitute.
* `events.SESSION_ID_REGEX` is the ANCHORED statement of the whitelist,
  beside the unanchored `SESSION_ID_RE` that the hooks copy -- which stays
  byte-for-byte untouched. The validator must use `fullmatch`, never `match`:
  Python's `$` matches before a trailing newline, the exact hole `taskview`
  once had.
* `events.append_event(..., strict_session=True)` validates the RESOLVED id
  before the events directory is made and before the append lock is taken, so
  a refusal writes nothing at all. The default, `strict_session=False`, keeps
  quarantining -- `post_tool_use` fires on every tool call and a hook that can
  throw is a hook that breaks the session.
* `cliparse.session_id_arg` delegates to the validator and composes both
  texts: the legacy `invalid session id` phrase two existing test files pin,
  and the `ValueError` text so an operator sees the rule they broke.

**P2 (appended later): the SuiteGate half.** The `SuiteGate` /
`"PELANGGARAN GATE"` tests from the same harness sketch belong to the phase
that creates `mindhive.orchestrator.gate`; they are deliberately NOT written
here, and that module is not imported. Do not add them to P0.

## The canaries

`test_valid_session_id_accepted` and
`test_strict_append_of_a_valid_id_lands_at_its_own_segment` are the
assertions a validator that refused EVERYTHING would fail; every refusal test
in this file passes against such a validator, and it would break the CLI.
`test_the_regex_alone_is_not_sufficient` is the assertion a validator that
checked ONLY the regex would fail: `unsafe-<16 hex>` satisfies the whitelist
and is still not its own segment.
"""
import argparse
import inspect

import pytest

from mindhive import cliparse, events, paths

B = chr(92)          # a literal backslash, kept out of the string literals below

# Every message the validator raises must satisfy THIS search, verbatim from
# the harness sketch: `pytest.raises(match=...)` is `re.search`, and `.*` does
# not cross a raw newline -- which is why the value is rendered with `!r`.
REFUSAL = "Format session_id.*tidak valid"

# The attack vectors, one row per way an id can stop being a single safe path
# component. Order is the plan's list, then the harness sketch's own four, then
# the canaries the review added.
ATTACK_VECTORS = [
    "../etc",                       # POSIX traversal
    "../x",
    ".." + B + "x",                 # Windows traversal
    "a/b",                          # bare separators
    "a" + B + "b",
    "C:x",                          # drive letter / ADS separator
    ".hidden",                      # leading dot: hidden, and `.`/`..` shaped
    "-lead",                        # shaped like an option
    "CON",                          # a Windows device name
    "unsafe-abc",                   # the reserved quarantine prefix
    "x" * 129,                      # one over the 128-char bound
    "",                             # empty
    "a" + chr(10) + "b",            # embedded newline
    "a*b", "a?b",                   # wildcards the Windows kernel refuses
    'a"b', "a<b", "a|b",
    "a" + chr(0) + "b",             # NUL byte
    # The harness sketch's own four (PDF §10.13.6).
    "../../etc/passwd", "session/sub", "invalid:name", "test*session",
    # Review canaries.
    "s1" + chr(10),                 # TRAILING newline: what `$` + `.match` lets by
    "UNSAFE-abc",                   # the reservation is case-insensitive (NTFS)
    "NUL", "com1",                  # device names, refused on POSIX too
]

# Ids the validator must hand back unchanged.
VALID_IDS = ["s1", "mn9-abc.def_1", "A" * 128, "session-2026-09-13.run_01"]

# The vectors the WHITELIST accepts and the SINK still refuses: the only place
# the two statements of the rule are allowed to differ, and the reason the
# validator asks the sink as well as the regex.
RESERVATION_GAP = ["CON", "unsafe-abc", "UNSAFE-abc", "NUL", "com1"]

# Conforming by the whitelist, inside the prefix the sink reserves -- the same
# gap `tests/test_cli_arg_wiring.py` pins for the CLI.
RESERVED = events.QUARANTINE_PREFIX + "0" * 16


def _accepts(session_id) -> bool:
    try:
        events.validate_session_id(session_id)
    except ValueError:
        return False
    return True


def _own_segment_on_nt(session_id) -> bool:
    """Whether *session_id* is its own segment name at the STRICTEST platform.
    Asked with `os_name="nt"` explicitly so the answer is the same on the
    ubuntu and the windows CI legs: a store is portable, so an id a Windows
    runner would quarantine must be refused everywhere."""
    return events.segment_name(session_id, os_name="nt") == session_id + ".jsonl"


# --- the two statements of the whitelist ---------------------------------------

def test_session_id_regex_is_the_anchored_twin_of_the_shared_rule():
    """`SESSION_ID_REGEX` is the anchored form the PDF names; `SESSION_ID_RE`
    is the unanchored form `hooks/lib.sh` and `hooks/lib.ps1` copy, and
    `tests/test_events_segment_path.py` pins its exact pattern. Adding the
    anchored twin must not touch the shared one, or the hooks drift."""
    assert events.SESSION_ID_REGEX.pattern == r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    assert events.SESSION_ID_RE.pattern == r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"


def test_the_regex_agrees_with_the_sink_outside_the_reservation_gap():
    """Over the broad sample, `SESSION_ID_REGEX.fullmatch(x)` is truthy exactly
    when `segment_name(x) == x + ".jsonl"`. The exceptions are enumerated, not
    discovered: the sink reserves `unsafe-` and the Windows device names ON TOP
    of the whitelist, and those are the only rows where the two may differ."""
    sample = [x for x in ATTACK_VECTORS + VALID_IDS if x not in RESERVATION_GAP]
    disagree = [x for x in sample
                if bool(events.SESSION_ID_REGEX.fullmatch(x)) != _own_segment_on_nt(x)]
    assert disagree == [], disagree
    # The comparison compared something: both outcomes occur in the sample.
    assert any(events.SESSION_ID_REGEX.fullmatch(x) for x in sample)
    assert any(not events.SESSION_ID_REGEX.fullmatch(x) for x in sample)


def test_the_regex_alone_is_not_sufficient():
    """THE CANARY for a regex-only validator. Every row of the reservation gap
    satisfies the whitelist and is still not its own segment on `nt`, and
    `unsafe-<16 hex>` is the chosen-merge case the sink reserved the prefix
    for. A validator that stopped at the regex accepts all of them and the CLI
    exits 0 while filing the rows under a different hash."""
    for x in RESERVATION_GAP + [RESERVED]:
        assert events.SESSION_ID_REGEX.fullmatch(x), x
        assert not _own_segment_on_nt(x), x
        with pytest.raises(ValueError, match=REFUSAL):
            events.validate_session_id(x)


def test_validate_session_id_accepts_exactly_regex_and_nt_sink_identity():
    """The accept rule, as one biconditional over every vector and every valid
    id: accepted iff the anchored regex fullmatches AND the id is its own
    segment at the strictest platform. Either half alone is the defect -- the
    regex alone lets the reservation gap through, the sink alone (asked on the
    runner in hand) accepts `CON` on the ubuntu CI leg."""
    disagree = []
    for x in ATTACK_VECTORS + VALID_IDS:
        expected = bool(events.SESSION_ID_REGEX.fullmatch(x)) and _own_segment_on_nt(x)
        if _accepts(x) != expected:
            disagree.append((x, _accepts(x), expected))
    assert disagree == [], disagree


# --- the refusal ---------------------------------------------------------------

@pytest.mark.parametrize("sid", ATTACK_VECTORS, ids=repr)
def test_session_id_path_traversal_blocked(sid):
    """Memastikan path traversal pada session_id ditolak seketika. Every
    vector raises `ValueError` whose text satisfies the harness sketch's own
    search, including the ones carrying a raw newline or a NUL byte."""
    with pytest.raises(ValueError, match=REFUSAL):
        events.validate_session_id(sid)


def test_the_refusal_names_the_value_and_starts_with_the_binding_prefix():
    """The message contract: `Format session_id '<value>' tidak valid: ` then
    the rule. Rendered with `!r`, which is byte-identical to the PDF's quoted
    form for an ordinary id and escapes control characters for a hostile one
    -- so the message never carries a raw newline, and `match=REFUSAL` (an
    `re.search` whose `.*` stops at a newline) can find it."""
    with pytest.raises(ValueError) as exc:
        events.validate_session_id("../x")
    assert str(exc.value).startswith("Format session_id '../x' tidak valid: ")

    with pytest.raises(ValueError) as exc:
        events.validate_session_id("a" + chr(10) + "b")
    assert chr(10) not in str(exc.value), repr(str(exc.value))
    assert "tidak valid" in str(exc.value)


@pytest.mark.parametrize("value", [None, 123, 4.5, b"s1", ["s1"], object()],
                         ids=["None", "int", "float", "bytes", "list", "object"])
def test_validate_session_id_refuses_a_non_string(value):
    """No substitute value, ever. The PDF's `sanitize_session_id` answered
    `"unknown"` for `None` and a non-`str`; the plan refuses instead, because
    a validator that can return something the caller did not pass is a
    validator whose accept path cannot be trusted. `None` is resolved to the
    environment or `"manual"` by `append_event` BEFORE validation, never
    here."""
    with pytest.raises(ValueError, match=REFUSAL):
        events.validate_session_id(value)


def test_validate_session_id_touches_no_filesystem(tmp_path):
    """A pure function: it is called before the events directory is made and
    before the lock is taken, so it must not be the thing that makes either.
    The isolated store is `tmp_path`; nothing appears in it."""
    before = sorted(p for p in tmp_path.rglob("*"))
    events.validate_session_id("s1")
    with pytest.raises(ValueError):
        events.validate_session_id("../x")
    assert sorted(p for p in tmp_path.rglob("*")) == before


# --- the acceptance (the canary) -------------------------------------------------

@pytest.mark.parametrize("sid", VALID_IDS, ids=lambda s: s[:12])
def test_valid_session_id_accepted(sid):
    """Memastikan session_id yang sah diterima -- VERBATIM. Not `.strip()`ped,
    not case-folded, not substituted: the value the caller passed is the value
    the row will carry, and a validator that rewrites is a second sanitiser
    competing with `segment_name`."""
    got = events.validate_session_id(sid)
    assert got == sid
    assert isinstance(got, str)


def test_a_128_character_id_is_the_upper_bound():
    """The bound is inclusive at 128 and exclusive at 129, on both statements
    of the rule; an off-by-one here silently rejects a real segment name."""
    assert events.validate_session_id("A" * 128) == "A" * 128
    assert events.SESSION_ID_REGEX.fullmatch("A" * 128)
    with pytest.raises(ValueError, match=REFUSAL):
        events.validate_session_id("A" * 129)


# --- strict_session on append_event ----------------------------------------------

def test_strict_session_is_a_keyword_only_flag_defaulting_to_quarantine():
    """The signature contract: every existing positional caller of
    `append_event(event_type, payload, session_id, tags, ts, project)` is
    unaffected, because `strict_session` is KEYWORD-ONLY and defaults to
    `False` -- the hook path keeps quarantining unless a caller asks for the
    refusal by name."""
    sig = inspect.signature(events.append_event)
    assert list(sig.parameters)[:6] == ["event_type", "payload", "session_id",
                                        "tags", "ts", "project"]
    strict = sig.parameters["strict_session"]
    assert strict.kind is inspect.Parameter.KEYWORD_ONLY
    assert strict.default is False


def test_strict_append_of_a_hostile_id_raises_before_touching_the_filesystem(
        tmp_path):
    """The PDF's ordering rule (§10.13.2: 'sebelum menyentuh operasi
    filesystem'), asserted on the store rather than on the code: the raise
    happens before `events_dir().mkdir`, before `_append_lock` mints
    `append.lock`, and before any row or marker -- so nothing under the
    isolated store exists afterwards, not even the directory."""
    with pytest.raises(ValueError, match=REFUSAL):
        events.append_event("decision", {"text": "x"}, session_id="../x",
                            strict_session=True)
    assert list(tmp_path.rglob("*.jsonl")) == []
    assert list(paths.events_dir().rglob("*.jsonl")) == []
    assert not paths.events_dir().exists()
    assert not (paths.project_dir() / "append.lock").exists()
    assert not (paths.project_dir() / "state" / "pending_embed.json").exists()


@pytest.mark.parametrize("sid", ["s1" + chr(10), RESERVED, "CON"],
                         ids=["trailing-newline", "reserved-prefix", "device"])
def test_strict_append_refuses_what_the_sink_would_quarantine(tmp_path, sid):
    """The three ids a weaker validator lets through to `segment_name`: a
    trailing newline (`$` + `.match`), the reserved prefix (regex-only), and a
    device name (sink asked on a POSIX runner). Strict mode refuses each and
    writes nothing; without the refusal each is quietly filed under a hash."""
    with pytest.raises(ValueError, match=REFUSAL):
        events.append_event("decision", {"text": "x"}, session_id=sid,
                            strict_session=True)
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_strict_append_of_a_valid_id_lands_at_its_own_segment():
    """THE CANARY for strict mode, adapted from the harness sketch's
    `test_valid_session_id_accepted`: a strict append with a conforming id
    returns the `Event` carrying that id and lands at `events/<id>.jsonl`. A
    strict mode that refused everything passes every refusal test above and
    breaks every caller that opts in."""
    sid = "session-2026-09-13.run_01"
    ev = events.append_event("decision", {"text": "x"}, session_id=sid,
                             strict_session=True)
    assert isinstance(ev, events.Event)
    assert ev.session_id == sid
    assert (paths.events_dir() / (sid + ".jsonl")).is_file()
    assert (paths.events_dir() / "session-2026-09-13.run_01.jsonl").exists()


def test_default_append_still_quarantines_a_hostile_id():
    """The hook path is unchanged: with the default `strict_session=False`,
    `../x` is quarantined to `events/unsafe-<sha256[:16]>.jsonl` exactly as
    before, the append returns its `Event`, and the row keeps the RAW id.
    `tests/test_events_segment_path.py` pins the same behaviour end to end;
    this row is here so the strict flag's introduction cannot flip the
    default."""
    ev = events.append_event("decision", {"text": "x"}, session_id="../x")
    assert isinstance(ev, events.Event)
    assert ev.session_id == "../x"
    unsafe = list(paths.events_dir().glob("unsafe-*.jsonl"))
    assert len(unsafe) == 1, [p.name for p in unsafe]
    assert unsafe[0].name == events.segment_name("../x")
    assert len(unsafe[0].name) == len("unsafe-") + 16 + len(".jsonl")
    assert list(paths.events_dir().glob("*.jsonl")) == unsafe


def test_default_append_performs_no_validation_call(monkeypatch):
    """`strict_session=False` means NO validation, not validation whose result
    is ignored: the validator is on the hook path only when asked for by name.
    A validator that raises on every call stands in for one; the default
    append must never reach it."""
    def explode(session_id):
        raise RuntimeError("validate_session_id called on the default path")
    monkeypatch.setattr(events, "validate_session_id", explode)

    ev = events.append_event("decision", {"text": "x"}, session_id="../x")
    assert ev.session_id == "../x"
    assert len(list(paths.events_dir().glob("unsafe-*.jsonl"))) == 1

    # The stand-in is reachable: strict mode DOES call through the module
    # name, or the assertion above proved nothing about the default path.
    with pytest.raises(RuntimeError, match="default path"):
        events.append_event("decision", {"text": "x"}, session_id="s1",
                            strict_session=True)


# --- the CLI surface delegates to the same rule ----------------------------------

def test_cli_session_id_arg_carries_the_value_error_text():
    """`cliparse.session_id_arg("../x")` exits argparse with 2, and the
    message composes BOTH texts: the legacy `invalid session id` phrase that
    `tests/test_cli_arg_wiring.py` and `tests/test_events_segment_path.py`
    pin in stderr, and the `ValueError` text so the operator sees the rule
    they broke rather than a paraphrase of it. Chained from the `ValueError`
    -- delegation by identity, not a second copy of the rule."""
    with pytest.raises(argparse.ArgumentTypeError) as exc:
        cliparse.session_id_arg("../x")
    msg = str(exc.value)
    assert "invalid session id" in msg
    assert "Format session_id" in msg
    assert "tidak valid" in msg
    assert "'../x'" in msg
    assert isinstance(exc.value.__cause__, ValueError)


@pytest.mark.parametrize("sid", [events.QUARANTINE_PREFIX + "abc", "CON", "NUL",
                                 "com1", "s1" + chr(10)],
                         ids=["reserved", "CON", "NUL", "com1", "trailing-newline"])
def test_cli_session_id_arg_refuses_the_gap_on_every_platform(sid):
    """Platform-neutral refusals: the reserved prefix and the Windows device
    names are refused on the ubuntu CI leg as well, because the validator asks
    the sink at its strictest platform. Each refusal carries the `ValueError`
    text, which is what tells a delegated refusal from the old platform-bound
    one that happened to agree on a Windows runner."""
    with pytest.raises(argparse.ArgumentTypeError) as exc:
        cliparse.session_id_arg(sid)
    assert "invalid session id" in str(exc.value)
    assert "Format session_id" in str(exc.value)


@pytest.mark.parametrize("sid", ["s1", "mn9-abc.def_1"])
def test_cli_session_id_arg_returns_a_valid_id_verbatim(sid):
    """THE CANARY for the CLI refusals: a conforming id comes back as the same
    string, so every `--session` on the built parser still parses."""
    assert cliparse.session_id_arg(sid) == sid


def test_cli_session_id_arg_keeps_its_identity():
    """`tests/test_cli_arg_wiring.py` compares `action.type is
    cliparse.session_id_arg` on every `--session` of the built parser. The
    delegation must happen INSIDE that function object, not by rebinding the
    name to a new one, or a wrapper -- the wiring test walks the parser, and
    the parser holds whatever object was bound at build time."""
    assert cliparse.session_id_arg.__name__ == "session_id_arg"
    assert cliparse.session_id_arg.__module__ == "mindhive.cliparse"
    assert inspect.isfunction(cliparse.session_id_arg)
