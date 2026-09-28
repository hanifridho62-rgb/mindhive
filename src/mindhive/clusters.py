# SPDX-License-Identifier: Apache-2.0
# src/mindhive/clusters.py
"""Cluster database + file-complexity classifier + parallel shard planner.

This is the upper "parallel multi-agent" orchestration layer that the
`/mindhive-orchestrator` execution stage stands on. It is deterministic and
dependency-free (mirrors ``plugins.py``): it walks the repo's source files,
groups them into directory-level **clusters**, scores each file's **complexity**
from cheap static metrics, and packs whole clusters into N file-ownership-safe
**lanes** so the orchestrator can fan work out to parallel sub-agents without
two agents ever editing the same file.

The cluster DB is DERIVED data (a snapshot of the tree), so — exactly like
``plugin_catalog.json`` — it lives in a single overwrite-in-place JSON sidecar
(``cluster_db.json``), never in the append-only, hash-chained event ledger.

Design contract (what the tests pin):
  * ``build_clusters`` is deterministic — same tree yields the same keys/order.
  * ``file_complexity`` is monotone — a big, deeply-nested, branchy file scores
    strictly higher than a tiny flat one.
  * ``shard_plan`` partitions files: every input file lands in exactly one lane,
    no file appears in two lanes, and aggregate complexity is balanced.
  * ``lanes`` is clamped to ``[1, n_clusters]`` and non-numeric input is rejected
    (defense against a caller passing ``--lanes abc``).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from . import events as ev_mod, paths

# Source-ish text we cluster. Small, explicit set — no vendored binaries.
SOURCE_SUFFIXES = {
    ".py", ".md", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml",
    ".yaml", ".yml", ".sh", ".ps1", ".rb", ".go", ".rs", ".java",
}

# Directories never worth clustering (vcs / generated / vendored / caches).
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache",
    "build", "dist", ".mypy_cache", ".ruff_cache", ".claude", ".idea",
}

# Complexity tier cutoffs on the raw score. low < MED <= med < HIGH <= high.
_TIER_MED = 40
_TIER_HIGH = 120

# Control-flow / definition keywords used as a cheap cyclomatic proxy.
_BRANCH_RE = re.compile(
    r"\b(if|elif|else|for|while|try|except|finally|with|def|class|switch|case|match)\b"
)


# --------------------------------------------------------------------------
# File discovery + clustering
# --------------------------------------------------------------------------

def iter_source_files(root) -> list[str]:
    """Return repo-relative POSIX paths of source files under ``root``, sorted.

    Vendored/generated/vcs directories are pruned so clustering reflects the
    hand-written tree. Sorting makes the whole pipeline reproducible.
    """
    root = Path(root)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune in place so os.walk doesn't descend into skipped dirs
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fn in sorted(filenames):
            if Path(fn).suffix.lower() in SOURCE_SUFFIXES:
                rel = Path(dirpath, fn).relative_to(root).as_posix()
                out.append(rel)
    return sorted(out)


def cluster_key(rel_posix: str) -> str:
    """The cluster a file belongs to: its parent directory (POSIX), or
    ``"(root)"`` for a top-level file. A whole directory is one ownership unit,
    so a lane that owns a cluster owns every file in it."""
    parent = str(PurePosixPath(rel_posix).parent)
    return "(root)" if parent == "." else parent


def build_clusters(root, files=None) -> dict[str, list[str]]:
    """Group source files into clusters keyed by parent directory.

    ``files`` (repo-relative POSIX paths) overrides on-disk discovery — handy for
    tests and for clustering an explicit work-set. Keys and file lists are sorted
    so the mapping is deterministic.
    """
    rels = list(files) if files is not None else iter_source_files(root)
    clusters: dict[str, list[str]] = {}
    for rel in rels:
        clusters.setdefault(cluster_key(rel), []).append(rel)
    return {k: sorted(v) for k, v in sorted(clusters.items())}


# --------------------------------------------------------------------------
# Complexity classification
# --------------------------------------------------------------------------

def file_complexity(text: str) -> int:
    """Deterministic static complexity score for one file's text.

    ``score = loc + 3*max_indent_levels + 2*branch_count``:
      * ``loc`` — non-blank lines (size),
      * ``max_indent_levels`` — deepest leading-whitespace nesting (tabs=4, /4),
      * ``branch_count`` — control-flow/def keyword hits (a cheap cyclomatic proxy).

    Weights are chosen so a big, deeply-nested, branchy file always outscores a
    small flat one — the only property downstream sharding relies on.
    """
    loc = 0
    max_indent_levels = 0
    branches = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        loc += 1
        indent = len(line) - len(line.lstrip(" \t"))
        # normalize tabs to 4 columns, then to nesting "levels" of 4 columns
        cols = line[:indent].replace("\t", "    ")
        max_indent_levels = max(max_indent_levels, len(cols) // 4)
        branches += len(_BRANCH_RE.findall(stripped))
    return loc + 3 * max_indent_levels + 2 * branches


def complexity_tier(score: int) -> str:
    """Map a raw complexity score to a tier. Monotone in ``score``."""
    if score >= _TIER_HIGH:
        return "high"
    if score >= _TIER_MED:
        return "med"
    return "low"


def git_churn(rel_posix: str, root=None) -> int:
    """Number of commits that touched a file — an optional churn weight.

    Guarded by ``shutil.which("git")`` so it is a no-op (returns 0) when git is
    unavailable, keeping the pipeline deterministic in tests and dodging the
    Windows ``git.CMD`` shim resolution problem. Never raises.
    """
    import shutil

    if not shutil.which("git"):
        return 0
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "--", rel_posix],
            cwd=str(root or Path.cwd()), capture_output=True, text=True,
            timeout=5, creationflags=paths.NO_WINDOW,
        )
        if r.returncode != 0:
            return 0
        return sum(1 for ln in r.stdout.splitlines() if ln.strip())
    except Exception:
        return 0


# --------------------------------------------------------------------------
# Shard planning — pack clusters into file-ownership-safe lanes
# --------------------------------------------------------------------------

def _clamp_lanes(lanes, n_clusters: int) -> int:
    """Coerce ``lanes`` to a sane int in ``[1, max(1, n_clusters)]``.

    Accepts an int or a numeric string; a non-numeric value (e.g. ``"abc"``)
    raises ``ValueError`` rather than silently defaulting — a bad ``--lanes`` is
    a caller error, not something to paper over. ``None`` means "one lane per
    cluster" (maximum safe parallelism).
    """
    upper = max(1, n_clusters)
    if lanes is None:
        return upper
    try:
        n = int(lanes)
    except (TypeError, ValueError):
        raise ValueError(f"lanes must be an integer, got {lanes!r}")
    return max(1, min(n, upper))


def shard_plan(clusters: dict[str, list[str]], weights: dict[str, int],
               lanes=None) -> list[dict]:
    """Pack whole clusters into balanced, file-ownership-safe lanes.

    Greedy longest-processing-time bin-packing: clusters are placed heaviest
    first into whichever lane is currently lightest, so aggregate complexity is
    balanced across lanes. Because a cluster is placed whole, no file ever lands
    in two lanes — the invariant parallel sub-agents rely on to avoid write
    contention.

    Returns a list of ``{"lane": i, "clusters": [...], "files": [...],
    "est_complexity": int}``. Empty ``clusters`` yields ``[]``.
    """
    if not clusters:
        return []
    n = _clamp_lanes(lanes, len(clusters))
    # heaviest cluster first; tie-break on name for determinism
    ordered = sorted(clusters.keys(), key=lambda k: (-int(weights.get(k, 0)), k))
    plan = [{"lane": i, "clusters": [], "files": [], "est_complexity": 0}
            for i in range(n)]
    for key in ordered:
        # lightest lane wins; then the lane with the fewest clusters (so
        # zero-weight clusters — e.g. dirs of empty files — spread across the
        # empty lanes instead of clumping into lane 0 and collapsing the
        # requested lane count); then lane index for determinism.
        target = min(plan, key=lambda ln: (ln["est_complexity"],
                                           len(ln["clusters"]), ln["lane"]))
        target["clusters"].append(key)
        target["files"].extend(clusters[key])
        target["est_complexity"] += int(weights.get(key, 0))
    for ln in plan:
        ln["clusters"].sort()
        ln["files"].sort()
    # drop empty lanes (possible when n_clusters < requested lanes only if
    # clamped up — clamp prevents it, but stay defensive) and renumber
    plan = [ln for ln in plan if ln["files"]]
    for i, ln in enumerate(plan):
        ln["lane"] = i
    return plan


# --------------------------------------------------------------------------
# Full pipeline + sidecar persistence
# --------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def build(root=None, lanes=None, use_git: bool = False) -> dict:
    """Full pipeline: discover -> cluster -> score -> shard.

    Returns the snapshot payload (also what ``build_and_store`` persists):
    per-file complexity+tier, per-cluster weight, and a shard plan.
    """
    root = Path(root or Path.cwd())
    clusters = build_clusters(root)
    files_meta: dict[str, dict] = {}
    weights: dict[str, int] = {}
    for key, rels in clusters.items():
        total = 0
        for rel in rels:
            score = file_complexity(_read_text(root / rel))
            if use_git:
                score += git_churn(rel, root=root)
            files_meta[rel] = {"score": score, "tier": complexity_tier(score)}
            total += score
        weights[key] = total
    plan = shard_plan(clusters, weights, lanes=lanes)
    return {
        "generated_at": ev_mod.now_iso(),
        "root": str(root),
        "counts": {
            "files": len(files_meta),
            "clusters": len(clusters),
            "lanes": len(plan),
        },
        "clusters": {
            key: {
                "weight": weights[key],
                "files": [{"path": rel, **files_meta[rel]} for rel in rels],
            }
            for key, rels in clusters.items()
        },
        "shard_plan": plan,
    }


def _db_path(project=None) -> Path:
    """Sidecar file holding the single latest cluster DB snapshot."""
    return paths.project_dir(project) / "cluster_db.json"


def build_and_store(root=None, lanes=None, project=None,
                    use_git: bool = False) -> dict:
    """Run ``build`` and overwrite the cluster-DB sidecar in place.

    Derived, ephemeral data — a single overwrite-in-place file, never the
    append-only ledger (which would bloat the hash chain with one snapshot per
    run and could never be pruned without breaking it). Mirrors
    ``plugins._write_catalog`` exactly, including the atomic ``tmp.replace``.
    """
    payload = build(root=root, lanes=lanes, use_git=use_git)
    p = _db_path(project)
    # Atomic overwrite-in-place via the shared writer (private tmp + retry); no
    # unbounded growth, and no `<name>.tmp` two concurrent writers could share.
    paths.write_text_atomic(p, ev_mod.canonical_json(payload))
    return payload


def latest(project=None) -> dict | None:
    """Return the latest cluster-DB snapshot, or None if never built."""
    p = _db_path(project)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
