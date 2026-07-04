# tests/test_hygiene.py
import json

from mindhive import __main__ as cli_mod, hygiene, paths


def _make_namespace(home, name, *, graph_bytes=0):
    d = home / name
    (d / "events").mkdir(parents=True)
    (d / "STATE.md").write_text("# STATE\n", encoding="utf-8")
    if graph_bytes:
        (d / "graph.json").write_text("x" * graph_bytes, encoding="utf-8")
    return d


def _register(home, *names):
    home.mkdir(parents=True, exist_ok=True)
    (home / "autosync.json").write_text(
        json.dumps([{"project": n} for n in names]), encoding="utf-8")


def test_scan_reports_largest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "small")
    _make_namespace(tmp_path, "big", graph_bytes=5000)
    rows = hygiene.scan()
    assert [r["namespace"] for r in rows] == ["big", "small"]
    assert rows[0]["bytes"] >= rows[1]["bytes"]


def test_scan_flags_oversized_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "bloated",
                    graph_bytes=hygiene.GRAPH_BLOB_WARN_BYTES + 1)
    row = hygiene.scan()[0]
    assert row["graph_oversized"] is True


def test_scan_marks_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "fabio")
    _make_namespace(tmp_path, "fabio")
    _make_namespace(tmp_path, "brave-brattain-28ae65")
    reg = {r["namespace"]: r["registered"] for r in hygiene.scan()}
    assert reg["fabio"] is True
    assert reg["brave-brattain-28ae65"] is False


def test_report_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "keepme", graph_bytes=100)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    hygiene.report()
    after = sorted(p.name for p in tmp_path.rglob("*"))
    assert before == after                       # nothing mutated


def test_prune_orphans_removes_only_unregistered(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "fabio")
    _make_namespace(tmp_path, "fabio")
    _make_namespace(tmp_path, "brave-brattain-28ae65")
    res = hygiene.prune(orphans=True)
    removed = [r["namespace"] for r in res["removed_namespaces"]]
    assert removed == ["brave-brattain-28ae65"]
    assert (tmp_path / "fabio").exists()
    assert not (tmp_path / "brave-brattain-28ae65").exists()


def test_prune_refuses_registered_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "fabio")
    _make_namespace(tmp_path, "fabio")
    res = hygiene.prune(target="fabio")
    assert "refused" in res
    assert (tmp_path / "fabio").exists()         # untouched


def test_prune_graph_blobs_keeps_events(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "orphan", graph_bytes=5000)
    res = hygiene.prune(graph_blobs=True)
    assert res["removed_blobs"]
    assert not (tmp_path / "orphan" / "graph.json").exists()
    assert (tmp_path / "orphan" / "events").exists()   # event log intact


def test_cli_gc_report_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "orphan")
    ret = cli_mod.main(["gc"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "orphan" in out and "UNREGISTERED" in out


def test_cli_gc_prune_without_target_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "orphan")
    ret = cli_mod.main(["gc", "--prune"])
    assert ret == 2
    assert "requires a target" in capsys.readouterr().err
    assert (tmp_path / "orphan").exists()        # nothing deleted


def test_cli_gc_prune_orphans(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "fabio")
    _make_namespace(tmp_path, "fabio")
    _make_namespace(tmp_path, "orphan")
    ret = cli_mod.main(["gc", "--prune", "--orphans"])
    assert ret == 0
    assert not (tmp_path / "orphan").exists()
    assert (tmp_path / "fabio").exists()


def test_cli_gc_prune_blocked_by_live_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "LIVE_LOCK").write_text("live", encoding="utf-8")
    _make_namespace(tmp_path, "orphan")
    ret = cli_mod.main(["gc", "--prune", "--orphans"])
    assert ret == 2
    assert "live_lock" in capsys.readouterr().err.lower()
    assert (tmp_path / "orphan").exists()        # refused, untouched
