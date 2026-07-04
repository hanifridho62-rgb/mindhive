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
    _register(tmp_path, "keeper")   # non-empty registry so bulk prune is allowed
    _make_namespace(tmp_path, "orphan", graph_bytes=5000)
    res = hygiene.prune(graph_blobs=True)
    assert res["removed_blobs"]
    assert not (tmp_path / "orphan" / "graph.json").exists()
    assert (tmp_path / "orphan" / "events").exists()   # event log intact


def test_prune_graph_blobs_skips_registered(tmp_path, monkeypatch):
    """Scope-all --graph-blobs must not delete a registered namespace's blob,
    but --force overrides."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "fabio")
    _make_namespace(tmp_path, "fabio", graph_bytes=5000)
    _make_namespace(tmp_path, "orphan", graph_bytes=5000)
    hygiene.prune(graph_blobs=True)
    assert (tmp_path / "fabio" / "graph.json").exists()        # registered kept
    assert not (tmp_path / "orphan" / "graph.json").exists()   # orphan removed
    hygiene.prune(graph_blobs=True, force=True)
    assert not (tmp_path / "fabio" / "graph.json").exists()    # force removes it


def test_prune_target_rejects_path_traversal(tmp_path, monkeypatch):
    """A traversal/absolute target must be refused, not resolved outside the
    store and deleted."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    sibling = tmp_path.parent / "DO_NOT_DELETE"
    sibling.mkdir(exist_ok=True)
    try:
        res = hygiene.prune(target="../DO_NOT_DELETE")
        assert "refused" in res
        assert sibling.exists()                      # untouched
    finally:
        sibling.rmdir()


def test_prune_target_rejects_non_namespace_dir(tmp_path, monkeypatch):
    """A dir under the store that isn't a namespace (no markers) can't be pruned
    by name."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "downloads").mkdir()                 # incidental, no markers
    res = hygiene.prune(target="downloads")
    assert "refused" in res
    assert (tmp_path / "downloads").exists()


def test_prune_target_normalizes_casing(tmp_path, monkeypatch):
    """`prune('Fabio')` normalizes to the registered 'fabio' and is refused,
    rather than slipping the guard and hitting the real dir."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "fabio")
    _make_namespace(tmp_path, "fabio")
    res = hygiene.prune(target="Fabio")
    assert "refused" in res
    assert (tmp_path / "fabio").exists()


def test_prune_orphans_refused_on_corrupt_registry(tmp_path, monkeypatch):
    """A corrupt autosync.json yields empty known_projects(); bulk orphan prune
    must refuse rather than treat every namespace as an orphan and wipe it."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "autosync.json").write_text("{not json", encoding="utf-8")
    _make_namespace(tmp_path, "fabio")
    res = hygiene.prune(orphans=True)
    assert "refused" in res
    assert (tmp_path / "fabio").exists()             # not wiped


def test_prune_orphans_refused_on_missing_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "fabio")
    res = hygiene.prune(orphans=True)
    assert "refused" in res
    assert (tmp_path / "fabio").exists()


def test_prune_force_overrides_registered_target(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "fabio")
    _make_namespace(tmp_path, "fabio")
    res = hygiene.prune(target="fabio", force=True)
    assert res["removed_namespaces"]
    assert not (tmp_path / "fabio").exists()


def test_report_renders_oversized_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "bloated",
                    graph_bytes=hygiene.GRAPH_BLOB_WARN_BYTES + 1)
    assert "OVERSIZED" in hygiene.report()


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
