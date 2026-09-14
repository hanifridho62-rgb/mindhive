# tests/test_hygiene.py
import json
import os
import shutil

import pytest

from mindhive import (__main__ as cli_mod, dashboard_data, events, harvest,
                      hygiene, paths)


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


# --- which registered project a directory IS, and what no name reaches -------

def _link_dir(link, target):
    """*link* as a second name for the directory *target*: a junction on
    Windows, which needs no privilege, and a symlink elsewhere. Skips where the
    platform refuses both, rather than passing without the link it is about."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"cannot create a directory link here: {exc}")


def _unlink_dir(link):
    """Remove the link itself, never what it points at."""
    if os.name == "nt":
        os.rmdir(link)
    else:
        os.unlink(link)


def test_a_registered_project_is_no_orphan_because_its_name_normalizes(
        tmp_path, monkeypatch, capsys):
    """DISCRIMINATING, and it was data loss.

    `known_projects` normalizes every registry entry, so a project registered as
    `My_Proj` is known as `my-proj` -- while its directory keeps `My_Proj`, the
    name every read and write uses. The orphan prune compared the two
    spellings, found no match, and deleted a registered project's ledger."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "My_Proj", "keeper")
    _make_namespace(tmp_path, "My_Proj")
    # Armed: the registry holds only the normalized spelling, the disk the raw.
    assert paths.known_projects() == {"my-proj", "keeper"}
    assert paths.list_namespaces() == ["My_Proj"]

    assert hygiene.scan()[0]["registered"] is True
    assert hygiene.prune(orphans=True) == {"removed_namespaces": []}
    assert (tmp_path / "My_Proj" / "events").is_dir()
    assert cli_mod.main(["projects"]) == 0
    assert "[registered  ] My_Proj" in capsys.readouterr().out


def test_a_link_onto_a_registered_namespace_is_protected_and_never_followed(
        tmp_path, monkeypatch):
    """A link is a second name for one directory. It counts as registered by
    IDENTITY, so an orphan prune leaves it; and it is refused as a target even
    with `force`, because `shutil.rmtree` cannot delete a link and would not
    delete the right thing if it could."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "fabio")
    real = _make_namespace(tmp_path, "fabio")
    (real / "events" / "s.jsonl").write_text("{}\n", encoding="utf-8")
    link = tmp_path / "fabio-link"
    _link_dir(link, real)
    try:
        # Armed: the link is listed as a namespace in its own right.
        assert paths.list_namespaces() == ["fabio", "fabio-link"]
        rows = {r["namespace"]: r["registered"] for r in hygiene.scan()}
        assert rows == {"fabio": True, "fabio-link": True}
        assert hygiene.prune(orphans=True) == {"removed_namespaces": []}
        assert "refused" in hygiene.prune(target="fabio-link")
        forced = hygiene.prune(target="fabio-link", force=True)
        assert "are links" in forced.get("refused", ""), forced
        assert (real / "events" / "s.jsonl").exists()
    finally:
        _unlink_dir(link)


def test_an_orphan_prune_with_a_link_among_the_orphans_deletes_nothing(
        tmp_path, monkeypatch):
    """DISCRIMINATING on ORDER. `shutil.rmtree` raises on a link, so the old loop
    deleted every orphan sorted before one and then stopped on a traceback, the
    prune half done. The refusal now comes before the first deletion."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _register(tmp_path, "keeper")
    _make_namespace(tmp_path, "aaa-orphan")
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    (outside / "events").mkdir(parents=True, exist_ok=True)
    link = tmp_path / "zzz-link"
    _link_dir(link, outside)
    try:
        assert paths.list_namespaces() == ["aaa-orphan", "zzz-link"]   # armed
        res = hygiene.prune(orphans=True)
        assert "zzz-link" in res.get("refused", ""), res
        assert (tmp_path / "aaa-orphan" / "events").is_dir()
        assert (outside / "events").is_dir()
    finally:
        _unlink_dir(link)
        shutil.rmtree(outside, ignore_errors=True)


@pytest.mark.skipif(os.name != "nt",
                    reason="Win32 path aliasing: a trailing dot is stripped")
def test_a_trailing_dot_twin_is_reported_and_never_pruned(tmp_path, monkeypatch):
    """DISCRIMINATING, and it was data loss on Windows.

    Win32 strips a trailing dot, so `abc.` IS `abc` to every ordinary call --
    but an extended-length path creates a real second entry under that name.
    `list_namespaces` listed it, the registry did not know it, and the orphan
    prune's `rmtree` of `abc.` removed the registered `abc` and its events."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    events.append_event("decision", {"text": "a decision in a registered namespace"},
                        session_id="probe-sess", project="abc")
    _register(tmp_path, "abc")
    twin = "\\\\?\\" + str(tmp_path / "abc.")
    os.makedirs(twin + "\\events")
    try:
        # Armed: two directory entries, and the ordinary API sees one directory.
        assert {"abc", "abc."} <= set(os.listdir(tmp_path))
        assert os.path.samefile(tmp_path / "abc", tmp_path / "abc.")

        assert paths.list_namespaces() == ["abc"]
        assert paths.unlisted_namespaces() == ["abc."]
        assert "'abc.'" in hygiene.report()
        assert hygiene.prune(orphans=True) == {"removed_namespaces": []}
        assert list((tmp_path / "abc" / "events").iterdir())
    finally:
        shutil.rmtree(twin, ignore_errors=True)


def test_a_directory_no_name_reaches_is_reported_and_never_listed(
        tmp_path, monkeypatch, capsys):
    """`list_namespaces` hands out the names callers read and prune BY, and a
    directory `namespace_name` would not hand back unchanged is not one of
    them. Left out -- and never silently: `hive gc` and `hive projects` both
    name it, in `repr`, since the difference is often invisible."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "My Project")
    events.append_event("decision", {"text": "a decision under a traversal name"},
                        session_id="probe-sess", project="../escape")
    events.append_event("decision", {"text": "a decision in a canonical namespace"},
                        session_id="probe-sess", project="visible")
    slot = paths.namespace_name("../escape")
    assert (tmp_path / slot / "events").is_dir()     # armed: the slot is on disk

    assert paths.list_namespaces() == ["visible"]
    assert set(paths.unlisted_namespaces()) == {"My Project", slot}
    assert paths.event_namespaces() == ["visible"]
    # The walkers that address projects BY NAME share that one list.
    assert harvest.existing_projects() == ["visible"]
    assert dashboard_data._all_projects() == ["visible"]
    for argv in (["gc"], ["projects"]):
        assert cli_mod.main(argv) == 0
        out = capsys.readouterr().out
        assert "visible" in out, (argv, out)
        assert repr("My Project") in out and repr(slot) in out, (argv, out)


def test_an_unlisted_directory_is_reported_when_nothing_else_is(
        tmp_path, monkeypatch, capsys):
    """The early return: with no canonical namespace at all, the note is still
    printed rather than a bare "(no namespaces on disk yet)"."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    _make_namespace(tmp_path, "My Project")
    assert "'My Project'" in hygiene.report()
    assert cli_mod.main(["projects"]) == 0
    out = capsys.readouterr().out
    assert "(no namespaces on disk yet)" in out and "'My Project'" in out
