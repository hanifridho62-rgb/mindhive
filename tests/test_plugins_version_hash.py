# tests/test_plugins_version_hash.py
"""Bug 8 (spec section 2.8, plan P4): the plugin catalog is stamped with the
plugin version it was scanned under.

`plugins._write_catalog` stores `version_hash = sha256(<.claude-plugin/plugin.json
version> + "\\n" + <inventory_signature>)` beside the snapshot, and
`plugins.latest_catalog` refuses a snapshot whose stamp no longer matches the
disk. A catalog scanned before a plugin version bump, or before a plugin was
installed or a skill added, is therefore never served as the current one -- the
session-start autoscan (`ensure_fresh`) sees None and rescans instead of
trusting a snapshot that merely looks recent.
"""
import hashlib
import json
from pathlib import Path

from mindhive import paths, plugins

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- fixture builders -------------------------------------------------------

def _repo(tmp_path, version="0.5.0"):
    """A checkout whose plugin manifest declares `version` (rewritten in place)."""
    root = tmp_path / "repo"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": "mindhive", "version": version}),
                        encoding="utf-8")
    return root


def _claude(tmp_path):
    """A Claude config dir with an empty plugin registry."""
    root = tmp_path / "claude"
    registry = root / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"version": 2, "plugins": {}}), encoding="utf-8")
    return root


def _add_skill(claude_dir, slug="fresh"):
    skill = claude_dir / "skills" / slug / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\nname: {slug}\ndescription: Use for tests.\n---\n",
                     encoding="utf-8")
    return skill


SNAPSHOT = {"recommendations": [], "scanned_at": "2026-09-28T00:00:00Z"}


# --- the manifest version ---------------------------------------------------

def test_the_manifest_version_is_read_from_the_real_repo():
    """Pins the committed manifest, not a fixture: the loader and the file
    cannot drift apart silently."""
    manifest = REPO_ROOT / ".claude-plugin" / "plugin.json"
    declared = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    assert declared
    assert plugins.plugin_manifest_version(REPO_ROOT) == declared
    # the default root is the checkout this module was imported from
    assert plugins.plugin_manifest_version() == declared


def test_a_repo_without_a_readable_manifest_has_the_empty_version(tmp_path):
    assert plugins.plugin_manifest_version(tmp_path) == ""       # no file
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text("{not json", encoding="utf-8")
    assert plugins.plugin_manifest_version(tmp_path) == ""       # corrupt
    manifest.write_text(json.dumps(["0.5.0"]), encoding="utf-8")
    assert plugins.plugin_manifest_version(tmp_path) == ""       # not an object
    # canary: the same path with a real manifest does answer
    manifest.write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    assert plugins.plugin_manifest_version(tmp_path) == "9.9.9"


# --- the stamp --------------------------------------------------------------

def test_the_version_hash_is_sha256_of_the_version_and_the_inventory(tmp_path):
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    signature = plugins.inventory_signature(claude)
    want = hashlib.sha256(f"0.5.0\n{signature}".encode("utf-8")).hexdigest()
    assert plugins.catalog_version_hash(claude_dir=claude, repo_root=repo) == want


def test_the_stamp_is_stable_and_moves_with_the_plugin_version(tmp_path):
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    before = plugins.catalog_version_hash(claude_dir=claude, repo_root=repo)
    assert plugins.catalog_version_hash(claude_dir=claude, repo_root=repo) == before
    _repo(tmp_path, version="0.6.0")
    assert plugins.catalog_version_hash(claude_dir=claude, repo_root=repo) != before


def test_the_stamp_moves_with_the_inventory(tmp_path):
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    before = plugins.catalog_version_hash(claude_dir=claude, repo_root=repo)
    _add_skill(claude)
    assert plugins.catalog_version_hash(claude_dir=claude, repo_root=repo) != before


# --- write and read ---------------------------------------------------------

def test_a_written_catalog_carries_the_stamp_and_reads_back(tmp_path):
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    stamp = plugins.catalog_version_hash(claude_dir=claude, repo_root=repo)
    stored = plugins._write_catalog(dict(SNAPSHOT), claude_dir=claude, repo_root=repo)
    assert stored["version_hash"] == stamp
    on_disk = json.loads(plugins._catalog_path().read_text(encoding="utf-8"))
    assert on_disk["version_hash"] == stamp
    got = plugins.latest_catalog(claude_dir=claude, repo_root=repo)
    assert got is not None
    assert got["version_hash"] == stamp
    assert got["scanned_at"] == SNAPSHOT["scanned_at"]


def test_the_caller_payload_is_not_mutated(tmp_path):
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    payload = dict(SNAPSHOT)
    plugins._write_catalog(payload, claude_dir=claude, repo_root=repo)
    assert "version_hash" not in payload


def test_a_plugin_version_bump_invalidates_the_stored_catalog(tmp_path):
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    plugins._write_catalog(dict(SNAPSHOT), claude_dir=claude, repo_root=repo)
    _repo(tmp_path, version="0.6.0")
    assert plugins.latest_catalog(claude_dir=claude, repo_root=repo) is None
    # canary: back at the scanned version the very same file is served again,
    # so the None above came from the stamp and not from a broken read
    _repo(tmp_path, version="0.5.0")
    assert plugins.latest_catalog(claude_dir=claude, repo_root=repo) is not None


def test_an_inventory_change_invalidates_the_stored_catalog(tmp_path):
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    plugins._write_catalog(dict(SNAPSHOT), claude_dir=claude, repo_root=repo)
    skill = _add_skill(claude)
    assert plugins.latest_catalog(claude_dir=claude, repo_root=repo) is None
    skill.unlink()                       # canary: the original disk state again
    assert plugins.latest_catalog(claude_dir=claude, repo_root=repo) is not None


def test_a_snapshot_without_a_stamp_is_not_served(tmp_path):
    """A snapshot written before stamping existed -- or by hand -- cannot say
    which plugin version scanned it, so it is not current."""
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    paths.write_text_atomic(plugins._catalog_path(), json.dumps(SNAPSHOT))
    assert plugins.latest_catalog(claude_dir=claude, repo_root=repo) is None
    # canary: the stamped write of the same snapshot is served
    plugins._write_catalog(dict(SNAPSHOT), claude_dir=claude, repo_root=repo)
    assert plugins.latest_catalog(claude_dir=claude, repo_root=repo) is not None


def test_a_forged_stamp_is_not_served(tmp_path):
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    forged = dict(SNAPSHOT, version_hash="0" * 64)
    paths.write_text_atomic(plugins._catalog_path(), json.dumps(forged))
    assert plugins.latest_catalog(claude_dir=claude, repo_root=repo) is None


WRONG_SHAPES = ([1], "text", {"version": 2, "plugins": [1]},
                {"version": 2, "plugins": {"x@y": 5}},
                {"version": 2, "plugins": {"x@y": [5, "e"]}})


def test_a_registry_of_the_wrong_shape_never_raises_out_of_the_catalog(tmp_path):
    """`latest_catalog` answered the payload or None before it computed a stamp,
    and it still must: a registry that PARSES into the wrong type (`[1]`, or a
    plugin whose entries are not a list) raised AttributeError / TypeError out
    of `_read_registry` and `inventory_signature`, and through them out of the
    graph build, the dashboard, `hive skills` and the session-start autoscan."""
    repo, claude = _repo(tmp_path), _claude(tmp_path)
    registry = claude / "plugins" / "installed_plugins.json"
    for body in WRONG_SHAPES:
        registry.write_text(json.dumps(body), encoding="utf-8")
        read = plugins._read_registry(claude)
        assert isinstance(read, dict), body
        assert all(isinstance(v, list) for v in read.values()), body
        assert len(plugins.inventory_signature(claude)) == 64, body
        assert plugins.inventory(claude)["counts"]["plugins"] == 0, body
        # a snapshot stamped under this very registry is served: an answer,
        # and the right one, never a raise
        stored = plugins._write_catalog(dict(SNAPSHOT), claude_dir=claude,
                                        repo_root=repo)
        assert plugins.latest_catalog(claude_dir=claude, repo_root=repo) == stored
    # canary: a well-formed registry's plugins are still read
    registry.write_text(json.dumps({"version": 2, "plugins": {
        "x@y": [{"scope": "user", "installPath": str(tmp_path / "x"),
                 "version": "1.0.0"}]}}), encoding="utf-8")
    assert list(plugins._read_registry(claude)) == ["x@y"]
    assert plugins.inventory(claude)["counts"]["plugins"] == 1


def test_scan_and_store_returns_the_stamped_snapshot_it_stored(tmp_path):
    payload = plugins.scan_and_store(cache_dir=tmp_path / "no-cache")
    assert payload["version_hash"] == plugins.catalog_version_hash()
    assert plugins.latest_catalog() == payload


def test_ensure_fresh_rescans_a_recent_catalog_from_another_plugin_version(
        tmp_path, monkeypatch):
    """The payoff at session start: a snapshot minutes old is still replaced
    when the plugin version it was scanned under is gone."""
    monkeypatch.setattr(plugins, "plugin_manifest_version",
                        lambda repo_root=None: "0.5.0")
    first = plugins.ensure_fresh(cache_dir=tmp_path / "no-cache")
    assert first["scanned"] is True
    # canary: same version, fresh snapshot -> no rescan
    again = plugins.ensure_fresh(cache_dir=tmp_path / "no-cache")
    assert again["scanned"] is False
    monkeypatch.setattr(plugins, "plugin_manifest_version",
                        lambda repo_root=None: "0.6.0")
    bumped = plugins.ensure_fresh(cache_dir=tmp_path / "no-cache")
    assert bumped["scanned"] is True
    assert bumped["payload"]["version_hash"] == plugins.catalog_version_hash()
