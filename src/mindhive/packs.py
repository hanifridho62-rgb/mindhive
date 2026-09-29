# SPDX-License-Identifier: Apache-2.0
# src/mindhive/packs.py
"""Deterministic quality gates for stack content packs.

A stack content pack is a family of in-repo skills (``.claude/skills/
<pack>-<role>/SKILL.md``) giving sessions framework-native guidance for one
stack this machine actually runs. The pattern is distilled from the ECC
framework quartets (django/laravel/quarkus/springboot x patterns/security/
tdd/verification); the gates exist because ECC shipped that content ungated
and its failure modes shipped invisibly with it — a 159-line stub in the same
role family as an 812-line exemplar, three drifting document templates, a 16%
same-ecosystem text overlap, and a load-test example that targets a sibling
framework's demo domain. Full provenance: docs/prds/2026-07-21-001 and
docs/packs/SPEC.md — the spec this module encodes.

Four gates, one manifest. Every threshold lives in ``docs/packs/packs.json``
(the manifest is the single source of truth; nothing numeric is hardcoded
here):

``structure``   flat frontmatter contract (the shared parser flattens nested
                keys, so nesting is rejected outright), slug grammar, required
                sections per role, phase counts for verification files.
``depth``       the non-empty line floor is the anti-stub gate; the code-block
                floor and the within-pack parity ratio are secondary; the
                quartet-depth target only warns (report-only ambition, not a
                blocker).
``similarity``  line-set overlap (|A∩B| / min set size) — a vocabulary-distance
                proxy, deliberately split in two: INTERNAL same-role pairs
                across packs (the real copy risk: three packs describing one
                machine) with the manifest's shared-preamble lines excluded so
                honest shared truth cannot trip it, and EXTERNAL comparison
                against a declared corpus (corroboration only; the corpus root
                comes from an environment variable so no machine path is
                committed, and a missing corpus is a warning, never an error).
``leak``        foreign domain markers — the ``/api/markets``-in-quarkus class
                of derivation fingerprint. Markers are pack-exclusive tokens;
                severity is the marker-owning pack's call.

The shape mirrors ``context.py`` (the proven read-only analyzer): frozen
dataclasses, pure gate functions over collected inputs, injectable roots so
tests never touch real state, and total reads — a malformed or missing file
becomes a Finding, never an exception. ``hive packs check`` exits 1 on any
error-severity finding (the same gate contract as ``hive context --check``);
``hive packs report`` is informational and always exits 0."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from .plugins import _parse_frontmatter, _strip_frontmatter

# Normalized lines shorter than this carry no authorship signal ("```", "---",
# table rules) and would swamp the overlap metric with structural noise.
MIN_NORM_LINE = 10

# Frontmatter keys every pack skill must carry — FLAT, top-level. The shared
# parser flattens any indented block into top-level keys, so a nested
# ``metadata:`` contract would be unverifiable; its presence is rejected.
REQUIRED_FRONT_KEYS = ("name", "description", "origin", "pack", "role", "kind")

_PHASE_RE = re.compile(r"(?m)^## Phase \d+")


@dataclass(frozen=True)
class PackFile:
    """One collected pack skill, measured once at collection time."""
    pack: str
    role: str
    name: str            # directory slug == expected frontmatter name
    path: str
    frontmatter: dict
    body: str            # markdown body, frontmatter stripped
    nonempty_lines: int
    code_blocks: int
    norm: frozenset      # normalized body lines (similarity currency)


@dataclass(frozen=True)
class CorpusFile:
    """One external-corpus skill, reduced to what comparison needs."""
    name: str
    role: str
    norm: frozenset


@dataclass(frozen=True)
class Finding:
    """One gate result. severity is "error" (fails `check`) or "warn"."""
    rule: str
    severity: str
    subject: str
    message: str


# --------------------------------------------------------------------------
# manifest + normalization
# --------------------------------------------------------------------------

MANIFEST_VERSION = 2


def load_manifest(path: Path) -> tuple[dict | None, list[Finding]]:
    """Load and sanity-check the manifest. Total: problems become findings.

    An unknown version warns but still loads — a newer manifest should degrade
    to the gates this build knows, not brick the command."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [Finding("manifest-missing", "error", str(path),
                              f"manifest not found: {path}")]
    except Exception as e:
        return None, [Finding("manifest-missing", "error", str(path),
                              f"manifest unreadable: {e}")]
    try:
        data = json.loads(text)
    except Exception as e:
        return None, [Finding("manifest-invalid", "error", str(path),
                              f"manifest is not valid JSON: {e}")]
    if not isinstance(data, dict) or not isinstance(data.get("packs"), list):
        return None, [Finding("manifest-invalid", "error", str(path),
                              "manifest must be an object with a 'packs' list")]
    findings: list[Finding] = []
    if data.get("version") != MANIFEST_VERSION:
        findings.append(Finding(
            "manifest-version", "warn", str(path),
            f"manifest version {data.get('version')!r} != expected "
            f"{MANIFEST_VERSION}; gates run with known-field defaults"))
    return data, findings


def norm_lines(text: str) -> frozenset:
    """The similarity currency: strip, collapse whitespace, lowercase, drop
    lines under MIN_NORM_LINE chars. Exactly the metric the ECC study used, so
    thresholds calibrated there transfer here unchanged."""
    out = set()
    for line in (text or "").splitlines():
        s = re.sub(r"\s+", " ", line.strip()).lower()
        if len(s) >= MIN_NORM_LINE:
            out.add(s)
    return frozenset(out)


def _pct(a: frozenset, b: frozenset) -> float:
    """|A∩B| / min(|A|,|B|) as a percentage; empty sets never divide."""
    smaller = min(len(a), len(b))
    if not smaller:
        return 0.0
    return 100.0 * len(a & b) / smaller


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

def collect_pack(skills_root: Path,
                 pack_decl: dict) -> tuple[list[PackFile], list[Finding]]:
    """Read every declared role skill of one pack. Total: a missing or
    unreadable SKILL.md is an error finding (a declared skill that does not
    exist is a real defect at steady state), and collection continues."""
    skills_root = Path(skills_root)
    pack = pack_decl.get("name") or "?"
    files: list[PackFile] = []
    findings: list[Finding] = []
    for role in pack_decl.get("roles") or []:
        slug = f"{pack}-{role}"
        path = skills_root / slug / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            findings.append(Finding("missing-skill", "error", slug,
                                    f"declared skill has no file: {path}"))
            continue
        except Exception as e:
            findings.append(Finding("unreadable-skill", "error", slug,
                                    f"could not read {path}: {e}"))
            continue
        body = _strip_frontmatter(text)
        body_lines = body.splitlines()
        files.append(PackFile(
            pack=pack, role=role, name=slug, path=str(path),
            frontmatter=_parse_frontmatter(text), body=body,
            nonempty_lines=sum(1 for ln in body_lines if ln.strip()),
            code_blocks=sum(1 for ln in body_lines
                            if ln.lstrip().startswith("```")) // 2,
            norm=norm_lines(body),
        ))
    return files, findings


# --------------------------------------------------------------------------
# gates
# --------------------------------------------------------------------------

def gate_structure(pf: PackFile, manifest: dict) -> list[Finding]:
    """Frontmatter contract, slug grammar, required sections, phase counts."""
    out: list[Finding] = []
    fm = pf.frontmatter or {}

    if "metadata" in fm:
        out.append(Finding(
            "nested-frontmatter", "error", pf.name,
            "frontmatter has a 'metadata:' block — the shared parser flattens "
            "nested keys, so the contract is FLAT top-level keys only "
            "(origin:/pack:/role:/kind:)"))

    for key in REQUIRED_FRONT_KEYS:
        if not (fm.get(key) or "").strip():
            out.append(Finding("frontmatter-key", "error", pf.name,
                               f"missing/empty required frontmatter key '{key}'"))

    declared = fm.get("name") or ""
    if declared and declared != pf.name:
        out.append(Finding("name-mismatch", "error", pf.name,
                           f"frontmatter name {declared!r} != directory slug "
                           f"{pf.name!r}"))
    for key, want in (("pack", pf.pack), ("role", pf.role)):
        got = fm.get(key) or ""
        if got and got != want:
            out.append(Finding("name-mismatch", "error", pf.name,
                               f"frontmatter {key} {got!r} != declared {want!r}"))

    limit = manifest.get("max_description_chars")
    desc = fm.get("description") or ""
    if isinstance(limit, int) and len(desc) > limit:
        out.append(Finding("description-length", "error", pf.name,
                           f"description is {len(desc)} chars (limit {limit})"))

    per_role = manifest.get("required_sections_by_role") or {}
    required = per_role.get(pf.role, manifest.get("required_sections") or [])
    present = {ln.strip() for ln in pf.body.splitlines()}
    for section in required:
        if section not in present:
            out.append(Finding("missing-section", "error", pf.name,
                               f"required section missing: {section}"))

    min_phases = (manifest.get("min_phase_sections_by_role") or {}).get(pf.role)
    if isinstance(min_phases, int):
        n = len(_PHASE_RE.findall(pf.body))
        if n < min_phases:
            out.append(Finding("phase-count", "error", pf.name,
                               f"{n} '## Phase N' section(s); role "
                               f"{pf.role!r} requires >= {min_phases}"))
    return out


def gate_depth(files: list[PackFile], manifest: dict) -> list[Finding]:
    """Floors (the anti-stub gate), within-pack parity ratio, depth target."""
    out: list[Finding] = []
    min_lines = manifest.get("min_lines")
    min_blocks_default = manifest.get("min_code_blocks")
    by_role = manifest.get("min_code_blocks_by_role") or {}
    target = manifest.get("quartet_depth_target_lines")
    ratio_max = manifest.get("depth_ratio_max")

    for f in files:
        if isinstance(min_lines, int) and f.nonempty_lines < min_lines:
            out.append(Finding("depth-floor", "error", f.name,
                               f"{f.nonempty_lines} non-empty lines "
                               f"(floor {min_lines}) — a stub at ECC "
                               f"springboot-tdd depth"))
        min_blocks = by_role.get(f.role, min_blocks_default)
        if isinstance(min_blocks, int) and f.code_blocks < min_blocks:
            out.append(Finding("block-floor", "error", f.name,
                               f"{f.code_blocks} fenced code block(s) "
                               f"(floor {min_blocks} for role {f.role!r})"))
        if isinstance(target, int) and f.nonempty_lines < target:
            out.append(Finding("below-target", "warn", f.name,
                               f"{f.nonempty_lines} non-empty lines is below "
                               f"the quartet-depth target of {target} "
                               f"(django/quarkus exemplars run 400-812)"))

    if isinstance(ratio_max, (int, float)) and len(files) >= 2:
        counts = [f.nonempty_lines for f in files]
        lo, hi = min(counts), max(counts)
        ratio = (hi / lo) if lo else float("inf")
        if ratio > ratio_max:
            pack = files[0].pack
            out.append(Finding("depth-ratio", "error", pack,
                               f"within-pack depth spread {ratio:.2f}x exceeds "
                               f"{ratio_max}x (max {hi} vs min {lo} non-empty "
                               f"lines) — the laravel 5.27x failure mode"))
    return out


def _pack_decl(manifest: dict, pack: str) -> dict:
    for decl in manifest.get("packs") or []:
        if decl.get("name") == pack:
            return decl
    return {}


def gate_similarity(files: list[PackFile], corpora: dict,
                    manifest: dict) -> list[Finding]:
    """Split thresholds: internal same-role pairs (with the shared-preamble
    exclusion — three packs describing one machine legitimately share
    environment truth) and external corpus comparison (full sets — the
    external gate keeps its whole strength)."""
    out: list[Finding] = []
    markers = [str(m).lower() for m in
               manifest.get("shared_preamble_markers") or []]
    internal_max = manifest.get("similarity_internal_same_role_max_pct")
    external_max = manifest.get("similarity_external_max_pct")
    external_warn = manifest.get("similarity_external_warn_pct")

    def strip_preamble(norm: frozenset) -> frozenset:
        if not markers:
            return norm
        return frozenset(ln for ln in norm
                         if not any(m in ln for m in markers))

    ordered = sorted(files, key=lambda f: f.name)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if a.pack == b.pack or a.role != b.role:
                continue
            pct = _pct(strip_preamble(a.norm), strip_preamble(b.norm))
            if isinstance(internal_max, (int, float)) and pct > internal_max:
                out.append(Finding(
                    "similarity-internal", "error", f"{a.name} ~ {b.name}",
                    f"internal same-role overlap {pct:.1f}% exceeds "
                    f"{internal_max}% (after shared-preamble exclusion) — "
                    f"re-domain or diverge the content"))

    for f in ordered:
        for corpus_name in _pack_decl(manifest, f.pack).get("compare_external") or []:
            peers = [c for c in corpora.get(corpus_name, []) if c.role == f.role]
            if not peers:
                continue
            worst = max(peers, key=lambda c: _pct(f.norm, c.norm))
            pct = _pct(f.norm, worst.norm)
            if isinstance(external_max, (int, float)) and pct > external_max:
                out.append(Finding(
                    "similarity-external", "error", f.name,
                    f"overlap with corpus {corpus_name!r} file "
                    f"{worst.name} is {pct:.1f}% (limit {external_max}%) — "
                    f"content must not be copied from the corpus"))
            elif isinstance(external_warn, (int, float)) and pct > external_warn:
                out.append(Finding(
                    "similarity-external", "warn", f.name,
                    f"overlap with corpus {corpus_name!r} file "
                    f"{worst.name} is {pct:.1f}% (warn at {external_warn}%)"))
    return out


def load_external_corpus(name: str, decl: dict,
                         env: dict | None = None
                         ) -> tuple[list[CorpusFile], list[Finding]]:
    """Resolve a corpus from its declared environment variable and read its
    family-allowlisted skills. No filesystem path is committed to the repo;
    an unresolvable corpus is a WARNING (comparison skipped) — CI never sees
    the real clone, so the corpus must not couple repo health to it."""
    env = os.environ if env is None else env
    var = decl.get("path_env") or ""
    root_str = env.get(var, "") if var else ""
    if not root_str:
        return [], [Finding("corpus-skipped", "warn", name,
                            f"corpus env var {var or '?'} unset — external "
                            f"comparison skipped")]
    root = Path(root_str)
    if not root.is_dir():
        return [], [Finding("corpus-skipped", "warn", name,
                            f"corpus path does not exist: {root}")]
    out: list[CorpusFile] = []
    for family in decl.get("families") or []:
        for d in sorted(root.glob(f"{family}-*")):
            try:
                text = (d / "SKILL.md").read_text(encoding="utf-8",
                                                  errors="replace")
            except Exception:
                continue
            out.append(CorpusFile(name=d.name,
                                  role=d.name.rsplit("-", 1)[-1],
                                  norm=norm_lines(_strip_frontmatter(text))))
    return out, []


def gate_leak(files: list[PackFile], manifest: dict) -> list[Finding]:
    """Foreign pack-exclusive markers in a pack's raw body — the
    template-and-redomain fingerprint. A pack's own markers are legal;
    severity is the marker-owning pack's leak_severity."""
    out: list[Finding] = []
    for f in files:
        for decl in manifest.get("packs") or []:
            owner = decl.get("name") or ""
            if owner == f.pack:
                continue
            severity = decl.get("leak_severity") or "warn"
            for marker in decl.get("domain_markers") or []:
                if marker and marker in f.body:
                    out.append(Finding(
                        "domain-leak", severity, f.name,
                        f"contains {owner!r}'s domain marker {marker!r} — "
                        f"the /api/markets-in-quarkus failure mode"))
    return out


# --------------------------------------------------------------------------
# analysis + rendering
# --------------------------------------------------------------------------

_SEV_ORDER = {"error": 0, "warn": 1}


def analyze(manifest_path: Path, skills_root: Path | None = None,
            env: dict | None = None) -> dict:
    """Run every gate over every declared pack; return one report dict.

    ``skills_root`` defaults from the manifest's repo position
    (<root>/docs/packs/packs.json -> <root>/.claude/skills), so `--manifest`
    is the only path input the CLI ever needs."""
    manifest_path = Path(manifest_path)
    manifest, findings = load_manifest(manifest_path)
    if manifest is None:
        return {"ok": False, "manifest": str(manifest_path),
                "findings": [asdict(f) for f in findings], "stats": {}}

    if skills_root is None:
        parents = manifest_path.resolve().parents
        root = parents[2] if len(parents) > 2 else parents[-1]
        skills_root = root / ".claude" / "skills"

    target = manifest.get("quartet_depth_target_lines")
    all_files: list[PackFile] = []
    pack_stats: dict = {}
    for decl in manifest.get("packs") or []:
        files, ffind = collect_pack(skills_root, decl)
        findings.extend(ffind)
        all_files.extend(files)
        counts = [f.nonempty_lines for f in files]
        pack_stats[decl.get("name") or "?"] = {
            "files": {f.role: {
                "path": f.path,
                "nonempty_lines": f.nonempty_lines,
                "code_blocks": f.code_blocks,
                "below_target": (isinstance(target, int)
                                 and f.nonempty_lines < target),
            } for f in files},
            "depth_ratio": (round(max(counts) / min(counts), 2)
                            if len(counts) >= 2 and min(counts) else None),
        }
        for f in files:
            findings.extend(gate_structure(f, manifest))
        findings.extend(gate_depth(files, manifest))

    corpora: dict = {}
    wanted = {name
              for decl in manifest.get("packs") or []
              for name in decl.get("compare_external") or []}
    for name in sorted(wanted):
        decl = (manifest.get("external_corpora") or {}).get(name)
        if decl is None:
            findings.append(Finding("corpus-skipped", "warn", name,
                                    "corpus referenced but not declared in "
                                    "external_corpora"))
            continue
        cfiles, cfind = load_external_corpus(name, decl, env=env)
        findings.extend(cfind)
        corpora[name] = cfiles

    findings.extend(gate_similarity(all_files, corpora, manifest))
    findings.extend(gate_leak(all_files, manifest))

    markers = [str(m).lower() for m in
               manifest.get("shared_preamble_markers") or []]

    def stripped(norm: frozenset) -> frozenset:
        return frozenset(ln for ln in norm
                         if not any(m in ln for m in markers))

    internal_rows = []
    ordered = sorted(all_files, key=lambda f: f.name)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if a.pack != b.pack and a.role == b.role:
                internal_rows.append({
                    "role": a.role, "a": a.name, "b": b.name,
                    "pct": round(_pct(stripped(a.norm), stripped(b.norm)), 1)})
    external_rows = []
    for f in ordered:
        for corpus_name in _pack_decl(manifest, f.pack).get("compare_external") or []:
            peers = [c for c in corpora.get(corpus_name, []) if c.role == f.role]
            if not peers:
                continue
            worst = max(peers, key=lambda c: _pct(f.norm, c.norm))
            external_rows.append({
                "corpus": corpus_name, "file": f.name, "peer": worst.name,
                "pct": round(_pct(f.norm, worst.norm), 1)})

    findings.sort(key=lambda f: (_SEV_ORDER.get(f.severity, 2),
                                 f.rule, f.subject, f.message))
    return {
        "ok": not any(f.severity == "error" for f in findings),
        "manifest": str(manifest_path),
        "findings": [asdict(f) for f in findings],
        "stats": {
            "packs": pack_stats,
            "similarity_internal": internal_rows,
            "similarity_external": external_rows,
        },
    }


def render_text(report: dict) -> str:
    """Human-readable report; `check` and `report` share it."""
    lines = [f"Pack gates over {report.get('manifest')}",
             f"verdict: {'OK' if report.get('ok') else 'NOT OK'}", ""]
    stats = report.get("stats") or {}
    for pack, slot in sorted((stats.get("packs") or {}).items()):
        ratio = slot.get("depth_ratio")
        lines.append(f"{pack}  (depth ratio "
                     f"{ratio if ratio is not None else 'n/a'})")
        for role, row in sorted((slot.get("files") or {}).items()):
            flag = "  [below target]" if row.get("below_target") else ""
            lines.append(f"  {role:<14} {row['nonempty_lines']:>4} non-empty "
                         f"lines  {row['code_blocks']:>2} block(s){flag}")
    rows = stats.get("similarity_internal") or []
    if rows:
        lines.append("")
        lines.append("internal same-role overlap (preamble-excluded):")
        for r in rows:
            lines.append(f"  {r['a']} ~ {r['b']}: {r['pct']}%")
    rows = stats.get("similarity_external") or []
    if rows:
        lines.append("")
        lines.append("external corpus overlap (worst peer):")
        for r in rows:
            lines.append(f"  {r['file']} vs {r['peer']} "
                         f"[{r['corpus']}]: {r['pct']}%")
    found = report.get("findings") or []
    if found:
        lines.append("")
        lines.append(f"findings ({len(found)}):")
        for f in found:
            lines.append(f"  [{f['severity']}] {f['rule']}: "
                         f"{f['subject']} — {f['message']}")
    lines.append("")
    lines.append("This command is read-only and changed nothing.")
    return "\n".join(lines)


def to_json(report: dict) -> str:
    """Machine-readable report. Stable keys and ordering by construction."""
    return json.dumps(report, indent=2, sort_keys=True)
