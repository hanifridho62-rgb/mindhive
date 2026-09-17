# tests/test_cli_plugins.py — `hive plugins list`, the affordance the CLI already
# advertised but did not have.
"""Two places promised this command before it existed:

  * the CLI (`__main__.py` then, `commands/registry_sop.py` now) prints the
    hint "(hive skills list / hive plugins list)" — the first half works, the
    second exited 2.
  * the MANDATORY SOP resident in MEMORY.md, re-injected on every prompt, carries
    "hive plugins list still does not run".

`recommend` prints the top-N free plugins. `list` prints the whole stored
snapshot — free and paid — and must never trigger a fresh scan: the catalog on
disk is the point, and walking the plugin cache is the expensive path `scan` and
`autoscan` already own.
"""
import json

from mindhive import __main__ as cli_mod

_FAKE_CATALOG = {
    "scanned_at": "2026-08-16T00:00:00Z",
    "engine": "test-engine",
    "cache_dir": "/nowhere",
    "counts": {"plugins_total": 3, "free": 2, "paid": 1, "skills_total": 12},
    "free_plugins": [
        {"name": "compound-engineering", "version": "1.0.0", "skills": [],
         "score": {"breadth_score": 52, "rank_score": 52.0,
                   "effectiveness": {"success_rate": 1.0, "demoted": False,
                                     "demote_reason": ""}}},
        {"name": "superpowers", "version": "2.1.0", "skills": [],
         "score": {"breadth_score": 19, "rank_score": 19.0,
                   "effectiveness": {"success_rate": 1.0, "demoted": False,
                                     "demote_reason": ""}}},
    ],
    "paid_plugins": [{"name": "some-paid", "version": "0.1",
                      "free_reason": "requires a licence key"}],
    "recommendations": [],
    "demoted": [],
}


def test_plugins_list_prints_every_catalogued_plugin(capsys, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "latest_catalog", lambda project=None: _FAKE_CATALOG)
    ret = cli_mod.main(["plugins", "list"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "compound-engineering" in out
    assert "superpowers" in out
    # `recommend` caps at top-N free; `list` is the whole snapshot, paid included
    assert "some-paid" in out


def test_plugins_list_as_json_emits_the_raw_snapshot(capsys, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "latest_catalog", lambda project=None: _FAKE_CATALOG)
    ret = cli_mod.main(["plugins", "list", "--json"])
    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["plugins_total"] == 3
    assert data["free_plugins"][0]["name"] == "compound-engineering"


def test_plugins_list_without_a_catalog_instructs_instead_of_crashing(capsys, monkeypatch):
    """Fail-open with the command that fixes it — the sibling `recommend`
    already sets this convention."""
    from mindhive import plugins
    monkeypatch.setattr(plugins, "latest_catalog", lambda project=None: None)
    ret = cli_mod.main(["plugins", "list"])
    assert ret == 0
    assert "hive plugins scan" in capsys.readouterr().out


def test_the_empty_case_still_parses_under_json(capsys, monkeypatch):
    """`--json` is a stream contract, and the empty case is where it broke: the
    prose hint went to STDOUT under `--json` too, so `hive plugins list --json |
    jq` failed to parse on exactly the machine that had never scanned. The hint
    is still emitted — on stderr, where a human reads it and a pipe does not."""
    from mindhive import plugins
    monkeypatch.setattr(plugins, "latest_catalog", lambda project=None: None)
    ret = cli_mod.main(["plugins", "list", "--json"])
    captured = capsys.readouterr()
    assert ret == 0
    assert json.loads(captured.out) == {}
    assert "hive plugins scan" in captured.err


def test_plugins_list_never_rescans(capsys, monkeypatch):
    """CANARY: a `list` that quietly walked the plugin cache would pass every
    assertion above while costing what `scan` costs."""
    from mindhive import plugins
    walked = []
    monkeypatch.setattr(plugins, "latest_catalog", lambda project=None: _FAKE_CATALOG)
    monkeypatch.setattr(plugins, "scan", lambda *a, **kw: walked.append("scan") or [])
    monkeypatch.setattr(plugins, "scan_and_store",
                        lambda *a, **kw: walked.append("scan_and_store") or {})
    monkeypatch.setattr(plugins, "ensure_fresh",
                        lambda *a, **kw: walked.append("ensure_fresh") or {})
    cli_mod.main(["plugins", "list"])
    assert walked == []
