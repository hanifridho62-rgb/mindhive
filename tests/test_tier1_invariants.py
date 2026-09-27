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

**P1d (appended): the SOP authorship half.** `advice.is_authorized_sop_author`
is the write gate BAB 2.3 asks for, and its tier-1 invariants are the three a
refactor is most likely to lose: an unconfigured host never authorises through
`compare_digest("", "")`, a refused `hive sop add` appends NOTHING, and no CLI
argument can stand in for the process token. The behavioural suite is
`tests/test_advice_authorship.py`; what is pinned here is only what must never
move.

**P2 (appended): the SuiteGate half.** The gate in front of a merge is
foundation for the same reason the ledger is -- every later phase trusts that
"validated" meant a test ran. Three properties are pinned here because no
later phase can repair them:

* a unit whose changed module no test mentions is REFUSED, not passed. The
  fail-open version of this branch called such a unit validated and said the
  closing full suite would judge it, which named a judge that runs after the
  merge;
* the greenfield refusal carries the exact path to create, and that path is
  one the gate itself would then accept. An instruction the gate does not
  recognise is a repair loop, not a fix;
* a unit may not edit the tests it is judged by (K13) -- the whole judge,
  `conftest.py` and the pytest configuration as much as `test_*.py` --
  measured from the commit its lane stood at before its FIRST attempt, which
  the gate pins ahead of the child. Not the lane's `HEAD`: a child that
  commits its edit moves `HEAD` with it, and a baseline the judged party can
  move is no baseline. A lane git cannot read is refused, never passed.

The behavioural suite is `tests/test_orchestrator_gate.py`, which runs real
git and real pytest; what is pinned here is only what must never move.

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


# --- P1d: SOP authorship (PDF BAB 2.3 / 10.13.3, Bug 3) ---------------------------

SOP_BODY = "Read the file before editing it, then run the whole suite."


@pytest.fixture()
def no_sop_credentials(monkeypatch):
    for name in ("MINDHIVE_ORCHESTRATION_SECRET", "MINDHIVE_PROCESS_TOKEN",
                 "MINDHIVE_SESSION"):
        monkeypatch.delenv(name, raising=False)


def test_an_unconfigured_host_never_authorises_an_orchestration_session(
        no_sop_credentials, monkeypatch):
    """`hmac.compare_digest("", "")` is True. The PDF's one-line check compared
    the two variables directly, so a host that had configured NOTHING authorised
    every process on it. Both set-but-empty and both-absent must fall through to
    the session rule instead."""
    from mindhive import advice
    assert advice.is_authorized_sop_author("mn9-x") is False
    monkeypatch.setenv("MINDHIVE_ORCHESTRATION_SECRET", "")
    monkeypatch.setenv("MINDHIVE_PROCESS_TOKEN", "")
    assert advice.is_authorized_sop_author("mn9-x") is False
    assert advice.is_authorized_sop_author("human-1") is True     # THE CANARY


def test_no_cli_argument_stands_in_for_the_process_token(no_sop_credentials,
                                                         monkeypatch, capsys):
    """The spoof BAB 2.3 names: `hive sop add --session s1` from an agent. In a
    process that sees the host secret the claimed session is not consulted at
    all, and the refusal leaves the ledger byte-for-byte as it was."""
    from mindhive import __main__ as cli_mod
    monkeypatch.setenv("MINDHIVE_ORCHESTRATION_SECRET", "s3cr3t")
    argv = ["sop", "add", "spoofed", "--title", "Spoofed", "--text", SOP_BODY,
            "--session", "s1"]
    assert cli_mod.main(argv) == 2
    assert "ditolak" in capsys.readouterr().err
    assert events.read_all() == []
    monkeypatch.setenv("MINDHIVE_PROCESS_TOKEN", "s3cr3t")
    assert cli_mod.main(argv) == 0                                # THE CANARY
    assert [e.type for e in events.read_all()] == ["sop"]


def test_the_process_token_is_withheld_from_every_spawned_agent(
        no_sop_credentials, monkeypatch):
    """The token is only process-bound while no spawner hands it down. The two
    spawners with an `env=` overlay scrub it AFTER that overlay, so neither
    inheritance nor an explicit `env=` can deliver it. The other agent spawners
    (board, loop, sandbox, suite, trainer) and the table that classifies EVERY
    module that spawns a process are pinned in `tests/test_advice_authorship.py`."""
    from mindhive import claude_cli
    from mindhive.mindnet import executor
    monkeypatch.setenv("MINDHIVE_PROCESS_TOKEN", "s3cr3t")
    overlay = {"MINDHIVE_PROCESS_TOKEN": "s3cr3t", "KEEP": "1"}
    for spawner in (executor._child_env, claude_cli._child_env):
        assert "MINDHIVE_PROCESS_TOKEN" not in spawner(None)
        child = spawner(dict(overlay))
        assert "MINDHIVE_PROCESS_TOKEN" not in child and child["KEEP"] == "1"


# --- P2: the gate in front of a merge -----------------------------------------

def _refusals():
    from mindhive.orchestrator import gate as gate_mod
    return gate_mod


def _joined(func):
    """A function's source with implicit string concatenation collapsed.

    These messages are read by a child and are pinned as text, but they are
    also wrapped to fit 79 columns — so the string in the file is four
    fragments and a search for the sentence finds nothing."""
    import re
    return re.sub(r'"\s*\n\s*f?"', "", inspect.getsource(func))


def test_no_test_mentions_it_is_a_refusal_not_a_pass():
    """THE inversion. `check` reaches this branch with an empty target list,
    and the only two ways out are a prescription or an explicit override."""
    gate_mod = _refusals()
    src = _joined(gate_mod.SuiteGate._untested)
    assert "ok=False" in src
    assert ("PELANGGARAN GATE: tidak ada tes yang menyebut modul yang "
            "diubah; gunakan --allow-untested" in src)
    assert src.count("ok=True") == 1, (
        "more than one way out of the untested branch passes the unit")


def test_the_prescribed_path_is_one_the_gate_would_accept():
    """The instruction has to be true on every input, not on the example.
    `canonical_test_path` names a file and `_named_for` decides whether a file
    counts -- if those two ever disagree the gate sends children somewhere it
    does not look."""
    gate_mod = _refusals()
    from pathlib import Path as _P
    for rel in ("src/pkg/lonely.py", "src/a/b/c/deep_module.py",
                "src\\pkg\\windows.py", "toplevel.py"):
        prescribed = gate_mod.canonical_test_path(rel)
        assert prescribed.startswith("tests/")
        stem = _P(rel.replace("\\", "/")).stem
        assert _P(prescribed).name in gate_mod._named_for(f"pkg.{stem}")


def test_the_refusal_names_the_module_and_the_file_to_write():
    """Verbatim MEB ORCH-01. A child reads this string and acts on it, so the
    two substitutions it turns on are pinned as text."""
    gate_mod = _refusals()
    src = _joined(gate_mod.SuiteGate._untested)
    assert "MODUL BARU TERDETEKSI: Modul '" in src
    assert "belum memiliki tes pendamping. Anda WAJIB membuat berkas tes pada:" in src
    assert "Instruksi: Tulis minimal satu pengujian unit di" in src


def test_the_override_cannot_reach_the_greenfield_case():
    """`--allow-untested` is for work no test could name. A new module always
    can be named -- the gate has just said which file -- so the greenfield
    branch is decided BEFORE the flag is consulted."""
    gate_mod = _refusals()
    src = _joined(gate_mod.SuiteGate._untested)
    assert src.index("MODUL BARU TERDETEKSI") < src.index("allow_untested")


def test_a_deleted_module_is_never_prescribed_a_test(tmp_path):
    """An instruction no attempt can satisfy is worse than no instruction."""
    gate_mod = _refusals()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "kept.py").write_text("x = 1", encoding="utf-8")
    assert gate_mod.greenfield_files(
        tmp_path, ["src/kept.py", "src/gone.py"]) == ["src/kept.py"]


def test_editing_the_tests_is_refused_and_says_which_flag_allows_it():
    gate_mod = _refusals()
    src = _joined(gate_mod.SuiteGate.check)
    assert ("PELANGGARAN GATE: berkas uji yang sudah ada diubah selama "
            "implementasi:" in src)
    assert "--allow-test-edits" in src


def test_the_immutable_check_runs_before_the_suite_does():
    """Refusing AFTER running would report the doctored suite's green as the
    unit's result. Asserted on the source order because the behavioural test
    needs a real lane and this file takes none."""
    gate_mod = _refusals()
    src = _joined(gate_mod.SuiteGate.check)
    assert src.index("tests_mutated") < src.index("self.targets(")


def test_the_immutable_baseline_is_pinned_before_the_child_runs():
    """The baseline is the commit the lane stood at before the unit's FIRST
    attempt. A snapshot taken when the gate first runs is taken after that
    attempt edited the file, and the lane's `HEAD` at gate time is no better:
    a child told not to commit still can, and committing makes `HEAD` agree
    with the edit. Every link is pinned because losing any one reopens it --
    `gated` asks for the pin BEFORE the runner on both paths, `begin` pins the
    lane's current commit and never overwrites a pin, and `check` compares
    against the pin, not `HEAD`."""
    gate_mod = _refusals()
    gated = _joined(gate_mod.gated)
    assert ("_begin(entry, lane)\n        return _finish(entry, lane, "
            "runner(entry, lane))" in gated), "the sync path pins after the child"
    assert (gated.index("run_in_executor(None, _begin, entry, lane)")
            < gated.index("out = await runner(entry, lane)")), (
        "the coroutine path pins after the child")
    begin = _joined(gate_mod.SuiteGate.begin)
    assert 'rev_of(lane, "HEAD")' in begin
    assert "setdefault" in begin, "a later attempt would re-pin"
    check = _joined(gate_mod.SuiteGate.check)
    assert "pinned = self.baseline(lane, task)" in check
    assert "tests_mutated(lane, base=base)" in check
    # A pin `begin` could not take is refused, not swapped for HEAD.
    assert (check.index("if not pinned:")
            < check.index("ok=False", check.index("if not pinned:"))
            < check.index("tests_mutated(lane, base=base)"))


def test_the_judge_is_more_than_the_test_modules():
    """An autouse fixture in a `conftest.py`, or `addopts = --deselect ...`,
    turns a red run green without one `test_*.py` changing. A gate watching
    only those passes the doctored run."""
    from mindhive.orchestrator import judge
    for rel in ("tests/conftest.py", "conftest.py", "src/pkg/conftest.py",
                "pytest.ini", "tests/data/expected.json", "tests/helpers.py",
                "tests/__init__.py"):
        assert judge.is_judge_path(rel), rel
    assert judge.PYTEST_SECTIONS == {"pyproject.toml": "tool.pytest",
                                     "setup.cfg": "tool:pytest",
                                     "tox.ini": "pytest"}
    assert not judge.is_judge_path("pyproject.toml"), (
        "the WHOLE pyproject judged would refuse a dependency bump")
    assert not judge.is_judge_path("src/pkg/writer.py")


def test_a_new_plugin_file_is_a_change_and_a_new_test_module_is_not():
    """Writing a test module is exactly what the greenfield refusal tells a
    child to do. Writing a `conftest.py` changes every run below it just by
    existing."""
    from mindhive.orchestrator import judge
    for rel in ("tests/conftest.py", "conftest.py", "pytest.ini",
                "tests/pytest.ini", "tests/__init__.py"):
        assert judge.is_implicit_load(rel), rel
    for rel in ("tests/test_lonely.py", "tests/helpers.py",
                "tests/data/expected.json"):
        assert not judge.is_implicit_load(rel), rel


def test_a_lane_git_cannot_read_is_refused_not_passed():
    """"Nothing I could see changed" is not "nothing changed". `None` is its
    own refusal, decided BEFORE the test that would read it as clean."""
    gate_mod = _refusals()
    check = _joined(gate_mod.SuiteGate.check)
    unknown = check.index("if mutated is None:")
    assert unknown < check.index("ok=False", unknown) < check.index("if mutated:")
    assert "could not verify that the lane's existing tests" in check


def test_the_lock_and_the_check_walk_one_list():
    """What the deterrent protects and what the gate compares cannot drift
    apart when both are the same walk."""
    from mindhive.orchestrator import judge
    from mindhive.orchestrator import worktree as wt_mod
    assert "judge.judge_files(" in inspect.getsource(wt_mod._chmod_tests)
    assert "judge_files(lane)" in inspect.getsource(judge.snapshot_tests)


def test_the_gate_defaults_to_closed_on_both_counts():
    """The defaults ARE the invariant: a permissive default leaves every
    property above reachable only by a caller who asked for it."""
    gate_mod = _refusals()
    plain = gate_mod.SuiteGate()
    assert plain.allow_untested is False
    assert plain.immutable_tests is True
    assert plain.allow_test_edits is False


def test_a_judge_edit_left_on_a_lane_is_every_later_units_change():
    """Rollback keeps commits, so the lane a second unit starts on may hold a
    judge edit the first unit's child committed under a refused unit. A pin
    taken at that HEAD would judge the second unit clean against a doctored
    suite. The pin advances only when the judge is the same at both commits."""
    gate_mod = _refusals()
    src = _joined(gate_mod.SuiteGate._pin)
    assert "judge_changes(lane, trusted, head)" in src
    assert "return trusted" in src, "a judge edit does not move the pin"
    assert src.index("if changed:") < src.index("self._trusted[lane_key] = head")
    assert "self._pin(" in _joined(gate_mod.SuiteGate.begin)


def test_the_judge_reads_a_checkout_only_at_its_top_level():
    """git asked inside a checkout answers for the tree around it, and a lane
    whose `.git` link is gone is a directory inside the MAIN checkout. Every
    reader that would act on that answer asks first."""
    from mindhive.mindnet import worktree as mn_wt
    from mindhive.orchestrator import judge
    from mindhive.orchestrator import worktree as wt_mod
    for func in (judge.tests_mutated, judge.judge_changes, mn_wt.commit_all,
                 wt_mod.rollback):
        assert "is_toplevel(" in inspect.getsource(func), func.__name__


def test_the_judge_is_named_by_import_as_well_as_by_path():
    """The interpreter imports `sitecustomize` by NAME and pytest loads every
    `pytest11` entry point the metadata on its path declares; matching one
    file name missed every other spelling."""
    from mindhive.orchestrator import judge
    assert judge.STARTUP_HOOKS == ("sitecustomize", "usercustomize")
    for rel in ("src/sitecustomize/__init__.py", "src/usercustomize.pyw",
                "src/sitecustomize.pyc", "src/x-1.0.dist-info/entry_points.txt",
                "pytest.toml", ".pytest.toml", "tests/pytest.toml"):
        assert judge.is_judge_path(rel) and judge.is_implicit_load(rel), rel
    assert not judge.is_judge_path("src/sitecustomize/__pycache__/x.pyc")


# --- P2 round 2, review fixes ---------------------------------------------------

def test_the_rollback_guard_is_asked_under_the_git_lock():
    """Between "this is a checkout of its own" and `checkout -- .` nothing may
    change what git answers for the path, so the guard runs under the lock
    the checkout runs under; and its seam is taken inside the try like the
    other two, so a rename is reported, not raised."""
    from mindhive.orchestrator import worktree as wt_mod
    src = inspect.getsource(wt_mod.rollback)
    assert src.index("with lock:") < src.index("is_toplevel(")
    assert src.index("mn_wt.is_toplevel") < src.index("except Exception as exc:")


def test_the_gate_reads_the_lanes_changes_once_per_check():
    """One list for `targets` and the greenfield search, read by `check` and
    handed down -- a second read could name a file the first never saw."""
    gate_mod = _refusals()
    assert _joined(gate_mod.SuiteGate.check).count("self._touched(") == 1
    assert "self._touched(" not in _joined(gate_mod.SuiteGate._untested)


def test_a_lane_nothing_vouched_for_stays_unvouched():
    """A first pin that failed leaves the lane with no trusted commit, and a
    later `HEAD` is not taken on trust in its place."""
    gate_mod = _refusals()
    src = _joined(gate_mod.SuiteGate._pin)
    assert "if trusted is None:" in src
    assert src.index("if trusted is None:") < src.index("judge_changes(")
