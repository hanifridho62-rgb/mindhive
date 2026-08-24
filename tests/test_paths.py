# tests/test_paths.py
import os
from pathlib import Path
from mindhive import paths


# --- path equivalence ---------------------------------------------------------
# Shared by `replenish` (has this caveat been addressed / this file logged) and
# `changelog` (does this entry cover this touched file). It lives in `paths`
# rather than in either of them because the change-log must not depend on the
# injector to compare two strings.

def test_a_hidden_directory_keeps_its_leading_dot():
    """`lstrip("./")` takes a CHARACTER SET, not a prefix.

    It ate the leading dot of every hidden path — `.hidden/x.py` became
    `hidden/x.py` — so two spellings of one path stopped matching, and the
    "has this been logged / addressed" comparisons that are the whole point of
    normalising silently answered no."""
    assert paths.normalise_path(".hidden/x.py") == ".hidden/x.py"
    assert paths.normalise_path(".claude/hooks/stop.ps1") \
        == ".claude/hooks/stop.ps1"


def test_a_relative_prefix_is_stripped_and_slashes_are_normalised():
    assert paths.normalise_path("./src/mindhive/x.py") == "src/mindhive/x.py"
    assert paths.normalise_path(".\\src\\mindhive\\x.py") == "src/mindhive/x.py"
    assert paths.normalise_path("././a.py") == "a.py"


def test_a_path_of_only_dots_is_not_annihilated():
    assert paths.normalise_path("...") == "..."


def test_normalise_path_is_total_on_junk():
    assert paths.normalise_path(None) == ""
    assert paths.normalise_path("") == ""


def test_an_absolute_worktree_path_yields_its_repo_relative_spelling():
    """The reconciliation this exists for.

    The ledger holds worktree-absolute paths written by every past checkout while
    git reports repo-relative ones. Measured on the live store, 3716 `file_touched`
    paths are absolute against 3 relative, while 1026 `code_change` entries are
    relative against 17 absolute — so a literal comparison subtracts nothing."""
    spellings = paths.path_spellings(
        "C:/Users/x/mindhive/src/mindhive/replenish.py")
    assert "src/mindhive/replenish.py" in spellings


def test_a_backslash_absolute_path_also_yields_the_relative_spelling():
    # Normalisation has to happen BEFORE the marker search, or every Windows
    # ledger path misses the reconciliation entirely.
    spellings = paths.path_spellings(
        "C:\\Users\\x\\mindhive\\tests\\test_paths.py")
    assert "tests/test_paths.py" in spellings


def test_a_repo_relative_path_is_its_own_only_spelling():
    assert paths.path_spellings("src/mindhive/x.py") == {"src/mindhive/x.py"}


def test_two_files_sharing_a_suffix_do_not_collapse():
    # The discriminating half: an over-eager reconciliation would call every
    # `x.py` in the tree the same file and silently discharge real debt.
    assert not (paths.path_spellings("src/mindhive/loop/x.py")
                & paths.path_spellings("src/mindhive/mindnet/x.py"))


def test_normalize():
    assert paths.normalize("FABIO HFT V18") == "fabio-hft-v18"
    assert paths.normalize("") == "default"

def test_store_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    assert paths.store_home() == tmp_path

def test_project_dir_uses_env_project(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "Fabio_V18")
    assert paths.project_dir() == tmp_path / "fabio-v18"
    assert paths.events_dir() == tmp_path / "fabio-v18" / "events"


def test_known_projects_reads_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "autosync.json").write_text(
        '[{"project": "Fabio HFT V18", "folder": "x", "transcripts": "y"}]',
        encoding="utf-8")
    assert paths.known_projects() == {"fabio-hft-v18"}


def test_known_projects_empty_without_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    assert paths.known_projects() == set()


def test_known_projects_corrupt_registry(tmp_path, monkeypatch):
    """A corrupt autosync.json must degrade to an empty set, not raise. (The
    downstream bulk-prune guard treats this empty set as unsafe — see
    tests/test_hygiene.py.)"""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "autosync.json").write_text("{not json", encoding="utf-8")
    assert paths.known_projects() == set()


def test_list_namespaces_filters_non_namespace_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "fabio" / "events").mkdir(parents=True)
    (tmp_path / "orphan").mkdir()
    (tmp_path / "orphan" / "STATE.md").write_text("# STATE\n", encoding="utf-8")
    (tmp_path / "downloads").mkdir()  # incidental dir, no namespace markers
    assert paths.list_namespaces() == ["fabio", "orphan"]


def test_is_freshly_minted(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("MINDHIVE_PROJECT", raising=False)
    # Registered project is never "freshly minted".
    (tmp_path / "autosync.json").write_text(
        '[{"project": "known-proj"}]', encoding="utf-8")
    assert paths.is_freshly_minted("known-proj") is False
    # An on-disk namespace is not freshly minted either.
    (tmp_path / "ondisk").mkdir()
    assert paths.is_freshly_minted("ondisk") is False
    # Unknown + not-on-disk = freshly minted.
    assert paths.is_freshly_minted("brave-brattain-28ae65") is True
    # An explicit MINDHIVE_PROJECT pin silences the signal.
    monkeypatch.setenv("MINDHIVE_PROJECT", "whatever")
    assert paths.is_freshly_minted("brave-brattain-28ae65") is False


def test_write_text_atomic_writes_content(tmp_path):
    p = tmp_path / "sub" / "file.json"        # parent does not exist yet
    paths.write_text_atomic(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_write_text_atomic_overwrites_existing(tmp_path):
    p = tmp_path / "file.txt"
    p.write_text("old", encoding="utf-8")
    paths.write_text_atomic(p, "new")
    assert p.read_text(encoding="utf-8") == "new"


def test_write_text_atomic_leaves_no_tmp_file(tmp_path):
    p = tmp_path / "file.txt"
    paths.write_text_atomic(p, "data")
    # The temp file is renamed onto the target — nothing should linger.
    assert [q.name for q in tmp_path.iterdir()] == ["file.txt"]
    assert not (tmp_path / "file.txt.tmp").exists()


def test_write_text_atomic_uses_a_private_temp_name(tmp_path, monkeypatch):
    """Two writers of ONE path must not share one temp file.

    The temp name was `<target>.tmp` for every writer of that target, and
    every hook is its own process. Two concurrent writers then wrote the same
    temp path and `os.replace` published whichever bytes happened to be there
    -- one process's snapshot landing under the other's name. That is the
    mechanism under the sidecar clobbering measured in tasktool: 19 call sites
    share this function, so the fix belongs here and not in any one of them.

    Asserted at the seam rather than by racing real processes: the name handed
    to `os.replace` has to differ between two calls on the SAME target.
    """
    import os

    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    target = tmp_path / "shared.json"
    paths.write_text_atomic(target, "first")
    paths.write_text_atomic(target, "second")
    assert len(seen) == 2
    assert seen[0] != seen[1], (
        "both writers used the same temp path (%s), so a concurrent writer "
        "can publish another process's bytes" % seen[0])
    assert target.read_text(encoding="utf-8") == "second"
    assert [q.name for q in tmp_path.iterdir()] == ["shared.json"]


# --- the atomic write, against a reader that is actually reading ---------------

def test_the_atomic_write_survives_a_concurrent_reader(tmp_path):
    """`os.replace` is atomic on Windows; it is not always ALLOWED.

    Windows refuses to replace a destination another process holds open without
    FILE_SHARE_DELETE, and Python's `open()` does not ask for it. So every
    reader of a file this function publishes is, for the instant it holds the
    handle, a reason for the write to raise PermissionError (WinError 5) --
    against the DESTINATION, a file that exists and is perfectly intact.

    Measured before the retry landed: 67 of 600 publishes never landed against
    the reader below, and 264 of 600 against one with no sleep at all. Nineteen
    call sites reach this function, and `chain.write_chain` is one of them with
    no try/except of its own, so a dashboard poll reading `chain.json` at the
    wrong microsecond could take `hive project` down with it.

    The reader sleeps 1ms between reads ON PURPOSE, and that calibration is
    what this test turns on. A reader with NO sleep holds the handle
    essentially 100% of the time, and no BOUNDED retry can beat it -- 8 tries
    over 635ms still lost, repeatedly. That is not a bug to fix, it is a
    contract to state: this function survives readers, not a reader that never
    lets go. 1ms is faster than any real consumer here (the dashboard polls
    every 2.5s) and still failed the unguarded code 67 times in 600, so the
    test discriminates without asserting the impossible.
    """
    import subprocess
    import sys

    target = tmp_path / "published.json"
    body = '{"count": 41000, "head": "%s", "pad": "%s"}' % ("a" * 64, "x" * 4000)
    target.write_text(body, encoding="utf-8")

    reader = tmp_path / "reader.py"
    reader.write_text(
        "import sys, time\n"
        "p, end = sys.argv[1], time.time() + float(sys.argv[2])\n"
        "while time.time() < end:\n"
        "    try:\n"
        "        open(p, encoding='utf-8').read()\n"
        "    except OSError:\n"
        "        pass\n"
        "    time.sleep(0.001)\n",
        encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(reader), str(target), "6"])
    try:
        for i in range(600):
            paths.write_text_atomic(target, body)      # must not raise
    finally:
        proc.kill()
        proc.wait(timeout=30)

    assert target.read_text(encoding="utf-8") == body
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], "a failed publish left its temp file behind: %s" % leftovers


def test_a_reader_is_not_denied_by_the_publish_it_races(tmp_path):
    """The other half of the same Windows race, from the reader's side.

    `write_text_atomic` makes torn CONTENT impossible -- measured, 6218 torn
    reads in 15977 became 0. What it leaves is a sharing violation: for the
    instant `os.replace` holds the destination, a reader's `open()` is refused.
    300 refusals in 56483 reads, against a writer publishing flat out.

    That is a better failure than silent garbage, but the callers turn it into
    a wrong ANSWER rather than an error: `chain.verify` catches OSError and
    reports `corrupt_chain_file`, showing the whole LEDGER as corrupt when only
    the checkpoint was mid-replace, and `harvest._load_state` catches
    everything and answers `{}`, a cursor reset wearing a first run's costume.

    The handshake below is the point of the test, not scaffolding. The first
    cut counted 400 reads and started them the moment `Popen` returned -- they
    all finished inside 0.1s, before a freshly spawned interpreter had finished
    importing `mindhive`, so the reads never raced anything. It passed five
    times out of five with the retry REMOVED. A test that cannot fail is not a
    weak test, it is a decoration, so this one waits for the writer to say it
    is publishing, reads for a WINDOW rather than a count, and asserts it got
    enough reads inside that window to have raced at all.
    """
    import subprocess
    import sys
    import time

    target = tmp_path / "published.json"
    ready = tmp_path / "writer-ready"
    body = '{"pad": "%s"}' % ("x" * 4000)
    target.write_text(body, encoding="utf-8")

    writer = tmp_path / "writer.py"
    writer.write_text(
        "import sys, time, pathlib\n"
        "sys.path.insert(0, %r)\n"
        "from mindhive import paths\n"
        "p, flag, secs, body = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]\n"
        "pathlib.Path(flag).write_text('go', encoding='utf-8')\n"
        "end = time.time() + secs\n"
        "while time.time() < end:\n"
        "    paths.write_text_atomic(p, body)\n"
        % str(Path(paths.__file__).resolve().parents[1]),
        encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(writer), str(target),
                             str(ready), "5", body])
    try:
        start_deadline = time.time() + 60
        while not ready.exists():
            assert time.time() < start_deadline, "the writer never started publishing"
            time.sleep(0.01)

        reads, end = 0, time.time() + 2.0
        while time.time() < end:
            assert paths.read_text_retry(target) == body
            reads += 1
        assert reads > 200, (
            "only %d reads landed inside the writer's window — this test has "
            "stopped racing anything" % reads)
    finally:
        proc.kill()
        proc.wait(timeout=30)

    missing = tmp_path / "not-there.json"
    try:
        paths.read_text_retry(missing)
        raise AssertionError("absence must raise, not be retried into a wrong answer")
    except FileNotFoundError:
        pass
