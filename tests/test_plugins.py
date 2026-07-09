# tests/test_plugins.py
import json
import pytest
from mindhive import plugins, events, invoker


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
# store round-trip (latest-only sidecar — NOT the append-only ledger)
# --------------------------------------------------------------------------

def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def test_scan_and_store_writes_sidecar_not_event(tmp_path, monkeypatch):
    """The catalog must NOT be appended to the hash-chained ledger — it goes to a
    sidecar so daily autoscans never bloat (and never tamper with) the chain."""
    _store(tmp_path, monkeypatch)
    cache = make_cache(tmp_path)
    plugins.scan_and_store(cache_dir=cache)
    assert not [e for e in events.read_all() if e.type == "plugin_catalog"]
    snap = plugins.latest_catalog()
    assert snap and snap["engine"] == "claude-opus-4-8"
    assert plugins._catalog_path().exists()


def test_repeated_scans_overwrite_sidecar_no_growth(tmp_path, monkeypatch):
    """N scans leave exactly ONE sidecar and ZERO ledger events — the bug the
    audit flagged (unbounded plugin_catalog growth) is gone by construction."""
    _store(tmp_path, monkeypatch)
    cache = make_cache(tmp_path)
    for _ in range(5):
        plugins.scan_and_store(cache_dir=cache)
    assert plugins._catalog_path().exists()
    assert not [e for e in events.read_all() if e.type == "plugin_catalog"]


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
    assert len(latest["recommendations"]) == 2     # the second (most recent) scan won


# --------------------------------------------------------------------------
# ensure_fresh — stale-gated auto-refresh (powers the session hook & panel)
# --------------------------------------------------------------------------

def _has_sidecar():
    return plugins._catalog_path().exists()


# --------------------------------------------------------------------------
# Live inventory — what is installed RIGHT NOW (registry-driven, not cache)
# --------------------------------------------------------------------------

def make_claude_dir(tmp_path):
    """Fake ~/.claude with one installed plugin, one cached-but-uninstalled
    plugin, and one standalone local skill."""
    claude = tmp_path / "claude"
    inst = claude / "plugins" / "cache" / "mkt" / "alpha" / "1.0.0"
    _plugin_json(inst, {"name": "alpha", "version": "1.0.0",
                        "description": "installed one"})
    _skill(inst, "s1", "one", "Use for testing.")
    _skill(inst, "s2", "two", "Use for deploys.")
    # cached but NOT in the registry -> uninstalled -> must not appear
    ghost = claude / "plugins" / "cache" / "mkt" / "ghost" / "1.0.0"
    _plugin_json(ghost, {"name": "ghost", "version": "1.0.0"})
    _skill(ghost, "g", "g", "Use for nothing.")
    _write(claude / "plugins" / "installed_plugins.json", json.dumps({
        "version": 2,
        "plugins": {"alpha@mkt": [{"scope": "user", "version": "1.0.0",
                                   "installPath": str(inst),
                                   "installedAt": "2026-06-11T04:07:43.030Z"}]},
    }))
    _write(claude / "skills" / "local-note" / "SKILL.md",
           "---\nname: local-note\ndescription: Use when noting locally.\n---\n"
           "\n# Local note\n\nBody text for the local skill.\n")
    return claude


def test_inventory_lists_only_installed_plugins(tmp_path):
    claude = make_claude_dir(tmp_path)
    inv = plugins.inventory(claude)
    names = {p["name"] for p in inv["plugins"]}
    assert names == {"alpha"}                    # ghost is cached but uninstalled
    alpha = inv["plugins"][0]
    assert alpha["marketplace"] == "mkt"
    assert alpha["version"] == "1.0.0"
    assert [s["name"] for s in alpha["skills"]] == ["one", "two"]


def test_inventory_includes_local_skills(tmp_path):
    claude = make_claude_dir(tmp_path)
    inv = plugins.inventory(claude)
    assert inv["local_skills"] == [
        {"name": "local-note", "trigger": "Use when noting locally."}]


def test_inventory_counts(tmp_path):
    claude = make_claude_dir(tmp_path)
    c = plugins.inventory(claude)["counts"]
    assert c == {"plugins": 1, "plugin_skills": 2,
                 "local_skills": 1, "skills_total": 3}


def test_inventory_missing_registry_lists_no_plugins(tmp_path):
    claude = tmp_path / "claude"
    _write(claude / "skills" / "solo" / "SKILL.md",
           "---\nname: solo\ndescription: Use alone.\n---\n")
    inv = plugins.inventory(claude)
    assert inv["plugins"] == []
    assert [s["name"] for s in inv["local_skills"]] == ["solo"]


def test_inventory_malformed_registry_lists_no_plugins(tmp_path):
    claude = tmp_path / "claude"
    _write(claude / "plugins" / "installed_plugins.json", "{ nope ")
    inv = plugins.inventory(claude)
    assert inv["plugins"] == []


def test_inventory_keeps_entry_when_install_path_is_gone(tmp_path):
    """Registry says installed but the folder vanished -> still listed (with a
    present:false marker) so the user sees the broken install, not silence."""
    claude = tmp_path / "claude"
    _write(claude / "plugins" / "installed_plugins.json", json.dumps({
        "version": 2,
        "plugins": {"lost@mkt": [{"version": "2.0.0",
                                  "installPath": str(tmp_path / "nowhere")}]},
    }))
    inv = plugins.inventory(claude)
    lost = inv["plugins"][0]
    assert lost["name"] == "lost" and lost["present"] is False
    assert lost["skills"] == []


def test_inventory_dedupes_registry_entries_newest_version_wins(tmp_path):
    claude = make_claude_dir(tmp_path)
    reg = json.loads((claude / "plugins" / "installed_plugins.json")
                     .read_text(encoding="utf-8"))
    old = claude / "plugins" / "cache" / "mkt" / "alpha" / "0.9.0"
    _plugin_json(old, {"name": "alpha", "version": "0.9.0"})
    reg["plugins"]["alpha@mkt"].append({"scope": "project", "version": "0.9.0",
                                        "installPath": str(old)})
    _write(claude / "plugins" / "installed_plugins.json", json.dumps(reg))
    inv = plugins.inventory(claude)
    assert len(inv["plugins"]) == 1
    assert inv["plugins"][0]["version"] == "1.0.0"


def test_signature_changes_on_uninstall(tmp_path):
    claude = make_claude_dir(tmp_path)
    sig1 = plugins.inventory_signature(claude)
    _write(claude / "plugins" / "installed_plugins.json",
           json.dumps({"version": 2, "plugins": {}}))
    assert plugins.inventory_signature(claude) != sig1


def test_signature_changes_when_local_skill_added_and_deleted(tmp_path):
    claude = make_claude_dir(tmp_path)
    sig1 = plugins.inventory_signature(claude)
    new = claude / "skills" / "fresh" / "SKILL.md"
    _write(new, "---\nname: fresh\ndescription: Use freshly.\n---\n")
    sig2 = plugins.inventory_signature(claude)
    assert sig2 != sig1
    new.unlink()
    new.parent.rmdir()
    sig3 = plugins.inventory_signature(claude)
    assert sig3 != sig2


def test_signature_changes_when_plugin_skill_deleted(tmp_path):
    claude = make_claude_dir(tmp_path)
    sig1 = plugins.inventory_signature(claude)
    gone = (claude / "plugins" / "cache" / "mkt" / "alpha" / "1.0.0"
            / "skills" / "s2" / "SKILL.md")
    gone.unlink()
    assert plugins.inventory_signature(claude) != sig1


def _ok_runner(p): return {"exit": 0, "stdout": '{"is_error": false, "result": "ok"}'}
def _fail_runner(p): return {"exit": 1, "stdout": '{"is_error": true, "result": "boom"}'}


def test_call_status_empty_is_empty():
    assert plugins.call_status(project="test") == {}


def test_call_status_reflects_last_outcome():
    invoker.invoke_skill("good", runner=_ok_runner, project="test")
    invoker.invoke_skill("bad", runner=_fail_runner, project="test")
    st = plugins.call_status(project="test")
    assert st["good"] == "ok"
    assert st["bad"] == "fail"


def test_call_status_last_call_wins_over_earlier():
    invoker.invoke_skill("x", runner=_fail_runner, project="test")   # red first
    invoker.invoke_skill("x", runner=_ok_runner, project="test")     # then green
    assert plugins.call_status(project="test")["x"] == "ok"


def test_call_history_newest_first_with_outcomes():
    invoker.invoke_skill("h", runner=_ok_runner, project="test")
    invoker.invoke_skill("h", runner=_fail_runner, project="test")
    hist = plugins.call_history("h", project="test")
    assert [e["ok"] for e in hist] == [False, True]     # newest (the fail) first
    assert all("ts" in e and "text" in e for e in hist)


def test_call_history_filters_by_skill():
    invoker.invoke_skill("a", runner=_ok_runner, project="test")
    invoker.invoke_skill("b", runner=_fail_runner, project="test")
    assert [e["ok"] for e in plugins.call_history("a", project="test")] == [True]
    assert [e["ok"] for e in plugins.call_history("b", project="test")] == [False]


def test_call_history_respects_limit():
    for _ in range(5):
        invoker.invoke_skill("x", runner=_ok_runner, project="test")
    assert len(plugins.call_history("x", project="test", limit=3)) == 3


def test_call_history_empty_for_uncalled_skill():
    assert plugins.call_history("never-called", project="test") == []


# --------------------------------------------------------------------------
# Effectiveness scoring + demotion gate
# --------------------------------------------------------------------------

def test_call_counts_tallies_every_occurrence(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    from mindhive import events
    for _ in range(3):
        events.append_event("invoke_result", {"name": "a:x", "text": "a:x"},
                            project="p")
    events.append_event("tool_failed", {"signature": "skill:a:x"}, project="p")
    assert plugins.call_counts(project="p") == {"a:x": {"ok": 3, "fail": 1}}


def test_effectiveness_neutral_without_history():
    plugin = {"skills": [{"name": "a:x", "trigger": "t"}]}
    eff = plugins.effectiveness(plugin, {})
    assert eff["calls"] == 0
    assert eff["success_rate"] == 0.5      # Laplace prior, neutral
    assert eff["demoted"] is False


def test_effectiveness_demotes_repeat_failures():
    plugin = {"skills": [{"name": "a:x", "trigger": "t"}]}
    counts = {"a:x": {"ok": 0, "fail": 4}}
    eff = plugins.effectiveness(plugin, counts)
    assert eff["calls"] == 4
    assert eff["demoted"] is True
    assert "fail" in eff["demote_reason"]


def test_effectiveness_scales_rank_score():
    plugin = {"skills": [{"name": "a:x", "trigger": "t"}]}
    good = plugins.effectiveness(plugin, {"a:x": {"ok": 8, "fail": 0}})
    bad = plugins.effectiveness(plugin, {"a:x": {"ok": 0, "fail": 8}})
    assert good["success_rate"] > 0.8 and bad["success_rate"] < 0.2


def test_strip_frontmatter_returns_body_only():
    body = plugins._strip_frontmatter(
        "---\nname: x\ndescription: d\n---\n\n# Title\n\nParagraph.\n")
    assert body == "# Title\n\nParagraph."
    # no frontmatter -> whole text back, trimmed
    assert plugins._strip_frontmatter("# Just body\n") == "# Just body"


def test_skill_detail_local_puts_intended_use_and_body(tmp_path):
    claude = make_claude_dir(tmp_path)
    d = plugins.skill_detail("local-note", claude_dir=claude)
    assert d["name"] == "local-note"
    assert d["intended_use"] == "Use when noting locally."
    assert "Body text for the local skill." in d["body"]
    assert d["source"] == "local"
    assert d["added_at"]                          # local skills date from mtime


def test_skill_detail_plugin_dates_from_registry(tmp_path):
    claude = make_claude_dir(tmp_path)
    d = plugins.skill_detail("one", plugin="alpha", claude_dir=claude)
    assert d["intended_use"] == "Use for testing."
    assert d["source"] == "alpha"
    assert d["added_at"] == "2026-06-11T04:07:43.030Z"


def test_skill_detail_missing_returns_none(tmp_path):
    claude = make_claude_dir(tmp_path)
    assert plugins.skill_detail("nope", claude_dir=claude) is None
    assert plugins.skill_detail("one", plugin="ghost", claude_dir=claude) is None


def test_live_inventory_caches_until_disk_changes(tmp_path):
    claude = make_claude_dir(tmp_path)
    first = plugins.live_inventory(claude)
    assert first["changed"] is True
    second = plugins.live_inventory(claude)
    assert second["changed"] is False
    assert second["inventory"] == first["inventory"]
    _write(claude / "skills" / "fresh" / "SKILL.md",
           "---\nname: fresh\ndescription: Use freshly.\n---\n")
    third = plugins.live_inventory(claude)
    assert third["changed"] is True
    assert "fresh" in {s["name"] for s in third["inventory"]["local_skills"]}


def test_ensure_fresh_scans_when_no_snapshot(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    res = plugins.ensure_fresh(cache_dir=tmp_path / "nope")
    assert res["scanned"] is True
    assert res["age_hours"] == 0.0
    assert _has_sidecar()


def test_ensure_fresh_skips_when_fresh(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    plugins.ensure_fresh(cache_dir=tmp_path / "nope")          # first, real scan
    res = plugins.ensure_fresh(cache_dir=tmp_path / "nope")    # second, should skip
    assert res["scanned"] is False
    assert res["payload"]                                       # still returns the snapshot


def test_ensure_fresh_rescans_when_stale(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    # seed an ancient sidecar snapshot directly
    plugins._write_catalog({"recommendations": [], "scanned_at": "2000-01-01T00:00:00Z"})
    res = plugins.ensure_fresh(cache_dir=tmp_path / "nope", max_age_hours=24)
    assert res["scanned"] is True
    # the stale snapshot was overwritten by a fresh one
    assert plugins.latest_catalog()["scanned_at"] != "2000-01-01T00:00:00Z"


def test_ensure_fresh_force_rescans_even_when_fresh(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    first = plugins.ensure_fresh(cache_dir=tmp_path / "nope")          # fresh snapshot exists
    res = plugins.ensure_fresh(cache_dir=tmp_path / "nope", force=True)
    assert res["scanned"] is True
