# tests/test_packs.py
"""Stack content pack validator (packs.py) — WS2 of the Stack Content Packs PRD.

The suite encodes docs/packs/SPEC.md: the manifest is the single source of
truth for every threshold, frontmatter is FLAT keys only, similarity is split
external/internal with a shared-preamble exclusion, and reads are total.
Fixture packs are built in tmp_path; the external-corpus comparison runs
against a hermetic fixture corpus (CI never sees the real ECC clone)."""
import json
from pathlib import Path

import pytest

from mindhive import packs

REPO = Path(__file__).resolve().parents[1]
REAL_MANIFEST = REPO / "docs" / "packs" / "packs.json"


# --------------------------------------------------------------------------
# fixture builders
# --------------------------------------------------------------------------

def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_manifest(tmp_path, **overrides) -> Path:
    """A complete, valid manifest; overrides replace top-level keys."""
    data = {
        "version": 2,
        "similarity_external_max_pct": 10.0,
        "similarity_external_warn_pct": 6.5,
        "similarity_internal_same_role_max_pct": 18.0,
        "shared_preamble_markers": ["run the full pytest suite", "hive log"],
        "depth_ratio_max": 2.5,
        "min_lines": 20,           # low floors so fixtures stay small;
        "min_code_blocks": 2,      # threshold-specific tests override them
        "min_code_blocks_by_role": {"verification": 1},
        "quartet_depth_target_lines": 30,
        "max_description_chars": 300,
        "required_sections": ["## When to Activate", "## Quick Reference"],
        "required_sections_by_role": {
            "verification": ["## When to Activate", "## Output Template"],
        },
        "min_phase_sections_by_role": {"verification": 3},
        "packs": [
            {"name": "alpha", "roles": ["patterns", "verification"],
             "domain_markers": ["alpha-widget"], "leak_severity": "warn",
             "compare_external": []},
            {"name": "beta", "roles": ["patterns"],
             "domain_markers": ["beta-gadget"], "leak_severity": "warn",
             "compare_external": []},
        ],
        "external_corpora": {},
    }
    data.update(overrides)
    return _write(tmp_path / "packs.json", json.dumps(data))


def make_body(pack: str, role: str, *, filler: int = 30, blocks: int = 3) -> str:
    """A template-conformant body whose filler lines are unique per pack+role
    (so fixture packs never trip the similarity gate by accident)."""
    lines = [f"# {pack} {role}", "",
             f"Fixture guidance for the {pack} {role} slice.", "",
             "## When to Activate", ""]
    lines += [f"- trigger {pack} {role} situation number {i}" for i in range(5)]
    lines.append("")
    if role == "verification":
        for n in range(1, 4):
            lines += [f"## Phase {n}: gate {pack} {n}", "",
                      "```bash", f"echo {pack}-{role}-phase-{n}", "```", "",
                      "If this fails, stop and fix before continuing.", ""]
        for _ in range(max(0, blocks - 3)):
            lines += ["```bash", f"echo extra {pack} {role}", "```", ""]
        lines += ["## Output Template", "", "```",
                  f"{pack.upper()} REPORT", "Overall: [READY / NOT READY]",
                  "```", ""]
    else:
        for n in range(blocks):
            lines += [f"## Topic {n} of {pack} {role}", "",
                      "```python", f"print('{pack} {role} example {n}')", "```", ""]
        lines += ["## Quick Reference", "",
                  "| Mechanic | Use |", "|---|---|",
                  f"| `{pack}-cmd` | {role} work |", ""]
    lines += [f"Filler: distinctive {pack} {role} sentence number {i} padding out depth."
              for i in range(filler)]
    return "\n".join(lines) + "\n"


def make_skill(root: Path, pack: str, role: str, *, body: str | None = None,
               front: str | None = None) -> Path:
    if front is None:
        front = (f"---\nname: {pack}-{role}\n"
                 f"description: Fixture {role} guidance for the {pack} stack.\n"
                 f"origin: mindhive\npack: {pack}\nrole: {role}\nkind: {role}\n---\n")
    if body is None:
        body = make_body(pack, role)
    return _write(root / f"{pack}-{role}" / "SKILL.md", front + body)


def make_tree(tmp_path, **manifest_overrides):
    """Manifest + a fully valid two-pack skill tree."""
    manifest = make_manifest(tmp_path, **manifest_overrides)
    root = tmp_path / "skills"
    make_skill(root, "alpha", "patterns")
    make_skill(root, "alpha", "verification")
    make_skill(root, "beta", "patterns")
    return manifest, root


def errors(findings):
    return [f for f in findings if f.severity == "error"]


def by_rule(findings, rule):
    return [f for f in findings if f.rule == rule]


# --------------------------------------------------------------------------
# manifest loader — first test validates the real WS1 manifest
# --------------------------------------------------------------------------

def test_load_real_ws1_manifest():
    manifest, findings = packs.load_manifest(REAL_MANIFEST)
    assert manifest is not None
    assert errors(findings) == []
    assert manifest["version"] == 2
    assert {p["name"] for p in manifest["packs"]} == {
        "mindhive-python", "windows-powershell", "claude-harness"}
    assert manifest["similarity_external_max_pct"] == 10.0
    assert manifest["similarity_internal_same_role_max_pct"] == 18.0
    assert manifest["depth_ratio_max"] == 2.5
    assert manifest["min_lines"] == 150
    assert manifest["external_corpora"]["ecc-quartets"]["path_env"] == \
        "MINDHIVE_ECC_SKILLS"
    # No committed filesystem path anywhere in the corpus declaration (C10).
    assert "path" not in manifest["external_corpora"]["ecc-quartets"]


def test_load_manifest_missing_is_a_finding_not_a_raise(tmp_path):
    manifest, findings = packs.load_manifest(tmp_path / "absent.json")
    assert manifest is None
    assert by_rule(errors(findings), "manifest-missing")


def test_load_manifest_corrupt_is_a_finding_not_a_raise(tmp_path):
    path = _write(tmp_path / "packs.json", "{not json")
    manifest, findings = packs.load_manifest(path)
    assert manifest is None
    assert by_rule(errors(findings), "manifest-invalid")


def test_load_manifest_unknown_version_warns_but_loads(tmp_path):
    path = make_manifest(tmp_path, version=99)
    manifest, findings = packs.load_manifest(path)
    assert manifest is not None
    assert by_rule(findings, "manifest-version")
    assert not errors(findings)


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------

def test_norm_lines_collapses_case_whitespace_and_short_lines():
    got = packs.norm_lines("  Run   The GATE now  \nshort\nrun the gate NOW\n")
    assert got == frozenset({"run the gate now"})


def test_norm_lines_handles_unicode():
    got = packs.norm_lines("Prüfung der Pipeline läuft\n")
    assert "prüfung der pipeline läuft" in got


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

def test_collect_pack_reads_valid_skills(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    files, findings = packs.collect_pack(root, manifest["packs"][0])
    assert errors(findings) == []
    assert {(f.pack, f.role) for f in files} == {
        ("alpha", "patterns"), ("alpha", "verification")}
    assert all(f.nonempty_lines > 0 and f.code_blocks > 0 for f in files)


def test_collect_pack_missing_skill_is_an_error(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    decl = dict(manifest["packs"][0], roles=["patterns", "verification", "tdd"])
    files, findings = packs.collect_pack(root, decl)
    assert len(files) == 2
    missing = by_rule(errors(findings), "missing-skill")
    assert missing and "alpha-tdd" in missing[0].subject


# --------------------------------------------------------------------------
# structure gate
# --------------------------------------------------------------------------

def _one_file(tmp_path, role="patterns", **skill_kwargs):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    make_skill(root, "alpha", role, **skill_kwargs)
    files, _ = packs.collect_pack(root, manifest["packs"][0])
    (pf,) = [f for f in files if f.role == role]
    return pf, manifest


def test_structure_valid_file_has_no_findings(tmp_path):
    pf, manifest = _one_file(tmp_path)
    assert packs.gate_structure(pf, manifest) == []


def test_structure_missing_flat_key_is_an_error(tmp_path):
    front = ("---\nname: alpha-patterns\n"
             "description: Fixture patterns guidance for the alpha stack.\n"
             "origin: mindhive\nrole: patterns\nkind: patterns\n---\n")  # no pack:
    pf, manifest = _one_file(tmp_path, front=front)
    assert by_rule(errors(packs.gate_structure(pf, manifest)), "frontmatter-key")


def test_structure_nested_metadata_is_an_error(tmp_path):
    front = ("---\nname: alpha-patterns\n"
             "description: Fixture patterns guidance for the alpha stack.\n"
             "metadata:\n  origin: mindhive\n"
             "pack: alpha\nrole: patterns\nkind: patterns\n---\n")
    pf, manifest = _one_file(tmp_path, front=front)
    assert by_rule(errors(packs.gate_structure(pf, manifest)), "nested-frontmatter")


def test_structure_name_slug_mismatch_is_an_error(tmp_path):
    front = ("---\nname: wrong-name\n"
             "description: Fixture patterns guidance for the alpha stack.\n"
             "origin: mindhive\npack: alpha\nrole: patterns\nkind: patterns\n---\n")
    pf, manifest = _one_file(tmp_path, front=front)
    assert by_rule(errors(packs.gate_structure(pf, manifest)), "name-mismatch")


def test_structure_overlong_description_is_an_error(tmp_path):
    front = (f"---\nname: alpha-patterns\ndescription: {'x' * 400}\n"
             "origin: mindhive\npack: alpha\nrole: patterns\nkind: patterns\n---\n")
    pf, manifest = _one_file(tmp_path, front=front)
    assert by_rule(errors(packs.gate_structure(pf, manifest)), "description-length")


def test_structure_missing_required_section_is_an_error(tmp_path):
    body = make_body("alpha", "patterns").replace("## Quick Reference", "## Cheatsheet")
    pf, manifest = _one_file(tmp_path, body=body)
    found = by_rule(errors(packs.gate_structure(pf, manifest)), "missing-section")
    assert found and "Quick Reference" in found[0].message


def test_structure_verification_required_sections_are_replaced(tmp_path):
    # verification needs Output Template but NOT Quick Reference.
    pf, manifest = _one_file(tmp_path, role="verification")
    assert packs.gate_structure(pf, manifest) == []
    body = make_body("alpha", "verification").replace("## Output Template", "## Results")
    pf, manifest = _one_file(tmp_path, role="verification", body=body)
    found = by_rule(errors(packs.gate_structure(pf, manifest)), "missing-section")
    assert found and "Output Template" in found[0].message


def test_structure_verification_phase_count_is_enforced(tmp_path):
    body = make_body("alpha", "verification").replace("## Phase 3", "## Stage 3")
    pf, manifest = _one_file(tmp_path, role="verification", body=body)
    assert by_rule(errors(packs.gate_structure(pf, manifest)), "phase-count")


# --------------------------------------------------------------------------
# depth gate
# --------------------------------------------------------------------------

def test_depth_stub_below_line_floor_is_an_error(tmp_path):
    pf, manifest = _one_file(tmp_path, body=make_body("alpha", "patterns", filler=0))
    manifest = dict(manifest, min_lines=500)
    assert by_rule(errors(packs.gate_depth([pf], manifest)), "depth-floor")


def test_depth_block_floor_respects_role_override(tmp_path):
    pf, manifest = _one_file(tmp_path, role="verification")
    manifest = dict(manifest, min_code_blocks=99,
                    min_code_blocks_by_role={"verification": 1})
    assert packs.gate_depth([pf], manifest) == []          # override applies
    manifest = dict(manifest, min_code_blocks_by_role={"verification": 99})
    assert by_rule(errors(packs.gate_depth([pf], manifest)), "block-floor")


def test_depth_ratio_over_ceiling_is_an_error(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    make_skill(root, "alpha", "patterns", body=make_body("alpha", "patterns", filler=200))
    make_skill(root, "alpha", "verification",
               body=make_body("alpha", "verification", filler=0))
    files, _ = packs.collect_pack(root, manifest["packs"][0])
    assert by_rule(errors(packs.gate_depth(files, manifest)), "depth-ratio")


def test_depth_below_target_is_a_warning_only(tmp_path):
    pf, manifest = _one_file(tmp_path, body=make_body("alpha", "patterns", filler=5))
    manifest = dict(manifest, min_lines=10, quartet_depth_target_lines=10_000)
    findings = packs.gate_depth([pf], manifest)
    assert by_rule(findings, "below-target")
    assert errors(findings) == []


# --------------------------------------------------------------------------
# similarity gate
# --------------------------------------------------------------------------

def test_similarity_internal_identical_bodies_error(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    shared = make_body("alpha", "patterns")
    make_skill(root, "alpha", "patterns", body=shared)
    make_skill(root, "beta", "patterns", body=shared)
    a, _ = packs.collect_pack(root, manifest["packs"][0])
    b, _ = packs.collect_pack(root, manifest["packs"][1])
    found = by_rule(errors(packs.gate_similarity(a + b, {}, manifest)),
                    "similarity-internal")
    assert found and "alpha-patterns" in found[0].subject


def test_similarity_internal_disjoint_bodies_pass(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    a, _ = packs.collect_pack(root, manifest["packs"][0])
    b, _ = packs.collect_pack(root, manifest["packs"][1])
    assert by_rule(packs.gate_similarity(a + b, {}, manifest),
                   "similarity-internal") == []


def test_similarity_internal_excludes_shared_preamble_lines(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    preamble = ("Always run the full pytest suite before you finish anything.\n"
                "Record the change with hive log before calling it done today.\n")
    # Two files whose ONLY shared content is preamble truth.
    make_skill(root, "alpha", "patterns",
               body=make_body("alpha", "patterns", filler=2) + preamble)
    make_skill(root, "beta", "patterns",
               body=make_body("beta", "patterns", filler=2) + preamble)
    a, _ = packs.collect_pack(root, manifest["packs"][0])
    b, _ = packs.collect_pack(root, manifest["packs"][1])
    files = [f for f in a + b if f.role == "patterns"]
    assert by_rule(packs.gate_similarity(files, {}, manifest),
                   "similarity-internal") == []
    # Without the exclusion the same pair must trip — the exclusion is
    # load-bearing, not vacuous (canary against a silently inert gate).
    stripped = dict(manifest, shared_preamble_markers=[],
                    similarity_internal_same_role_max_pct=1.0)
    assert by_rule(errors(packs.gate_similarity(files, {}, stripped)),
                   "similarity-internal")


def _corpus_tree(tmp_path, text: str) -> Path:
    root = tmp_path / "ecc"
    _write(root / "django-patterns" / "SKILL.md", "---\nname: django-patterns\n---\n" + text)
    _write(root / "other-patterns" / "SKILL.md", "---\nname: other-patterns\n---\n" + text)
    return root


def test_similarity_external_thresholds_and_families(tmp_path, monkeypatch):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    manifest = dict(manifest, external_corpora={
        "fixture": {"path_env": "PACKS_TEST_CORPUS", "families": ["django"]}})
    manifest["packs"] = [dict(manifest["packs"][0], compare_external=["fixture"]),
                         manifest["packs"][1]]
    alpha_body = make_body("alpha", "patterns")
    corpus_root = _corpus_tree(tmp_path, alpha_body)   # identical -> ~100%
    monkeypatch.setenv("PACKS_TEST_CORPUS", str(corpus_root))
    corpora, cfind = packs.load_external_corpus(
        "fixture", manifest["external_corpora"]["fixture"])
    assert not cfind
    # Family allowlist: other-patterns is not a django-* dir, so only one file.
    assert [c.name for c in corpora] == ["django-patterns"]
    files, _ = packs.collect_pack(root, manifest["packs"][0])
    found = by_rule(errors(packs.gate_similarity(
        files, {"fixture": corpora}, manifest)), "similarity-external")
    assert found and "django-patterns" in found[0].message


def test_similarity_external_missing_env_is_a_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("PACKS_TEST_CORPUS", raising=False)
    corpora, findings = packs.load_external_corpus(
        "fixture", {"path_env": "PACKS_TEST_CORPUS", "families": ["django"]})
    assert corpora == []
    assert by_rule(findings, "corpus-skipped")
    assert not errors(findings)


# --------------------------------------------------------------------------
# leak gate
# --------------------------------------------------------------------------

def test_leak_foreign_marker_found_at_owner_severity(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    body = make_body("beta", "patterns") + "\nThe alpha-widget subsystem is great.\n"
    make_skill(root, "beta", "patterns", body=body)
    a, _ = packs.collect_pack(root, manifest["packs"][0])
    b, _ = packs.collect_pack(root, manifest["packs"][1])
    found = by_rule(packs.gate_leak(a + b, manifest), "domain-leak")
    assert found and found[0].severity == "warn"
    assert "alpha-widget" in found[0].message and "beta-patterns" in found[0].subject


def test_leak_own_marker_is_ignored_and_severity_configurable(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    manifest, _ = packs.load_manifest(manifest_path)
    body = make_body("alpha", "patterns") + "\nOur own alpha-widget stays legal.\n"
    make_skill(root, "alpha", "patterns", body=body)
    a, _ = packs.collect_pack(root, manifest["packs"][0])
    b, _ = packs.collect_pack(root, manifest["packs"][1])
    assert by_rule(packs.gate_leak(a + b, manifest), "domain-leak") == []
    strict = dict(manifest)
    strict["packs"] = [dict(manifest["packs"][0], leak_severity="error"),
                       manifest["packs"][1]]
    leaked = make_body("beta", "patterns") + "\nalpha-widget mentioned here.\n"
    make_skill(root, "beta", "patterns", body=leaked)
    b2, _ = packs.collect_pack(root, strict["packs"][1])
    found = by_rule(packs.gate_leak(a + b2, strict), "domain-leak")
    assert found and found[0].severity == "error"


# --------------------------------------------------------------------------
# analyze / rendering / CLI
# --------------------------------------------------------------------------

def test_analyze_valid_tree_is_ok(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    report = packs.analyze(manifest_path, skills_root=root)
    assert report["ok"] is True
    assert [f for f in report["findings"] if f["severity"] == "error"] == []
    stats = report["stats"]["packs"]
    assert set(stats) == {"alpha", "beta"}
    assert stats["alpha"]["files"]["patterns"]["nonempty_lines"] > 0


def test_analyze_missing_manifest_not_ok(tmp_path):
    report = packs.analyze(tmp_path / "absent.json", skills_root=tmp_path)
    assert report["ok"] is False


def test_to_json_is_parseable_and_stable(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    report = packs.analyze(manifest_path, skills_root=root)
    payload = json.loads(packs.to_json(report))
    assert set(payload) >= {"ok", "findings", "stats", "manifest"}
    assert packs.to_json(report) == packs.to_json(
        packs.analyze(manifest_path, skills_root=root))


def test_render_text_mentions_totals_and_findings(tmp_path):
    manifest_path, root = make_tree(tmp_path)
    make_skill(root, "beta", "patterns",
               body=make_body("beta", "patterns", filler=0))
    report = packs.analyze(manifest_path, skills_root=root)
    text = packs.render_text(report)
    assert "alpha" in text and "beta" in text
    assert "below" in text.lower() or "finding" in text.lower()


def test_cli_check_exit_codes(tmp_path):
    from mindhive import __main__ as cli_mod
    manifest_text = make_manifest(tmp_path).read_text(encoding="utf-8")
    # analyze() derives skills_root from the manifest location
    # (<root>/docs/packs/packs.json -> <root>/.claude/skills), so lay the
    # fixture out in repo shape for the CLI path.
    repo = tmp_path / "repo"
    mp = _write(repo / "docs" / "packs" / "packs.json", manifest_text)
    for pack, role in (("alpha", "patterns"), ("alpha", "verification"),
                       ("beta", "patterns")):
        make_skill(repo / ".claude" / "skills", pack, role)
    assert cli_mod.main(["packs", "check", "--manifest", str(mp)]) == 0
    (repo / ".claude" / "skills" / "beta-patterns" / "SKILL.md").unlink()
    assert cli_mod.main(["packs", "check", "--manifest", str(mp)]) == 1


def test_cli_report_always_exits_zero_and_emits_json(tmp_path, capsys):
    from mindhive import __main__ as cli_mod
    repo = tmp_path / "repo"
    mp = _write(repo / "docs" / "packs" / "packs.json",
                make_manifest(tmp_path).read_text(encoding="utf-8"))
    # Deliberately broken tree (missing skills) — report still exits 0.
    assert cli_mod.main(["packs", "report", "--manifest", str(mp), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_real_repo_analyze_when_packs_exist():
    """Real-flow gate: runs only once WS3 content lands (skip cleanly before)."""
    manifest, _ = packs.load_manifest(REAL_MANIFEST)
    assert manifest is not None
    skills_root = REPO / ".claude" / "skills"
    expected = [skills_root / f"{p['name']}-{r}" / "SKILL.md"
                for p in manifest["packs"] for r in p["roles"]]
    if not all(p.exists() for p in expected):
        pytest.skip("WS3 pack content not yet authored")
    report = packs.analyze(REAL_MANIFEST)
    assert [f for f in report["findings"] if f["severity"] == "error"] == []
    assert report["ok"] is True
