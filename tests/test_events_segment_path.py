# tests/test_events_segment_path.py
"""`events.segment_name` — the guard between a `session_id` and the filesystem.

## The defect

`append_event` built its segment path by raw concatenation:

    seg = d / f"{session_id}.jsonl"

and the id arrives from `--session` on the CLI or from a hook payload, neither
of which was checked on the Python side. So `--session ../../x` wrote the ledger
outside the namespace, and an id carrying a character the Windows kernel refuses
(`:` `*` `?` `|`) raised `OSError` from the middle of an append.

## The design these tests pin

Three properties, each of which a plausible alternative fix would break:

1. **Quarantine, not coercion to one name.** Mapping every hostile id to
   `unknown.jsonl` would merge distinct writers into one segment — a
   second-order corruption of the thing being protected. Hence
   `test_two_different_hostile_ids_do_not_collide`.
2. **Quarantine, not a raise.** This is on the hook path; `post_tool_use` fires
   on every tool call, and a hook that throws breaks the session. The loud
   rejection lives at the surface a human typed (`cliparse.session_id_arg`),
   which `test_the_cli_rejects_a_hostile_session_id_loudly` covers.
3. **The row keeps the RAW id.** The value is inert as JSON — `canonical_json`
   escapes it — and it is the only record of who wrote the row. Sanitizing the
   DATA would close the hole by deleting the evidence. Hence
   `test_the_stored_row_keeps_the_raw_session_id`.

## The reserved prefix

A hash cannot collide by ACCIDENT, but `unsafe-<16 hex>` is itself a conforming
id, so before the prefix was reserved anybody could CHOOSE the quarantine slot of
a hostile id by typing its name — and every object whose `__str__` raised shared
one constant name without anybody choosing anything. Both are the merge property
1 forbids. `events.segment_name` and `paths.namespace_name` each reserve the
prefix, and `test_the_two_quarantine_prefixes_are_one_constant` pins their two
spellings of it together, because `paths` cannot import it from `events`.

The reservation only means something if it is case-insensitive: on NTFS
`UNSAFE-<hex>.jsonl` and `unsafe-<hex>.jsonl` are one file, and
`UNSAFE-<hex>/` is the quarantine directory. The `test_a_case_variant_*` pair
asserts that on the NAME, so a case-sensitive runner catches it too; the extra
`SAFE` entries stop the fix from widening into "anything that mentions unsafe".

## The other half of the same path

The segment path is `store_home() / project / "events" / segment_name(sid)`.
Hardening the last component left the second one raw: `project_dir` slugged the
DETECTED namespace but concatenated an explicit one verbatim, so `--project
../../pwned` wrote the ledger above the store with a perfectly conforming session
id. `paths.namespace_name` is that component's `segment_name`, and the section
"the namespace component" pins the same properties on it.

## The canary

`test_a_normal_session_id_is_its_own_filename` is the assertion a `segment_name`
that quarantined EVERYTHING would fail. Without it, every other test in this
file passes against a function that has destroyed the ledger's layout. It is
also the migration proof: all 2418 segments in the live store match the
whitelist, so nothing on disk moves. `test_a_real_namespace_is_its_own_directory`
is the same canary for the namespace half.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import events, paths, taskview

B = chr(92)          # a literal backslash, kept out of the string literals below

HOSTILE = [
    "../../x",                      # POSIX traversal
    ".." + B + ".." + B + "x",      # Windows traversal
    "/abs/path",                    # absolute POSIX
    "C:" + B + "evil",              # absolute Windows
    "a/b",                          # bare separator
    "a" + B + "b",
    "a:b",                          # ADS / drive separator
    "a*b", "a?b", "a|b",            # wildcards the Windows kernel refuses
    'a"b', "a<b", "a>b",
    "..", ".",                      # the directory entries themselves
    "",                             # empty
    "-flag",                        # shaped like an option
    "x" * 500,                      # over the 128-char bound
    "ok" + chr(10) + "id",          # embedded newline
    "trail" + chr(10),              # TRAILING newline: what a `$` anchor lets by
    "sp ace",                       # whitespace
]

SAFE = ["manual", "unknown", "mn9-T-14", "lm-abc123",
        "39151937-fd17-48c0-bb12-fd5e596b1c1f", "a", "A.b_c-d", "0",
        # Near the reserved prefix without being in it: a reservation widened to
        # "contains unsafe" or "starts with unsafe" would quarantine these.
        "Unsafe_mode", "not-unsafe-x", "unsafe"]


# --- fixture builders ----------------------------------------------------------

class _StrRaises:
    """`str()` raises; `repr()` still works, and differs per live instance."""
    def __str__(self):
        raise RuntimeError("str")


class _NothingWorks:
    """`str()` AND `repr()` raise, so only the type is left to identify it."""
    def __str__(self):
        raise RuntimeError("str")

    def __repr__(self):
        raise RuntimeError("repr")


class _AlsoNothingWorks(_NothingWorks):
    """A second class of the same kind: tells a type-name fallback from a
    constant."""


def _quarantine_name_of(raw: str) -> str:
    """The quarantine name a hostile *raw* is filed under, computed the way
    somebody choosing it would -- without calling the code under test."""
    return "unsafe-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _lexical(p) -> Path:
    """*p* with `..` collapsed WITHOUT touching the filesystem. `resolve()` would
    serve the traversal cases, but with the guard reverted `//server/share/x`
    becomes a UNC path, and resolving that asks the network."""
    return Path(os.path.normpath(str(p)))


# --- the shape of a quarantined name -------------------------------------------

@pytest.mark.parametrize("sid", HOSTILE)
def test_a_hostile_session_id_never_leaves_the_events_directory(tmp_path, sid):
    """The property that matters, asserted on the RESOLVED path rather than on
    the string: `Path` is what actually decides where a write lands, so a check
    that only inspected the name could be satisfied by a name `Path` still
    interprets."""
    d = tmp_path / "events"
    d.mkdir()
    resolved = (d / events.segment_name(sid)).resolve()
    assert resolved.parent == d.resolve(), (sid, resolved)


@pytest.mark.parametrize("sid", HOSTILE)
def test_a_hostile_session_id_yields_an_os_legal_single_component(sid):
    name = events.segment_name(sid)
    assert name.endswith(".jsonl")
    assert "/" not in name and B not in name
    assert not name.startswith("-")
    assert not any(ch in name for ch in ':*?|"<>' + chr(10) + chr(13))
    assert name not in (".jsonl", "..jsonl")
    assert len(name) <= 128


def test_two_different_hostile_ids_do_not_collide():
    """Coercing every bad id to one name would merge distinct writers into one
    segment. A hash cannot collide by accident; a sanitizing rewrite can —
    `a/b` and `a*b` both reduce to `a_b` under any strip-the-bad-characters
    scheme."""
    names = [events.segment_name(s) for s in HOSTILE]
    assert len(set(names)) == len(names), \
        [n for n in names if names.count(n) > 1]


def test_a_quarantined_name_is_recognisable():
    """An operator triaging the store has to be able to SEE that something was
    quarantined; a silently-renamed segment is indistinguishable from a real
    one."""
    assert events.segment_name("../../x").startswith("unsafe-")


def test_segment_name_never_raises():
    """Totality is the contract, because this sits on the hook path. An object
    whose `__str__` raises is the case a bare `str(...)` would let through."""
    class Hostile:
        def __str__(self):
            raise RuntimeError("boom")
    for value in (None, 123, 4.5, [], {}, object(), Hostile(), _NothingWorks()):
        name = events.segment_name(value)
        assert name.endswith(".jsonl")
        assert "/" not in name and B not in name


@pytest.mark.parametrize("name_of", [events.segment_name, paths.namespace_name],
                         ids=["segment_name", "namespace_name"])
def test_two_distinct_unstringifiable_objects_do_not_merge(name_of):
    """The constant fallback was the zero-effort instance of the merge the
    quarantine forbids: EVERY object whose `__str__` raised was given one name,
    so unrelated writers shared it without anybody choosing to. The fallback
    walks `repr`, then the type name; a constant is left only for an object
    whose `type(x).__name__` raises too.

    Over both functions because `paths.namespace_name` restates
    `segment_name`'s shape and cites its reasons, "two distinct values must not
    merge" among them -- a second copy of the shape that kept the old fallback
    would be the drift this file exists to catch."""
    # Both instances kept alive: a freed object's address, and so its repr, can
    # be handed straight to the next one.
    a, b = _StrRaises(), _StrRaises()
    c, d = _NothingWorks(), _AlsoNothingWorks()
    # The fixture reaches the fallback, or this test proves nothing.
    with pytest.raises(RuntimeError):
        str(a)
    with pytest.raises(RuntimeError):
        repr(d)

    names = [name_of(x) for x in (a, b, c, d)]

    assert all(n.startswith("unsafe-") for n in names), names
    assert len(set(names)) == 4, names


# --- the reserved prefix ---------------------------------------------------------

def test_the_two_quarantine_prefixes_are_one_constant():
    """`paths` spells the prefix instead of importing it -- `events` imports
    `paths`, and `paths` stays dependency-free so `autosync` can import it. Two
    spellings of one reserved name drift silently: `events` would reserve one
    prefix while `paths` quarantined into another, and an operator triaging
    `unsafe-*` would see half of what was refused. This is the pin `paths.py`'s
    comment names."""
    assert events.QUARANTINE_PREFIX == paths.QUARANTINE_PREFIX
    assert events.segment_name("../../x").startswith(events.QUARANTINE_PREFIX)
    assert paths.namespace_name("../../x").startswith(paths.QUARANTINE_PREFIX)


def test_a_conforming_id_cannot_choose_a_quarantine_segment():
    """`unsafe-<16 hex>` satisfies `SESSION_ID_RE` on its own, so without the
    reservation it took the conforming branch and was written straight into the
    quarantine segment of whichever hostile id hashes to it. Chosen, not
    collided."""
    hostile = "../../x"
    chosen = _quarantine_name_of(hostile)
    # The fixture reproduces the defect, or this test proves nothing: the chosen
    # id IS conforming, and it spells exactly the hostile id's segment.
    assert events.SESSION_ID_RE.fullmatch(chosen)
    assert events.segment_name(hostile) == chosen + ".jsonl"

    assert events.segment_name(chosen) != events.segment_name(hostile)
    assert events.segment_name(chosen).startswith(events.QUARANTINE_PREFIX)


def test_a_chosen_quarantine_id_does_not_join_the_hostile_writers_segment(
        tmp_path, monkeypatch):
    """The review's reproduction, end to end: before the reservation the events
    directory held ONE segment carrying rows from two unrelated authors."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "probe")

    events.append_event("decision", {"text": "hostile"}, session_id="../../x")
    events.append_event("decision", {"text": "chosen"},
                        session_id=_quarantine_name_of("../../x"))

    segs = sorted(paths.events_dir("probe").glob("*.jsonl"))
    assert len(segs) == 2, [p.name for p in segs]
    for seg in segs:
        assert len(seg.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_a_conforming_name_cannot_choose_a_quarantine_namespace():
    """The same reservation on the namespace component: a namespace spelled
    `unsafe-<16 hex>` must not be the quarantine DIRECTORY of the hostile
    project that hashes to it."""
    hostile = "../../x"
    chosen = _quarantine_name_of(hostile)
    assert paths.PROJECT_RE.fullmatch(chosen)
    assert paths.namespace_name(hostile) == chosen

    assert paths.namespace_name(chosen) != paths.namespace_name(hostile)
    assert paths.namespace_name(chosen).startswith(paths.QUARANTINE_PREFIX)


# The reserved prefix in the spellings a case-SENSITIVE `startswith` lets by.
CASE_SPELLINGS = ["UNSAFE-", "Unsafe-", "uNSAFE-"]


def _case_variant(prefix: str, hostile: str) -> str:
    """*hostile*'s quarantine name with its prefix respelled as *prefix*."""
    return prefix + _quarantine_name_of(hostile)[len("unsafe-"):]


@pytest.mark.parametrize("prefix", CASE_SPELLINGS)
def test_a_case_variant_cannot_choose_a_quarantine_segment(prefix):
    """The reservation, on the filesystem this repo targets. NTFS is
    case-insensitive, so `UNSAFE-<hex>.jsonl` IS `unsafe-<hex>.jsonl`, and a
    reservation that only refuses the lower-case spelling leaves the chosen
    merge one Shift key away. Observed on the Windows dev box before this test
    existed: `../../x`, then `UNSAFE-9cdf6a50100a862e`, then
    `Unsafe-9cdf6a50100a862e` all appended to ONE segment holding 3 rows.

    Asserted on the NAME, not by counting files, so it fails on a
    case-sensitive runner too -- there the two spellings are two files whatever
    the guard does, and an end-to-end count would pass without proving
    anything."""
    hostile = "../../x"
    chosen = _case_variant(prefix, hostile)
    # The fixture reproduces the defect, or this proves nothing: the chosen id
    # is conforming, and it case-folds onto the hostile id's segment.
    assert events.SESSION_ID_RE.fullmatch(chosen)
    assert (chosen + ".jsonl").lower() == events.segment_name(hostile)

    got = events.segment_name(chosen)
    assert got.lower() != events.segment_name(hostile).lower(), got
    assert got.startswith(events.QUARANTINE_PREFIX), got


@pytest.mark.parametrize("prefix", CASE_SPELLINGS)
def test_a_case_variant_cannot_choose_a_quarantine_namespace(prefix):
    """The same bypass one component up. Observed in the same probe:
    `project="UNSAFE-19ebe2efaa5205a2"` appended its segment INTO
    `unsafe-19ebe2efaa5205a2/`, the quarantine directory of `../../pwned`."""
    hostile = "../../pwned"
    chosen = _case_variant(prefix, hostile)
    assert paths.PROJECT_RE.fullmatch(chosen)
    assert chosen.lower() == paths.namespace_name(hostile)

    got = paths.namespace_name(chosen)
    assert got.lower() != paths.namespace_name(hostile).lower(), got
    assert got.startswith(paths.QUARANTINE_PREFIX), got


# --- names the OS stores as something else (Windows) ---------------------------

DEVICE_NAMES = ["NUL", "nul", "CON", "Aux", "PRN", "COM1", "com0", "LPT9",
                "con.backup"]

# Near misses: a rule widened to "starts with" or "contains" a device name
# would quarantine every one of these.
NEAR_DEVICE_NAMES = ["console", "nullable", "com10", "auxiliary", "lpt", "com",
                     "a.con", "prn-queue"]


@pytest.mark.parametrize("name", DEVICE_NAMES)
def test_a_windows_device_name_is_quarantined_on_nt(name):
    """Measured on the Windows dev box: `append_event(project="NUL")` raised
    FileNotFoundError from inside the append, because `NUL/events` is a path
    through a device. Which members an extension frees has moved between
    Windows builds, so the STEM decides, ignoring case, for both components."""
    assert events.SESSION_ID_RE.fullmatch(name) and paths.PROJECT_RE.fullmatch(name)
    assert events.segment_name(name, os_name="nt").startswith(
        events.QUARANTINE_PREFIX)
    assert paths.namespace_name(name, os_name="nt").startswith(
        paths.QUARANTINE_PREFIX)


@pytest.mark.parametrize("name", DEVICE_NAMES + ["abc."])
def test_a_name_legal_on_posix_is_its_own_name_there(name):
    """THE CANARY for the Windows-only rules. On POSIX `aux` and `abc.` are
    ordinary names a store may already hold, and quarantining them there would
    point an existing caller at a different, empty namespace -- the failure
    `namespace_name` refuses `normalize` for."""
    assert events.segment_name(name, os_name="posix") == name + ".jsonl"
    assert paths.namespace_name(name, os_name="posix") == name


@pytest.mark.parametrize("name", NEAR_DEVICE_NAMES)
def test_a_name_that_only_resembles_a_device_is_not_quarantined(name):
    assert events.segment_name(name, os_name="nt") == name + ".jsonl"
    assert paths.namespace_name(name, os_name="nt") == name


def test_a_trailing_dot_aliases_a_namespace_but_not_a_segment():
    """Win32 strips a trailing dot, so `abc.` and `abc` were ONE directory
    (measured: creating `abc.` and then `abc` raised FileExistsError). The rule
    runs on the whole component, suffix included, so the segment `abc..jsonl`
    -- which ends in no dot -- is left alone."""
    assert paths.namespace_name("abc.", os_name="nt").startswith(
        paths.QUARANTINE_PREFIX)
    assert paths.namespace_name("abc", os_name="nt") == "abc"
    assert events.segment_name("abc.", os_name="nt") == "abc..jsonl"


@pytest.mark.skipif(os.name != "nt",
                    reason="device and trailing-dot aliasing are Windows "
                           "filesystem behaviour")
def test_a_device_or_dotted_project_appends_inside_the_store_on_windows(
        tmp_path, monkeypatch):
    """End to end on the filesystem that aliases. Before the rule `NUL` raised
    from inside the append and `abc.` wrote into `abc`; now each of the three
    is its own directory and `abc` holds only its own row."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("MINDHIVE_PROJECT", raising=False)
    for project in ("NUL", "abc.", "abc"):
        events.append_event("decision", {"text": project}, session_id="s-1",
                            project=project)
    dirs = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert "abc" in dirs, dirs
    assert len([d for d in dirs if d.startswith(paths.QUARANTINE_PREFIX)]) == 2, dirs
    seg = tmp_path / "abc" / "events" / "s-1.jsonl"
    assert len(seg.read_text(encoding="utf-8").splitlines()) == 1


# --- the canary ----------------------------------------------------------------

@pytest.mark.parametrize("sid", SAFE)
def test_a_normal_session_id_is_its_own_filename(sid):
    """THE CANARY. Every assertion above is satisfied by a `segment_name` that
    quarantines everything — which would relocate the entire ledger. This is the
    one it cannot pass, and it is also the migration proof: every segment in the
    live store matches this whitelist."""
    assert events.segment_name(sid) == sid + ".jsonl"


# --- end to end through append_event -------------------------------------------

def test_append_writes_inside_the_namespace_for_a_hostile_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "probe")
    before = sorted(p.name for p in tmp_path.rglob("*.jsonl"))
    events.append_event("decision", {"text": "hostile"}, session_id="../../pwned")
    after = sorted(p.name for p in tmp_path.rglob("*.jsonl"))
    assert after != before
    d = paths.events_dir("probe").resolve()
    for p in tmp_path.rglob("*.jsonl"):
        assert p.resolve().parent == d, p
    assert not (tmp_path.parent / "pwned.jsonl").exists()


def test_the_stored_row_keeps_the_raw_session_id(tmp_path, monkeypatch):
    """Sanitize the PATH, not the DATA. The id is the only record of who wrote
    the row, and it is inert as a JSON value."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "probe")
    ev = events.append_event("decision", {"text": "hostile"},
                             session_id="../../pwned")
    assert ev.session_id == "../../pwned"
    seg = next(paths.events_dir("probe").glob("unsafe-*.jsonl"))
    row = json.loads(seg.read_text(encoding="utf-8").strip().splitlines()[0])
    assert row["session_id"] == "../../pwned"


def test_a_windows_illegal_character_no_longer_raises(tmp_path, monkeypatch):
    """`a*b` used to raise OSError from inside the append. Nothing about the id
    should be able to fail a write."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "probe")
    for sid in ("a*b", "a?b", "a|b", "a:b"):
        events.append_event("decision", {"text": sid}, session_id=sid)
    assert len(list(paths.events_dir("probe").glob("unsafe-*.jsonl"))) == 4


def test_a_normal_append_still_lands_at_its_own_segment(tmp_path, monkeypatch):
    """The end-to-end half of the canary above."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "probe")
    events.append_event("decision", {"text": "ok"}, session_id="probe-session-1")
    assert (paths.events_dir("probe") / "probe-session-1.jsonl").is_file()


# --- the namespace component -----------------------------------------------------

HOSTILE_PROJECTS = HOSTILE + [
    "C:/evil",                      # absolute Windows, forward slashes
    "//server/share/x",             # UNC: a write to a network share
    "unsafe-" + "0" * 16,           # conforming, but inside the reserved prefix
]

SAFE_PROJECTS = ["test", "probe", "mindhive", "__hivemind__",
                 "pc-optimization-mind-hive", "a--b", "My.Project_1", "0"]


@pytest.mark.parametrize("name", HOSTILE_PROJECTS)
def test_a_hostile_project_is_quarantined_to_one_component(name):
    got = paths.namespace_name(name)
    assert got.startswith(paths.QUARANTINE_PREFIX), (name, got)
    assert paths.PROJECT_RE.fullmatch(got), got
    assert "/" not in got and B not in got


@pytest.mark.parametrize("name", HOSTILE_PROJECTS)
def test_project_dir_stays_a_direct_child_of_the_store_for_a_hostile_name(name):
    """The property that matters, on the path rather than the string. Before
    `namespace_name`, `project_dir("../../pwned")` was two levels above the store,
    `C:/evil` was `C:\\evil`, `//server/share/x` a network share, and `..` the
    store's parent -- where two namespaces' events would share one directory.
    (`""` falls through to the detected namespace, which is also a child.)"""
    home = _lexical(paths.store_home())
    assert _lexical(paths.project_dir(name)).parent == home, name
    assert _lexical(paths.events_dir(name)).parent.parent == home, name


def test_two_different_hostile_projects_do_not_collide():
    """Property 1 for namespaces: a quarantine that merged hostile names would
    fold unrelated writers' ledgers into one directory."""
    names = [paths.namespace_name(n) for n in HOSTILE_PROJECTS]
    assert len(set(names)) == len(names), \
        [n for n in names if names.count(n) > 1]


def test_namespace_name_is_total():
    """It runs under every projection read, so nothing about the value may make
    it throw. `None`, `123` and `4.5` stringify to conforming names and pass
    through; the rest quarantine -- all to one legal component."""
    for value in (None, 123, 4.5, [], {}, object(), _StrRaises(), _NothingWorks()):
        got = paths.namespace_name(value)
        assert isinstance(got, str) and got, value
        assert paths.PROJECT_RE.fullmatch(got), (value, got)
        assert "/" not in got and B not in got


@pytest.mark.parametrize("name", SAFE_PROJECTS)
def test_a_real_namespace_is_its_own_directory(name):
    """THE CANARY for the namespace half, and its migration proof: every
    namespace directory in the live store matched `PROJECT_RE`, so none moves.
    `__hivemind__` is a real namespace this code writes -- the reason the rule
    admits a leading `_`. `a--b` and `My.Project_1` kill the tempting
    alternative: `paths.normalize` would refuse the traversal too, but it
    collapses `--` and lowercases, silently pointing an existing caller at a
    different, empty namespace."""
    assert paths.namespace_name(name) == name
    assert paths.project_dir(name) == paths.store_home() / name


def test_is_freshly_minted_probes_the_directory_project_dir_would_use(
        tmp_path, monkeypatch):
    """`is_freshly_minted` kept a raw `store_home() / name` join after
    `project_dir` stopped using one, so for a hostile name it probed a
    DIFFERENT path from the one the read lands on -- here a directory beside
    the store, which exists, so it answered "not fresh" for a namespace that
    has never been written."""
    home = tmp_path / "store"
    home.mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setenv("MINDHIVE_HOME", str(home))
    monkeypatch.delenv("MINDHIVE_PROJECT", raising=False)
    # The fixture reaches the defect: the raw join names an existing directory.
    assert (paths.store_home() / "../outside").exists()

    assert paths.is_freshly_minted("../outside") is True
    (home / "probe").mkdir()
    assert paths.is_freshly_minted("probe") is False, "canary: a real namespace"


def test_append_with_a_hostile_project_writes_inside_the_store(tmp_path, monkeypatch):
    """End to end through a real writer, with a perfectly conforming session id:
    the review's reproduction. The store sits two levels down so `../../pwned`
    resolved against it lands inside `tmp_path` -- visible to this test, and
    never anywhere real even with the guard reverted."""
    home = tmp_path / "a" / "b"
    home.mkdir(parents=True)
    monkeypatch.setenv("MINDHIVE_HOME", str(home))
    monkeypatch.setenv("MINDHIVE_PROJECT", "probe")

    events.append_event("decision", {"text": "hostile"},
                        session_id="probe-session-1", project="../../pwned")

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written, "nothing was written, so the containment check is vacuous"
    assert [p for p in written if home not in p.parents] == []
    assert not (tmp_path / "pwned").exists()


# --- the loud half, at the surface a human typed -------------------------------

def _cli_env(tmp_path) -> dict:
    """The real environment plus a private store.

    Inherited rather than built from scratch: a hand-rolled env drops
    USERPROFILE/HOMEPATH, and `Path.home()` then raises "Could not determine
    home directory" -- which the canary below would report as the CLI refusing a
    valid id. A test that fails for a reason unrelated to the code under test is
    worse than no test."""
    env = dict(os.environ)
    env["MINDHIVE_HOME"] = str(tmp_path)
    env["MINDHIVE_PROJECT"] = "probe"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env


@pytest.mark.parametrize("sid", ["../../pwned", "C:" + B + "evil", "a*b", "a/b", ""])
def test_the_cli_rejects_a_hostile_session_id_loudly(tmp_path, sid):
    """`append_event` quarantines silently because it runs on the hook path.
    `--session` is a human at a keyboard, and filing their events under a hash
    without saying so is the wrong answer there — argparse exits 2 with the
    reason and nothing is written at all."""
    env = _cli_env(tmp_path)
    r = subprocess.run([sys.executable, "-m", "mindhive", "append", "decision",
                        "probe", "--session", sid],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "invalid session id" in r.stderr
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_the_cli_accepts_a_normal_session_id(tmp_path):
    """THE CANARY for the rejection above: a validator that refused everything
    would pass every parametrised case and break the CLI."""
    env = _cli_env(tmp_path)
    r = subprocess.run([sys.executable, "-m", "mindhive", "append", "decision",
                        "probe", "--session", "probe-session-1"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (tmp_path / "probe" / "events" / "probe-session-1.jsonl").is_file()


def test_every_statement_of_the_session_id_whitelist_agrees(tmp_path, monkeypatch):
    """`events.SESSION_ID_RE`, `hooks/lib.sh` and `hooks/lib.ps1` each state the
    same rule at a different boundary. Copies drift; this fails when they do,
    rather than letting one boundary quietly accept what another refuses.

    `taskview` held a FOURTH copy this test did not enumerate, and it had
    already drifted: compiled with a `$` anchor and applied with `.match`, and
    Python's `$` matches immediately BEFORE a trailing newline, so `"abc\\n"`
    passed the guard in front of `d / f"{session_id}.jsonl"`. It now reads
    `events`' pattern. Asserted by BEHAVIOUR as well as by the absence of a
    compiled copy, because a copy inlined as `re.match(r"^...$", sid)` inside the
    function leaves no module attribute behind. And "returns None" alone cannot
    tell the two apart on Windows, where the lookup after a passed guard always
    misses (no filename holds a newline) -- whether the id REACHED the lookup
    can."""
    repo = Path(__file__).resolve().parents[1]
    rule = "[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    assert events.SESSION_ID_RE.pattern == rule
    sh = (repo / "hooks" / "lib.sh").read_text(encoding="utf-8")
    ps = (repo / "hooks" / "lib.ps1").read_text(encoding="utf-8")
    assert rule in sh, "hooks/lib.sh no longer states the shared session-id rule"
    assert rule in ps, "hooks/lib.ps1 no longer states the shared session-id rule"

    # Identity, not spelling: `from .events import SESSION_ID_RE` is the owner's
    # own object and is the fix, not a copy of it.
    copies = [name for name, value in vars(taskview).items()
              if isinstance(value, re.Pattern)
              and value is not events.SESSION_ID_RE
              and "A-Za-z0-9" in value.pattern]
    assert copies == [], "taskview compiles its own session-id pattern again"

    looked = []
    monkeypatch.setattr(taskview, "transcripts_root",
                        lambda: looked.append(True) or tmp_path / "no-transcripts")
    assert taskview.find_transcript("abc" + chr(10)) is None
    assert looked == [], "a newline-terminated id got past find_transcript's guard"
    # The canary: a conforming id DOES reach the lookup, so the probe can fire.
    assert taskview.find_transcript("abc") is None
    assert looked == [True]
