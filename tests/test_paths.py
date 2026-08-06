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
