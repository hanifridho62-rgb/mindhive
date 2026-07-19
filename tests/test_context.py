# tests/test_context.py
"""Tests for the context-budget analyzer.

The autouse ``isolate_store`` fixture in conftest points MINDHIVE_HOME at a
throwaway tmp dir and pins MINDHIVE_PROJECT to "test", so every collector here
reads a synthetic store. The Claude-surface collector takes its roots as
arguments for the same reason — nothing in this file may touch the real
~/.mindhive or ~/.claude."""
import json
from pathlib import Path

from mindhive import context, paths


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_plugin_cache(root: Path, plugins: dict[str, dict[str, str]]) -> Path:
    """Build a plugin cache matching the layout plugins.scan() walks:
    <cache>/<marketplace>/<plugin>/<version>/.claude-plugin/plugin.json
    <cache>/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md
    `plugins` maps plugin name -> {skill name: SKILL.md body}."""
    for pname, skills in plugins.items():
        base = root / "mkt" / pname / "1.0.0"
        _write(base / ".claude-plugin" / "plugin.json",
               json.dumps({"name": pname, "version": "1.0.0",
                           "description": f"{pname} plugin"}))
        for sname, body in skills.items():
            _write(base / "skills" / sname / "SKILL.md", body)
    return root


def _skill_md(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody text here.\n"


def _seg(name="s", origin="hive", tokens=10, nbytes=40, kind="artifact", path=""):
    return context.Segment(name=name, origin=origin, path=path, nbytes=nbytes,
                           tokens=tokens, kind=kind)


# --------------------------------------------------------------------------
# U1 — token estimation
# --------------------------------------------------------------------------

def test_estimate_tokens_empty_is_zero():
    assert context.estimate_tokens("") == 0


def test_estimate_tokens_known_string_is_stable():
    # 40 chars at 4 chars/token -> 10. Pinned so the ratio can't drift silently.
    assert context.estimate_tokens("x" * 40) == 10


def test_estimate_tokens_non_empty_costs_at_least_one():
    assert context.estimate_tokens("a") >= 1


def test_estimate_tokens_is_monotonic():
    prev = 0
    for n in range(0, 500, 37):
        cur = context.estimate_tokens("x" * n)
        assert cur >= prev, "appending text must never lower the estimate"
        prev = cur


# --------------------------------------------------------------------------
# U1 — hive collector
# --------------------------------------------------------------------------

def test_collect_hive_measures_namespace_artifacts(tmp_path):
    pdir = paths.project_dir()
    _write(pdir / "STATE.md", "state " * 100)
    _write(pdir / "BRIEFING.md", "brief " * 50)
    _write(pdir / "SOUL.md", "soul " * 10)
    mem = _write(tmp_path / "mem" / "MEMORY.md", "memory " * 80)

    segs = context.collect_hive(memory_path=mem)
    names = {s.name for s in segs}
    assert {"STATE.md", "BRIEFING.md", "SOUL.md", "MEMORY.md"} <= names
    assert all(s.origin == "hive" for s in segs)

    state = next(s for s in segs if s.name == "STATE.md")
    assert state.nbytes == (pdir / "STATE.md").stat().st_size
    assert state.tokens > 0


def test_collect_hive_skips_missing_files(tmp_path):
    pdir = paths.project_dir()
    _write(pdir / "STATE.md", "only state")
    segs = context.collect_hive(memory_path=tmp_path / "nope" / "MEMORY.md")
    names = {s.name for s in segs}
    assert "STATE.md" in names
    assert "BRIEFING.md" not in names
    assert "MEMORY.md" not in names


def test_collect_hive_on_empty_store_returns_empty_list(tmp_path):
    segs = context.collect_hive(memory_path=tmp_path / "absent" / "MEMORY.md")
    assert segs == []


def test_collect_hive_survives_invalid_utf8(tmp_path):
    pdir = paths.project_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "STATE.md").write_bytes(b"valid text \xff\xfe then more")
    segs = context.collect_hive(memory_path=tmp_path / "absent" / "MEMORY.md")
    assert [s.name for s in segs] == ["STATE.md"]
    assert segs[0].nbytes > 0


# --------------------------------------------------------------------------
# U2 — claude surface collector
# --------------------------------------------------------------------------

def test_collect_claude_measures_instructions_and_skills(tmp_path):
    home = tmp_path / "home"
    _write(home / ".claude" / "CLAUDE.md", "global instructions " * 20)
    cache = _make_plugin_cache(tmp_path / "cache", {
        "alpha": {"a1": _skill_md("a1", "does the first thing"),
                  "a2": _skill_md("a2", "does the second thing"),
                  "a3": _skill_md("a3", "does the third thing")},
        "beta": {"b1": _skill_md("b1", "beta one"),
                 "b2": _skill_md("b2", "beta two"),
                 "b3": _skill_md("b3", "beta three")},
    })

    segs = context.collect_claude(home=home, cache_dir=cache)
    assert all(s.origin == "claude" for s in segs)
    assert any(s.name == "CLAUDE.md" for s in segs)

    skills = [s for s in segs if s.kind == "skill"]
    assert len(skills) == 6, f"expected 6 skill segments, got {[s.name for s in skills]}"
    assert all(s.tokens > 0 for s in skills)


def test_collect_claude_charges_description_not_body(tmp_path):
    home = tmp_path / "home"
    cache = _make_plugin_cache(tmp_path / "cache", {
        "alpha": {"big": "---\nname: big\ndescription: tiny\n---\n" + ("BODY " * 5000)},
    })
    segs = context.collect_claude(home=home, cache_dir=cache)
    skills = [s for s in segs if s.kind == "skill"]
    assert len(skills) == 1
    # "tiny" is 4 chars -> 1 token. A 25k-char body must not be charged.
    assert skills[0].tokens <= 2


def test_collect_claude_skips_malformed_frontmatter(tmp_path):
    home = tmp_path / "home"
    cache = _make_plugin_cache(tmp_path / "cache", {
        "alpha": {"good": _skill_md("good", "a real description"),
                  "broken": "---\nname: broken\ndescription: never closed\n"},
    })
    segs = context.collect_claude(home=home, cache_dir=cache)
    skills = [s for s in segs if s.kind == "skill"]
    assert [s.name for s in skills] == ["alpha:good"]


def test_collect_claude_skips_skill_without_description(tmp_path):
    home = tmp_path / "home"
    cache = _make_plugin_cache(tmp_path / "cache", {
        "alpha": {"nodesc": "---\nname: nodesc\n---\n\nbody",
                  "yes": _skill_md("yes", "present")},
    })
    segs = context.collect_claude(home=home, cache_dir=cache)
    skills = [s for s in segs if s.kind == "skill"]
    assert [s.name for s in skills] == ["alpha:yes"]


def test_collect_claude_without_plugins_dir_returns_only_instructions(tmp_path):
    home = tmp_path / "home"
    _write(home / ".claude" / "CLAUDE.md", "just instructions")
    segs = context.collect_claude(home=home, cache_dir=tmp_path / "no-cache")
    assert [s.name for s in segs] == ["CLAUDE.md"]


def test_collect_claude_on_absent_home_returns_empty(tmp_path):
    segs = context.collect_claude(home=tmp_path / "ghost",
                                  cache_dir=tmp_path / "ghost-cache")
    assert segs == []


# --------------------------------------------------------------------------
# U3 — analyze
# --------------------------------------------------------------------------

def test_analyze_ranks_descending_and_shares_sum_to_one():
    segs = [_seg("small", tokens=10), _seg("big", tokens=100), _seg("mid", tokens=50)]
    rep = context.analyze(segs)
    assert [r["name"] for r in rep["segments"]] == ["big", "mid", "small"]
    assert abs(sum(r["share"] for r in rep["segments"]) - 1.0) < 1e-9
    assert rep["total_tokens"] == 160


def test_analyze_tie_order_is_deterministic():
    segs = [_seg("b", tokens=10), _seg("a", tokens=10), _seg("c", tokens=10)]
    first = [r["name"] for r in context.analyze(segs)["segments"]]
    second = [r["name"] for r in context.analyze(list(reversed(segs)))["segments"]]
    assert first == second == ["a", "b", "c"]


def test_analyze_empty_does_not_divide_by_zero():
    rep = context.analyze([])
    assert rep["total_tokens"] == 0
    assert rep["count"] == 0
    assert rep["segments"] == []


def test_analyze_budget_variants():
    segs = [_seg(tokens=100)]
    assert context.analyze(segs)["over_budget"] is None
    assert context.analyze(segs, budget=40)["over_budget"] == 60
    assert context.analyze(segs, budget=250)["over_budget"] == -150


def test_analyze_groups_by_origin():
    segs = [_seg("h", origin="hive", tokens=30), _seg("c", origin="claude", tokens=70)]
    rep = context.analyze(segs)
    assert rep["by_origin"]["hive"]["tokens"] == 30
    assert rep["by_origin"]["claude"]["tokens"] == 70


# --------------------------------------------------------------------------
# U3 — findings: each rule fires, and stays silent on clean input
# --------------------------------------------------------------------------

def test_dominant_rule_fires_and_is_silent_when_balanced():
    hot = [_seg("hog", tokens=900), _seg("tiny", tokens=100)]
    assert any(f.rule == "dominant_artifact" for f in context.findings(hot))

    balanced = [_seg(f"s{i}", tokens=100) for i in range(10)]
    assert not any(f.rule == "dominant_artifact" for f in context.findings(balanced))


def test_oversized_rule_fires_and_is_silent_when_small():
    big = [_seg("STATE.md", nbytes=context.OVERSIZED_BYTES + 1, tokens=10)]
    assert any(f.rule == "oversized_artifact" for f in context.findings(big))

    small = [_seg("STATE.md", nbytes=100, tokens=10)]
    assert not any(f.rule == "oversized_artifact" for f in context.findings(small))


def test_duplicate_lines_rule_fires_and_is_silent_when_unique(tmp_path):
    dup = _write(tmp_path / "dup.md", ("- a repeated path line that is long\n" * 5))
    segs = [_seg("dup.md", path=str(dup), tokens=10)]
    assert any(f.rule == "duplicate_lines" for f in context.findings(segs))

    uniq = _write(tmp_path / "uniq.md",
                  "".join(f"- distinct long line number {i} here\n" for i in range(5)))
    segs = [_seg("uniq.md", path=str(uniq), tokens=10)]
    assert not any(f.rule == "duplicate_lines" for f in context.findings(segs))


def test_dead_path_rule_flags_missing_and_not_existing(tmp_path):
    real = _write(tmp_path / "real.txt", "x")
    doc = _write(tmp_path / "doc.md",
                 f"live: {real}\ndead: {tmp_path / 'gone' / 'nowhere.py'}\n")
    found = context.findings([_seg("doc.md", path=str(doc), tokens=10)])
    dead = [f for f in found if f.rule == "dead_path"]
    assert len(dead) == 1
    assert "nowhere.py" in dead[0].message or "1 absolute path" in dead[0].message

    only_live = _write(tmp_path / "live.md", f"live: {real}\n")
    found2 = context.findings([_seg("live.md", path=str(only_live), tokens=10)])
    assert not any(f.rule == "dead_path" for f in found2)


def test_dead_path_rule_ignores_documentation_examples(tmp_path):
    """Instruction files teach path hygiene using example paths that will never
    exist. Firing on those makes the rule cry wolf, so they must be skipped."""
    doc = _write(tmp_path / "guide.md", "\n".join([
        "never use absolute paths like /Users/name/Code/project/src/file.ts",
        "or C:\\Users\\<you>\\repo\\thing.py",
        "or /Users/.../repo/elided.py",
        "or /path/to/your/file.py",
    ]))
    found = context.findings([_seg("guide.md", path=str(doc), tokens=10)])
    assert not any(f.rule == "dead_path" for f in found), \
        "documentation example paths must not be reported as dead references"


def test_is_illustrative_still_allows_real_dead_paths(tmp_path):
    """The example filter must not swallow genuine stale references."""
    doc = _write(tmp_path / "state.md",
                 f"touched: {tmp_path / 'worktrees' / 'gone' / 'mod.py'}\n")
    found = context.findings([_seg("state.md", path=str(doc), tokens=10)])
    assert any(f.rule == "dead_path" for f in found)


def test_memory_folder_resolves_to_a_real_directory():
    """MEMORY.md is keyed by the MAIN repo folder. From a linked worktree, using
    cwd would miss the file entirely and silently understate the report."""
    folder = context._memory_folder()
    assert isinstance(folder, str) and folder
    assert Path(folder).exists()
    # A worktree lives under <main>/.claude/worktrees/<name>; the resolved
    # folder must be the main repo, never the worktree itself.
    assert ".claude" not in Path(folder).parts


def test_outlier_skill_rule_fires_and_is_silent_when_uniform():
    skew = [_seg(f"p:s{i}", kind="skill", tokens=10) for i in range(6)]
    skew.append(_seg("p:whale", kind="skill", tokens=500))
    assert any(f.rule == "outlier_description" for f in context.findings(skew))

    uniform = [_seg(f"p:s{i}", kind="skill", tokens=10) for i in range(6)]
    assert not any(f.rule == "outlier_description" for f in context.findings(uniform))


def test_findings_does_not_mutate_input():
    segs = [_seg("a", tokens=900), _seg("b", tokens=10)]
    before = list(segs)
    snapshot = [(s.name, s.tokens, s.nbytes) for s in segs]
    context.findings(segs)
    assert segs == before
    assert [(s.name, s.tokens, s.nbytes) for s in segs] == snapshot


def test_findings_on_clean_input_is_empty():
    clean = [_seg(f"s{i}", tokens=100, nbytes=400) for i in range(8)]
    assert context.findings(clean) == []


# --------------------------------------------------------------------------
# U4 — rendering
# --------------------------------------------------------------------------

def test_to_json_round_trips_and_has_stable_keys():
    rep = context.analyze([_seg("a", tokens=10)], budget=5)
    found = context.findings([_seg("a", tokens=10)])
    payload = json.loads(context.to_json(rep, found))
    for key in ("total_tokens", "total_bytes", "count", "by_origin",
                "segments", "budget", "over_budget", "findings",
                "estimated", "chars_per_token"):
        assert key in payload, f"missing documented key: {key}"
    assert isinstance(payload["findings"], list)


def test_render_text_on_empty_is_a_message_not_a_traceback():
    out = context.render_text(context.analyze([]), [])
    assert isinstance(out, str) and out.strip()
    assert "No context contributors" in out


def test_render_text_top_limits_rows_but_not_totals():
    segs = [_seg(f"s{i}", tokens=(i + 1) * 10) for i in range(10)]
    rep = context.analyze(segs)
    full = context.render_text(rep, [], top=None)
    trimmed = context.render_text(rep, [], top=3)
    assert "more (use --top" in trimmed
    assert f"{rep['total_tokens']:,}" in trimmed
    assert len(trimmed.splitlines()) < len(full.splitlines())


def test_render_text_reports_budget_verdict():
    rep_over = context.analyze([_seg(tokens=100)], budget=40)
    assert "OVER by" in context.render_text(rep_over, [])
    rep_under = context.analyze([_seg(tokens=10)], budget=400)
    assert "under by" in context.render_text(rep_under, [])
