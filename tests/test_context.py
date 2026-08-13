# tests/test_context.py
"""Tests for the context-budget analyzer.

The autouse ``isolate_store`` fixture in conftest points MINDHIVE_HOME at a
throwaway tmp dir and pins MINDHIVE_PROJECT to "test", so every collector here
reads a synthetic store. The Claude-surface collector takes its roots as
arguments for the same reason — nothing in this file may touch the real
~/.mindhive or ~/.claude."""
import json
from pathlib import Path

from mindhive import context, outcomes, paths


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


def test_one_unreadable_skill_does_not_drop_every_plugin(tmp_path):
    """A single unreadable SKILL.md must not zero out the whole skills measure.

    The scan is wrapped as a unit, so an unguarded read failure deep inside it
    would discard every plugin's segments silently — the report would claim
    zero installed skills. A directory named SKILL.md forces a read error
    portably (no chmod semantics to depend on)."""
    home = tmp_path / "home"
    cache = _make_plugin_cache(tmp_path / "cache", {
        "alpha": {"ok1": _skill_md("ok1", "first good skill"),
                  "ok2": _skill_md("ok2", "second good skill")},
    })
    # Replace one skill's SKILL.md with a directory of the same name.
    broken = cache / "mkt" / "alpha" / "1.0.0" / "skills" / "broken"
    (broken / "SKILL.md").mkdir(parents=True)

    segs = context.collect_claude(home=home, cache_dir=cache)
    skills = [s for s in segs if s.kind == "skill"]
    assert {s.name for s in skills} == {"alpha:ok1", "alpha:ok2"}, \
        "healthy skills must survive an unreadable sibling"


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


def test_tmp_path_is_extractable_canary(tmp_path):
    """Canary for the negative dead-path tests below.

    Those tests assert that NO dead_path finding fires. If the extractor stops
    recognising the platform's tmp root, they would all pass vacuously while
    testing nothing — which is exactly how a Linux-only regression shipped
    green on Windows. This asserts the fixture root is actually extractable,
    so the suite fails loudly instead of silently going hollow."""
    probe = tmp_path / "canary" / "file.py"
    assert context._path_candidates(f"ref {probe}\n") == [str(probe)], (
        f"tmp_path root {tmp_path} is not matched by _ABS_PATH_RE — the "
        f"negative dead-path tests below are vacuous on this platform"
    )


def test_dead_path_ignores_paths_containing_spaces(tmp_path):
    """The tokenizer stops at whitespace, so 'C:\\Users\\x\\My Documents\\a.txt'
    truncates to a stub that does not exist. Reporting that stub as a dead
    reference is a false positive on a very common Windows path shape."""
    doc = _write(tmp_path / "d.md",
                 "see C:\\Users\\hanif\\My Documents\\notes.txt for details\n")
    found = context.findings([_seg("d.md", path=str(doc), tokens=10)])
    assert not any(f.rule == "dead_path" for f in found)


def test_dead_path_ignores_urls(tmp_path):
    """'/mnt/...' inside a URL was never a filesystem reference."""
    doc = _write(tmp_path / "d.md", "docs at http://example.org/mnt/guide/x.md ok\n")
    found = context.findings([_seg("d.md", path=str(doc), tokens=10)])
    assert not any(f.rule == "dead_path" for f in found)


def test_dead_path_trims_trailing_sentence_punctuation(tmp_path):
    """Prose ends paths with a period. On POSIX 'x.py.' is a different,
    non-existent name from 'x.py', so the period must be trimmed before probing."""
    real = _write(tmp_path / "real.py", "x")
    doc = _write(tmp_path / "d.md", f"it lives at {real}.\n")
    found = context.findings([_seg("d.md", path=str(doc), tokens=10)])
    assert not any(f.rule == "dead_path" for f in found)


def test_dead_path_requires_a_file_extension(tmp_path):
    """Directory-shaped candidates are too ambiguous to call dead."""
    doc = _write(tmp_path / "d.md", f"dir {tmp_path / 'gone' / 'subdir'}\n")
    found = context.findings([_seg("d.md", path=str(doc), tokens=10)])
    assert not any(f.rule == "dead_path" for f in found)


def test_outlier_uses_true_median_on_even_length_input():
    """ordered[n//2] is the upper-median on an even-length list, which inflates
    the threshold and hides genuine outliers."""
    # 8 skills: [10,10,10,10,50,50,50,200].
    # True median = (10+50)/2 = 30 -> threshold 120 -> the 200 fires.
    # Upper-median ordered[4] = 50 -> threshold 200 -> 200 is not > 200, silent.
    segs = [_seg(f"p:s{i}", kind="skill", tokens=10) for i in range(4)]
    segs += [_seg(f"p:b{i}", kind="skill", tokens=50) for i in range(3)]
    segs.append(_seg("p:whale", kind="skill", tokens=200))
    found = context.findings(segs)
    assert any(f.rule == "outlier_description" and "whale" in f.subject
               for f in found)


def test_memory_folder_accepts_injected_cwd(tmp_path):
    """The resolver must be drivable from a caller-supplied directory rather
    than only the process cwd, so it is testable off the real machine layout."""
    folder = context._memory_folder(cwd=tmp_path)
    assert isinstance(folder, str) and folder
    assert Path(folder).exists()


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


# --------------------------------------------------------------------------
# persisted budget + enforcement (--check) + local-skills collector
# --------------------------------------------------------------------------

def test_budget_save_load_roundtrip():
    context.save_budget(30000, project="test")
    assert context.load_budget("test") == 30000


def test_budget_missing_is_none():
    assert context.load_budget("test") is None


def test_budget_corrupt_file_is_none():
    _write(paths.project_dir("test") / context.BUDGET_FILE, "{not json")
    assert context.load_budget("test") is None


def test_budget_nonpositive_is_none():
    context.save_budget(0, project="test") if hasattr(context, "save_budget") else None
    _write(paths.project_dir("test") / context.BUDGET_FILE,
           '{"budget": -5}')
    assert context.load_budget("test") is None


def test_collect_claude_measures_local_skills(tmp_path):
    home = tmp_path / "home"
    _write(home / ".claude" / "skills" / "ecc-benchmark" / "SKILL.md",
           _skill_md("ecc-benchmark", "Use when benchmarking agent harnesses."))
    cache = _make_plugin_cache(tmp_path / "cache", {})
    segs = context.collect_claude(home=home, cache_dir=cache)
    local = [s for s in segs if s.name == "local:ecc-benchmark"]
    assert len(local) == 1
    assert local[0].kind == "skill"
    assert local[0].tokens == context.estimate_tokens(
        "Use when benchmarking agent harnesses.")


def test_collect_claude_local_skills_folded_description(tmp_path):
    # The adopt path meets ECC-style folded descriptions; the analyzer must
    # charge the real text, not the literal '>-' marker.
    home = tmp_path / "home"
    _write(home / ".claude" / "skills" / "folded" / "SKILL.md",
           "---\nname: folded\ndescription: >-\n  Use when folding\n  things.\n---\n")
    segs = context.collect_claude(home=home, cache_dir=tmp_path / "nocache")
    (seg,) = [s for s in segs if s.name == "local:folded"]
    assert seg.tokens == context.estimate_tokens("Use when folding things.")


def test_cli_set_budget_persists(capsys):
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["context", "--set-budget", "31000"]) == 0
    assert context.load_budget(None) == 31000


def test_cli_check_exit_codes(tmp_path, monkeypatch):
    from mindhive import __main__ as cli_mod
    # A hive-origin artifact with known weight in the isolated namespace. The
    # native MEMORY.md is pointed away from this machine's real one: the
    # `--check` gate now also judges that file's LINE count, so leaving it
    # resolved would make this test's exit code depend on the developer's own
    # digest — passing on a fresh CI runner and failing on a working machine.
    _point_memory_at(monkeypatch, tmp_path / "no-memory-here.md")
    _write(paths.project_dir("test") / "STATE.md", "x" * 4000)   # ~1000 tokens
    context.save_budget(10, project="test")
    assert cli_mod.main(["context", "--origin", "hive", "--check"]) == 1
    context.save_budget(1_000_000, project="test")
    assert cli_mod.main(["context", "--origin", "hive", "--check"]) == 0


def test_cli_check_without_budget_is_an_error(tmp_path, monkeypatch, capsys):
    from mindhive import __main__ as cli_mod
    _point_memory_at(monkeypatch, tmp_path / "no-memory-here.md")
    assert cli_mod.main(["context", "--origin", "hive", "--check"]) == 2


# --------------------------------------------------------------------------
# the MEMORY.md line budget: context paid for and never delivered
# --------------------------------------------------------------------------
#
# Every other rule in this module is about COST — tokens spent. This one is
# about LOSS: the native loader reads only the first N lines of MEMORY.md, so a
# digest past that limit is charged in full and delivered in part, and the part
# that vanishes is the tail — where `sync` writes the lessons and the decisions.
# The caps in `sync` bound item COUNTS, and no item count bounds lines.

def _memory(tmp_path, lines: int, tail_section="Key decisions") -> Path:
    body = [f"line {i}" for i in range(lines - 2)]
    return _write(tmp_path / "MEMORY.md",
                  "\n".join(body + [f"## {tail_section}", "- something"]))


def test_memory_pressure_reports_the_overflow_and_what_is_lost(tmp_path):
    path = _memory(tmp_path, 260)
    pressure = context.memory_pressure(path, budget=200)
    assert pressure["lines"] == 260
    assert pressure["over"] == 60
    assert pressure["dropped"] == ["Key decisions"]
    assert pressure["readable"] is True


def test_memory_pressure_is_silent_when_the_file_fits(tmp_path):
    """CANARY for the test above: a short digest names nothing as dropped, so
    the finding is a measurement rather than a constant."""
    pressure = context.memory_pressure(_memory(tmp_path, 20), budget=200)
    assert pressure["over"] == 0 and pressure["dropped"] == []


def test_memory_pressure_distinguishes_unreadable_from_fine(tmp_path):
    """DISCRIMINATING: both answer "over: 0", and only one of them means the
    file is OK. `readable` is the field that tells them apart."""
    pressure = context.memory_pressure(tmp_path / "nope.md", budget=200)
    assert pressure["readable"] is False and pressure["over"] == 0


def test_memory_line_budget_is_configurable_per_namespace():
    assert context.load_memory_line_budget("test") == context.MEMORY_LINE_BUDGET
    context.save_budget(30000, project="test", memory_lines=400)
    assert context.load_memory_line_budget("test") == 400


def test_setting_the_token_budget_does_not_wipe_the_line_budget():
    """The sidecar holds both, and the two are set at different moments."""
    context.save_budget(30000, project="test", memory_lines=400)
    context.save_budget(31000, project="test")
    assert context.load_memory_line_budget("test") == 400
    assert context.load_budget("test") == 31000


def test_corrupt_line_budget_falls_back_to_the_default():
    _write(paths.project_dir("test") / context.BUDGET_FILE,
           '{"budget": 100, "memory_lines": "banana"}')
    assert context.load_memory_line_budget("test") == context.MEMORY_LINE_BUDGET


def test_findings_flag_a_truncated_memory_digest(tmp_path):
    path = _memory(tmp_path, 300)
    seg = context.Segment(name="MEMORY.md", origin="hive", path=str(path),
                          nbytes=path.stat().st_size, tokens=100,
                          kind="artifact")
    found = context.findings([seg], project="test")
    truncated = [f for f in found if f.rule == "memory_truncated"]
    assert len(truncated) == 1
    assert truncated[0].severity == "high"
    assert "Key decisions" in truncated[0].message


def test_a_short_digest_produces_no_truncation_finding(tmp_path):
    """CANARY for the test above."""
    path = _memory(tmp_path, 30)
    seg = context.Segment(name="MEMORY.md", origin="hive", path=str(path),
                          nbytes=path.stat().st_size, tokens=100,
                          kind="artifact")
    assert not [f for f in context.findings([seg], project="test")
                if f.rule == "memory_truncated"]


def _point_memory_at(monkeypatch, path):
    from mindhive import sync as sync_mod
    monkeypatch.setattr(sync_mod, "native_memory_path",
                        lambda folder, home=None: Path(path))


def test_cli_check_fails_on_a_truncated_digest(tmp_path, monkeypatch, capsys):
    from mindhive import __main__ as cli_mod
    _point_memory_at(monkeypatch, _memory(tmp_path, 300))
    context.save_budget(1_000_000, project="test")      # token budget is fine
    assert cli_mod.main(["context", "--origin", "hive", "--check"]) == 1
    assert "TRUNCATED" in capsys.readouterr().err


def test_cli_check_passes_when_the_digest_fits(tmp_path, monkeypatch, capsys):
    """CANARY for the test above: same command, same budget, shorter file."""
    from mindhive import __main__ as cli_mod
    _point_memory_at(monkeypatch, _memory(tmp_path, 30))
    context.save_budget(1_000_000, project="test")
    assert cli_mod.main(["context", "--origin", "hive", "--check"]) == 0
    assert "TRUNCATED" not in capsys.readouterr().err


def test_truncation_is_reported_even_with_no_token_budget(tmp_path, monkeypatch,
                                                          capsys):
    """A missing token budget is a configuration gap; a truncated digest is a
    fact. The fact does not wait for the configuration."""
    from mindhive import __main__ as cli_mod
    _point_memory_at(monkeypatch, _memory(tmp_path, 300))
    assert cli_mod.main(["context", "--origin", "hive", "--check"]) == 1
    assert "TRUNCATED" in capsys.readouterr().err


def test_cli_set_memory_lines_needs_a_token_budget(capsys):
    """One sidecar holds both, so there is no half-configured state to write."""
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["context", "--set-memory-lines", "300"]) == 2
    assert cli_mod.main(["context", "--set-budget", "40000",
                         "--set-memory-lines", "300"]) == 0
    assert context.load_memory_line_budget(None) == 300


def test_sync_warns_when_the_digest_it_just_wrote_is_truncated(tmp_path,
                                                              monkeypatch,
                                                              capsys):
    """The loader's own warning arrives in the NEXT session, to the agent. This
    one arrives now, to whoever ran sync."""
    from mindhive import __main__ as cli_mod
    from mindhive import sync as sync_mod
    written = _memory(tmp_path, 300)
    monkeypatch.setattr(sync_mod, "sync",
                        lambda folder, **kw: written)
    assert cli_mod.main(["sync"]) == 0
    err = capsys.readouterr().err
    assert "MEMORY.md is 300 lines" in err and "Key decisions" in err


# --------------------------------------------------------------------------
# latency budget
# --------------------------------------------------------------------------

def test_the_findings_pipeline_stays_inside_its_latency_budget(tmp_path):
    """`hive context --check` is meant to be cheap enough to run in an adoption
    flow or a pre-commit script, and the rules that read file CONTENT (duplicate
    lines, dead paths, truncation) are the ones that can quietly make it
    expensive. The bound is deliberately loose — this is a regression guard
    against an accidental O(n²) rule or a per-rule re-read, not a benchmark, and
    a tight number on a shared CI runner would fail for reasons unrelated to the
    code."""
    import time
    segs = []
    for i in range(40):
        p = _write(tmp_path / f"a{i}.md", ("a line of prose here\n" * 200))
        segs.append(context.Segment(name=f"a{i}.md", origin="hive", path=str(p),
                                    nbytes=p.stat().st_size, tokens=1000,
                                    kind="artifact"))
    start = time.perf_counter()
    context.findings(segs, project="test")
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"findings over 40 artifacts took {elapsed:.2f}s"


# --------------------------------------------------------------------------
# the outcomes-registry probe
# --------------------------------------------------------------------------
# `memory_pressure` already reports the numbers; these are about the three
# states it deliberately does NOT distinguish on its own — absent, unreadable,
# and over — arriving as three different outcomes instead of one "readable:
# False" a caller can forget to look at.

def test_probe_reports_an_unwritten_digest_as_the_honest_zero(tmp_path):
    """Nothing delivered means nothing lost. An unwritten digest is not a
    truncated one, and reporting it as degraded would cry wolf on every fresh
    checkout."""
    report = context._probe(path=tmp_path / "never-synced.md")
    assert report.outcome == outcomes.NOTHING_TO_DO
    assert report.worked
    assert "hive sync" in report.summary


def test_probe_reports_a_present_but_unreadable_digest_as_degraded(tmp_path):
    """DISCRIMINATING against the test above: `memory_pressure` answers
    `readable: False` for both, so the existence check is what separates "not
    written yet" from "written and we cannot read it"."""
    unreadable = tmp_path / "MEMORY.md"
    unreadable.mkdir()          # a directory reads as present and unreadable
    report = context._probe(path=unreadable)
    assert report.outcome == outcomes.DEGRADED
    assert not report.worked
    assert "not the same as fine" in report.summary


def test_probe_names_the_sections_that_never_reach_a_session(tmp_path):
    context.save_budget(30000, project="test", memory_lines=200)
    report = context._probe(project="test", path=_memory(tmp_path, 260))
    assert report.outcome == outcomes.DEGRADED
    assert "over by 60" in report.summary
    assert "Key decisions" in report.summary


def test_probe_reports_a_digest_that_fits_as_did_work(tmp_path):
    """CANARY for the test above: the same probe on a short digest must not
    report loss, or the degraded branch is a constant."""
    context.save_budget(30000, project="test", memory_lines=200)
    report = context._probe(project="test", path=_memory(tmp_path, 20))
    assert report.outcome == outcomes.DID_WORK
    assert report.worked
    assert "every section reaches a session" in report.summary


def test_probe_is_registered_and_defaults_to_the_main_repo_digest(monkeypatch):
    """The default path must come from `_memory_folder`, not the cwd: run from a
    linked worktree, a cwd-keyed lookup misses the real file and reports "no
    MEMORY.md yet" about a digest that is in fact being truncated."""
    surface = outcomes.get("memory-budget")
    assert surface is not None
    assert surface.probe is context._probe
    seen = []
    monkeypatch.setattr(context, "_memory_folder",
                        lambda *a, **k: seen.append(1) or "C:/Users/x/repo")
    context._probe()
    assert seen, "the probe resolved its path without _memory_folder"
