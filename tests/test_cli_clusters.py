# tests/test_cli_clusters.py — `hive clusters {build,plan,show}` CLI surface.
# This is the seam the /mindhive-orchestrator execution stage calls to classify
# the repo and lay out file-ownership-safe lanes for parallel sub-agents.
import pytest

from mindhive import __main__ as cli_mod, clusters


def _seed_repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def f():\n    if x:\n        return 1\n")
    (tmp_path / "pkg" / "b.py").write_text("y = 2\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("# hi\n")
    return tmp_path


def test_clusters_build_writes_sidecar(tmp_path, capsys):
    _seed_repo(tmp_path)
    ret = cli_mod.main(["clusters", "build", "--root", str(tmp_path)])
    assert ret == 0
    out = capsys.readouterr().out
    assert "clustered" in out and "cluster_db.json" in out
    # store is isolated by conftest; the sidecar is readable back
    assert clusters.latest() is not None
    assert clusters.latest()["counts"]["files"] == 3


def test_clusters_plan_respects_lane_cap(tmp_path, capsys):
    _seed_repo(tmp_path)                       # 2 clusters (pkg, docs)
    ret = cli_mod.main(["clusters", "plan", "--root", str(tmp_path), "--lanes", "3"])
    assert ret == 0
    out = capsys.readouterr().out
    # only 2 clusters exist, so at most 2 lanes even though 3 were requested
    assert "2 parallel lane(s)" in out


def test_clusters_plan_clamps_negative_lanes_to_one(tmp_path, capsys):
    _seed_repo(tmp_path)
    ret = cli_mod.main(["clusters", "plan", "--root", str(tmp_path), "--lanes=-5"])
    assert ret == 0
    assert "1 parallel lane(s)" in capsys.readouterr().out


def test_clusters_plan_rejects_non_numeric_lanes(tmp_path):
    _seed_repo(tmp_path)
    # argparse int-validation rejects a non-numeric --lanes: SystemExit, not a crash
    with pytest.raises(SystemExit):
        cli_mod.main(["clusters", "plan", "--root", str(tmp_path), "--lanes", "abc"])


def test_clusters_show_before_build_is_graceful(capsys):
    ret = cli_mod.main(["clusters", "show"])
    assert ret == 0
    assert "no cluster_db yet" in capsys.readouterr().out


def test_clusters_show_after_build_prints_clusters(tmp_path, capsys):
    _seed_repo(tmp_path)
    cli_mod.main(["clusters", "build", "--root", str(tmp_path)])
    capsys.readouterr()                                    # drain the build output
    ret = cli_mod.main(["clusters", "show"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "cluster DB from" in out
    assert "weight" in out and "files" in out              # per-cluster detail line
    assert "pkg" in out                                    # a real cluster key
