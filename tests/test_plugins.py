# tests/test_plugins.py
import json
import pytest
from mindhive import plugins, events


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


# --------------------------------------------------------------------------
# Frontmatter / SKILL.md parsing (skill-level)
# --------------------------------------------------------------------------

def test_frontmatter_extracts_name_and_description():
    fm = plugins._parse_frontmatter(
        "---\nname: my-skill\ndescription: Use when X happens.\n---\n\n# Body\n")
    assert fm["name"] == "my-skill"
    assert fm["description"] == "Use when X happens."


def test_frontmatter_strips_quotes():
    fm = plugins._parse_frontmatter('---\nname: "quoted"\ndescription: \'single\'\n---\n')
    assert fm["name"] == "quoted"
    assert fm["description"] == "single"


def test_frontmatter_missing_block_returns_empty():
    assert plugins._parse_frontmatter("# no frontmatter here\n") == {}


def test_frontmatter_unterminated_block_returns_empty():
    assert plugins._parse_frontmatter("---\nname: oops\nno closing fence\n") == {}


def test_skill_name_falls_back_to_directory(tmp_path):
    cache = tmp_path / "cache"
    v = cache / "mkt" / "p" / "1.0.0"
    _plugin_json(v, {"name": "p", "version": "1.0.0"})
    # SKILL.md with no name key -> should fall back to the skill directory name
    _write(v / "skills" / "the-dir" / "SKILL.md",
           "---\ndescription: Use for git rebases.\n---\n\n# x\n")
    found = next(p for p in plugins.scan(cache) if p["name"] == "p")
    assert found["skills"][0]["name"] == "the-dir"
    assert found["skills"][0]["trigger"] == "Use for git rebases."


def test_plugin_with_no_skills_dir(tmp_path):
    cache = tmp_path / "cache"
    v = cache / "mkt" / "bare" / "1.0.0"
    _plugin_json(v, {"name": "bare", "version": "1.0.0"})
    p = next(x for x in plugins.scan(cache) if x["name"] == "bare")
    assert p["skills"] == []
    s = plugins.score(p)
    assert s == {"trigger_count": 0, "topic_spread": 0, "breadth_score": 0.0}
    assert plugins.is_free(p)[0] is True


# --------------------------------------------------------------------------
# is_free classification variants (plugin-level)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["SVC_API_KEY", "MY_TOKEN", "X_SECRET", "FOO_KEY",
                                 "svc_api_key", "Some_Token"])
def test_is_free_flags_secret_config_variants(key):
    p = {"_config_required": {key: "v"}, "_mcp_servers": {}, "_dependencies": []}
    free, reason = plugins.is_free(p)
    assert free is False
    assert key in reason


def test_is_free_ignores_nonsecret_config():
    # a required-but-not-secret config knob should not mark a plugin paid
    p = {"_config_required": {"LOG_LEVEL": "info"}, "_mcp_servers": {}, "_dependencies": []}
    assert plugins.is_free(p)[0] is True


@pytest.mark.parametrize("dep", ["openai", "openai>=1.0", "openai==1.2.3",
                                 "anthropic ~= 0.3", "cohere[all]", "mistralai!=0.1"])
def test_is_free_flags_paid_sdk_specifiers(dep):
    p = {"_config_required": {}, "_mcp_servers": {}, "_dependencies": [dep]}
    free, reason = plugins.is_free(p)
    assert free is False
    assert "SDK" in reason


def test_is_free_allows_ordinary_dependency():
    p = {"_config_required": {}, "_mcp_servers": {}, "_dependencies": ["requests>=2", "rich"]}
    assert plugins.is_free(p)[0] is True


def test_is_free_precedence_secret_before_mcp():
    # both signals present -> reason should cite the secret (checked first)
    p = {"_config_required": {"X_API_KEY": "v"}, "_mcp_servers": {"s": {}},
         "_dependencies": []}
    free, reason = plugins.is_free(p)
    assert free is False and "X_API_KEY" in reason


# --------------------------------------------------------------------------
# topics + scoring (plugin-level)
# --------------------------------------------------------------------------

def test_topics_match_keywords(tmp_path):
    cache = tmp_path / "cache"
    v = cache / "mkt" / "p" / "1.0.0"
    _plugin_json(v, {"name": "p", "version": "1.0.0"})
    # chosen to avoid substring overlaps (e.g. data/database) so the count is exact
    _skill(v, "a", "a", "Use for security and deploy tasks.")          # security, deploy
    _skill(v, "b", "b", "Use for frontend mobile design.")            # frontend, mobile, design
    p = next(x for x in plugins.scan(cache) if x["name"] == "p")
    s = plugins.score(p)
    assert s["trigger_count"] == 2
    assert s["topic_spread"] == 5     # security, deploy, frontend, mobile, design
    assert s["breadth_score"] == 2 + 5 * plugins.BREADTH_WEIGHT


def test_topics_substring_overlap_is_known_behavior(tmp_path):
    # documents the approximation: "database" also satisfies the "data" keyword
    cache = tmp_path / "cache"
    v = cache / "mkt" / "p" / "1.0.0"
    _plugin_json(v, {"name": "p", "version": "1.0.0"})
    _skill(v, "a", "a", "Use for database tuning.")
    p = next(x for x in plugins.scan(cache) if x["name"] == "p")
    assert plugins._topics(p) >= {"data", "database"}


# --------------------------------------------------------------------------
# scan structural correctness
# --------------------------------------------------------------------------

def test_scan_ignores_codex_and_cursor_mirrors(tmp_path):
    cache = tmp_path / "cache"
    v = cache / "mkt" / "p" / "1.0.0"
    _plugin_json(v, {"name": "p", "version": "1.0.0"})          # .claude-plugin
    _write(v / ".codex-plugin" / "plugin.json", json.dumps({"name": "p", "version": "1.0.0"}))
    _write(v / ".cursor-plugin" / "plugin.json", json.dumps({"name": "p", "version": "1.0.0"}))
    found = [p for p in plugins.scan(cache) if p["name"] == "p"]
    assert len(found) == 1          # mirrors must not produce duplicates


def test_scan_records_marketplace(tmp_path):
    cache = make_cache(tmp_path)
    free = next(p for p in plugins.scan(cache) if p["name"] == "free-plugin")
    assert free["marketplace"] == "mkt"


# --------------------------------------------------------------------------
# recommend ranking / limits
# --------------------------------------------------------------------------

def test_recommend_respects_top_n(tmp_path):
    cache = tmp_path / "cache"
    # 7 free plugins with descending breadth (more skills = higher score)
    for i in range(7):
        v = cache / "mkt" / f"p{i}" / "1.0.0"
        _plugin_json(v, {"name": f"p{i}", "version": "1.0.0"})
        for j in range(i + 1):
            _skill(v, f"s{j}", f"s{j}", "Use for testing.")
    payload = plugins.evaluate(cache, top_n=3)
    assert len(payload["recommendations"]) == 3
    # highest skill count (p6) first
    assert payload["recommendations"][0]["name"] == "p6"
    scores = [r["breadth_score"] for r in payload["recommendations"]]
    assert scores == sorted(scores, reverse=True)


def test_recommend_tiebreak_alphabetical(tmp_path):
    cache = tmp_path / "cache"
    # two plugins, identical breadth -> alphabetical order is stable
    for name in ("zebra", "alpha"):
        v = cache / "mkt" / name / "1.0.0"
        _plugin_json(v, {"name": name, "version": "1.0.0"})
        _skill(v, "a", "a", "Use for testing.")
    payload = plugins.evaluate(cache, top_n=5)
    names = [r["name"] for r in payload["recommendations"]]
    assert names == ["alpha", "zebra"]


# --------------------------------------------------------------------------
# store round-trip (event integration)
# --------------------------------------------------------------------------

def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def test_scan_and_store_appends_one_event(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    cache = make_cache(tmp_path)
    plugins.scan_and_store(cache_dir=cache)
    cat = [e for e in events.read_all() if e.type == "plugin_catalog"]
    assert len(cat) == 1
    assert cat[0].payload["engine"] == "claude-opus-4-8"


def test_latest_catalog_returns_none_when_absent(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    assert plugins.latest_catalog() is None


def test_latest_catalog_returns_most_recent(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    # a cache with 3 free plugins so top_n actually distinguishes the snapshots
    cache = tmp_path / "cache3"
    for name in ("aa", "bb", "cc"):
        v = cache / "mkt" / name / "1.0.0"
        _plugin_json(v, {"name": name, "version": "1.0.0"})
        _skill(v, "s", "s", "Use for testing.")
    plugins.scan_and_store(cache_dir=cache, top_n=3)
    plugins.scan_and_store(cache_dir=cache, top_n=2)
    latest = plugins.latest_catalog()
    assert len(latest["recommendations"]) == 2     # the second (most recent) scan
    assert len([e for e in events.read_all() if e.type == "plugin_catalog"]) == 2
