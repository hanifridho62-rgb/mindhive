# SPDX-License-Identifier: Apache-2.0
"""The MEB's line-count forecast (section 3.1) set beside the tree as it is.

The MEB ("Mind Hive blueprint v1.0/v1.1") forecast, per module and per group,
how long the code would be once the blueprint landed. A forecast is not a
measurement, so this script measures and prints both, row by row:

* `MEB_ROWS` copies the MEB's table verbatim -- label, baseline, target, and
  the policy sentence -- and names the files each row stands for in THIS tree.
  Where the MEB's path is wrong (it puts `lintester.py` at the package root; it
  lives in `orchestrator/`) the row says so rather than silently correcting it.
* Every row is measured with the counter `tests/test_file_size_cap.py` uses
  (`review._line_count`: lines as a text iterator yields them), re-implemented
  here so the script runs without the package on the path; `tests/test_loc_
  targets.py` holds the two to the same answer on every file.
* Each row gets a verdict: `met` (at or under target), `over` (above the
  forecast -- reported, not failed: the MEB forecast lengths it never
  measured), `shrank` (a shrink-only row below its baseline), `missing` (a
  named file absent). Group rows also check the MEB's per-module bound
  (commands/ <= 400, parsers/ <= 250, new modules < 1000).
* The MEB's own totals are recomputed from its rows. They do not add up
  (the baseline column sums to 13,870, not the stated 14,771), and the report
  says so instead of repeating them.

`--check` enforces only the ceilings the MEB states as HARD and the repository
has adopted: `__main__.py` under 90 lines; `orchestrator/lintester.py` never
above 997 (the MEB's zero-edit policy); the four shrink-only modules never above
their recorded counts (the `ALLOWED` ceiling in `tests/test_file_size_cap.py`,
which the test holds equal to `SHRINK_ONLY`); and no other module over 1000.
Exit 1 names every breach; the forecasts never fail a run.

Usage: `python scripts/loc_report.py [--root DIR] [--json] [--check]`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

__all__ = [
    "CAP", "MAIN_CAP", "LINTESTER_CEILING", "SHRINK_ONLY", "MEB_ROWS",
    "MEB_STATED_TOTALS", "line_count", "measure", "report_rows",
    "meb_totals", "hard_breaches", "render_markdown", "main",
]

CAP = 1000                 # the hard ceiling for every module (MEB 3, repo cap)
MAIN_CAP = 90              # `__main__.py` < 90 lines (MEB 3.1, spec 10.16)
LINTESTER_CEILING = 997    # zero-edit policy: never one line longer
SHRINK_ONLY = {
    "replenish.py": 2781,
    "workspace.py": 1462,
    "research.py": 1353,
    "mindnet/controller.py": 1016,
}

# The MEB's section 3.1 table, verbatim in its numbers and order. `files` are
# paths under src/mindhive in THIS tree; a trailing `/*.py` is a glob.
# `module_bound` is the MEB's per-module bound for a group row, when it states
# one; `target_is_max` marks the one row whose target is "< 90", a strict bound.
MEB_ROWS = [
    {"label": "src/mindhive/__main__.py", "baseline": 4237, "target": 90,
     "target_is_max": True, "files": ["__main__.py"],
     "policy": "Dekomposisi penuh ke paket commands/ dan subpaket "
               "commands/parsers/. Berkas lama menjadi shim re-export tipis."},
    {"label": "src/mindhive/lintester.py", "baseline": 997, "target": 997,
     "files": ["orchestrator/lintester.py"],
     "note": "the MEB's path is wrong: the module is orchestrator/lintester.py",
     "policy": "Zero-Edit Policy."},
    {"label": "src/mindhive/replenish.py", "baseline": 2781, "target": 2781,
     "files": ["replenish.py"], "policy": "shrink-only allowlist",
     "shrink_only": True},
    {"label": "src/mindhive/workspace.py", "baseline": 1462, "target": 1462,
     "files": ["workspace.py"], "policy": "shrink-only allowlist",
     "shrink_only": True},
    {"label": "src/mindhive/research.py", "baseline": 1353, "target": 1353,
     "files": ["research.py"], "policy": "shrink-only allowlist",
     "shrink_only": True},
    {"label": "src/mindhive/mindnet/controller.py", "baseline": 1016,
     "target": 1016, "files": ["mindnet/controller.py"],
     "policy": "shrink-only allowlist", "shrink_only": True},
    {"label": "src/mindhive/events.py", "baseline": 684, "target": 745,
     "files": ["events.py"], "policy": "Tetap < 1.000 LOC."},
    {"label": "src/mindhive/paths.py", "baseline": 118, "target": 135,
     "files": ["paths.py"], "policy": "paths.state_dir"},
    {"label": "src/mindhive/cliparse.py", "baseline": 95, "target": 108,
     "files": ["cliparse.py"], "policy": "session_id_arg"},
    {"label": "src/mindhive/vectors.py", "baseline": 380, "target": 475,
     "files": ["vectors.py"], "policy": "Tetap < 500 LOC."},
    {"label": "src/mindhive/tasktool.py", "baseline": 462, "target": 498,
     "files": ["tasktool.py"], "policy": "Tetap < 500 LOC."},
    {"label": "src/mindhive/recommend.py", "baseline": 285, "target": 340,
     "files": ["recommend.py"], "policy": "Laplace"},
    {"label": "Modul-Modul Baru (14 Modul)", "baseline": 0, "target": 2885,
     "module_count": 14, "module_bound": CAP - 1,
     "files": ["models.py", "worksessions.py", "adaptivetail.py", "advice.py",
               "orchestrator/driver.py", "orchestrator/interceptor.py",
               "orchestrator/breaker.py", "watcher.py", "ci/*.py",
               "storage/*.py"],
     "note": "the MEB names ten components for fourteen modules; ci/ and "
             "storage/ are counted whole",
     "policy": "models, worksessions, adaptivetail, advice, driver, "
               "interceptor, breaker, ci, watcher, storage SPI"},
    {"label": "Subpaket commands/ (8 Modul)", "baseline": 0, "target": 2450,
     "module_count": 8, "module_bound": 400, "files": ["commands/*.py"],
     "policy": "masing-masing <= 400 LOC"},
    {"label": "Subpaket commands/parsers/ (6 Modul)", "baseline": 0,
     "target": 1180, "module_count": 6, "module_bound": 250,
     "files": ["commands/parsers/*.py"], "policy": "masing-masing <= 250 LOC"},
]

MEB_STATED_TOTALS = {"baseline": 14771, "target": 16126}


def line_count(path: Path) -> int:
    """Lines as a text iterator yields them -- `review._line_count`'s answer;
    0 for a file that cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _expand(src: Path, pattern: str) -> list:
    if pattern.endswith("/*.py"):
        folder = src / pattern[: -len("/*.py")]
        return sorted(p for p in folder.glob("*.py") if p.is_file())
    path = src / pattern
    return [path] if path.is_file() else []


def measure(root: Path, row: dict) -> dict:
    """The files *row* stands for under *root*/src/mindhive, each counted."""
    src = Path(root) / "src" / "mindhive"
    counts, missing = {}, []
    for pattern in row["files"]:
        found = _expand(src, pattern)
        if not found:
            missing.append(pattern)
        for path in found:
            counts[path.relative_to(src).as_posix()] = line_count(path)
    return {"counts": counts, "missing": missing}


def _verdict(row: dict, actual: int, missing: list, over_bound: dict) -> str:
    if missing:
        return "missing"
    target = row["target"]
    within = actual < target if row.get("target_is_max") else actual <= target
    if not within:
        return "over"
    if row.get("shrink_only") and actual < row["baseline"]:
        return "shrank"
    return "over-bound" if over_bound else "met"


def report_rows(root: Path) -> list:
    """One dict per MEB row: the MEB's numbers, the measured total, the
    difference from the target, per-module detail, and the verdict."""
    rows = []
    for row in MEB_ROWS:
        got = measure(root, row)
        actual = sum(got["counts"].values())
        bound = row.get("module_bound")
        over_bound = {name: n for name, n in got["counts"].items()
                      if bound is not None and n > bound}
        rows.append({
            "label": row["label"], "baseline": row["baseline"],
            "target": row["target"],
            "target_text": ("< %d" % row["target"]) if row.get("target_is_max")
            else str(row["target"]),
            "actual": actual, "delta_vs_target": actual - row["target"],
            "modules": len(got["counts"]), "meb_modules": row.get("module_count"),
            "module_bound": bound, "over_bound": over_bound,
            "counts": got["counts"], "missing": got["missing"],
            "note": row.get("note", ""),
            "verdict": _verdict(row, actual, got["missing"], over_bound),
        })
    return rows


def meb_totals(rows: list) -> dict:
    """The MEB's stated totals beside the sums of its own rows and the tree."""
    return {
        "stated_baseline": MEB_STATED_TOTALS["baseline"],
        "computed_baseline": sum(r["baseline"] for r in rows),
        "stated_target": MEB_STATED_TOTALS["target"],
        "computed_target": sum(r["target"] for r in rows),
        "actual": sum(r["actual"] for r in rows),
    }


def hard_breaches(root: Path) -> list:
    """Every HARD ceiling the tree breaks, as `(module, lines, rule)`."""
    src = Path(root) / "src" / "mindhive"
    out = []
    for path in sorted(src.rglob("*.py")):
        name = path.relative_to(src).as_posix()
        n = line_count(path)
        if name == "__main__.py" and n >= MAIN_CAP:
            out.append((name, n, "__main__.py must stay under %d" % MAIN_CAP))
        elif name == "orchestrator/lintester.py" and n > LINTESTER_CEILING:
            out.append((name, n, "zero-edit: never above %d" % LINTESTER_CEILING))
        elif name in SHRINK_ONLY and n > SHRINK_ONLY[name]:
            out.append((name, n, "shrink-only: never above %d" % SHRINK_ONLY[name]))
        elif name not in SHRINK_ONLY and n > CAP:
            out.append((name, n, "over the %d-line cap" % CAP))
    return out


def render_markdown(rows: list, totals: dict) -> str:
    lines = [
        "| MEB row | MEB baseline | MEB target | Measured | vs target | "
        "Modules (MEB) | Verdict |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for r in rows:
        mods = str(r["modules"])
        if r["meb_modules"] is not None:
            mods += " (%d)" % r["meb_modules"]
        verdict = r["verdict"]
        if r["over_bound"]:
            verdict += "; over %d: %s" % (r["module_bound"], ", ".join(
                "%s %d" % kv for kv in sorted(r["over_bound"].items())))
        if r["missing"]:
            verdict += "; missing: " + ", ".join(r["missing"])
        if r["note"]:
            verdict += " (%s)" % r["note"]
        lines.append("| %s | %d | %s | %d | %+d | %s | %s |" % (
            r["label"], r["baseline"], r["target_text"], r["actual"],
            r["delta_vs_target"], mods, verdict))
    lines.append("")
    lines.append(
        "MEB totals: stated %(stated_baseline)d -> %(stated_target)d; its rows "
        "sum to %(computed_baseline)d -> %(computed_target)d; measured over the "
        "same rows: %(actual)d." % totals)
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="loc_report", description="MEB section 3.1 forecast vs the tree.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                        help="repository root (default: this checkout)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when a HARD ceiling is broken")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not (root / "src" / "mindhive").is_dir():
        print("loc_report: no src/mindhive under %s" % root, file=sys.stderr)
        return 2
    rows = report_rows(root)
    totals = meb_totals(rows)
    breaches = hard_breaches(root)
    if args.json:
        print(json.dumps({"rows": rows, "totals": totals,
                          "breaches": [list(b) for b in breaches]}, indent=2))
    else:
        print(render_markdown(rows, totals))
        for name, n, rule in breaches:
            print("BREACH %s %d: %s" % (name, n, rule))
    return 1 if (args.check and breaches) else 0


if __name__ == "__main__":
    sys.exit(main())
