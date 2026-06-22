# tests/test_plugins.py
import json
from mindhive import plugins


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _skill(version_dir, slug, name, description):
    _write(version_dir / "skills" / slug / "SKILL.md",
           f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n")


def _plugin_json(version_dir, data):
    _write(version_dir / ".claude-plugin" / "plugin.json", json.dumps(data))


def make_cache(tmp_path):
    """Build a fake plugin cache exercising free/paid/dedupe/malformed cases."""
    cache = tmp_path / "cache"

    # free plugin: 3 skills across >=2 topics
    free = cache / "mkt" / "free-plugin" / "1.2.0"
    _plugin_json(free, {"name": "free-plugin", "version": "1.2.0",
                        "description": "a free one"})
    _skill(free, "a", "alpha", "Use when writing test suites and unit tests.")
    _skill(free, "b", "beta", "Use for security review and auth hardening.")
    _skill(free, "c", "gamma", "Use for frontend design work.")

    # older version of the same plugin (must be ignored in favor of 1.2.0)
    old = cache / "mkt" / "free-plugin" / "1.0.0"
    _plugin_json(old, {"name": "free-plugin", "version": "1.0.0",
                       "description": "old"})
    _skill(old, "a", "alpha", "Use when writing tests.")

    # paid plugin: requires an API key
    paid = cache / "mkt" / "paid-plugin" / "1.0.0"
    _plugin_json(paid, {"name": "paid-plugin", "version": "1.0.0",
                        "description": "needs key",
                        "configuration": {"required": {"OPENAI_API_KEY": "k"}}})
    _skill(paid, "x", "x", "Use for data analysis.")

    # mcp plugin: auth-gated server
    mcp = cache / "mkt" / "mcp-plugin" / "2.0.0"
    _plugin_json(mcp, {"name": "mcp-plugin", "version": "2.0.0",
                       "description": "mcp", "mcpServers": {"svc": {}}})

    # sdk plugin: depends on a paid SDK
    sdk = cache / "mkt" / "sdk-plugin" / "1.0.0"
    _plugin_json(sdk, {"name": "sdk-plugin", "version": "1.0.0",
                       "description": "sdk", "dependencies": ["anthropic>=1.0"]})

    # malformed plugin.json (must be skipped, not crash)
    bad = cache / "mkt" / "bad-plugin" / "1.0.0"
    _write(bad / ".claude-plugin" / "plugin.json", "{ not json ")

    # temp_local scratch dir (must be ignored)
    tmp = cache / "temp_local_123" / "whatever" / "1.0.0"
    _plugin_json(tmp, {"name": "scratch", "version": "1.0.0"})

    return cache


def test_scan_finds_plugins_and_skills(tmp_path):
    cache = make_cache(tmp_path)
    found = plugins.scan(cache)
    names = {p["name"] for p in found}
    assert "free-plugin" in names
    assert "scratch" not in names           # temp_local skipped
    assert "bad-plugin" not in names         # malformed skipped
    free = next(p for p in found if p["name"] == "free-plugin")
    assert free["version"] == "1.2.0"        # newest version wins
    assert len(free["skills"]) == 3
    assert free["skills"][0]["trigger"].startswith("Use when")


def test_is_free_classifies(tmp_path):
    cache = make_cache(tmp_path)
    found = {p["name"]: p for p in plugins.scan(cache)}
    assert plugins.is_free(found["free-plugin"])[0] is True
    assert plugins.is_free(found["paid-plugin"])[0] is False
    assert "OPENAI_API_KEY" in plugins.is_free(found["paid-plugin"])[1]
    assert plugins.is_free(found["mcp-plugin"])[0] is False
    assert "MCP" in plugins.is_free(found["mcp-plugin"])[1]
    assert plugins.is_free(found["sdk-plugin"])[0] is False


def test_score_is_deterministic(tmp_path):
    cache = make_cache(tmp_path)
    free = next(p for p in plugins.scan(cache) if p["name"] == "free-plugin")
    s1, s2 = plugins.score(free), plugins.score(free)
    assert s1 == s2
    assert s1["trigger_count"] == 3
    assert s1["topic_spread"] >= 2           # test + security + frontend
    assert s1["breadth_score"] == 3 + s1["topic_spread"] * plugins.BREADTH_WEIGHT


def test_evaluate_recommends_free_only(tmp_path):
    cache = make_cache(tmp_path)
    payload = plugins.evaluate(cache, top_n=5)
    assert payload["counts"]["free"] == 1
    assert payload["counts"]["paid"] == 3
    rec_names = {r["name"] for r in payload["recommendations"]}
    assert rec_names == {"free-plugin"}
    assert all(p["free"] for p in payload["free_plugins"])
    assert len(payload["recommendations"]) <= 5


def test_evaluate_empty_cache(tmp_path):
    payload = plugins.evaluate(tmp_path / "nope")
    assert payload["counts"]["plugins_total"] == 0
    assert payload["recommendations"] == []
    # provenance is stamped even on an empty scan
    assert payload["engine"] == "claude-opus-4-8"


def test_evaluate_stamps_engine(tmp_path):
    cache = make_cache(tmp_path)
    payload = plugins.evaluate(cache)
    assert payload["engine"] == plugins.ENGINE == "claude-opus-4-8"
