# tests/test_clusters.py
"""The parallel-orchestration layer: cluster DB, file-complexity classifier, and
the file-ownership-safe shard planner. These pin the load-bearing contract the
`/mindhive-orchestrator` execution stage relies on to fan work out to parallel
sub-agents without two agents ever touching the same file."""
import json
from pathlib import Path

import pytest

from mindhive import clusters


# --- build_clusters: deterministic directory-level grouping -----------------

def test_build_clusters_groups_by_directory_deterministically():
    files = [
        "src/mindhive/loop/driver.py",
        "src/mindhive/loop/engine.py",
        "src/mindhive/plugins.py",
        "skills/circle/SKILL.md",
        "README.md",
    ]
    got = clusters.build_clusters(root=".", files=files)
    assert got == {
        "(root)": ["README.md"],
        "skills/circle": ["skills/circle/SKILL.md"],
        "src/mindhive": ["src/mindhive/plugins.py"],
        "src/mindhive/loop": ["src/mindhive/loop/driver.py",
                              "src/mindhive/loop/engine.py"],
    }
    # same input -> byte-identical output (keys and lists sorted)
    assert clusters.build_clusters(root=".", files=list(reversed(files))) == got


def test_build_clusters_walks_a_real_tree_skipping_vendored(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "b.md").write_text("# doc\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.py").write_text("nope\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")  # non-source suffix
    got = clusters.build_clusters(root=tmp_path)
    assert got == {"pkg": ["pkg/a.py", "pkg/b.md"]}  # cache + png excluded


# --- file_complexity: monotone in size, nesting, and branching --------------

def test_complexity_big_nested_file_outscores_tiny_flat_file():
    tiny_flat = "x = 1\n"
    big_nested = "".join(
        f"{'    ' * (i % 5)}if cond_{i}:\n" for i in range(60)
    )
    assert clusters.file_complexity(big_nested) > clusters.file_complexity(tiny_flat)


def test_complexity_tiers_are_monotone():
    assert clusters.complexity_tier(0) == "low"
    assert clusters.complexity_tier(39) == "low"
    assert clusters.complexity_tier(40) == "med"
    assert clusters.complexity_tier(119) == "med"
    assert clusters.complexity_tier(120) == "high"
    assert clusters.complexity_tier(10_000) == "high"


def test_complexity_blank_lines_do_not_count():
    assert clusters.file_complexity("\n\n\n") == 0
    assert clusters.file_complexity("a = 1\n\n\nb = 2\n") == 2  # two non-blank LOC


# --- git_churn: guarded, never raises ---------------------------------------

def test_git_churn_returns_zero_when_git_missing(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _name: None)  # git "not on PATH"
    assert clusters.git_churn("anything.py", root=".") == 0


# --- shard_plan: file-ownership-safe, balanced partition --------------------

def _sample():
    clus = {
        "a": ["a/1.py", "a/2.py"],
        "b": ["b/1.py"],
        "c": ["c/1.py", "c/2.py", "c/3.py"],
        "d": ["d/1.py"],
    }
    weights = {"a": 30, "b": 10, "c": 100, "d": 5}
    return clus, weights


def test_shard_plan_partitions_every_file_exactly_once():
    clus, weights = _sample()
    plan = clusters.shard_plan(clus, weights, lanes=2)
    assert len(plan) <= 2
    all_files = [f for lane in plan for f in lane["files"]]
    every_input = sorted(f for fs in clus.values() for f in fs)
    assert sorted(all_files) == every_input          # no drops
    assert len(all_files) == len(set(all_files))      # no overlap


def test_shard_plan_balances_complexity():
    clus, weights = _sample()  # total weight 145, two lanes
    plan = clusters.shard_plan(clus, weights, lanes=2)
    loads = sorted(lane["est_complexity"] for lane in plan)
    # heaviest cluster (c=100) dominates; greedy LPT still keeps the split as
    # even as the indivisible clusters allow (100 vs 45 here).
    assert loads == [45, 100]


def test_shard_plan_never_splits_a_cluster_across_lanes():
    clus, weights = _sample()
    plan = clusters.shard_plan(clus, weights, lanes=3)
    seen_clusters = [c for lane in plan for c in lane["clusters"]]
    assert sorted(seen_clusters) == ["a", "b", "c", "d"]
    assert len(seen_clusters) == len(set(seen_clusters))  # each cluster once


def test_shard_plan_clamps_lane_bounds():
    clus, weights = _sample()             # 4 clusters
    assert len(clusters.shard_plan(clus, weights, lanes=0)) == 1        # clamp up to 1
    assert len(clusters.shard_plan(clus, weights, lanes=-5)) == 1       # negative -> 1
    assert len(clusters.shard_plan(clus, weights, lanes=99)) == 4       # clamp down to n
    assert len(clusters.shard_plan(clus, weights, lanes="3")) == 3      # numeric string ok


def test_shard_plan_rejects_non_numeric_lanes():
    clus, weights = _sample()
    with pytest.raises(ValueError):
        clusters.shard_plan(clus, weights, lanes="abc")


def test_shard_plan_empty_is_empty():
    assert clusters.shard_plan({}, {}, lanes=3) == []


# --- build + sidecar round-trip ---------------------------------------------

def test_build_and_store_writes_roundtrippable_sidecar(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def f():\n    if x:\n        return 1\n")
    (tmp_path / "top.md").write_text("# hi\n")
    payload = clusters.build_and_store(root=tmp_path, lanes=2)  # store dir isolated by conftest
    assert payload["counts"]["files"] == 2
    assert payload["counts"]["clusters"] == 2
    assert payload["shard_plan"]                     # non-empty
    reloaded = clusters.latest()
    assert reloaded is not None
    assert reloaded["counts"]["files"] == 2
    assert reloaded["shard_plan"] == payload["shard_plan"]  # sidecar round-trips


def test_latest_returns_none_before_any_build():
    assert clusters.latest() is None
