# tests/test_worksession_usage.py — the Oversee context bar's figures and the
# verbose usage report (blueprint BAB 5.3.3 and 5.5; plan phase P1b).
"""`worksession_usage.py`: the BAB 5.3.3 report verbatim, and finding and
measuring a namespace's synced MEMORY.md -- through the autosync registry
first, then the encoded folder names, never handing one namespace a sibling's
digest, and never reporting "0 % used" for a digest it could not read."""
import os

import pytest

from mindhive import sync
from mindhive import worksession_usage as wu


def _memory(home, encoded, text, mtime=None):
    p = home / ".claude" / "projects" / encoded / "memory" / "MEMORY.md"
    p.parent.mkdir(parents=True)
    p.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _find(name, home, **kw):
    kw.setdefault("registry", [])
    kw.setdefault("namespaces", [])
    return wu.memory_path_for_project(name, home=home, **kw)


# --- 1. the verbose report tag ------------------------------------------------------------

def test_usage_report_tag_is_the_bab_5_3_3_example_verbatim():
    assert wu.make_usage_report_tag(48250, 100000, trim_lines=14, trim_bytes=1280) == (
        "[USAGE: 48,250 tokens / 100,000 cap | Occupancy: 48.25% | "
        "Trim: 14 lines / 1,280 bytes dropped from digest tail]")
    assert wu.make_usage_report_tag(1000, 100000) == \
        "[USAGE: 1,000 tokens / 100,000 cap | Occupancy: 1.00% | Trim: none]"


def test_usage_tag_from_pressure_reads_the_two_overflows():
    pressure = {"lines": 214, "budget": 200, "over": 14, "bytes": 26280,
                "byte_budget": 25000, "over_bytes": 1280, "cut": "lines",
                "truncated": True, "dropped": ["Key decisions"], "readable": True}
    assert wu.usage_tag_from_pressure(48250, 100000, pressure).endswith(
        "Trim: 14 lines / 1,280 bytes dropped from digest tail]")
    assert wu.usage_tag_from_pressure(48250, 100000, dict(pressure, truncated=False)) \
        .endswith("Trim: none]")


# --- 2. finding the digest -------------------------------------------------------------------

def test_memory_path_for_project_matches_the_encoded_folder_tail(tmp_path):
    a = _memory(tmp_path, "C--Users-x-mindhive", "# a\n", mtime=1_000)
    _memory(tmp_path, "C--Users-x-mindhive--claude-worktrees-b", "# wt\n", mtime=3_000)
    _memory(tmp_path, "C--Users-x-other", "# o\n", mtime=4_000)
    assert _find("mindhive", tmp_path) == a
    assert _find("other", tmp_path).parent.parent.name == "C--Users-x-other"
    assert _find("absent", tmp_path) is None
    assert _find("x", tmp_path / "nowhere") is None


def test_memory_path_prefers_the_most_recently_written_of_two_same_named_folders(tmp_path):
    _memory(tmp_path, "C--a-proj", "# old\n", mtime=1_000)
    new = _memory(tmp_path, "D--b-proj", "# new\n", mtime=2_000)
    assert _find("proj", tmp_path) == new


def test_a_namespace_never_takes_a_longer_siblings_digest(tmp_path):
    known = ["app", "web-app"]
    web = _memory(tmp_path, "C--Users-x-web-app", "# web\n", mtime=2_000)
    assert _find("app", tmp_path, namespaces=known) is None
    assert _find("web-app", tmp_path, namespaces=known) == web
    app = _memory(tmp_path, "C--Users-x-app", "# app\n", mtime=1_000)
    assert _find("app", tmp_path, namespaces=known) == app      # older, but its own
    # The v1 / blueprint-v1 shape from the review: same rule.
    _memory(tmp_path, "C--Users-x-blueprint-v1", "# bp\n", mtime=3_000)
    assert _find("v1", tmp_path, namespaces=["v1", "blueprint-v1"]) is None


def test_a_linked_worktrees_digest_is_never_a_namespaces_own(tmp_path):
    """The review's exact repro: no worktree is ever a known namespace
    (`paths.detect_project` collapses it onto its repo), so the longer-namespace
    rule alone handed ``v1`` a worktree's digest."""
    _memory(tmp_path, "C--Users-x-mindhive--claude-worktrees-blueprint-v1", "x" * 4000,
            mtime=3_000)
    assert _find("v1", tmp_path, namespaces=["v1", "mindhive"]) is None
    assert wu.usage_for_project("v1", home=tmp_path, registry=[],
                                namespaces=["v1", "mindhive"])["measured"] is False
    own = _memory(tmp_path, "C--Users-x-v1", "# mine\n", mtime=1_000)
    assert _find("v1", tmp_path, namespaces=["v1", "mindhive"]) == own


def test_another_projects_registered_folder_is_never_a_candidate(tmp_path):
    """``api`` is unregistered; ``payments`` registered ``C:\\work\\payments-api``,
    whose encoded tail ends in ``-api``. That digest is payments', not api's."""
    registry = [{"project": "payments", "folder": r"C:\work\payments-api"}]
    theirs = sync.native_memory_path(registry[0]["folder"], home=tmp_path)
    theirs.parent.mkdir(parents=True)
    theirs.write_text("# payments\n", encoding="utf-8")
    assert _find("api", tmp_path, registry=registry) is None
    assert _find("payments", tmp_path, registry=registry) == theirs
    own = _memory(tmp_path, "C--Users-x-api", "# api\n", mtime=1_000)
    assert _find("api", tmp_path, registry=registry) == own


def test_the_autosync_registry_is_asked_first(tmp_path):
    folder = r"C:\Users\x\app"
    own = sync.native_memory_path(folder, home=tmp_path)
    own.parent.mkdir(parents=True)
    own.write_text("# mine\n", encoding="utf-8")
    os.utime(own, (1_000, 1_000))
    _memory(tmp_path, "C--Users-y-web-app", "# newer sibling\n", mtime=9_000)
    registry = [{"project": "other", "folder": r"C:\Users\x\other"},
                {"project": "app", "transcripts": "t", "folder": folder}]
    assert _find("app", tmp_path, registry=registry) == own
    # A registered namespace whose digest is absent reads None: no guessing.
    assert _find("other", tmp_path, registry=registry) is None
    # Malformed rows are skipped, not trusted: the folder names answer instead,
    # and the known longer namespace keeps its own digest.
    assert _find("app", tmp_path, registry=["junk", {"project": "app"}],
                 namespaces=["app", "web-app"]) == own


def test_the_defaults_read_the_isolated_store(tmp_path):
    from mindhive import autosync
    folder = r"C:\Users\x\proj"
    autosync.register("proj", "transcripts", folder)
    own = sync.native_memory_path(folder, home=tmp_path)
    own.parent.mkdir(parents=True)
    own.write_text("# mine\n", encoding="utf-8")
    assert wu.memory_path_for_project("proj", home=tmp_path) == own


# --- 3. measuring it ------------------------------------------------------------------------

def test_usage_for_project_reports_bytes_tokens_and_the_safe_line(tmp_path):
    text = "# Current State\n" + ("x" * 99 + "\n") * 40        # 4000 LF bytes
    p = _memory(tmp_path, "C--Users-x-mindhive", text)
    u = wu.usage_for_project("mindhive", home=tmp_path, registry=[], namespaces=[])
    assert u["path"] == str(p) and u["measured"] is True
    assert u["bytes"] == 4016 and u["tokens_est"] == 1004 and u["lines"] == 41
    assert u["occupancy_pct"] == round(1004 / 200_000 * 100, 2) == 0.5
    assert u["context_budget_tokens"] == 200_000 and u["safe_budget_tokens"] == 160_000
    assert u["truncated"] is False


def test_usage_for_project_is_unmeasured_zeros_when_no_digest_exists(tmp_path):
    u = wu.usage_for_project("mindhive", home=tmp_path, registry=[], namespaces=[])
    assert u == {"context_budget_tokens": 200_000, "safe_budget_tokens": 160_000,
                 "bytes": 0, "tokens_est": 0, "occupancy_pct": 0.0, "lines": 0,
                 "truncated": False, "measured": False, "path": ""}


def test_an_unreadable_digest_is_unmeasured_not_empty(tmp_path):
    missing = tmp_path / "gone" / "MEMORY.md"
    u = wu.usage_for_project("mindhive", path=missing)
    assert u["measured"] is False and u["bytes"] == 0 and u["path"] == str(missing)


def test_usage_for_project_flags_a_truncated_digest(tmp_path):
    text = "# S\n" + "\n".join(f"line {i}" for i in range(260)) + "\n"
    p = _memory(tmp_path, "C--Users-x-mindhive", text)
    u = wu.usage_for_project("mindhive", home=tmp_path, path=p)
    assert u["truncated"] is True and u["lines"] == 261 and u["measured"] is True


def test_the_memory_path_index_is_one_walk_shared_by_every_lookup(tmp_path, monkeypatch):
    a = _memory(tmp_path, "C--Users-x-alpha", "# a\n", mtime=1_000)
    b = _memory(tmp_path, "C--Users-x-beta", "# bb\n", mtime=2_000)
    (tmp_path / ".claude" / "projects" / "no-digest-here").mkdir()
    index = wu.memory_path_index(tmp_path)
    assert sorted((tail, path) for tail, path, _m in index) == [
        ("c-users-x-alpha", a), ("c-users-x-beta", b)]      # `paths.normalize`d
    assert wu.memory_path_index(tmp_path / "nowhere") == []
    walks = []
    monkeypatch.setattr(wu, "memory_path_index",
                        lambda home=None: walks.append(home) or index)
    assert wu.usage_for_project("alpha", index=index, registry=[],
                                namespaces=[])["bytes"] == 4
    assert wu.usage_for_project("beta", index=index, registry=[],
                                namespaces=[])["bytes"] == 5
    assert walks == []                  # a supplied index is never re-walked


@pytest.mark.parametrize("name", ["alpha", "Alpha"])
def test_the_lookup_normalises_the_namespace(tmp_path, name):
    a = _memory(tmp_path, "C--Users-x-alpha", "# a\n")
    assert _find(name, tmp_path) == a
